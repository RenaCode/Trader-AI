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
    BrakDostawcy, Dostawca, Odmowa, kto_wykonal, limit_dobowy_usd,
    limit_wywolan_dobowy, zbuduj_dostawce,
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


# (nazwa sekretu w bazie, zmienna srodowiskowa, nazwa dostawcy dla `zbuduj_dostawce`).
ZRODLA_KLUCZY = (
    ("anthropic_api_key", "ANTHROPIC_API_KEY", "anthropic"),
    ("gemini_api_key", "GEMINI_API_KEY", "gemini"),
)


def odczytaj_klucze(conn) -> dict[str, str | None]:
    """Same klucze, bez budowania czegokolwiek. Dwa SELECT-y i tyle.

    Wydzielone z `wybierz_dostawce`, bo odczyt i budowa maja teraz ROZNA
    CZESTOTLIWOSC: czytamy co cykl (klucz pojawia sie w bazie wtedy, gdy
    uzytkownik wpisze go w interfejsie), a dostawce budujemy dopiero wtedy,
    gdy zestaw kluczy sie zmienil - patrz `dostawca_na_cykle`.

    Zwraca WYLACZNIE `str` albo `None`. Nic z polaczenia - ani wiersza, ani
    kursora - nie ma prawa wyjsc z tej funkcji: zwrocony slownik idzie do
    dostawcy, ktory przezywa cykl, a polaczenie ginie razem z cyklem.
    """
    return {nazwa: _klucz(conn, sekret, env)
            for sekret, env, nazwa in ZRODLA_KLUCZY}


def wybierz_dostawce(conn, wymagany: bool = True) -> Dostawca | None:
    """Dostawca zlozony z dostepnych kluczy.

    `wymagany=False` zwraca None zamiast konczyc proces - dla trybu, ktory
    potrafi pracowac bez klucza (pobieranie artykulow bez analizy).

    NIE WOLAJ TEGO W PETLI CYKLI. Zwrocony obiekt trzyma stan bezpiecznika
    i pule polaczen; zbudowany od nowa co cykl kasuje jedno i porzuca drugie.
    W petli jest `dostawca_na_cykle`, ktore uzywa tej funkcji tylko wtedy, gdy
    naprawde trzeba zbudowac nowy obiekt.
    """
    return _zbuduj_albo_skoncz(odczytaj_klucze(conn), wymagany)


def _zbuduj_albo_skoncz(klucze: dict[str, str | None],
                        wymagany: bool) -> Dostawca | None:
    d = zbuduj_dostawce(klucze)
    if d is not None:
        return d
    if not wymagany:
        return None
    sys.exit(
        "Brak klucza dostawcy analizy. Wpisz go w interfejsie "
        "(Ustawienia -> Klucz API Anthropic albo Klucz API Google Gemini) "
        "albo ustaw ANTHROPIC_API_KEY / GEMINI_API_KEY."
    )


async def zamknij_dostawce(ai: Dostawca | None) -> None:
    """Domyka pule polaczen dostawcy. NIGDY nie podnosi wlasnego wyjatku.

    Wolane w dwoch miejscach: przy przebudowie dostawcy (`dostawca_na_cykle`)
    i przy KAZDYM wyjsciu z procesu (`main`, blok `finally`).

    Cisza przy bledzie jest tu decyzja. Sprzatamy albo dlatego, ze proces sie
    konczy, albo dlatego, ze wlasnie wymieniono klucz - w obu wypadkach powod
    sprzatania jest wazniejszy od jego niepowodzenia. Wyjatek z `zamknij()`
    puszczony dalej przesloniłby prawdziwa przyczyne zakonczenia (albo zamienil
    kod wyjscia 0 w awarie) i kazalby szukac bledu w zamykaniu gniazda zamiast
    tam, gdzie on jest. Zapisujemy go w logu i idziemy dalej.
    """
    if ai is None:
        return
    try:
        await ai.zamknij()
    except Exception as e:                                   # noqa: BLE001
        print(f"    [uwaga] nie udało się zamknąć dostawcy {ai.nazwa}: "
              f"{type(e).__name__}: {e}")


async def dostawca_na_cykle(conn, poprzedni: Dostawca | None,
                            poprzednie_klucze: dict[str, str | None] | None,
                            wymagany: bool) -> tuple[Dostawca | None,
                                                     dict[str, str | None]]:
    """Dostawca, ktory PRZEZYWA cykle, ale zauwaza wpisanie nowego klucza.

    DWA WYMAGANIA, KTORE POZORNIE SIE WYKLUCZAJA

    (1) OBIEKT MA PRZEZYC CYKL. Trzyma stan bezpiecznika - licznik odmow i
    chwile, do ktorej wyczerpanego dostawcy nie pytamy (`llm.Dostawca`).
    Budowany od nowa co cykl zaczyna ten stan od zera, wiec zwarcie „na 60
    minut" trwalo dokladnie tyle, co cykl: w logu z 2026-09-10 komunikat
    „[llm] triaz: gemini — wyczerpany limit DOBOWY; przechodze na anthropic na
    60 min" wraca co ~6 minut zamiast raz na godzine, a kazde takie odkrycie
    jest platne. Trzyma tez PULE POLACZEN (httpx u Gemini, SDK u Anthropica),
    ktorych nikt po nim nie domykal - 338 cykli w jednym procesie to bylo 338
    porzuconych pul.

    (2) KLUCZ MA ZADZIALAC BEZ RESTARTU. Pojawia sie w bazie wtedy, gdy
    uzytkownik wpisze go w interfejsie, a nie wtedy, gdy restartujemy poda.
    Odczyt raz na starcie oznaczalby, ze proces pobiera artykuly bez analizy
    jeszcze dobe po tym, jak klucz juz jest.

    POGODZENIE: ROZDZIELAMY ODCZYT OD BUDOWY. Klucze czytamy co cykl - to dwa
    SELECT-y, koszt zerowy wobec pojedynczego wywolania LLM. Obiekt budujemy
    tylko wtedy, gdy zestaw kluczy sie ZMIENIL. Zestaw bez zmian znaczy ten sam
    obiekt, wiec bezpiecznik i pule zyja dalej; zmiana znaczy nowy obiekt, wiec
    nowy klucz dziala od nastepnego cyklu.

    DLACZEGO NIE `DostawcaMieszany.przejmij_stan`. llm.py dopuszcza tez druga
    droge: budowac co cykl i przenosic stan bezpiecznika. Ona jednak leczy tylko
    skutek (1) - pul polaczen nie ratuje wcale, bo nowa instancja to i tak nowy
    `httpx.AsyncClient` i nowy `AsyncAnthropic`. Trwaly obiekt zamyka oba skutki
    jednym warunkiem, zamiast latac drugi osobno. `przejmij_stan` zostaje jako
    droga dla wolajacego, ktory trwalego obiektu trzymac nie moze.

    ZMIANA KLUCZA NIE PRZENOSI STANU - I TAK MA BYC. Nowy klucz to jedyna rzecz,
    ktora naprawia awarie trwala; przeniesienie na niego starego odciecia
    kazaloby wlascicielowi czekac szesc godzin po doladowaniu konta. Stary obiekt
    idzie do `zamknij_dostawce`, bo wymiana klucza bez zamkniecia bylaby po
    prostu kolejnym wyciekiem puli, tylko rzadszym.

    NAJPIERW BUDUJEMY, DOPIERO POTEM ZAMYKAMY STAREGO - i to jest warunek, a nie
    kosmetyka. Budowa moze sie nie udac: `DostawcaAnthropic` importuje pakiet
    `anthropic` leniwie, wiec dopisanie klucza Anthropica w obrazie zlozonym pod
    samo Gemini konczy sie tu wyjatkiem. Przy odwrotnej kolejnosci stary dostawca
    byl juz wtedy ZAMKNIETY, a wyjatek przerywal przypisanie - `ai` zostawalo
    przy zamknietym obiekcie, `klucze_ai` przy STARYM zestawie kluczy. Gdy
    wlasciciel cofnal nieudany klucz, zestaw znowu zgadzal sie z zapamietanym,
    warunek „bez zmian" zwracal tego zamknietego dostawce i kazde wywolanie do
    konca zycia procesu padalo na „client has been closed" - cicho, bo w logu
    dalej stalo „Tryb: pobieranie i ANALIZA".

    Przy tej kolejnosci nieudana budowa nie rusza NICZEGO: stary dostawca zyje
    dalej ze swoja pula i swoim bezpiecznikiem, a zapamietane klucze wciaz go
    opisuja. Dwie pule naraz nie istnieja - zamkniecie idzie zaraz po udanej
    budowie, w tej samej funkcji.

    POLACZENIE ZOSTAJE PRZY CYKLU. `conn` jest tu wylacznie po to, zeby odczytac
    klucze; do dostawcy trafia sam slownik `str | None` (patrz `odczytaj_klucze`
    i `llm.zbuduj_dostawce` - czysta funkcja bez bazy). Gdyby dostawca zatrzymal
    polaczenie albo kursor, przezylby cykl razem z nimi i po pierwszym restarcie
    bazy pracowal na zerwanym polaczeniu - czyli dokladnie ta awaria, przed ktora
    broni `psycopg.connect` w kazdym cyklu.
    """
    klucze = odczytaj_klucze(conn)
    if poprzednie_klucze is not None and klucze == poprzednie_klucze:
        return poprzedni, poprzednie_klucze
    nowy = _zbuduj_albo_skoncz(klucze, wymagany)
    await zamknij_dostawce(poprzedni)
    return nowy, klucze


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


