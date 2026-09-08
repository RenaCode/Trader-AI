#!/usr/bin/env python3
"""Skaner wiadomosci: RSS -> filtr istotnosci -> analiza -> sygnal.

Dwustopniowosc jest celowa. Okolo 95% artykulow w kanalach crypto to szum
(podsumowania, powtorki, teksty sponsorowane). Tani model odsiewa je za ulamek
kosztu, drogi analizuje wylacznie to, co przeszlo. Bez tego rachunek za API
rosnie kilkunastokrotnie bez zadnego zysku informacyjnego.

Dwa znaczniki czasu sa tu krytyczne:
  event_time  = publikacja artykulu
  ingested_at = moment, w ktorym MY skonczylismy analize
Decyzja podjeta przed `ingested_at` nie moze tego artykulu widziec - to jest
jedyna rzecz chroniaca warstwe uczenia przed uczeniem sie z przyszlosci.

Uzycie:
    DATABASE_URL=... ANTHROPIC_API_KEY=... python services/ingest/news_worker.py --once
    DATABASE_URL=... ANTHROPIC_API_KEY=... python services/ingest/news_worker.py   # petla
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import feedparser
import httpx
import psycopg
from psycopg.rows import dict_row

from services.api.security import SecretBox
from services.ingest import puls
from services.ingest.llm import (
    Dostawca, DostawcaAnthropic, DostawcaGemini, DostawcaMieszany, Odmowa,
    limit_dobowy_usd,
)

# Nazwy modeli nalezą do DOSTAWCY, nie do tego pliku: te same prompty ida do
# Claude'a albo do Gemini w zaleznosci od tego, ktory klucz jest ustawiony.

# Schemat wymuszony na modelu. Bez `strict` model potrafi dorzucic pole albo
# zwrocic liczbe jako tekst, a to konczy sie wyjatkiem przy zapisie do bazy.
ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "instruments": {"type": "array", "items": {"type": "string"}},
        "direction": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
        "magnitude": {"type": "number"},
        "horizon": {"type": "string", "enum": ["minutes", "hours", "days", "weeks"]},
        "confidence": {"type": "number"},
        "novelty": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": ["relevant", "instruments", "direction", "magnitude", "horizon",
                 "confidence", "novelty", "reasoning"],
    "additionalProperties": False,
}

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "worth_analyzing": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["worth_analyzing", "reason"],
    "additionalProperties": False,
}

# Prefiks stabilny miedzy wywolaniami -> trafia w cache promptow (~10x taniej
# przy powtarzalnych zapytaniach). Wszystko zmienne idzie PO nim.
TRIAGE_SYSTEM = """Jesteś filtrem istotności dla systemu tradingowego.

Oceniasz, czy nagłówek wiadomości może w ogóle wpłynąć na cenę instrumentu
finansowego w horyzoncie od minut do tygodni.

Odrzucaj: podsumowania tygodnia, materiały sponsorowane, poradniki, powtórzenia
tego samego zdarzenia, spekulacje bez nowego faktu, ceny podane bez kontekstu.

Przyjmuj: decyzje regulatorów i banków centralnych, wyniki spółek, przepływy
instytucjonalne, awarie i włamania, zmiany w protokołach, dane makro, wypowiedzi
osób realnie wpływających na rynek.

Bądź surowy. Fałszywe przyjęcie kosztuje analizę drogim modelem; fałszywe
odrzucenie kosztuje jeden sygnał wśród wielu."""

ANALYSIS_SYSTEM = """Analizujesz wiadomości rynkowe dla autonomicznego systemu
tradingowego. Twoja ocena wchodzi jako JEDNA z wielu cech do modelu, który sam
kalibruje jej wagę — nie podejmujesz decyzji o transakcji.

Wypełniaj pola według tych znaczeń:

- instruments: symbole, których wiadomość realnie dotyczy (np. BTCUSDT, ETHUSDT,
  SOLUSDT, SPY, AAPL). Pusta lista, jeśli żaden z nich.
- direction: kierunek spodziewanego wpływu na cenę.
- magnitude 0..1: siła oczekiwanego ruchu. 0.1 to szum, 0.9 to zdarzenie
  przestawiające rynek.
- horizon: w jakim czasie wpływ się zrealizuje.
- confidence 0..1: jak pewny jesteś SWOJEJ oceny, nie tego, że rynek zareaguje.
- novelty 0..1: ile z tego jest NOWĄ informacją, której rynek jeszcze nie wycenił.
  To najważniejsze i najczęściej mylone pole. Wiadomość o mocnym wydźwięku, ale
  powtarzająca coś znanego od tygodnia, ma novelty bliskie zeru — i wtedy jej
  wartość dla systemu jest bliska zeru niezależnie od magnitude.
- reasoning: dwa, trzy zdania po polsku. Powiedz wprost, czego NIE wiesz.

