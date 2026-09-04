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
from anthropic import AsyncAnthropic
from psycopg.rows import dict_row

from services.api.security import SecretBox

ANALYSIS_MODEL = os.environ.get("ANALYSIS_MODEL", "claude-opus-5")
TRIAGE_MODEL = os.environ.get("TRIAGE_MODEL", "claude-haiku-4-5")

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


def anthropic_key(conn, wymagany: bool = True) -> str | None:
    """Klucz z bazy (wpisany w interfejsie), a w razie jego braku ze srodowiska.

    Kolejnosc jest celowa: klucz podany przez uzytkownika w UI ma pierwszenstwo,
    bo to on jest wymieniany bez redeployu. Zmienna srodowiskowa zostaje jako
    zapasowa sciezka dla CI i uruchomien lokalnych.

    `wymagany=False` zwraca None zamiast konczyc proces - dla trybu, ktory potrafi
    pracowac bez klucza (pobieranie artykulow bez analizy). Czytamy przy KAZDYM
    cyklu, bo klucz pojawia sie wtedy, gdy uzytkownik wpisze go w UI, a nie wtedy,
    gdy restartujemy poda.
    """
    row = conn.execute("SELECT value_enc FROM app.secret WHERE name = 'anthropic_api_key'").fetchone()
    if row:
        return SecretBox().decrypt(bytes(row[0]))
    env = os.environ.get("ANTHROPIC_API_KEY")
    if env:
        return env
    if not wymagany:
        return None
    sys.exit(
        "Brak klucza Anthropic. Wpisz go w interfejsie (Ustawienia -> Klucz API Anthropic) "
        "albo ustaw ANTHROPIC_API_KEY."
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


async def triage(client: AsyncAnthropic, title: str,
                 summary: str) -> tuple[bool, str, dict]:
    r = await client.messages.create(
        model=TRIAGE_MODEL,
        max_tokens=300,
        system=[{"type": "text", "text": TRIAGE_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        output_config={"format": {"type": "json_schema", "schema": TRIAGE_SCHEMA}},
        messages=[{"role": "user", "content": f"Tytuł: {title}\n\nZajawka: {summary[:600]}"}],
    )
    usage = {"input": r.usage.input_tokens, "output": r.usage.output_tokens,
             "cache_read": getattr(r.usage, "cache_read_input_tokens", 0) or 0}
    if r.stop_reason == "refusal":
        return False, "model odmówił oceny", usage
    data = json.loads(next(b.text for b in r.content if b.type == "text"))
    return data["worth_analyzing"], data["reason"], usage


async def analyze(client: AsyncAnthropic, title: str, summary: str,
                  source: str, published: datetime, symbols: list[str]) -> tuple[dict, dict]:
    r = await client.messages.create(
        model=ANALYSIS_MODEL,
        max_tokens=1500,
        thinking={"type": "adaptive"},
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA},
        },
        system=[{"type": "text", "text": ANALYSIS_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{
            "role": "user",
            "content": (
                f"Instrumenty w obserwacji: {', '.join(symbols)}\n"
                f"Źródło: {source}\n"
                f"Opublikowano: {published:%Y-%m-%d %H:%M} UTC\n"
                f"Tytuł: {title}\n\n{summary[:3000]}"
            ),
        }],
    )
    if r.stop_reason == "refusal":
        raise RuntimeError("model odmówił analizy")
    data = json.loads(next(b.text for b in r.content if b.type == "text"))
    usage = {
        "input": r.usage.input_tokens,
        "output": r.usage.output_tokens,
        "cache_read": getattr(r.usage, "cache_read_input_tokens", 0) or 0,
    }
    return data, usage


# Cennik (USD za milion tokenow) - do policzenia kosztu per artykul.
PRICE = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def cost_usd(model: str, usage: dict) -> float:
    inp, out = PRICE.get(model, (5.0, 25.0))
    # Odczyt z cache kosztuje ~10% ceny wejscia.
    billed_in = (usage["input"] - usage["cache_read"]) + usage["cache_read"] * 0.1
    return round(billed_in / 1e6 * inp + usage["output"] / 1e6 * out, 6)


# Twardy limit dobowy. Domyslnie ostrozny - przy zmierzonych 128 artykulach
# na dobe realne zuzycie to okolo 0,60 USD, wiec 5 USD zostawia szeroki zapas,
# a jednoczesnie zatrzyma petle, ktora zaczela analizowac w kolko to samo
# (np. gdy kanal zmieni identyfikatory wpisow i deduplikacja przestanie dzialac).
DAILY_LIMIT_USD = float(os.environ.get("ANTHROPIC_DAILY_USD", "5.0"))


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


async def run_cycle(conn, ai: AsyncAnthropic | None, http: httpx.AsyncClient,
                    verbose: bool, analizuj: bool = True) -> dict:
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

    cur.execute("""
        SELECT id, name, url, engine, poll_interval_s, last_polled_at
        FROM news.feed
        WHERE enabled
          AND (last_polled_at IS NULL
               OR last_polled_at < now() - make_interval(secs => poll_interval_s))
        ORDER BY last_polled_at NULLS FIRST
    """)
    feeds = cur.fetchall()

    stats = {"kanaly": len(feeds), "pobrane": 0, "nowe": 0, "przeszly_filtr": 0,
             "przeanalizowane": 0, "zapisane": 0, "koszt": 0.0, "bledy": 0}

    for feed in feeds:
        entries, status = await fetch_feed(http, feed["url"])
        stats["pobrane"] += len(entries)
        if status != "ok":
            stats["bledy"] += 1

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
                if verbose:
                    print(f"    filtr padl: {type(e).__name__}: {e}")
                continue

            tri_cost = cost_usd(TRIAGE_MODEL, tri_usage)
            record_spend(cur, TRIAGE_MODEL, "triage", tri_usage, tri_cost)
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
                if verbose:
                    print(f"    analiza padla: {type(e).__name__}: {e}")
                continue

            if not data.get("relevant"):
                continue

            price = cost_usd(ANALYSIS_MODEL, usage)
            record_spend(cur, ANALYSIS_MODEL, "analysis", usage, price)
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

            cur.execute(
                "INSERT INTO news.article_analysis (article_id, model, prompt_version, direction,"
                " magnitude, horizon, confidence, novelty, reasoning, raw, input_tokens,"
                " output_tokens, cost_usd, event_time, ingested_at)"
                " VALUES (%s,%s,'v1',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())",
                (article_id, ANALYSIS_MODEL, data["direction"],
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
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    async with httpx.AsyncClient(timeout=20.0) as http:
        with psycopg.connect(url) as conn:
            # TRYB WYBIERANY PRZY KAZDYM CYKLU, nie raz przy starcie.
            #
            # Klucz pojawia sie w bazie wtedy, gdy uzytkownik wpisze go w UI -
            # a nie wtedy, gdy restartujemy poda. Sprawdzanie raz na starcie
            # oznaczaloby, ze proces pobiera artykuly jeszcze dobe po tym, jak
            # klucz juz jest.
            def tryb() -> tuple[AsyncAnthropic | None, bool]:
                if args.bez_analizy:
                    return None, False
                klucz = anthropic_key(conn, wymagany=not args.auto)
                if not klucz:
                    return None, False
                return AsyncAnthropic(api_key=klucz), True

            poprzedni_tryb: bool | None = None
            while True:
                started = datetime.now(timezone.utc)
                try:
                    ai, analizuj = tryb()
                    if analizuj != poprzedni_tryb:
                        print("Tryb: " + ("pobieranie i ANALIZA" if analizuj else
                                          "wylacznie pobieranie (brak klucza Anthropic)"))
                        poprzedni_tryb = analizuj
                    s = await run_cycle(conn, ai, http, verbose=not args.quiet,
                                        analizuj=analizuj)
                    took = (datetime.now(timezone.utc) - started).total_seconds()
                    # Podsumowanie musi rozrozniac ZAPISANE od PRZEANALIZOWANYCH.
                    # Pierwsza wersja liczyla zapisane jako przeanalizowane, wiec
                    # cykl bez klucza raportowal "zapisanych 0" po zapisaniu 234
                    # artykulow - log gorszy niz zaden, bo wygladal na awarie.
                    analiza = (f"przez filtr {s['przeszly_filtr']}, "
                               f"przeanalizowanych {s['przeanalizowane']}, "
                               f"koszt ${s['koszt']:.4f}"
                               if analizuj else "bez analizy (brak klucza)")
                    print(f"[{started:%H:%M:%S}] kanałów {s['kanaly']}, wpisów {s['pobrane']}, "
                          f"nowych {s['nowe']}, zapisanych {s['zapisane']}, {analiza}, "
                          f"błędów {s['bledy']}, {took:.0f} s")
                except Exception as e:                        # noqa: BLE001
                    print(f"[{started:%H:%M:%S}] cykl przerwany: {type(e).__name__}: {e}")
                    conn.rollback()

                if args.once:
                    return 0
                await asyncio.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