# Parametry, ktore opisuja SKAD przyszedl czytelnik, a nie CO czyta. Ten sam
# artykul przychodzi z nimi w roznych postaciach z roznych kanalow.
#
# NAZWY DOKLADNE, nie przedrostki - poza rodzina `utm_`, ktora przedrostkiem
# JEST. Dopasowanie po przedrostku sciagneloby przy okazji `ref` z `refresh`
# i `source` z `source_id`, czyli parametry, ktore u kogos moga byc czescia
# adresu artykulu. Blad w te strone sklejalby rozne teksty w jeden - a to jest
# gorsze niz duplikat, bo KASUJE material zamiast go zawyzac.
PRZEDROSTKI_SLEDZACE = ("utm_",)
PARAMETRY_SLEDZACE = frozenset((
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref_src", "__twitter_impression",
    "igshid", "cmpid", "ncid", "at_medium", "at_campaign"))

# Jak daleko wstecz szukamy tego samego adresu. Nie „od zawsze" z dwoch powodow:
# zapytanie trafia wtedy w indeks `article_ingested_at_idx` zamiast przegladac
# cala tabele, a redakcyjna poprawka tytulu przychodzi w minutach albo godzinach
# od pierwszej publikacji - nie po tygodniach. Adres wracajacy po miesiacu to
# najczesciej nowy material pod starym slugiem, wiec zaslanianie go byloby gorsze
# niz jego przepuszczenie.
OKNO_DEDUP_URL_H = 48


def normalizuj_url(url: str) -> str:
    """Adres w postaci, w ktorej DWIE KOPIE TEGO SAMEGO ARTYKULU sa rowne.

    DLACZEGO SAM `content_hash` NIE WYSTARCZA

    `content_hash` to sha256(tytul + zajawka), wiec KAZDA zmiana tekstu tworzy
    nowy artykul. Wydawca po dwudziestu minutach zdejmuje prefiks „BREAKING:"
    i przeredagowuje lead - ten sam URL, nowy hash. Artykul wchodzi jako nowy,
    placi drugi triaz i druga analize, a `features.compute_news` SUMUJE po
    `news.article_instrument`, wiec jedno zdarzenie wchodzi do cechy DWA RAZY
    z pelna waga. Kanaly regenerujace zajawke przy kazdym odpytaniu robia to
    przy KAZDYM cyklu - dokladnie ten ksztalt wypalil 11.09.2026 dobowy limit
    Gemini w kilkanascie minut.

    CO TU ODPADA I DLACZEGO AKURAT TO

      * schemat i „www." - `http` vs `https` oraz `www.` to ten sam zasob,
      * fragment (`#czesc-2`) - nie dociera nawet do serwera,
      * parametry sledzace - opisuja czytelnika, nie tresc (`PARAMETRY_SLEDZACE`),
      * koncowy ukosnik i wielkosc liter w hoscie.

    CZEGO TU NIE MA, SWIADOMIE: reszta parametrow zapytania zostaje. `?id=4471`
    i `?page=2` potrafia byc calym adresem artykulu, a obciecie ich skleiloby
    ze soba teksty, ktore nie maja ze soba nic wspolnego. Blad w te strone jest
    gorszy niz duplikat: duplikat tylko zawyza cechę, sklejenie KASUJE material.

    Pusty napis znaczy „nie ma czego porownywac" - i wolajacy MA to sprawdzic
    przed uzyciem (patrz `run_cycle`).
    """
    from urllib.parse import parse_qsl, urlsplit, urlunsplit, urlencode

    url = (url or "").strip()
    if not url:
        return ""
    try:
        czesci = urlsplit(url)
    except ValueError:
        # Adres nie do rozebrania nie jest powodem do przewrocenia cyklu -
        # po prostu nie bierze udzialu w deduplikacji po URL.
        return ""
    if not czesci.netloc:
        # Adres wzgledny albo smiec. Porownywanie go miedzy kanalami skleiloby
        # artykuly z roznych serwisow.
        return ""
    host = czesci.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    pytania = [(k, v) for k, v in parse_qsl(czesci.query, keep_blank_values=True)
               if k.lower() not in PARAMETRY_SLEDZACE
               and not k.lower().startswith(PRZEDROSTKI_SLEDZACE)]
    sciezka = czesci.path.rstrip("/")
    return urlunsplit(("https", host, sciezka, urlencode(pytania), ""))


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
        # Konczy sie wiec werdyktem 'rejected', a NIE 'failed' - i to jest
        # decyzja, nie skrot. Odmowa to filtr tresci dostawcy, deterministyczny
        # dla tego samego tekstu: ponawianie dawaloby tę samą odpowiedź, tylko
        # platnie. Ponawiamy wylacznie to, co moze sie udac pozniej - patrz
        # PROBY_PO_AWARII_MAX.
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
        #
        # 16000, a nie 4000, od 2026-09-23. Analiza chodzi z rozszerzonym
        # myśleniem (`thinking={"type": "adaptive"}` w `DostawcaAnthropic.
        # zapytaj`), a tokeny rozumowania licza sie do TEGO SAMEGO limitu co
        # odpowiedz - dluzszy artykul wyczerpywal wiec 4000 na samym mysleniu,
        # zanim padlo pierwsze slowo wyniku. Ucieta odpowiedz nie jest wynikiem,
        # ale JEST platna i wraca przy kazdym nadrabianiu zaleglosci: ten sam
        # artykul kupowal sobie trzy takie wywolania (PROBY_PO_AWARII_MAX),
        # a zaden nie mogl sie udac. 16000 to zdrowy domysl dla wywolania BEZ
        # strumieniowania - miesci sie pod limitem czasu klienta HTTP (180 s),
        # wiec nie zamienia jednej awarii w druga. Realne zuzycie zostaje
        # niezmienione: model konczy tam, gdzie ma co powiedziec, a limit jest
        # sufitem, nie zamowieniem.
        max_tokens=16000,
    )


# Twardy limit dobowy. Domyslnie ostrozny - przy zmierzonych 128 artykulach
# na dobe realne zuzycie to okolo 0,60 USD, wiec 5 USD zostawia szeroki zapas,
# a jednoczesnie zatrzyma petle, ktora zaczela analizowac w kolko to samo
# (np. gdy kanal zmieni identyfikatory wpisow i deduplikacja przestanie dzialac).
DAILY_LIMIT_USD = limit_dobowy_usd()

# DRUGA BRAMKA: LICZBA WYWOLAN, NIE KWOTA.
#
# Bramka kwotowa nie zadzialala ANI RAZU. 11.09.2026 potok stanal calkowicie
# przy $0,114 z $5 - zatrzymal go DARMOWY prog Gemini, liczony w zapytaniach,
# ktorego dziennik wydatkow nie widzi w ogole. Licznik pilnowal wielkosci, ktora
# nie byla wiazaca, i pokazywal przy tym "wykorzystanie 2%".
#
# Kwota i liczba wywolan mierza dwie rozne rzeczy i zadna nie zastepuje drugiej:
# petla na tanim modelu (4404 wywolania 09.09, $4,85) rozbija sie o kwote dopiero
# po godzinach, a te same 4404 wywolania przekraczaja dobowy prog dostawcy
# w kilkanascie minut. Bramka po liczbie lapie wlasnie ten ksztalt.
#
# 1500 to trzykrotnosc zdrowej doby (503-516 wywolan dziennie od 07 do 11.09)
# i jedna trzecia doby z petla (4404). Lapie ucieczke, nie dotyka pracy.
DZIENNY_LIMIT_WYWOLAN = limit_wywolan_dobowy()