Nie zawyżaj pewności. System mierzy kalibrację twoich ocen i rozjazd między
deklarowaną a rzeczywistą trafnością obniża wagę tej cechy."""


# Kolejnosc rozstrzyga, gdy dostepny jest DOKLADNIE JEDEN klucz. Gdy sa oba,
# nie wybieramy miedzy nimi - laczymy je (patrz `zbuduj_dostawce`).
def _klucz(conn, nazwa_sekretu: str, nazwa_env: str) -> str | None:
    """Klucz z bazy, a gdy go nie ma - ze srodowiska.

    Baza ma pierwszenstwo, bo to ona jest wymieniana bez redeployu; zmienna
    srodowiskowa zostaje jako sciezka dla CI i uruchomien lokalnych.
    """
    row = conn.execute("SELECT value_enc FROM app.secret WHERE name = %s",
                       (nazwa_sekretu,)).fetchone()
    if row:
        return SecretBox().decrypt(bytes(row[0]))
    return os.environ.get(nazwa_env) or None


def zbuduj_dostawce(klucze: dict[str, str | None]) -> Dostawca | None:
    """Dostawca z tego, co jest. CZYSTA FUNKCJA — bez bazy i bez srodowiska.

    OBA KLUCZE ZNACZA MIESZANKE, NIE WYBOR. Triaz to 94% wywolan i tam Gemini
    jest 5,5x tanszy; analiza kosztuje u obu tyle samo, wiec o niej decyduje
    jakosc. Wybieranie jednego dostawcy do wszystkiego bylo by w tej sytuacji
    gorsze pod kazdym wzgledem naraz — patrz `DostawcaMieszany`.

    Jeden klucz znaczy jednego dostawce, bez zmiany zachowania.
    """
    a = klucze.get("anthropic")
    g = klucze.get("gemini")
    if a and g:
        return DostawcaMieszany(triaz=DostawcaGemini(g), analiza=DostawcaAnthropic(a))
    if a:
        return DostawcaAnthropic(a)
    if g:
        return DostawcaGemini(g)
    return None


def wybierz_dostawce(conn, wymagany: bool = True) -> Dostawca | None:
    """Dostawca zlozony z dostepnych kluczy.

    Czytamy przy KAZDYM cyklu, bo klucz pojawia sie wtedy, gdy uzytkownik wpisze
    go w interfejsie, a nie wtedy, gdy restartujemy poda.

    `wymagany=False` zwraca None zamiast konczyc proces - dla trybu, ktory
    potrafi pracowac bez klucza (pobieranie artykulow bez analizy).
    """
    d = zbuduj_dostawce({
        nazwa: _klucz(conn, sekret, env)
        for sekret, env, nazwa in (("anthropic_api_key", "ANTHROPIC_API_KEY", "anthropic"),
                                   ("gemini_api_key", "GEMINI_API_KEY", "gemini"))})
    if d is not None:
        return d
    if not wymagany:
        return None
    sys.exit(
        "Brak klucza dostawcy analizy. Wpisz go w interfejsie "
        "(Ustawienia -> Klucz API Anthropic albo Klucz API Google Gemini) "
        "albo ustaw ANTHROPIC_API_KEY / GEMINI_API_KEY."
    )


def content_hash(title: str, summary: str) -> bytes:
    return hashlib.sha256(f"{title}\n{summary}".encode()).digest()


# Naglowek domyslny. Zawiera adres kontaktowy, bo SEC tego WYMAGA w swojej
# polityce dostepu automatycznego i bez niego oddaje 403.
UA_DEFAULT = "Trader-AI/0.1 (+https://trader.renacode.com; mbeczynski@gmail.com)"

# Zapasowy naglowek przegladarkowy. Czesc wydawcow (np. Bitcoin Magazine za
# Cloudflare) odrzuca kazdy niebrowserowy User-Agent. Uzywany WYLACZNIE po
# odmowie 403 - nie udajemy przegladarki tam, gdzie uczciwy naglowek wystarcza.
UA_FALLBACK = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


async def fetch_feed(client: httpx.AsyncClient, url: str) -> tuple[list, str]:
    """Zwraca (wpisy, status). Blad kanalu nie moze zatrzymac calego cyklu."""
    try:
        r = await client.get(url, follow_redirects=True,
                             headers={"User-Agent": UA_DEFAULT})
        if r.status_code == 403:
            r = await client.get(url, follow_redirects=True,
                                 headers={"User-Agent": UA_FALLBACK})
        if r.status_code != 200:
            return [], f"http_{r.status_code}"
        parsed = feedparser.parse(r.content)
        # Kanal, ktory oddaje 200 i zero wpisow, jest rownie zepsuty co 403,
        # tylko ciszej. Rozrozniamy go, zeby nie wygladal na zdrowy w UI.
        if not parsed.entries:
            return [], "pusty_kanal"
        return parsed.entries, "ok"
    except httpx.TimeoutException:
        return [], "timeout"
    except Exception as e:                                   # noqa: BLE001
        return [], f"blad: {type(e).__name__}"


def entry_time(entry) -> datetime:
    for key in ("published_parsed", "updated_parsed"):
        t = getattr(entry, key, None)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


async def triage(ai: Dostawca, title: str, summary: str) -> tuple[bool, str, dict]:
    try:
        data, usage = await ai.zapytaj(
            model=ai.model_triazu,
            system=TRIAGE_SYSTEM,
            user=f"Tytuł: {title}\n\nZajawka: {summary[:600]}",
            schema=TRIAGE_SCHEMA,
            # Budzet obejmuje tez tokeny rozumowania, ktore modele 3.x zuzywaja
            # nawet na tak prostym zadaniu. Przy 300 odpowiedz konczyla sie na
            # limicie, zanim padlo pierwsze slowo wyniku.
            max_tokens=1200,
        )
    except Odmowa as e:
        # Odmowa na triazu nie jest awaria: artykul po prostu nie idzie dalej.
        return False, str(e), e.usage
    return data["worth_analyzing"], data["reason"], usage


async def analyze(ai: Dostawca, title: str, summary: str,
                  source: str, published: datetime, symbols: list[str]) -> tuple[dict, dict]:
    return await ai.zapytaj(
        model=ai.model_analizy,
        system=ANALYSIS_SYSTEM,
        user=(f"Instrumenty w obserwacji: {', '.join(symbols)}\n"
              f"Źródło: {source}\n"
              f"Opublikowano: {published:%Y-%m-%d %H:%M} UTC\n"
              f"Tytuł: {title}\n\n{summary[:3000]}"),
        schema=ANALYSIS_SCHEMA,
        # Jak przy triazu: limit musi pomiescic rozumowanie razem z odpowiedzia.
        max_tokens=4000,
    )


# Twardy limit dobowy. Domyslnie ostrozny - przy zmierzonych 128 artykulach
# na dobe realne zuzycie to okolo 0,60 USD, wiec 5 USD zostawia szeroki zapas,
# a jednoczesnie zatrzyma petle, ktora zaczela analizowac w kolko to samo
# (np. gdy kanal zmieni identyfikatory wpisow i deduplikacja przestanie dzialac).
DAILY_LIMIT_USD = limit_dobowy_usd()


def zapisz_nieudane(cur, conn, ai, model: str, purpose: str, blad: Exception) -> float:
    """Koszt wywolania, ktore nic nie dalo. Zwraca wydana kwote (0, gdy nieznana).

    Wywolanie, ktore poszlo do dostawcy i wrocilo bezuzyteczne - odmowa, odpowiedz
    ucieta na limicie, pusta odpowiedz - JEST PLATNE. Bez tego zapisu limit dobowy
    nie widzi tych pieniedzy, a to wlasnie one potrafia sie powtarzac w petli:
    artykul, ktorego analiza zawsze konczy sie na limicie tokenow, wraca przy
    kazdym cyklu nadrabiania zaleglosci.

    Blad sieciowy nie niesie zuzycia i wtedy nie ma czego zapisywac - `usage`
    jest wtedy zerowe, a wiersz z samymi zerami tylko zasmiecalby tabele.
    """
    u = getattr(blad, "usage", None)
    if not u or not (u.get("input") or u.get("output")):
        return 0.0
    koszt = ai.koszt(model, u)
    record_spend(cur, model, purpose, u, koszt, ok=False)
    conn.commit()
    return koszt


def record_spend(cur, model: str, purpose: str, usage: dict, cost: float,
                 article_id: int | None = None, ok: bool = True) -> None:
    cur.execute(
        "INSERT INTO news.api_spend (model, purpose, input_tokens, cache_read,"
        " output_tokens, cost_usd, article_id, ok) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (model, purpose, usage.get("input", 0), usage.get("cache_read", 0),
         usage.get("output", 0), cost, article_id, ok))


def spent_today(cur) -> float:
    cur.execute("SELECT COALESCE(sum(cost_usd), 0) AS s FROM news.api_spend"
                " WHERE event_time >= date_trunc('day', now())")
    return float(cur.fetchone()["s"])


def zapisz_analize(cur, article_id: int, model: str, data: dict, usage: dict,
                   price: float, published, instruments: dict[str, int]) -> None:
    """Zapis wyniku analizy i powiazan z instrumentami.

    Wydzielone, bo korzystaja z tego DWIE sciezki: analiza swiezo pobranego
    artykulu i nadrabianie zaleglosci. Dwie kopie tego zapisu rozjechalyby sie
    przy pierwszej zmianie schematu, a roznica byla by niewidoczna.

    `ingested_at = now()` jest tu istotne i celowe: mowi, KIEDY POZNALISMY ten
    wynik. compute_news bierze pod uwage wylacznie analizy gotowe przed dana
    swieca (`an.ingested_at <= b.t`), wiec artykul przeanalizowany z opoznieniem
    nie wchodzi wstecz do swiec, ktorych wtedy nie mogl dotyczyc.
    """
    cur.execute(
        "INSERT INTO news.article_analysis (article_id, model, prompt_version, direction,"
        " magnitude, horizon, confidence, novelty, reasoning, raw, input_tokens,"
        " output_tokens, cost_usd, event_time, ingested_at)"
        " VALUES (%s,%s,'v1',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())",
        (article_id, model, data["direction"],
         max(0.0, min(1.0, data["magnitude"])), data["horizon"],
         max(0.0, min(1.0, data["confidence"])), max(0.0, min(1.0, data["novelty"])),
         data["reasoning"], json.dumps(data), usage["input"], usage["output"],
         price, published),
    )
    for sym in data.get("instruments", []):
        iid = instruments.get(sym.upper())
        if iid:
            cur.execute(
                "INSERT INTO news.article_instrument (article_id, instrument_id, relevance)"
                " VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (article_id, iid, max(0.0, min(1.0, data["confidence"]))),
            )


# Okno, w ktorym artykul moze jeszcze wejsc do jakiejkolwiek swiecy.
#
# NIE JEST TO LICZBA DOBRANA NA OKO. compute_news liczy sentyment swiecy `t`
# z artykulow spelniajacych `a.event_time > t - 24 godziny` ORAZ analiz
# spelniajacych `an.ingested_at <= t`. Artykul opublikowany o P i przeanalizowany
# o A wchodzi wiec do swiecy t tylko wtedy, gdy A <= t < P + 24 h - czyli
# WYLACZNIE gdy analiza nastapila w ciagu doby od publikacji.
#
# Starszy artykul nie zasili juz zadnej swiecy, niezaleznie od tego, ile za jego
# analize zaplacimy.
OKNO_ZALEGLOSCI_H = 24


async def nadrob_zaleglosci(conn, cur, ai: Dostawca, instruments: dict[str, int],
                            symbols: list[str], wydano: float, stats: dict,
                            verbose: bool) -> float:
    """Analiza artykulow zapisanych wczesniej, ktore analizy nie dostaly.

    DLACZEGO TO ISTNIEJE

    Proces potrafi pobierac artykuly bez klucza Anthropic (tryb --auto), zeby
    material nie przepadal - kanal RSS nie odda jutro tego, czego dzis nie
    pobralismy. Ale zwykly cykl pomija artykul, ktorego `content_hash` juz jest
    w bazie, wiec po pojawieniu sie klucza zaleglosci nie zostalyby tkniete.

    ILE TO REALNIE ODZYSKUJE

    Tylko artykuly z ostatniej doby - patrz OKNO_ZALEGLOSCI_H. Zmierzone
    2026-09-06: z 496 zapisanych artykulow w oknie miescily sie 23. Reszta
    zostaje jako material badawczy i nie da sie jej juz uzyc jako cechy.

    KOLEJNOSC OD NAJNOWSZYCH

    Gdy budzet dobowy sie skonczy, tracimy najstarsze - czyli te, ktorym do
    wypadniecia z okna zostalo najmniej czasu.
    """
    cur.execute(
        "SELECT a.id, a.title, a.summary, a.event_time, f.name AS feed"
        " FROM news.article a"
        " JOIN news.feed f ON f.id = a.feed_id"
        " LEFT JOIN news.article_analysis an ON an.article_id = a.id"
        " WHERE an.id IS NULL"
        "   AND a.event_time > now() - make_interval(hours => %s)"
        " ORDER BY a.event_time DESC", (OKNO_ZALEGLOSCI_H,))
    zalegle = cur.fetchall()
    if not zalegle:
        return wydano
    if verbose:
        print(f"  zaległości do przeanalizowania: {len(zalegle)}")

    for a in zalegle:
        if wydano >= DAILY_LIMIT_USD:
            if verbose:
                print(f"    [stop] limit dobowy ${DAILY_LIMIT_USD:.2f} osiągnięty, "
                      f"zostało {len(zalegle) - stats['zaleglosci']} zaległych")
            break
        try:
            worth, why, tri_usage = await triage(ai, a["title"], a["summary"] or "")
        except Exception as e:                                # noqa: BLE001
            stats["bledy"] += 1
            stats["opisy_bledow"].append(f"triaż {ai.model_triazu}: {type(e).__name__}: {e}")
            stracone = zapisz_nieudane(cur, conn, ai, ai.model_triazu, "triage", e)
            stats["koszt"] += stracone
            wydano += stracone
            if verbose:
                print(f"    filtr padl: {type(e).__name__}: {e}")
            continue
        # MODEL, KTORY NAPRAWDE PRACOWAL, nie ten, o ktory prosilismy.
        # Mieszanka schodzi na dostawce zapasowego przy limicie; wpisanie
        # tanszego modelu pokazywaloby w dzienniku tansza stawke za prace,
        # ktorej on nie wykonal - a limit dobowy liczy sie wlasnie stad.
        model_tri = ai.uzyty_model(ai.model_triazu, tri_usage)
        tri_cost = ai.koszt(model_tri, tri_usage)
        record_spend(cur, model_tri, "triage", tri_usage, tri_cost)
        conn.commit()
        stats["koszt"] += tri_cost
        wydano += tri_cost
        if not worth:
            continue

        try:
            data, usage = await analyze(ai, a["title"], a["summary"] or "", a["feed"],
                                        a["event_time"], symbols)
        except Exception as e:                                # noqa: BLE001
            stats["bledy"] += 1
            stats["opisy_bledow"].append(f"analiza {ai.model_analizy}: {type(e).__name__}: {e}")
            stracone = zapisz_nieudane(cur, conn, ai, ai.model_analizy, "analysis", e)
            stats["koszt"] += stracone
            wydano += stracone
            if verbose:
                print(f"    analiza padla: {type(e).__name__}: {e}")
            continue
        if not data.get("relevant"):
            continue

        price = ai.koszt(ai.model_analizy, usage)
        record_spend(cur, ai.model_analizy, "analysis", usage, price)
        stats["koszt"] += price
        wydano += price
        zapisz_analize(cur, a["id"], ai.model_analizy, data, usage, price,
                       a["event_time"], instruments)
        conn.commit()
        stats["zaleglosci"] += 1
        if verbose:
            print(f"    [zaległy] {data['direction']:>7s} {a['title'][:60]}")
    return wydano


async def run_cycle(conn, ai: Dostawca | None, http: httpx.AsyncClient,
                    verbose: bool, analizuj: bool = True,
                    silniki: list[str] | None = None) -> dict:
    """Jeden cykl: pobierz kanaly, odsiej, przeanalizuj, zapisz.

    TRYB BEZ ANALIZY istnieje, bo bez klucza Anthropic ten proces nie robil
    NICZEGO - konczyl sie komunikatem, zanim zdazyl dotknac RSS-a. Skutki byly
    dwa i oba niewidoczne:

      1. artykul zapisuje sie dopiero PO udanej analizie, wiec kazda doba bez
         klucza to newsy bezpowrotnie stracone - kanaly RSS oddaja tylko swieze
         wpisy, archiwum nie da sie odtworzyc,
      2. nikt nie wiedzial, czy te dziesiec kanalow w ogole odpowiada. Byly
         skonfigurowane od poczatku projektu i ANI RAZU nieodpytane.

    W tym trybie pobieramy i zapisujemy artykuly, nie dotykajac Anthropica.
    Analiza dolaczy do nich pozniej.
    """
    cur = conn.cursor(row_factory=dict_row)

    wydano = spent_today(cur)
    if analizuj and wydano >= DAILY_LIMIT_USD:
        print(f"Limit dobowy osiągnięty: ${wydano:.2f} z ${DAILY_LIMIT_USD:.2f}. "
              f"Cykl pominięty — wznowienie po północy UTC.")
        return {"kanaly": 0, "pobrane": 0, "nowe": 0, "przeszly_filtr": 0,
                "przeanalizowane": 0, "bledy": 0, "koszt": 0.0, "limit": True}

    cur.execute("SELECT id, symbol FROM ref.instrument WHERE active")
    instruments = {r["symbol"]: r["id"] for r in cur.fetchall()}
    symbols = sorted(instruments)

    # KANALY OGRANICZONE DO SILNIKOW, KTORE ISTNIEJA.
    #
    # `news.feed.engine` byla w schemacie od poczatku i NIE BYLA UZYWANA nigdzie -
    # proces pobieral i analizowal wszystko. Przy wylacznie krypto w produkcji
    # oznaczalo to, ze polowa ruchu to zgloszenia 8-K spolek gieldowych (225 z 476
    # artykulow w ciagu dwoch dob), za ktorych filtrowanie zaplacilby silnik, dla
    # ktorego nie moga mieć znaczenia. Przy limicie 5 USD na dobe to nie jest
    # drobiazg.
    #
    # Kanaly BEZ przypisanego silnika (Fed, EBC) zostaja zawsze: polityka pieniezna
    # dotyczy kazdego rynku i nie da sie jej przypisac do jednego z nich.
    cur.execute("""
        SELECT id, name, url, engine, poll_interval_s, last_polled_at
        FROM news.feed
        WHERE enabled
          AND (%s::text[] IS NULL OR engine IS NULL OR engine::text = ANY(%s))
          AND (last_polled_at IS NULL
               OR last_polled_at < now() - make_interval(secs => poll_interval_s))
        ORDER BY last_polled_at NULLS FIRST
    """, (silniki, silniki))
    feeds = cur.fetchall()

    stats = {"kanaly": len(feeds), "pobrane": 0, "nowe": 0, "przeszly_filtr": 0,
             "przeanalizowane": 0, "zapisane": 0, "zaleglosci": 0,
             "koszt": 0.0, "bledy": 0, "opisy_bledow": []}

    for feed in feeds:
        entries, status = await fetch_feed(http, feed["url"])
        stats["pobrane"] += len(entries)
        if status != "ok":
            # STATUS TRAFIA DO OPISU, nie tylko do licznika. Kanal, ktory
            # przestal odpowiadac, jest nie do odroznienia od kanalu bez nowych
            # wiadomosci, dopoki jedynym sladem jest liczba.
            stats["bledy"] += 1
            stats["opisy_bledow"].append(f"kanał {feed['name']}: {status}")

        seen = kept = 0
        for entry in entries[:40]:
            title = (getattr(entry, "title", "") or "").strip()
            summary = (getattr(entry, "summary", "") or getattr(entry, "description", "") or "").strip()
            if not title:
                continue
            seen += 1

            h = content_hash(title, summary)
            cur.execute("SELECT 1 FROM news.article WHERE content_hash = %s", (h,))
            if cur.fetchone():
                continue
            stats["nowe"] += 1

            published = entry_time(entry)
            # Artykuly starsze niz doba nie maja wartosci sygnalowej, a kosztuja
            # tyle samo - przy pierwszym uruchomieniu kanal potrafi zwrocic archiwum.
            #
            # Prog dotyczy WYLACZNIE analizy. Samo zapisanie artykulu nic nie
            # kosztuje, a material, ktorego dzis nie zapiszemy, przepadnie:
            # kanal RSS nie odda go jutro.
            if analizuj and published < datetime.now(timezone.utc) - timedelta(days=1):
                continue

            if not analizuj:
                cur.execute(
                    "INSERT INTO news.article (feed_id, url, title, summary, content_hash,"
                    " event_time, ingested_at) VALUES (%s,%s,%s,%s,%s,%s, now())"
                    " ON CONFLICT (content_hash) DO NOTHING",
                    (feed["id"], getattr(entry, "link", "") or feed["url"], title,
                     summary[:4000], h, published),
                )
                conn.commit()
                kept += 1
                stats["zapisane"] += 1
                if verbose:
                    print(f"    [zapisany] {title[:70]}")
                continue

            if wydano >= DAILY_LIMIT_USD:
                if verbose:
                    print(f"    [stop] limit dobowy ${DAILY_LIMIT_USD:.2f} osiągnięty")
                break

            try:
                worth, why, tri_usage = await triage(ai, title, summary)
            except Exception as e:                            # noqa: BLE001
                stats["bledy"] += 1
                stats["opisy_bledow"].append(f"triaż {ai.model_triazu}: {type(e).__name__}: {e}")
                stracone = zapisz_nieudane(cur, conn, ai, ai.model_triazu, "triage", e)
                stats["koszt"] += stracone
                wydano += stracone
                if verbose:
                    print(f"    filtr padl: {type(e).__name__}: {e}")
                continue

            # MODEL, KTORY NAPRAWDE PRACOWAL, nie ten, o ktory prosilismy.
            # Mieszanka schodzi na dostawce zapasowego przy limicie; wpisanie
            # tanszego modelu pokazywaloby w dzienniku tansza stawke za prace,
            # ktorej on nie wykonal - a limit dobowy liczy sie wlasnie stad.
            model_tri = ai.uzyty_model(ai.model_triazu, tri_usage)
            tri_cost = ai.koszt(model_tri, tri_usage)
            record_spend(cur, model_tri, "triage", tri_usage, tri_cost)
            conn.commit()
            stats["koszt"] += tri_cost
            wydano += tri_cost

            if not worth:
                if verbose:
                    print(f"    [odsiane] {title[:70]} — {why}")
                continue
            stats["przeszly_filtr"] += 1
            kept += 1

            try:
                data, usage = await analyze(ai, title, summary, feed["name"], published, symbols)
            except Exception as e:                            # noqa: BLE001
                stats["bledy"] += 1
                stats["opisy_bledow"].append(f"analiza {ai.model_analizy}: {type(e).__name__}: {e}")
                stracone = zapisz_nieudane(cur, conn, ai, ai.model_analizy, "analysis", e)
                stats["koszt"] += stracone
                wydano += stracone
                if verbose:
                    print(f"    analiza padla: {type(e).__name__}: {e}")
                continue

            if not data.get("relevant"):
                continue

            price = ai.koszt(ai.uzyty_model(ai.model_analizy, usage), usage)
            record_spend(cur, ai.model_analizy, "analysis", usage, price)
            conn.commit()
            stats["koszt"] += price
            wydano += price
            stats["przeanalizowane"] += 1
            stats["zapisane"] += 1

            cur.execute(
                "INSERT INTO news.article (feed_id, url, title, summary, content_hash,"
                " event_time, ingested_at) VALUES (%s,%s,%s,%s,%s,%s, now())"
                " ON CONFLICT (content_hash) DO NOTHING RETURNING id",
                (feed["id"], getattr(entry, "link", "") or feed["url"], title,
                 summary[:4000], h, published),
            )
            row = cur.fetchone()
            if not row:
                continue
            article_id = row["id"]

            zapisz_analize(cur, article_id, ai.model_analizy, data, usage, price,
                           published, instruments)

            if verbose:
                print(f"    [{data['direction']:>7s}] siła {data['magnitude']:.2f} "
                      f"nowość {data['novelty']:.2f}  {title[:60]}")
            conn.commit()

        cur.execute(
            "UPDATE news.feed SET last_polled_at = now(), last_status = %s,"
            " articles_seen = articles_seen + %s, articles_kept = articles_kept + %s"
            " WHERE id = %s",
            (status, seen, kept, feed["id"]),
        )
        conn.commit()

    # ZALEGLOSCI NA KONCU, nie na poczatku.
    #
    # Swiezo pobrany artykul ma pierwszenstwo przed zaleglym: jest blizej srodka
    # swojego okna waznosci, wiec zdazy zasilic wiecej swiec. Przy wyczerpaniu
    # budzetu dobowego tracimy wiec to, co i tak stracilibysmy najszybciej.
    if analizuj and ai is not None:
        wydano = await nadrob_zaleglosci(conn, cur, ai, instruments, symbols,
                                         wydano, stats, verbose)

    return stats


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="jeden cykl zamiast petli")
    ap.add_argument("--interval", type=int, default=120, help="przerwa miedzy cyklami (s)")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--bez-analizy", action="store_true", dest="bez_analizy",
                    help="pobieraj i zapisuj artykuly, nie wywoluj Anthropica. "
                         "Do pracy przed pojawieniem sie klucza: material, ktorego "
                         "dzis nie zapiszemy, przepadnie - RSS nie odda go jutro")
    ap.add_argument("--auto", action="store_true",
                    help="wybierz tryb sam: z kluczem analizuj, bez klucza pobieraj")
    ap.add_argument("--silniki", default="",
                    help="lista silnikow po przecinku - pobieraj tylko kanaly do nich "
                         "przypisane. Kanaly bez przypisania (makro: Fed, EBC) zostaja "
                         "zawsze. Puste = wszystkie kanaly")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    silniki = [x.strip() for x in args.silniki.split(",") if x.strip()] or None
    if silniki:
        print(f"Kanały ograniczone do silników: {', '.join(silniki)} "
              f"(oraz kanały makro bez przypisania)")

    async with httpx.AsyncClient(timeout=20.0) as http:
        # POLACZENIE NA KAZDY CYKL, NIE NA CALY PROCES.
        #
        # Pierwsza wersja otwierala je raz, przed petla. Po restarcie bazy kazdy
        # kolejny cykl padal na tym samym zerwanym polaczeniu, a proces szedl
        # dalej w nieskonczonosc - potok byl martwy i wygladal na zywy.
        POD_RZAD_MAX = 5
        pod_rzad = 0
        poprzedni_tryb: str | None = None
        # Nazwa w `risk.worker_heartbeat` - ROLA w potoku, nie egzemplarz
        # procesu. Lista silnikow i tryb ida do `detail`, bo sa argumentem.
        PULS = "news"

        while True:
            started = datetime.now(timezone.utc)
            try:
                with psycopg.connect(url) as conn:
                    # TRYB WYBIERANY PRZY KAZDYM CYKLU, nie raz przy starcie.
                    #
                    # Klucz pojawia sie w bazie wtedy, gdy uzytkownik wpisze go
                    # w UI - a nie wtedy, gdy restartujemy poda. Sprawdzanie raz
                    # na starcie oznaczaloby, ze proces pobiera artykuly jeszcze
                    # dobe po tym, jak klucz juz jest.
                    ai = (None if args.bez_analizy
                          else wybierz_dostawce(conn, wymagany=not args.auto))
                    analizuj = ai is not None

                    # ZMIANA DOSTAWCY JEST ZDARZENIEM, nie szczegolem. Oceny
                    # dwoch roznych modeli nie sa tym samym pomiarem, a cecha
                    # `news_sent` sklejona z obu ma w srodku uskok, ktorego
                    # kalibracja wagi nie zaklada. Musi byc widoczna w logu.
                    tryb_opis = (f"pobieranie i ANALIZA ({ai.nazwa}: {ai.model_triazu} → "
                                 f"{ai.model_analizy})" if ai else
                                 "wylacznie pobieranie (brak klucza dostawcy)")
                    if tryb_opis != poprzedni_tryb:
                        print("Tryb: " + tryb_opis)
                        poprzedni_tryb = tryb_opis

                    try:
                        s = await run_cycle(conn, ai, http, verbose=not args.quiet,
                                            analizuj=analizuj, silniki=silniki)
                    except Exception as e:                    # noqa: BLE001
                        # Cykl przerwany NIE ODSWIEZA `last_beat_at` - dopisuje
                        # tylko szczegoly, zeby czujka umiala odroznic „proces
                        # stoi" od „proces pracuje i nie daje rady". `raise`
                        # oddaje wyjatek licznikowi nizej: jedna awaria ma miec
                        # jedno miejsce, w ktorym sie liczy.
                        puls.zglos_awarie(conn, PULS, args.interval, pod_rzad + 1,
                                          f"{type(e).__name__}: {e}")
                        raise

                    # PULS PO UDANYM CYKLU, na tym samym polaczeniu.
                    #
                    # „Udany" znaczy tu DOKLADNIE to, co znaczylo dla licznika
                    # `pod_rzad` juz wczesniej: cykl bez wyjatku. Bledy
                    # pojedynczych kanalow (`s['bledy']`) awaria cyklu NIE SA -
                    # jeden martwy kanal RSS wsrod kilkunastu nie ma prawa
                    # wprowadzic poda w CrashLoopBackOff - i ta decyzja zostaje
                    # tam, gdzie byla. Ida za to do `detail`, zeby przy
                    # ewentualnym alarmie bylo widac, co ten worker robil
                    # ostatnim razem, bez zagladania w log poda.
                    #
                    # Blad zapisu pulsu celowo NIE jest lapiany (patrz
                    # `puls.bij`): policzy sie jako awaria cyklu, bo worker,
                    # ktorego czujka nie widzi, jest dla nadzoru nieodrozninalny
                    # od workera, ktorego nie ma.
                    puls.bij(conn, PULS, args.interval,
                             {"analiza": analizuj, "kanaly": s["kanaly"],
                              "zapisane": s["zapisane"],
                              "przeanalizowane": s["przeanalizowane"],
                              "bledy_kanalow": s["bledy"],
                              "silniki": silniki or []})
                took = (datetime.now(timezone.utc) - started).total_seconds()
                # Podsumowanie musi rozrozniac ZAPISANE od PRZEANALIZOWANYCH.
                # Pierwsza wersja liczyla zapisane jako przeanalizowane, wiec
                # cykl bez klucza raportowal "zapisanych 0" po zapisaniu 234
                # artykulow - log gorszy niz zaden, bo wygladal na awarie.
                analiza = (f"przez filtr {s['przeszly_filtr']}, "
                           f"przeanalizowanych {s['przeanalizowane']}"
                           + (f", zaległych {s['zaleglosci']}" if s['zaleglosci'] else "")
                           + f", koszt ${s['koszt']:.4f}"
                           if analizuj else "bez analizy (brak klucza)")
                print(f"[{started:%H:%M:%S}] kanałów {s['kanaly']}, wpisów {s['pobrane']}, "
                      f"nowych {s['nowe']}, zapisanych {s['zapisane']}, {analiza}, "
                      f"błędów {s['bledy']}, {took:.0f} s")
                # BLEDY WYPISUJEMY ZAWSZE, TAKZE PRZY --quiet.
                #
                # `--quiet` istnieje po to, zeby wyciszyc rutynowy postep, a nie
                # awarie. Na produkcji byl ustawiony w charcie i przez to cykl
                # raportowal "bledow 11" bez ani jednego slowa o tym, czym byly -
                # dokladnie ten sam ksztalt, ktory wczesniej ukryl serie odmow
                # 429 u dostawcy analizy.
                for blad in dict.fromkeys(s.get("opisy_bledow", [])):
                    print(f"    BŁĄD {blad}")
                pod_rzad = 0
            except Exception as e:                            # noqa: BLE001
                pod_rzad += 1
                print(f"[{started:%H:%M:%S}] cykl przerwany "
                      f"({pod_rzad}/{POD_RZAD_MAX}): {type(e).__name__}: {e}")
                if pod_rzad >= POD_RZAD_MAX:
                    # Wypisanie bledu do logu nie jest widocznoscia - logu poda
                    # nikt nie czyta. Restart poda JEST widoczny.
                    print(f"{POD_RZAD_MAX} cykli pod rząd zakończonych błędem — "
                          f"kończę proces, żeby restart był WIDOCZNY zamiast cichy")
                    return 1

            if args.once:
                return 0
            await asyncio.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