class Budzet:
    """Ile DZIS wydano i ile wywolan wykonano - jedno i drugie z dziennika.

    DWIE LICZBY W JEDNYM OBIEKCIE, bo musza byc czytane i podbijane razem.
    Dopoki przez potok szla sama kwota, dolozenie drugiego ogranicznika
    znaczyloby drugi parametr w kazdej sygnaturze i drugie miejsce, w ktorym
    mozna go zapomniec - a zapomniany ogranicznik wyglada dokladnie tak samo
    jak dzialajacy.

    Stan poczatkowy czytamy z `news.api_spend`, a nie z licznika w pamieci:
    proces restartuje sie w srodku doby i licznik od zera znaczylby limit
    liczony od zera.

    DZIENNIK TO JEDNAK NIE WSZYSTKO, CO WIDZI DOSTAWCA.

    Wiersz w `api_spend` powstaje raz na WYWOLANIE LOGICZNE (jeden triaz, jedna
    analiza), a dostawca liczy ZADANIA HTTP. Jedno wywolanie triazu to u Gemini
    do pieciu POST-ow, a wywolanie zakonczone `Przeciazony`/`Wyczerpany`/
    timeoutem nie niesie `usage`, wiec `zapisz_nieudane` nie wstawia wiersza
    w ogole. Doba z presja limitu tempa: 500 wywolan logicznych, do 2500 zadan
    do Google - bramka czytala 500/1500 i meldowala "wykorzystanie 33%", gdy
    darmowy prog 500/dobe byl juz przekroczony pieciokrotnie.

    Dlatego od 2026-09-23 budzet pyta DOSTAWCE o nadwyzke zadan, ktorych
    dziennik nie widzi (`Dostawca.zadania_poza_dziennikiem`). Kwota dalej idzie
    z dziennika - tam liczy sie zuzycie tokenow, a nie liczba pakietow.

    `dostawca` jest opcjonalny wylacznie po to, zeby testy nie musialy budowac
    atrapy dostawcy dla sprawdzenia samej bramki kwotowej. Potok podaje go
    zawsze (`run_cycle`), a bez niego bramka wraca do starego, zanizonego
    pomiaru - czyli jest to domysl BEZPIECZNY DLA TESTU, nie dla produkcji.
    """

    def __init__(self, usd: float, wywolania: int, dostawca=None):
        self.usd = usd
        self.wywolania = wywolania
        self._dostawca = dostawca

    def dolicz(self, usd: float) -> None:
        """Jedno wywolanie u dostawcy. Wolane RAZEM z `record_spend`.

        Koszt bywa zerowy (darmowy prog dostawcy), wywolanie nie jest wtedy
        mniej prawdziwe - i wlasnie takie wywolania wypalily limit 11.09.
        """
        self.usd += usd
        self.wywolania += 1
        if self._dostawca is not None:
            # Meldunek w druga strone: to zadanie MA juz swoj wiersz w dzienniku,
            # wiec nie nalezy do nadwyzki. Bez tego kazde udane wywolanie
            # liczyloby sie bramce dwa razy.
            self._dostawca.zanotuj_rozliczenie()

    def zadania(self) -> int:
        """Ile zadan dostawca policzyl sobie dzis - z dziennika I z nadwyzki.

        To jest wielkosc, ktora porownuje sie z progiem dostawcy. `self.wywolania`
        pokrywa to, co jest w `api_spend` (takze z wczesniejszych procesow),
        a nadwyzka - ponowienia i odmowy bez `usage` z tego procesu.
        """
        nadwyzka = (self._dostawca.zadania_poza_dziennikiem()
                    if self._dostawca is not None else 0)
        return self.wywolania + nadwyzka

    def wyczerpany(self) -> str:
        """Pusty napis znaczy 'wolno pytac dalej'. Inaczej - powod do logu."""
        if self.usd >= DAILY_LIMIT_USD:
            return (f"limit dobowy ${DAILY_LIMIT_USD:.2f} osiągnięty "
                    f"(wydano ${self.usd:.4f})")
        zadan = self.zadania()
        if zadan >= DZIENNY_LIMIT_WYWOLAN:
            # Komunikat rozroznia OBIE liczby, bo one sie rozjezdzaja i to
            # wlasnie rozjazd jest tu informacja: "1512 żądań (wierszy w
            # dzienniku 604)" mowi wprost, ze potok pali prog dostawcy na
            # ponowieniach, a nie na pracy.
            return (f"limit dobowy {DZIENNY_LIMIT_WYWOLAN} żądań do dostawcy "
                    f"osiągnięty ({zadan}; wywołań w dzienniku {self.wywolania})")
        return ""


def zapisz_nieudane(cur, conn, ai, model: str, purpose: str, blad: Exception,
                    *, budzet: "Budzet | None" = None) -> float:
    """Koszt wywolania, ktore nic nie dalo. Zwraca wydana kwote (0, gdy nieznana).

    Wywolanie, ktore poszlo do dostawcy i wrocilo bezuzyteczne - odmowa, odpowiedz
    ucieta na limicie, pusta odpowiedz - JEST PLATNE. Bez tego zapisu limit dobowy
    nie widzi tych pieniedzy, a to wlasnie one potrafia sie powtarzac w petli:
    artykul, ktorego analiza zawsze konczy sie na limicie tokenow, wraca przy
    kazdym cyklu nadrabiania zaleglosci.

    Blad sieciowy nie niesie zuzycia i wtedy nie ma czego zapisywac - `usage`
    jest wtedy zerowe, a wiersz z samymi zerami tylko zasmiecalby tabele.

    `model` MA BYC tym, ktory naprawde wykonal wywolanie (`llm.kto_wykonal`),
    a nie tym, o ktory prosilismy: wedlug jego stawki liczony jest tu koszt,
    wiec wpisanie modelu zadanego dawaloby kwote z cennika dostawcy, ktory tej
    pracy nie wykonal.
    """
    u = getattr(blad, "usage", None)
    if not u or not (u.get("input") or u.get("output")):
        return 0.0
    koszt = ai.koszt(model, u)
    record_spend(cur, model, purpose, u, koszt, ok=False)
    # Wywolanie nieudane TEZ JEST WYWOLANIEM u dostawcy i tez zjada jego prog
    # dobowy. `budzet` jest opcjonalny wylacznie dlatego, ze nazwany - wolajacy
    # w potoku podaje go zawsze.
    if budzet is not None:
        budzet.dolicz(koszt)
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


def wywolania_dzis(cur) -> int:
    """Ile zapytan poszlo dzis do dostawcow - z tego samego dziennika co kwota.

    Doba liczona tak samo jak w `spent_today` (`date_trunc('day', now())`, strefa
    bazy to UTC), zeby obie bramki odnawialy sie w tej samej chwili. Dwie rozne
    granice doby znaczylyby okno, w ktorym jedna juz zwolnila, a druga jeszcze
    nie - i nikt by nie wiedzial, ktora wlasnie zatrzymala potok.
    """
    cur.execute("SELECT count(*) AS n FROM news.api_spend"
                " WHERE event_time >= date_trunc('day', now())")
    return int(cur.fetchone()["n"])


def puste_stats(limit: bool = False) -> dict:
    """Komplet licznikow cyklu - JEDNO miejsce, w ktorym ten slownik powstaje.

    Byly dwa: pelny na koncu `run_cycle` i skrocony na sciezce osiagnietego
    limitu. Skrocony zamienial zadzialanie bezpiecznika w `KeyError`, czyli
    w "cykl przerwany" - awarie tam, gdzie zadzialala ochrona. Dopisanie klucza
    w jednym miejscu i zapomnienie o drugim jest bledem, ktorego nie widac
    do dnia, w ktorym limit sie osiagnie.
    """
    return {"kanaly": 0, "pobrane": 0, "nowe": 0, "przeszly_filtr": 0,
            "przeanalizowane": 0, "zapisane": 0, "odsiane": 0, "zaleglosci": 0,
            "koszt": 0.0, "bledy": 0, "opisy_bledow": [],
            # Stan POTOKU, nie przebieg cyklu - uzupelnia `diagnoza_potoku`.
            "odciety": "", "limit_powod": "", "cisza": "",
            # Artykuly ZAPISANE, ale nieocenione: budzet albo odciety dostawca.
            # Wroca w `nadrob_zaleglosci` z nietknietym licznikiem prob.
            "pominiete": 0,
            # Wiersze odrzucone przez ograniczenie bazy MIMO ze oba pytania
            # dedupu uznaly je za nowe. Osobny licznik, a nie `bledy`: `bledy`
            # liczy kanaly, ktore nie odpowiedzialy, i idzie do `bledy_kanalow`
            # w pulsie. Zlanie tych dwoch rzeczy w jedna liczbe zamieniloby
            # „baza odrzucila zapis" w „kanal RSS nie odpowiedzial" - dwie rozne
            # awarie i dwa rozne miejsca, do ktorych trzeba pojsc.
            "kolizje_zapisu": 0,
            "czekaja": 0, "porzucone": 0, "od_ostatniej_analizy_h": None,
            "limit": limit}


def diagnoza_potoku(cur) -> dict:
    """Ile pracy CZEKA, ile juz PORZUCONO i jak dawno powstala ostatnia analiza.

    To sa liczby o STANIE POTOKU, a nie o przebiegu tego cyklu, i wlasnie
    dlatego sa potrzebne. Cykl, ktory nie mial nic do zrobienia, i cykl, ktory
    nie byl w stanie zrobic niczego, wygladaja w licznikach przebiegu
    identycznie: zero, zero, zero. Rozroznia je dopiero pytanie, czy cokolwiek
    czekalo i kiedy ostatnio cokolwiek powstalo.

    `porzucone` to artykuly, ktore wyczerpaly limit prob i nie wroca juz nigdy.
    Na produkcji bylo ich 106 i nie mowila o nich ANI JEDNA linia logu.

    Limit prob jest PARAMETREM zapytania, a nie stala wpisana w SQL: stala
    w dwoch miejscach rozjezdza sie przy pierwszej zmianie, a tutaj rozjazd
    objawilby sie liczba w logu, ktorej nikt nie ma jak zweryfikowac.
    """
    cur.execute(
        "SELECT"
        " count(*) FILTER (WHERE a.triage_verdict IS NULL"
        "                    OR (a.triage_verdict = 'failed'"
        "                        AND a.triage_attempts < %(proby)s)"
        "                    OR (a.triage_verdict = 'accepted'"
        "                        AND (a.analysis_verdict IS NULL"
        "                             OR (a.analysis_verdict = 'failed'"
        "                                 AND a.analysis_attempts < %(proby)s)))"
        "                 ) AS czekaja,"
        " count(*) FILTER (WHERE (a.triage_verdict = 'failed'"
        "                         AND a.triage_attempts >= %(proby)s)"
        "                    OR (a.analysis_verdict = 'failed'"
        "                        AND a.analysis_attempts >= %(proby)s)"
        "                 ) AS porzucone"
        " FROM news.article a"
        " LEFT JOIN news.article_analysis an ON an.article_id = a.id"
        " WHERE an.id IS NULL"
        "   AND a.event_time > now() - make_interval(hours => %(okno)s)",
        {"proby": PROBY_PO_AWARII_MAX, "okno": OKNO_ZALEGLOSCI_H})
    r = cur.fetchone() or {}
    # Wiek ostatniej analizy liczymy z `ingested_at`, czyli z chwili, w ktorej
    # MY poznalismy wynik - nie z daty publikacji artykulu. Pytanie brzmi
    # "kiedy ten potok ostatnio cos wyprodukowal", a nie "jak swiezy byl news".
    cur.execute("SELECT EXTRACT(EPOCH FROM (now() - max(ingested_at))) / 3600"
                " AS h FROM news.article_analysis")
    w = cur.fetchone() or {}
    h = w.get("h")
    return {"czekaja": int(r.get("czekaja") or 0),
            "porzucone": int(r.get("porzucone") or 0),
            "od_ostatniej_analizy_h": float(h) if h is not None else None}


def opis_ciszy(s: dict, analizuj: bool) -> str:
    """DLACZEGO w tym cyklu nie powstala ani jedna analiza. Pusty, gdy powstala.

    NIEPUSTY ZAWSZE, gdy `analizuj` i potok NIE WYPRODUKOWAL ANI JEDNEJ ANALIZY.
    To jest warunek twardy, nie uprzejmosc wobec czytajacego.

    „Ani jednej analizy" znaczy `przeanalizowane == 0` I `zaleglosci == 0` -
    a do 2026-09-23 znaczylo tylko to pierwsze. `nadrob_zaleglosci` zlicza swoje
    analizy WYLACZNIE w `stats["zaleglosci"]` (patrz tam), wiec cykl, w ktorym
    kanaly nie dały nic nowego, ale nadrabianie przeanalizowalo 12 zaleglych
    artykulow, konczyl sie linia:

        ⚠ BEZ ANALIZ (brak artykułów do analizy); ostatnia 0 h temu

    Zdanie wewnetrznie sprzeczne i nieprawdziwe - analizy powstaly, a „ostatnia
    0 h temu" bylo tego dowodem w tej samej linii. Ostrzezenie pojawiajace sie
    przy ZDROWEJ pracy uczy czytac je nieuwaznie akurat wtedy, gdy dotyczy
    prawdziwej ciszy. Falszywy alarm psuje czujke skuteczniej niz jej brak.

    Linia z produkcji, powtarzana co piec minut przez dwie i pol doby:

        [04:55:05] kanałów 4, wpisów 100, nowych 0, zapisanych 0, przez filtr 0,
        odsianych 0, przeanalizowanych 0, koszt $0.0000, błędów 0, 1 s

    Wszystko w niej bylo prawda. `news.article_analysis` nie dostalo w tym
    czasie ANI JEDNEGO wiersza, cecha `news_sent` byla martwa, 106 artykulow
    lezalo trwale porzuconych - i nic z tego nie dalo sie z tej linii odczytac,
    bo cisza potoku wyglada w licznikach przebiegu tak samo jak spokojna doba
    bez newsow. Cisza ma byc odroznialna od pracy Z SAMEJ LINII LOGU.
    """
    if not analizuj or s.get("przeanalizowane") or s.get("zaleglosci"):
        return ""
    powody = []
    if s.get("odciety"):
        powody.append(f"dostawca odcięty — {s['odciety']}")
    if s.get("limit_powod"):
        powody.append(s["limit_powod"])
    if s.get("porzucone"):
        powody.append(f"porzuconych po wyczerpaniu prób: {s['porzucone']}")
    if not powody:
        powody.append(f"czeka {s['czekaja']}, żaden nie doszedł do analizy"
                      if s.get("czekaja") else "brak artykułów do analizy")
    if s.get("pominiete"):
        powody.append(f"zapisanych bez oceny: {s['pominiete']}")
    wiek = s.get("od_ostatniej_analizy_h")
    kiedy = "NIGDY" if wiek is None else f"{wiek:.0f} h temu"
    return f"  ⚠ BEZ ANALIZ ({'; '.join(powody)}); ostatnia {kiedy}"


def cisza_zamiast_pracy(s: dict, analizuj: bool) -> str:
    """Czy ten cykl to CISZA POTOKU, a nie spokojna doba. Pusty napis = praca.

    DLACZEGO TO NIE JEST TO SAMO CO `opis_ciszy`

    `opis_ciszy` pisze do LOGU i odzywa sie takze wtedy, gdy nie bylo czego
    analizowac - bo czytajacy log ma prawo wiedziec, ze cykl nic nie wyprodukowal.
    Ta funkcja odpowiada na wezsze pytanie: czy stan potoku ma zatrzymac PULS.
    Alarm o „braku analiz" w dobie, w ktorej kanaly nie dały nic nowego, byłby
    falszywy - a falszywy alarm psuje czujke skuteczniej niz jej brak.

    DLACZEGO PULS W OGOLE MA SIE O TO POTYKAC

    `puls.bij` bezwarunkowo ustawia `last_beat_at = now()`, a
    `dozor.ocen_puls_workerow` alarmuje WYLACZNIE wiekiem tego znacznika (mowi to
    wprost jego wlasny docstring). Cykl, w ktorym obaj dostawcy byli odcieci albo
    limit dobowy byl wyczerpany, konczyl sie bez wyjatku: artykuly zapisane,
    `przeanalizowane = 0`, `odciety` wypelniony - i bil zdrowy puls co piec minut.
    `kubectl get pods` pokazywal `Running`, czujka milczala, a
    `news.article_analysis` nie dostawalo ani jednego wiersza.

    To jest DOKLADNIE mechanizm ciszy 09.09-22.09.2026. Jedynym sladem byla
    linia `⚠ BEZ ANALIZ` na stdout poda, ktorego nikt nie czyta. Trzynascie dni.

    WARUNEK JEST WASKI CELOWO. Alarmujemy tylko wtedy, gdy potok MIAL CO ROBIC
    i nie zrobil nic: `czekaja > 0` (sa artykuly bez analizy w oknie 24 h),
    a `przeanalizowane + zaleglosci == 0`. Doba bez newsow nie jest awaria;
    doba, w ktorej 40 artykulow czeka, a zaden nie przeszedl, jest.
    """
    if not analizuj:
        # Bez klucza dostawcy brak analiz jest stanem ZADANYM, nie awaria.
        # Ta sciezka ma wlasna widocznosc: „Tryb: wylacznie pobieranie".
        return ""
    if s.get("przeanalizowane") or s.get("zaleglosci"):
        return ""
    if not s.get("czekaja"):
        return ""
    powody = []
    if s.get("odciety"):
        powody.append(f"dostawca odcięty — {s['odciety']}")
    if s.get("limit_powod"):
        powody.append(s["limit_powod"])
    if s.get("porzucone"):
        powody.append(f"porzuconych po wyczerpaniu prób: {s['porzucone']}")
    if not powody:
        powody.append("żaden nie doszedł do analizy")
    wiek = s.get("od_ostatniej_analizy_h")
    kiedy = "NIGDY" if wiek is None else f"{wiek:.0f} h temu"
    return (f"BEZ ANALIZ: czeka {s['czekaja']}, przeanalizowanych 0 "
            f"({'; '.join(powody)}); ostatnia analiza {kiedy}")


# Ile razy artykul moze wrocic na ocene PO AWARII DOSTAWCY.
#
# Awaria nie jest werdyktem: 503, zerwane polaczenie albo odpowiedz ucieta na
# limicie tokenow nie mowia nic o tym, czy artykul jest wart analizy. Gdyby taki
# artykul zostawal odrzucony na zawsze, kilkuminutowa niedostepnosc dostawcy
# kasowalaby cale okno newsow bez sladu.
#
# Ale ponawianie BEZ LIMITU jest tym samym bledem, ktory ten plik naprawia, tylko
# wolniejszym: bledy trwale - wyczerpany kredyt, zly klucz, model wycofany -
# powtarzaja sie identycznie przy kazdej probie, a kazda z nich jest platna
# (patrz `zapisz_nieudane`). Trzy proby wystarczaja na przerwe u dostawcy
# i koncza sie, zanim zdaza zjesc budzet.
PROBY_PO_AWARII_MAX = 3

# Minimalny odstep miedzy probami. BEZ NIEGO LIMIT WYZEJ NIE ZNACZY NIC.
#
# Nadrabianie zaleglosci idzie na koncu TEGO SAMEGO cyklu, wiec artykul, ktorego
# triaz przed chwila padl, poszedlby do dostawcy jeszcze raz kilkanascie sekund
# pozniej - prosto w te sama niedostepnosc. Trzy proby konczylyby sie w poltora
# cyklu i minutowa awaria kasowalaby artykul NA ZAWSZE, czyli dokladnie to,
# przed czym ma chronic sama mozliwosc ponowienia.
#
# 900 s to trzy cykle produkcyjne (--interval 300); trzy proby mieszcza sie
# w godzinie, a wiec z zapasem w oknie doby, w ktorym artykul moze jeszcze
# cokolwiek zasilic (OKNO_ZALEGLOSCI_H).
PRZERWA_PO_AWARII_S = 900


def zapisz_werdykt_triazu(cur, article_id: int, werdykt: str, powod: str,
                          model: str | None) -> None:
    """Slad po ocenie taniego filtru - JEDYNA rzecz, ktora chroni przed petla.

    Bez tego zapisu artykul odrzucony przez triaz nie roznil sie w bazie od
    artykulu, ktorego nigdy nie widzielismy, wiec wracal jako "nowy" przy kazdym
    cyklu. Przy odrzuceniu ~95% strumienia i cyklu co 5 minut oznaczalo to ~288
    platnych ocen tego samego tekstu na dobe zamiast jednej.

    Licznik prob rosnie WYLACZNIE przy 'failed'. Werdykt - przepuszczony albo
    odrzucony - jest ostateczny i nie ma czego liczyc.
    """
    cur.execute(
        "UPDATE news.article SET triage_verdict = %s, triage_reason = %s,"
        " triage_model = %s, triage_at = now(),"
        " triage_attempts = triage_attempts + %s WHERE id = %s",
        (werdykt, (powod or "")[:2000], model,
         1 if werdykt == "failed" else 0, article_id),
    )


def zapisz_werdykt_analizy(cur, article_id: int, werdykt: str) -> None:
    """To samo o jeden etap dalej, dla drogiego modelu.

    `relevant=false` nie zapisuje wiersza do `news.article_analysis`, wiec bez
    tego znacznika artykul wracalby w zaleglosciach i byl analizowany w kolko -
    ta sama petla, ale za cene analizy, nie triazu.
    """
    cur.execute(
        "UPDATE news.article SET analysis_verdict = %s, analysis_at = now(),"
        " analysis_attempts = analysis_attempts + %s WHERE id = %s",
        (werdykt, 1 if werdykt == "failed" else 0, article_id),
    )


def wolno_ponowic(kiedy) -> bool:
    """Czy od ostatniej nieudanej proby minela PRZERWA_PO_AWARII_S.

    Brak znacznika (wiersz sprzed migracji 0044) traktujemy jak "dawno" -
    inaczej jedna kolumna bez wartosci zablokowalaby ponowienia na zawsze.
    """
    if kiedy is None:
        return True
    if kiedy.tzinfo is None:
        kiedy = kiedy.replace(tzinfo=timezone.utc)
    return kiedy < datetime.now(timezone.utc) - timedelta(seconds=PRZERWA_PO_AWARII_S)


def etap_do_wykonania(a: dict) -> str | None:
    """Czego zaleglemu artykulowi jeszcze brakuje: 'triaz', 'analiza' albo nic.

    POLITYKA PONAWIANIA JEST TUTAJ, A NIE W SQL-u. Zapytanie o zaleglosci odsiewa
    to, co jest rozstrzygniete NA ZAWSZE (odrzucone przez triaz), bo tego jest
    95% tabeli i nie ma po co tego czytac. Ile razy wolno ponowic AWARIE, wie
    wylacznie ten kod - stala w predykacie indeksu rozjechalaby sie ze stala
    w Pythonie przy pierwszej zmianie i nikt by tego nie zauwazyl.
    """
    werdykt = a.get("triage_verdict")
    if werdykt is None:
        return "triaz"
    if werdykt == "failed":
        return "triaz" if _mozna_jeszcze(a, "triage") else None
    if werdykt != "accepted":
        return None                       # 'rejected' - werdykt, nie awaria
    analiza = a.get("analysis_verdict")
    if analiza is None:
        return "analiza"
    if analiza == "failed":
        return "analiza" if _mozna_jeszcze(a, "analysis") else None
    return None                           # 'relevant' / 'irrelevant' - ocena zapadla


def _mozna_jeszcze(a: dict, etap: str) -> bool:
    return ((a.get(f"{etap}_attempts") or 0) < PROBY_PO_AWARII_MAX
            and wolno_ponowic(a.get(f"{etap}_at")))


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
                            symbols: list[str], budzet: Budzet, stats: dict,
                            verbose: bool) -> None:
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

    CZEGO TU CELOWO NIE MA

    Artykulow, ktore triaz JUZ OCENIL. Samo zapisywanie artykulu przed decyzja
    triazu nie wystarcza: bez tego warunku odrzucony artykul nie wracalby jako
    "nowy" z kanalu RSS, tylko jako "zalegly" z wlasnej bazy - ta sama petla
    tylnymi drzwiami, tylko wolniejsza i trwalsza, bo baza nie zapomina tak jak
    RSS.
    """
    # ODCIETY DOSTAWCA - WYCHODZIMY PRZED CZYTANIEM BAZY.
    #
    # Kontrakt `llm.Dostawca` mowi wprost: gdy `czy_ma_czym_pytac()` zwraca
    # False, wolajacy ma PRZESTAC wolac `triage`/`analyze`. Do 12.09.2026 nikt
    # tego warunku nie sprawdzal (`grep -c czy_ma_czym_pytac` w tym pliku dawal
    # zero), wiec zamiast wyjsc, potok szedl przez cala liste zaleglosci
    # i kazdemu artykulowi zapisywal 'failed'.
    if not ai.czy_ma_czym_pytac():
        stats["odciety"] = ai.powod_niedostepnosci()
        return

    # Werdykt 'rejected' odpada JUZ W BAZIE, bo to 95% tabeli. Reszta warunkow
    # (limit ponowien) jest w `etap_do_wykonania` - patrz komentarz tam.
    #
    # `triage_at` I `analysis_at` MUSZA TU BYC. Stoi na nich `wolno_ponowic`,
    # a kolumna niepobrana to w slowniku brak klucza, czyli `None`, czyli
    # "dawno" - i odstep PRZERWA_PO_AWARII_S przestaje obowiazywac, po cichu.
    # Tak wlasnie 11.09.2026 kazdy z 106 artykulow wyczerpal trzy proby w 301
    # sekundach zamiast w 1800, po czym zostal porzucony na zawsze. Dopisujac
    # tu kolumne, dopisz ja takze do atrapy w tescie - atrapa oddajaca wiersz
    # spoza SELECT-a pilnuje zachowania, ktorego produkcja nie wykonuje.
    cur.execute(
        "SELECT a.id, a.title, a.summary, a.event_time, a.triage_verdict,"
        " a.triage_at, a.triage_attempts, a.analysis_verdict,"
        " a.analysis_at, a.analysis_attempts,"
        " f.name AS feed"
        " FROM news.article a"
        " JOIN news.feed f ON f.id = a.feed_id"
        " LEFT JOIN news.article_analysis an ON an.article_id = a.id"
        " WHERE an.id IS NULL"
        "   AND a.event_time > now() - make_interval(hours => %s)"
        "   AND (a.triage_verdict IS NULL"
        "        OR a.triage_verdict IN ('accepted', 'failed'))"
        " ORDER BY a.event_time DESC", (OKNO_ZALEGLOSCI_H,))
    zalegle = cur.fetchall()
    if not zalegle:
        return
    if verbose:
        print(f"  zaległości do przeanalizowania: {len(zalegle)}")

    for a in zalegle:
        powod = budzet.wyczerpany()
        if powod:
            stats["limit_powod"] = powod
            if verbose:
                print(f"    [stop] {powod}, zostało "
                      f"{len(zalegle) - stats['zaleglosci']} zaległych")
            break
        # STAN DOSTAWCY ZMIENIA SIE W TRAKCIE PETLI - pierwszy artykul dostaje
        # `Wyczerpany` i dopiero przez niego bezpiecznik odcina dostawce.
        # Warunek sprzed petli tego nie zlapie.
        if not ai.czy_ma_czym_pytac():
            stats["odciety"] = ai.powod_niedostepnosci()
            break
        etap = etap_do_wykonania(a)
        if etap is None:
            continue
        # Artykul, ktorego triaz juz przepuscil, NIE IDZIE NA TRIAZ DRUGI RAZ.
        # Brakuje mu wylacznie analizy - tanszy model nie powie o nim nic nowego,
        # a zaplacilibysmy za to samo zdanie po raz drugi.
        if etap == "triaz":
            try:
                worth, why, tri_usage = await triage(ai, a["title"], a["summary"] or "")
            except BrakDostawcy as e:
                # `BrakDostawcy` TO NIE PROBA I NIE WERDYKT.
                #
                # Kontrakt `llm.Dostawca`: "wywolanie NIE poszlo do API". Nikogo
                # nie zapytano, wiec nie ma kosztu do zapisania, nie ma awarii
                # dostawcy do policzenia i - przede wszystkim - nie ma czego
                # dopisac do `triage_attempts`. Ten licznik mierzy PROBY
                # U DOSTAWCY; podbicie go tutaj kaze bazie twierdzic, ze artykul
                # byl oceniany trzy razy, gdy nie byl ani razu.
                #
                # Tak zginelo 106 artykulow 11.09.2026 miedzy 14:13 a 22:10:
                # `triage_attempts = 3`, powod "BrakDostawcy: obaj dostawcy
                # odcieci", zaden nigdy nieoceniony, wszystkie trwale porzucone.
                stats["odciety"] = ai.powod_niedostepnosci() or str(e)
                break
            except Exception as e:                            # noqa: BLE001
                # KTO ODMOWIL, a nie kogo pytalismy - patrz `kto_wykonal`.
                # Mieszanka schodzi na dostawce zapasowego, wiec te dwie rzeczy
                # rozjezdzaja sie dokladnie wtedy, gdy log jest najbardziej
                # potrzebny.
                kto = kto_wykonal(e, ai, ai.model_triazu)
                stats["bledy"] += 1
                stats["opisy_bledow"].append(f"triaż {kto}: {type(e).__name__}: {e}")
                stracone = zapisz_nieudane(cur, conn, ai, kto.model, "triage", e,
                                           budzet=budzet)
                stats["koszt"] += stracone
                zapisz_werdykt_triazu(cur, a["id"], "failed",
                                      f"{type(e).__name__}: {e}", kto.model)
                conn.commit()
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
            budzet.dolicz(tri_cost)
            zapisz_werdykt_triazu(cur, a["id"],
                                  "accepted" if worth else "rejected", why, model_tri)
            conn.commit()
            stats["koszt"] += tri_cost
            if not worth:
                stats["odsiane"] += 1
                continue

        try:
            data, usage = await analyze(ai, a["title"], a["summary"] or "", a["feed"],
                                        a["event_time"], symbols)
        except BrakDostawcy as e:
            # Jak wyzej, o jeden etap dalej: bez wywolania nie ma proby.
            stats["odciety"] = ai.powod_niedostepnosci() or str(e)
            break
        except Exception as e:                                # noqa: BLE001
            kto = kto_wykonal(e, ai, ai.model_analizy)
            stats["bledy"] += 1
            stats["opisy_bledow"].append(f"analiza {kto}: {type(e).__name__}: {e}")
            stracone = zapisz_nieudane(cur, conn, ai, kto.model, "analysis", e,
                                       budzet=budzet)
            stats["koszt"] += stracone
            zapisz_werdykt_analizy(cur, a["id"], "failed")
            conn.commit()
            if verbose:
                print(f"    analiza padla: {type(e).__name__}: {e}")
            continue
        if not data.get("relevant"):
            zapisz_werdykt_analizy(cur, a["id"], "irrelevant")
            conn.commit()
            continue

        # Stawka i wpis w dzienniku MUSZA dotyczyc tego samego modelu. Wczesniej
        # koszt liczyl `uzyty_model`, a `record_spend` zapisywal model zadany -
        # dziennik nazywal wiec inny model, niz ten, wedlug ktorego policzono
        # kwote. Na tym dzienniku stoi limit dobowy.
        model_ana = ai.uzyty_model(ai.model_analizy, usage)
        price = ai.koszt(model_ana, usage)
        record_spend(cur, model_ana, "analysis", usage, price)
        budzet.dolicz(price)
        stats["koszt"] += price
        zapisz_werdykt_analizy(cur, a["id"], "relevant")
        zapisz_analize(cur, a["id"], model_ana, data, usage, price,
                       a["event_time"], instruments)
        conn.commit()
        stats["zaleglosci"] += 1
        if verbose:
            print(f"    [zaległy] {data['direction']:>7s} {a['title'][:60]}")


async def run_cycle(conn, ai: Dostawca | None, http: httpx.AsyncClient,
                    verbose: bool, analizuj: bool = True,
                    silniki: list[str] | None = None) -> dict:
    """Jeden cykl: pobierz, ZAPISZ, odsiej, przeanalizuj.

    KOLEJNOSC W TYM ZDANIU JEST CALA POPRAWKA. Wczesniej bylo "pobierz, odsiej,
    przeanalizuj, zapisz" i wiersz w `news.article` powstawal WYLACZNIE po udanej
    analizie. Deduplikacja pyta o `content_hash`, wiec artykul odrzucony przez
    triaz, artykul, na ktorym triaz padl, i artykul uznany przez analize za
    nieistotny nie zostawialy zadnego sladu - i wracaly jako "nowe" przy kazdym
    cyklu. Log produkcyjny z 2026-09-10 pokazuje to w kazdym z 338 cykli:
    "nowych 65, zapisanych 0". Przy odrzuceniu ~95% strumienia i cyklu co 5 minut
    to ~288 platnych ocen tego samego tekstu na dobe zamiast jednej - i stad
    dobowy limit Gemini wypalony w kilkanascie minut.

    Zapis jest DARMOWY, a decyzja platna, wiec zapis idzie pierwszy - takze dla
    artykulu za starego na analize, ktory wczesniej wypadal z cyklu bez sladu
    z tym samym skutkiem.

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

    # DOSTAWCA IDZIE DO BUDZETU, bo tylko on wie, ile ZADAN HTTP naprawde
    # poszlo - dziennik zna wylacznie wywolania logiczne. Patrz `Budzet`.
    budzet = Budzet(spent_today(cur), wywolania_dzis(cur), ai)
    powod_limitu = budzet.wyczerpany() if analizuj else ""
    if powod_limitu:
        # WYCZERPANY BUDZET SCHODZI DO TRYBU BEZ ANALIZY, A NIE POMIJA CYKLU.
        #
        # Do 12.09.2026 bylo tu `return`: osiagniecie limitu o 14:00 znaczylo,
        # ze WSZYSTKO, co kanaly opublikuja do polnocy UTC, przepada
        # bezpowrotnie - bo RSS oddaje tylko swieze wpisy, a my nie zapisywalismy
        # nic. Kontrakt `llm.Dostawca` mowi o tej samej sytuacji wprost:
        # "PRZERWANIE CALEGO CYKLU byloby lekarstwem gorszym od choroby".
        #
        # Limit ma zatrzymac WYDAWANIE PIENIEDZY. Pobieranie i zapis sa darmowe
        # i dzialaja dalej; analize dolozy `nadrob_zaleglosci` po polnocy, o ile
        # artykul zmiesci sie jeszcze w oknie OKNO_ZALEGLOSCI_H. Czesc i tak
        # przepadnie - ale czesc, a nie wszystko, i widac ktora.
        print(f"{powod_limitu}. Schodzę na sam zapis — wznowienie analizy "
              f"po północy UTC.")
        analizuj = False

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

    stats = puste_stats(limit=bool(powod_limitu))
    stats["limit_powod"] = powod_limitu
    stats["kanaly"] = len(feeds)

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

            # DRUGA DEDUPLIKACJA - PO ADRESIE, nie po tresci.
            #
            # `content_hash` lapie tylko IDENTYCZNY tekst. Redakcyjna poprawka
            # tytulu („BREAKING:" zdjete po dwudziestu minutach) daje ten sam URL
            # i nowy hash, wiec artykul wchodzil jako nowy: drugi platny triaz,
            # druga platna analiza, a `features.compute_news` SUMUJE po
            # `news.article_instrument` - jedno zdarzenie wchodzilo do cechy dwa
            # razy z pelna waga. Kanal regenerujacy zajawke przy kazdym odpytaniu
            # robil to co cykl; ten ksztalt wypalil 11.09.2026 limit Gemini
            # w kilkanascie minut.
            #
            # ADRES BIERZEMY Z WPISU, NIGDY Z KANALU. `link` bywa pusty i nizej
            # podstawia sie wtedy `feed["url"]` - gdyby ta wartosc szla do
            # porownania, WSZYSTKIE artykuly takiego kanalu mialyby jeden adres
            # i kanal oddawalby w calym swoim zyciu jeden artykul. Pusty
            # `url_dedup` znaczy wiec „ten wpis nie bierze udzialu w dedupie po
            # URL" i tak ma byc.
            link = (getattr(entry, "link", "") or "").strip()
            url_dedup = normalizuj_url(link)

            # CO WPISUJEMY W KOLUMNE `url`, GDY WPIS NIE MA WLASNEGO ADRESU.
            #
            # Do 2026-09-23 stalo tu `getattr(entry, "link", "") or feed["url"]`,
            # czyli przy braku `link` szedl do bazy ADRES KANALU. Dwa wpisy bez
            # `link` w jednej partii dawaly wiec dwa wiersze o identycznym `url`.
            # Dzis `news.article` nie ma na tej kolumnie zadnego ograniczenia
            # (jedyne to `UNIQUE (content_hash)`), wiec konczylo sie to samym
            # powielonym adresem. Ale byl to zapis NIEPRAWDZIWY niezaleznie od
            # ograniczen: odnosnik w interfejsie prowadzil do kanalu RSS zamiast
            # do artykulu, o ktorym byl wiersz.
            #
            # Kolumna jest `NOT NULL`, wiec „nic" nie da sie zapisac. Zostaje
            # pusty napis - i to jest wybor swiadomy: pusty `url` MOWI, ze wpis
            # nie mial adresu, a adres kanalu KLAMIE, ze mial. Interfejs
            # pokazujacy pusty odnosnik jest poprawialny; pokazujacy zly odnosnik
            # wyglada na dzialajacy.
            #
            # Przy okazji znika bomba z opoznionym zaplonem: gdyby ktos dolozyl
            # kiedys unikalny indeks na `url`, powtorzony adres kanalu byl
            # pierwszym, ktory by w niego uderzyl.
            url_wpisu = url_dedup or ""

            if url_dedup:
                cur.execute(
                    "SELECT 1 FROM news.article WHERE url = %s"
                    "   AND ingested_at > now() - make_interval(hours => %s)",
                    (url_dedup, OKNO_DEDUP_URL_H))
                if cur.fetchone():
                    continue
            stats["nowe"] += 1

            published = entry_time(entry)

            # ZAPIS PRZED KAZDA DECYZJA. To jest miejsce, w ktorym zamykala sie
            # petla: dopoki wiersza nie ma, deduplikacja po `content_hash` nie ma
            # o co zahaczyc i ten sam tekst wraca jako "nowy" co pieciu minut.
            #
            # ZAPISUJEMY ADRES ZNORMALIZOWANY, bo to on jest porownywany wyzej.
            # Zapis surowego przy porownywaniu znormalizowanego dawalby dedup,
            # ktory nigdy nie trafia - czyli kod wygladajacy na dzialajacy
            # i niedzialajacy. Adres znormalizowany prowadzi do tego samego
            # zasobu, wiec odnosnik w interfejsie dalej dziala.
            #
            # `ON CONFLICT DO NOTHING` BEZ WSKAZANIA OGRANICZENIA - i to nie jest
            # niedbalstwo, tylko jedyna bezpieczna forma w TYM miejscu.
            #
            # Wersja z `(content_hash)` obsluguje DOKLADNIE JEDNO ograniczenie.
            # Kazde inne - dzis `news.article` nie ma zadnego, ale unikalny indeks
            # na `url` jest pierwsza rzecza, ktora ktos tu dolozy - wracaloby jako
            # `IntegrityError`. Ten `cur.execute` NIE LEZY W ZADNYM `try` wewnatrz
            # `run_cycle`, wiec wyjatek przewraca caly cykl: transakcja przechodzi
            # w stan `aborted`, wszystkie pozostale artykuly tej partii i wszystkie
            # kolejne kanaly przepadaja, a RSS ich JUTRO NIE ODDA. To jest ta sama
            # kategoria straty, co limit dobowy osiagniety o 14:00, tylko wywolana
            # duplikatem - czyli awaria GORSZA od tej, przed ktora dedup broni.
            #
            # Bez wskazania ograniczenia klauzula obejmuje wszystkie naraz, wiec
            # duplikat zostaje tym, czym ma byc: brakiem wiersza w `RETURNING`,
            # obsluzonym nizej przez `if not row: continue`.
            cur.execute(
                "INSERT INTO news.article (feed_id, url, title, summary, content_hash,"
                " event_time, ingested_at) VALUES (%s,%s,%s,%s,%s,%s, now())"
                " ON CONFLICT DO NOTHING RETURNING id",
                (feed["id"], url_wpisu, title, summary[:4000], h, published),
            )
            row = cur.fetchone()
            conn.commit()
            # BRAK WIERSZA MA TU DWA ROZNE ZNACZENIA i nie wolno ich zlac
            # w jedno ciche `continue`.
            #
            # 1. Ten sam tekst przyszedl drugi raz rownolegle z innego kanalu -
            #    deduplikacja zadzialala, nie ma czego oceniac, cisza jest
            #    wlasciwa.
            # 2. Wiersz odrzucilo ograniczenie, ktorego NIE przewidzielismy.
            #    Oba pytania wyzej (`content_hash` i `url`) powiedzialy „nowy",
            #    wiec konflikt oznacza albo wyscig, albo ograniczenie dolozone
            #    migracja, o ktorej ten kod nie wie. Drugie znaczy, ze dedup po
            #    URL nie zadzialal w oknie - i to jest informacja, nie szczegol.
            #
            # Rozroznienie jest tanie, bo odpowiedzi na oba pytania juz mamy.
            # Bez niego ograniczenie dolozone w przyszlosci objawialoby sie jako
            # artykuly znikajace bez sladu - czyli awaria wygladajaca jak spokojna
            # doba, dokladnie ten ksztalt, ktory ten plik naprawia w innych
            # miejscach.
            if not row:
                stats["kolizje_zapisu"] += 1
                stats["opisy_bledow"].append(
                    f"kanał {feed['name']}: zapis artykułu odrzucony przez "
                    f"ograniczenie bazy mimo że dedup uznał go za nowy "
                    f"(adres {url_wpisu[:120]!r})")
                continue
            article_id = row["id"]
            stats["zapisane"] += 1

            if not analizuj:
                kept += 1
                if verbose:
                    print(f"    [zapisany] {title[:70]}")
                continue

            # Artykuly starsze niz doba nie maja wartosci sygnalowej, a kosztuja
            # tyle samo - przy pierwszym uruchomieniu kanal potrafi zwrocic archiwum.
            #
            # Prog dotyczy WYLACZNIE analizy, a nie zapisu (patrz wyzej): material,
            # ktorego dzis nie zapiszemy, przepadnie, bo kanal RSS nie odda go
            # jutro. Werdyktu triazu tez tu NIE stawiamy - artykul nie zostal
            # oceniony, tylko przeterminowal, a `nadrob_zaleglosci` i tak patrzy
            # wylacznie na okno doby.
            if published < datetime.now(timezone.utc) - timedelta(days=1):
                continue

            # `continue`, NIE `break` - I TO JEST CALA ROZNICA.
            #
            # Zapis artykulu stoi WYZEJ w tej petli, analiza nizej. `break`
            # ucinal wiec razem z analiza takze ZAPIS pozostalych wpisow tego
            # kanalu - a kanal RSS nie odda jutro tego, czego dzis nie
            # pobralismy. Wyczerpany budzet ma zatrzymac WYDAWANIE PIENIEDZY,
            # nie zbieranie darmowego materialu; te dwie rzeczy `break`
            # sklejal w jedna.
            powod = budzet.wyczerpany()
            if powod:
                stats["limit_powod"] = powod
                if verbose and not stats["pominiete"]:
                    print(f"    [stop] {powod}")
                stats["pominiete"] += 1
                continue

            # DOSTAWCA ODCIETY - ZAPISUJEMY DALEJ, PYTAC PRZESTAJEMY.
            #
            # Dokladnie to mowi kontrakt `llm.Dostawca` ("MA DALEJ ZAPISYWAC
            # artykuly") i dokladnie tego nikt nie sprawdzal do 12.09.2026.
            # Artykul jest juz w bazie, wiec `nadrob_zaleglosci` wroci po niego
            # w oknie 24 h, gdy dostawca wroci - I NIE ZASTANIE GO Z WYPALONYM
            # LICZNIKIEM PROB.
            if not ai.czy_ma_czym_pytac():
                stats["odciety"] = ai.powod_niedostepnosci()
                stats["pominiete"] += 1
                continue

            try:
                worth, why, tri_usage = await triage(ai, title, summary)
            except BrakDostawcy as e:
                # Bezpiecznik zwarl sie w trakcie tej petli - patrz ten sam
                # warunek w `nadrob_zaleglosci`. Bez wywolania nie ma proby,
                # a pozostale wpisy tego kanalu nadal maja zostac ZAPISANE.
                stats["odciety"] = ai.powod_niedostepnosci() or str(e)
                stats["pominiete"] += 1
                continue
            except Exception as e:                            # noqa: BLE001
                # KTO ODMOWIL, a nie kogo pytalismy - patrz `kto_wykonal`.
                kto = kto_wykonal(e, ai, ai.model_triazu)
                stats["bledy"] += 1
                stats["opisy_bledow"].append(f"triaż {kto}: {type(e).__name__}: {e}")
                stracone = zapisz_nieudane(cur, conn, ai, kto.model, "triage", e,
                                           budzet=budzet)
                stats["koszt"] += stracone
                # Awaria dostawcy NIE JEST werdyktem - artykul wroci w
                # zaleglosciach, ale policzona liczba prob (PROBY_PO_AWARII_MAX)
                # nie pozwoli mu wracac w nieskonczonosc.
                zapisz_werdykt_triazu(cur, article_id, "failed",
                                      f"{type(e).__name__}: {e}", kto.model)
                conn.commit()
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
            budzet.dolicz(tri_cost)
            zapisz_werdykt_triazu(cur, article_id,
                                  "accepted" if worth else "rejected", why, model_tri)
            conn.commit()
            stats["koszt"] += tri_cost

            if not worth:
                stats["odsiane"] += 1
                if verbose:
                    print(f"    [odsiane] {title[:70]} — {why}")
                continue
            stats["przeszly_filtr"] += 1
            kept += 1

            try:
                data, usage = await analyze(ai, title, summary, feed["name"], published, symbols)
            except BrakDostawcy as e:
                stats["odciety"] = ai.powod_niedostepnosci() or str(e)
                stats["pominiete"] += 1
                continue
            except Exception as e:                            # noqa: BLE001
                kto = kto_wykonal(e, ai, ai.model_analizy)
                stats["bledy"] += 1
                stats["opisy_bledow"].append(f"analiza {kto}: {type(e).__name__}: {e}")
                stracone = zapisz_nieudane(cur, conn, ai, kto.model, "analysis", e,
                                           budzet=budzet)
                stats["koszt"] += stracone
                zapisz_werdykt_analizy(cur, article_id, "failed")
                conn.commit()
                if verbose:
                    print(f"    analiza padla: {type(e).__name__}: {e}")
                continue

            if not data.get("relevant"):
                # Ocena ZAPADLA, tylko nie ma czego zapisac do
                # `news.article_analysis`. Bez tego znacznika artykul wracalby
                # w zaleglosciach i byl analizowany DROGIM modelem w kolko.
                zapisz_werdykt_analizy(cur, article_id, "irrelevant")
                conn.commit()
                continue

            # Stawka, wpis w dzienniku i wiersz analizy - ten sam model. Rozjazd
            # miedzy nimi znaczy dziennik nazywajacy inny model, niz ten, wedlug
            # ktorego policzono kwote, a limit dobowy liczy sie wlasnie stad.
            model_ana = ai.uzyty_model(ai.model_analizy, usage)
            price = ai.koszt(model_ana, usage)
            record_spend(cur, model_ana, "analysis", usage, price)
            budzet.dolicz(price)
            stats["koszt"] += price
            stats["przeanalizowane"] += 1

            zapisz_werdykt_analizy(cur, article_id, "relevant")
            zapisz_analize(cur, article_id, model_ana, data, usage, price,
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
        await nadrob_zaleglosci(conn, cur, ai, instruments, symbols,
                                budzet, stats, verbose)

    # DIAGNOZA POTOKU NA KONIEC KAZDEGO CYKLU - patrz `opis_ciszy`.
    stats.update(diagnoza_potoku(cur))
    stats["cisza"] = opis_ciszy(stats, analizuj)
    return stats


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="jeden cykl zamiast petli")
    ap.add_argument("--interval", type=int, default=120, help="przerwa miedzy cyklami (s)")
    ap.add_argument("--quiet", action="store_true")
    # LIMIT CZASU JEDNEGO CYKLU.
    #
    # Zawieszenie NIE JEST WYJATKIEM, wiec cala obsluga bledow nizej byla na nie
    # slepa: licznik `pod_rzad` nie rosl, puls nie byl bity, a pod stal "Running"
    # i nie robil nic. Zdarzylo sie 2026-09-08: worker zamilkl na 41 minut przy
    # interwale 300 s, bo klient Anthropica czekal bez ograniczenia.
    #
    # 600 s to okolo szesciokrotnosc typowego cyklu (~100 s) i poltorakrotnosc
    # najdluzszego zaobserwowanego (374 s), wiec nie utnie pracy zdrowego cyklu.
    # Po przekroczeniu leci TimeoutError, ktory liczy sie jak kazda inna awaria
    # cyklu: puls dostaje powod, a po POD_RZAD_MAX z rzedu proces konczy sie
    # i restart poda jest WIDOCZNY zamiast cichy.
    ap.add_argument("--limit-cyklu", type=int, default=600, dest="limit_cyklu",
                    help="maksymalny czas jednego cyklu (s)")
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
        # OSOBNY licznik cykli BEZ ANI JEDNEJ ANALIZY. Nie miesza sie z
        # `pod_rzad`, bo to dwie rozne awarie i maja rozne skutki: awaria cyklu
        # po piatej z rzedu KONCZY proces, cisza potoku - nie (restart nie
        # przywroci limitu dobowego, a straciłby artykuly, ktorych kanał już nie
        # odda). Idzie do `awarii_pod_rzad` w pulsie, zeby czujka widziala nie
        # tylko „cicho", ale i „od ilu cykli".
        cisz_pod_rzad = 0
        poprzedni_tryb: str | None = None
        # DOSTAWCA ZYJE DLUZEJ NIZ CYKL - inaczej niz polaczenie wyzej.
        #
        # Polaczenie MA ginac co cykl (zerwane po restarcie bazy nie naprawi sie
        # samo), dostawca NIE MA: trzyma stan bezpiecznika i pule polaczen HTTP.
        # Te dwa cykle zycia sa rozne i nie wolno ich sklejac. Klucze trzymamy
        # obok, bo to ich zmiana - a nie uplyw cyklu - kaze zbudowac nowy obiekt.
        # Caly warunek jest w `dostawca_na_cykle`.
        ai: Dostawca | None = None
        klucze_ai: dict[str, str | None] | None = None
        # Nazwa w `risk.worker_heartbeat` - ROLA w potoku, nie egzemplarz
        # procesu. Lista silnikow i tryb ida do `detail`, bo sa argumentem.
        PULS = "news"

        try:
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
                        #
                        # CZYTAMY klucze co cykl, ale BUDUJEMY dopiero wtedy, gdy
                        # sie zmienily: obiekt dostawcy ma przezyc cykl, zeby
                        # bezpiecznik i pule polaczen nie zaczynaly od zera co
                        # piec minut. Caly warunek: `dostawca_na_cykle`.
                        if not args.bez_analizy:
                            ai, klucze_ai = await dostawca_na_cykle(
                                conn, ai, klucze_ai, wymagany=not args.auto)
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
                            # Limit obejmuje CALY cykl, nie pojedyncze wywolanie.
                            # Timeout na kliencie HTTP chroni przed jednym zawieszonym
                            # zapytaniem; ten limit chroni przed wszystkim innym, co
                            # moze stanac - takze przed kodem, ktorego jeszcze nie ma.
                            async with asyncio.timeout(args.limit_cyklu):
                                s = await run_cycle(conn, ai, http,
                                                    verbose=not args.quiet,
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
                        #
                        # CYKL BEZ ANI JEDNEJ ANALIZY NIE JEST UDANY - i to jest
                        # poprawka z 2026-09-23. Do niej warunek brzmial „cykl bez
                        # wyjatku" i nic wiecej, wiec potok, w ktorym obaj dostawcy
                        # byli odcieci, bil zdrowy puls co piec minut przez 13 dni.
                        # `dozor.ocen_puls_workerow` alarmuje WYLACZNIE wiekiem
                        # `last_beat_at`, wiec swiezy puls jest dla nadzoru
                        # rownoznaczny ze zdrowiem - a `news.article_analysis` nie
                        # dostawalo ani jednego wiersza. Szczegoly warunku
                        # (dlaczego waski) stoja w `cisza_zamiast_pracy`.
                        #
                        # ZGLOSZENIE, A NIE WYJATEK. Cisza nie konczy procesu:
                        # licznik `pod_rzad` zostaje przy awariach CYKLU, bo cykl
                        # sie udal - zapisal artykuly, ktorych kanal RSS nie odda
                        # jutro. Restart potoku nie przywroci wyczerpanego limitu
                        # ani nie doladuje konta, wiec CrashLoopBackOff bylby tu
                        # lekarstwem gorszym od choroby: traciłby material.
                        # Osobny licznik `cisz_pod_rzad` idzie do `awarii_pod_rzad`,
                        # zeby czujka widziala, jak dlugo to trwa.
                        cisza = cisza_zamiast_pracy(s, analizuj)
                        if cisza:
                            cisz_pod_rzad += 1
                            puls.zglos_awarie(conn, PULS, args.interval,
                                              cisz_pod_rzad, cisza)
                        else:
                            cisz_pod_rzad = 0
                            puls.bij(conn, PULS, args.interval,
                                     {"analiza": analizuj, "kanaly": s["kanaly"],
                                      "zapisane": s["zapisane"],
                                      "przeanalizowane": s["przeanalizowane"],
                                      "zaleglosci": s["zaleglosci"],
                                      "bledy_kanalow": s["bledy"],
                                      "silniki": silniki or []})
                    took = (datetime.now(timezone.utc) - started).total_seconds()
                    # Podsumowanie musi rozrozniac ZAPISANE od PRZEANALIZOWANYCH.
                    # Pierwsza wersja liczyla zapisane jako przeanalizowane, wiec
                    # cykl bez klucza raportowal "zapisanych 0" po zapisaniu 234
                    # artykulow - log gorszy niz zaden, bo wygladal na awarie.
                    #
                    # ODSIANE SA W LOGU OSOBNO, bo po tej poprawce "nowych 65,
                    # zapisanych 65" to zdanie prawdziwe, ale nic nie mowiace o tym,
                    # gdzie te artykuly poszly. Roznica miedzy dzisiejszym mylacym
                    # "zapisanych 0" a stanem zdrowym ma byc widoczna z jednej linii:
                    # zapisane = wszystko, co pobralismy, odsiane = ile z tego triaz
                    # odrzucil RAZ NA ZAWSZE.
                    analiza = (f"przez filtr {s['przeszly_filtr']}, "
                               f"odsianych {s['odsiane']}, "
                               f"przeanalizowanych {s['przeanalizowane']}"
                               + (f", zaległych {s['zaleglosci']}" if s['zaleglosci'] else "")
                               + f", koszt ${s['koszt']:.4f}"
                               if analizuj else "bez analizy (brak klucza)")
                    # `cisza` DOPISUJE SIE DO TEJ SAMEJ LINII, a nie obok.
                    # Osobna linia ginie w `--quiet` i w przewijaniu; ta ma byc
                    # nie do przeoczenia tam, gdzie czyta sie log.
                    print(f"[{started:%H:%M:%S}] kanałów {s['kanaly']}, wpisów {s['pobrane']}, "
                          f"nowych {s['nowe']}, zapisanych {s['zapisane']}, {analiza}, "
                          f"błędów {s['bledy']}, {took:.0f} s" + s.get("cisza", ""))
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
        finally:
            # SPRZATANIE NA KAZDYM WYJSCIU, a nie tylko na tym przewidzianym:
            # `--once`, POD_RZAD_MAX cykli pod rzad z bledem, wyjatek spoza
            # petli, sygnal. Kazde z nich konczy proces, ktory trzyma otwarta
            # pule polaczen dostawcy - a przy `--once` w trybie wsadowym to
            # jedyna okazja, zeby ja domknac.
            #
            # `zamknij_dostawce` nie podnosi wlasnego wyjatku (patrz tam), wiec
            # ten `finally` nie ma prawa ani przeslonic przyczyny zakonczenia,
            # ani zamienic kodu wyjscia 0 w awarie.
            await zamknij_dostawce(ai)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
