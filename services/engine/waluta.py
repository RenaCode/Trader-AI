#!/usr/bin/env python3
"""Druga krzywa kapitału — w walucie, w której właściciel wpłaca i wypłaca.

DLACZEGO TO MUSI ISTNIEĆ

Warunek właściciela brzmi „wpłacę 10 tysięcy i nie chcę, żeby rachunek poszedł
na minus". Cały system mierzy jednak w `quote_ccy`, czyli w USDC: `equity`
w `exec.equity_point` jest w USDC, próg `min_equity` z 0028 jest progiem w USDC,
wszystkie procenty liczą się z liczb w USDC. Rail fiatowy podmiotu unijnego to
EUR — i to w EUR (albo w PLN) właściciel naprawdę wpłacił.

Znaczy to, że stan

    kill-switch zielony, wszystkie limity w normie, rachunek 10 000 USDC

jest W PEŁNI ZGODNY ze stanem

    właściciel ma o kilka procent mniej, niż wpłacił.

Bezpiecznik jest z konstrukcji ślepy na tę stratę, bo mierzy ją w walucie,
w której jej nie widać.

To nie jest hipoteza. Zmierzone na USDCEUR z api.bybit.eu, 453 świece dzienne
2025-06-13 … 2026-09-08:

    min 0,8318   max 0,8806   zakres 5,87%

przy zmierzonym wyniku strategii około 2,9% rocznie. Ruch kursu o 5% w roku —
całkowicie zwyczajny dla pary stablecoin/euro — jest większy niż DWULETNIA
przewaga strategii. Nie jest to więc szum, o którym da się nie mówić.

CZEGO TEN MODUŁ NIE ROBI, ŚWIADOMIE

NIE ZMIENIA TEGO, W CZYM LICZY RISK KERNEL. Nie dotyka `risk_rules.py`, nie
przelicza progów, nie zapisuje niczego, z czego czyta `risk.py`. Zmiana
jednostki bezpiecznika byłaby zmianą jego ZACHOWANIA, a nie dodaniem miary —
a to jest decyzja właściciela i ma ją podjąć świadomie. Tutaj powstaje
wyłącznie DRUGA LICZBA, pokazywana obok pierwszej.

SKĄD KURS

`api.bybit.eu` wystawia parę USDCEUR publicznie, bez klucza — sprawdzone
2026-09-08 na `/v5/market/tickers` i `/v5/market/kline` (także na hoście demo).
To jest właściwa księga: podmiot unijny to ta sama giełda, na której właściciel
faktycznie wymienia USDC na euro, więc jego kurs jest kursem, po którym
naprawdę by wyszedł.

Adapter ma już `cena_odniesienia(symbol)` i JEST ONA UŻYWANA (patrz
`kurs_biezacy`): czyta z hosta podmiotu, nie wymaga klucza i zwraca None, gdy
odczyt się nie uda — czyli ma dokładnie ten kontrakt, o który tu chodzi.
Nie wystarcza jednak na historię: to ticker, jedna liczba „teraz".
`fetch_candles` też nie, bo ma na sztywno `host=PUBLIC_HOST` (api.bybit.com),
a tam pary USDCPLN nie ma w ogóle. Dlatego historia idzie własnym, minimalnym
odczytem świec z hosta podmiotu — `kursy_dzienne` niżej.

DLACZEGO KURS JEST ZAPISYWANY W CZASIE

Przeliczenie historycznej krzywej dzisiejszym kursem pokazałoby przeszłość,
której nie było: rachunek sprzed pół roku wyglądałby tak, jakby właściciel
sprzedał wtedy USDC po dzisiejszym kursie. Przy 5,9% zakresu jest to błąd
większy niż dwuletni wynik strategii — druga krzywa kłamałaby dokładnie o tyle,
o ile miała ostrzegać. To ten sam błąd, przed którym broni bitemporalność
w `feat.as_of()`.

Stąd `market.fx_rate` (migracja 0036) z dwoma znacznikami czasu i stąd zasada
świecy zamkniętej: zamknięcie świecy dziennej zaczynającej się o północy jest
ZNANE dopiero o północy następnego dnia i tym czasem jest stemplowane.

BRAK KURSU TO LUKA, NIE ZERO

Krzywa bez kursu mówi „nie przeliczono" i podaje powód. NIGDY nie pokazuje
wartości w USDC udającej euro i nigdy nie podstawia zera — jedno kłamałoby
o kilka procent, drugie o całość. Ta zasada wraca w tym projekcie wielokrotnie
(`cena_odniesienia`, `Nieuzgadnialne`, `pokrycie_wypelnien`) i tutaj obowiązuje
tak samo.

FUNKCJE LICZĄCE SĄ CZYSTE

Cała arytmetyka — dopasowanie kursu do chwili, przeliczenie krzywej, wynik
właściciela — jest w funkcjach bez bazy i bez sieci, na wzór `rozjazdy`
w `uzgodnienie.py`. Test tej logiki nie ma prawa wymagać giełdy: giełda w
teście to już nie jest test tej logiki.
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# ------------------------------------------------------------------ ustawienia

# Krok świecy, z której bierzemy historię kursu. Dzienna wystarcza: kurs
# stablecoina do euro nie ma dziennej zmienności, dla której warto by trzymać
# świece godzinowe, a rzadsza seria to mniej okazji do luki.
KROK = timedelta(days=1)

# Ile kurs może być stary, zanim przestanie opisywać daną chwilę.
#
# Przy świecy dziennej najświeższy DOSTĘPNY kurs dla chwili „teraz" ma z
# definicji do 24 godzin (zamknięcie ostatniej domkniętej świecy). Próg musi
# więc być wyraźnie większy niż doba, inaczej każdy ostatni punkt krzywej byłby
# luką. 36 godzin mieści jeden pełny krok i zapas na opóźniony odczyt, a nie
# mieści już sytuacji „źródło milczy od dwóch dni" — i o to chodzi.
MAX_WIEK = timedelta(hours=36)

# Waluta, w której mierzy silnik. Domyśl; prawdziwa wartość przychodzi
# z app.waluta_odniesienia.
MIERZONA_DOMYSLNA = "USDC"


class BrakZrodlaKursu(RuntimeError):
    """Dla tej waluty nie znamy publicznego źródła kursu.

    Wyjątek, a nie ciche przejście na EUR: raport w walucie, o którą nikt nie
    prosił, jest gorszy niż brak raportu, bo wygląda na odpowiedź.
    """


@dataclass(frozen=True)
class Zrodlo:
    """Gdzie szukać kursu danej waluty odniesienia."""
    para: str            # symbol na giełdzie, np. 'USDCEUR'
    host: str            # host PODMIOTU, na którym para istnieje
    kategoria: str       # kategoria rynku Bybit
    zastrzezenie: str | None = None   # co o tym źródle trzeba wiedzieć


# Host podmiotu unijnego. Świadomie NIE api.bybit.com: pary USDCPLN tam nie ma
# (10001 "Not supported symbols", sprawdzone 2026-09-08), a dla USDCEUR liczy
# się księga, w której właściciel naprawdę wymienia — czyli jego własnego
# podmiotu. Host da się nadpisać z ref.venue.live_host, żeby nie trzymać tej
# samej prawdy w dwóch miejscach.
HOST_EU = "https://api.bybit.eu"

ZRODLA: dict[str, Zrodlo] = {
    "EUR": Zrodlo("USDCEUR", HOST_EU, "spot"),
    "PLN": Zrodlo(
        "USDCPLN", HOST_EU, "spot",
        # Wypisane, bo różnica jakości między tymi dwoma źródłami jest realna
        # i właściciel ma prawo o niej wiedzieć, zanim przestawi ustawienie.
        # Zmierzone 2026-09-08: USDCPLN ma 132 świece dzienne (od 2026-04-30),
        # obrót dobowy 50 tys. USDC wobec 488 tys. na USDCEUR. Historia sprzed
        # 2026-04-30 NIE ISTNIEJE u źródła — krzywa w PLN sięgająca głębiej ma
        # tam lukę i tak ma ją pokazać.
        "para istnieje wyłącznie na podmiocie unijnym, historia od 2026-04-30, "
        "obrót ok. 10x cieńszy niż USDCEUR"),
}


def zrodlo_dla(waluta: str) -> Zrodlo:
    """Źródło kursu dla waluty odniesienia. Nieznana waluta KOŃCZY SIĘ BŁĘDEM."""
    z = ZRODLA.get(waluta.upper())
    if z is None:
        raise BrakZrodlaKursu(
            f"nie znam publicznego źródła kursu {MIERZONA_DOMYSLNA}/{waluta.upper()}; "
            f"obsługiwane: {', '.join(sorted(ZRODLA))}")
    return z


# --------------------------------------------------------------- czyste liczenie

@dataclass(frozen=True)
class Kurs:
    """Kurs waluty pomiaru do waluty odniesienia w konkretnej chwili.

    `czas` to chwila, w której kurs BYŁ ZNANY — dla świecy dziennej jej koniec.
    """
    czas: datetime
    kurs: Decimal
    zrodlo: str


@dataclass(frozen=True)
class Punkt:
    """Jeden punkt DWÓCH krzywych naraz.

    `equity` jest zawsze — to pierwsza krzywa, ta, którą liczy risk kernel.
    `equity_odniesienia` bywa None i wtedy `luka` mówi dlaczego. Te dwa pola
    nigdy nie są jednocześnie wypełnione i puste, i nigdy nie zastępują się
    nawzajem: punkt bez kursu jest punktem NIEPRZELICZONYM, a nie punktem
    o wartości równej `equity`.
    """
    czas: datetime
    equity: Decimal
    equity_odniesienia: Decimal | None
    kurs: Decimal | None
    kurs_z: datetime | None
    luka: str | None


def kurs_na(czas: datetime, kursy: list[Kurs],
            max_wiek: timedelta = MAX_WIEK) -> tuple[Kurs | None, str | None]:
    """Kurs obowiązujący W CHWILI `czas`. CZYSTA FUNKCJA — bez bazy i bez sieci.

    Zwraca (kurs, None) albo (None, powód luki). `kursy` muszą być posortowane
    rosnąco po czasie — wołający robi to raz, zamiast N razy tutaj.

    DWIE REGUŁY, KTÓRE SĄ CAŁĄ TREŚCIĄ TEJ FUNKCJI:

    1. NIGDY KURS PÓŹNIEJSZY NIŻ `czas`. Wzięcie najbliższego kursu bez
       względu na kierunek wyglądałoby dokładniej i byłoby zaglądaniem
       w przyszłość: punkt krzywej z czerwca przeliczony kursem z lipca
       pokazuje rachunek, którego wtedy nie było. Ten sam błąd, przed którym
       broni `feat.as_of()`.

    2. KURS ZBYT STARY TO LUKA, NIE KURS. Przeciąganie ostatniej znanej liczby
       w nieskończoność zamienia przerwę w źródle w płaski odcinek krzywej —
       czyli w stwierdzenie „kurs się nie zmieniał", którego nikt nie zmierzył.
    """
    if not kursy:
        return None, "nie mamy w bazie ani jednego kursu tej pary"
    czasy = [k.czas for k in kursy]
    i = bisect.bisect_right(czasy, czas) - 1
    if i < 0:
        return None, (f"najstarszy zapisany kurs pochodzi z "
                      f"{kursy[0].czas:%Y-%m-%d %H:%M}, czyli PO tej chwili — "
                      f"przeliczenie użyłoby kursu z przyszłości")
    k = kursy[i]
    wiek = czas - k.czas
    if wiek > max_wiek:
        return None, (f"najświeższy kurs sprzed tej chwili pochodzi z "
                      f"{k.czas:%Y-%m-%d %H:%M}, czyli sprzed {_ile(wiek)} — "
                      f"powyżej progu {_ile(max_wiek)}")
    return k, None


def _ile(t: timedelta) -> str:
    """Odstęp czasu po polsku, z dokładnością, która wystarcza w komunikacie."""
    godzin = t.total_seconds() / 3600
    if godzin < 48:
        return f"{godzin:.0f} h"
    return f"{godzin / 24:.0f} dni"


def przelicz(kwota: Decimal, kurs: Decimal) -> Decimal:
    """Kwota w walucie pomiaru -> kwota w walucie odniesienia."""
    return kwota * kurs


def krzywa_dwuwalutowa(punkty, kursy: list[Kurs],
                       max_wiek: timedelta = MAX_WIEK) -> list[Punkt]:
    """Druga krzywa obok pierwszej. CZYSTA FUNKCJA.

    `punkty` to pary (czas, equity) z `exec.equity_point` — czyli pierwsza
    krzywa, w walucie pomiaru. Wynik ma OBIE naraz, punkt po punkcie, żeby
    interfejs nie musiał ich zszywać po czasie i żeby luka była widoczna
    dokładnie tam, gdzie powstała.

    Kursy są sortowane tutaj raz. Punkty NIE — kolejność wejścia jest
    kolejnością wyjścia, bo to wołający wie, jak chce je pokazać.
    """
    posortowane = sorted(kursy, key=lambda k: k.czas)
    wynik: list[Punkt] = []
    for czas, equity in punkty:
        k, powod = kurs_na(czas, posortowane, max_wiek)
        wynik.append(Punkt(
            czas=czas,
            equity=Decimal(equity),
            equity_odniesienia=przelicz(Decimal(equity), k.kurs) if k else None,
            kurs=k.kurs if k else None,
            kurs_z=k.czas if k else None,
            luka=powod,
        ))
    return wynik


def podsumowanie(krzywa: list[Punkt]) -> dict:
    """Jedna liczba, dla której cały ten moduł istnieje: RÓŻNICA MIĘDZY KRZYWYMI.

    Obie zmiany procentowe są liczone MIĘDZY TYMI SAMYMI DWOMA PUNKTAMI —
    pierwszym i ostatnim, który dało się przeliczyć. Policzenie zmiany w USDC
    po całej krzywej, a zmiany w EUR po jej przeliczonym wycinku, dałoby dwie
    liczby opisujące dwa różne okresy i różnicę, która nic nie znaczy.

    Gdy przeliczalnych punktów jest mniej niż dwa, obie liczby odniesienia są
    None razem z powodem. Nie ma tu wariantu „policzone częściowo": procent
    policzony od jednego końca jest liczbą bez sensu, a nie liczbą niepewną.
    """
    przeliczone = [p for p in krzywa if p.equity_odniesienia is not None]
    braki = [p for p in krzywa if p.equity_odniesienia is None]
    out = {
        "punktow": len(krzywa),
        "przeliczonych": len(przeliczone),
        "luk": len(braki),
        # Powód PIERWSZEJ luki, nie wszystkich: przy przerwie w źródle powody
        # są identyczne dla setek punktów i lista byłaby ścianą tekstu.
        "powod_luki": braki[0].luka if braki else None,
        "zmiana_pomiaru_pct": None,
        "zmiana_odniesienia_pct": None,
        "roznica_pp": None,
        "od": None,
        "do": None,
    }
    if len(przeliczone) < 2:
        out["powod_luki"] = out["powod_luki"] or (
            "krzywa ma mniej niż dwa punkty — nie ma czego porównać")
        return out

    a, b = przeliczone[0], przeliczone[-1]
    out["od"], out["do"] = a.czas, b.czas
    if a.equity > 0:
        out["zmiana_pomiaru_pct"] = float(
            (b.equity - a.equity) / a.equity * 100)
    if a.equity_odniesienia > 0:
        out["zmiana_odniesienia_pct"] = float(
            (b.equity_odniesienia - a.equity_odniesienia)
            / a.equity_odniesienia * 100)
    if out["zmiana_pomiaru_pct"] is not None \
            and out["zmiana_odniesienia_pct"] is not None:
        # Punkty procentowe, nie procent z procentu: różnica dwóch stóp zwrotu
        # jest wielkością addytywną i tak się ją czyta.
        out["roznica_pp"] = (out["zmiana_odniesienia_pct"]
                             - out["zmiana_pomiaru_pct"])
    return out


def wynik_wlasciciela(przeplywy, equity: Decimal, kursy: list[Kurs],
                      czas: datetime | None = None,
                      max_wiek: timedelta = MAX_WIEK) -> dict:
    """Odpowiedź na PYTANIE WŁAŚCICIELA: czy rachunek poszedł na minus.

    CZYSTA FUNKCJA. `przeplywy` to pary (czas, kwota) z `exec.cash_flow` —
    kwota dodatnia to wpłata, ujemna wypłata, jedno i drugie w walucie POMIARU.

    DLACZEGO KAŻDY PRZEPŁYW PO WŁASNYM KURSIE, A NIE SUMA PO DZISIEJSZYM

    Właściciel nie wpłacił „10 000 USDC" — wpłacił tyle euro, ile ich wtedy
    kosztowało, i to jest kwota, którą chce zobaczyć z powrotem. Przeliczenie
    całej sumy dzisiejszym kursem podstawiłoby pod jego wpłatę kurs, po którym
    on nigdy nie kupował, i schowałoby dokładnie tę stratę, o którą pyta.

    LUKA W JEDNYM PRZEPŁYWIE UNIEWAŻNIA CAŁĄ ODPOWIEDŹ. Nie da się powiedzieć
    „wpłacił mniej więcej tyle": suma wpłat jest punktem odniesienia dla progu
    „nie na minus", a punkt odniesienia policzony z części wpłat jest gorszy niż
    jego brak, bo wygląda tak samo jak pełny.
    """
    posortowane = sorted(kursy, key=lambda k: k.czas)
    wplacone_pomiar = sum((Decimal(kw) for _, kw in przeplywy), Decimal(0))

    out = {
        "wplacone_pomiar": wplacone_pomiar,
        "equity_pomiar": Decimal(equity),
        "wynik_pomiar": Decimal(equity) - wplacone_pomiar,
        "wplacone_odniesienia": None,
        "equity_odniesienia": None,
        "wynik_odniesienia": None,
        "luka": None,
    }

    suma = Decimal(0)
    for t, kwota in przeplywy:
        k, powod = kurs_na(t, posortowane, max_wiek)
        if k is None:
            out["luka"] = (f"przepływ z {t:%Y-%m-%d %H:%M} nie ma kursu: {powod}. "
                           f"Suma wpłat w walucie odniesienia jest NIEZNANA, "
                           f"więc nie da się powiedzieć, czy rachunek jest "
                           f"pod wodą")
            return out
        suma += przelicz(Decimal(kwota), k.kurs)
    if not przeplywy:
        out["luka"] = ("nie ma ani jednego zapisanego przepływu — nie wiadomo, "
                       "ile właściciel włożył")
        return out

    k, powod = kurs_na(czas or datetime.now(timezone.utc), posortowane, max_wiek)
    if k is None:
        out["wplacone_odniesienia"] = suma
        out["luka"] = (f"znamy sumę wpłat, ale nie bieżący kurs: {powod}. "
                       f"Wartość rachunku w walucie odniesienia jest NIEZNANA")
        return out

    out["wplacone_odniesienia"] = suma
    out["equity_odniesienia"] = przelicz(Decimal(equity), k.kurs)
    out["wynik_odniesienia"] = out["equity_odniesienia"] - suma
    return out


# ------------------------------------------------------------- odczyt z giełdy
#
# Od tego miejsca w dół funkcje dotykają sieci i bazy. Nic z tego nie liczy —
# liczenie jest wyżej i da się je przetestować bez jednego i drugiego.

def _etykieta(host: str) -> str:
    """'https://api.bybit.eu' -> 'api.bybit.eu'. Do kolumny `source`."""
    return host.split("//")[-1].rstrip("/")


async def kursy_dzienne(host: str, para: str, kategoria: str = "spot",
                        od: datetime | None = None,
                        teraz: datetime | None = None) -> list[Kurs]:
    """Historia kursu ze świec dziennych podmiotu. Zwraca rosnąco po czasie.

    DLACZEGO WŁASNY ODCZYT, A NIE `adapter.fetch_candles`

    Tamta metoda ma na sztywno `host=PUBLIC_HOST`, czyli api.bybit.com. Dla
    danych rynkowych uniwersum jest to decyzja słuszna i opisana w 0018 — model
    trenowano na księdze globalnej. Dla kursu wymiany jest odwrotnie: liczy się
    księga, w której właściciel NAPRAWDĘ wymieni USDC na euro, czyli jego
    własnego podmiotu. Pary USDCPLN na hoście globalnym nie ma zresztą w ogóle
    (10001 „Not supported symbols", sprawdzone 2026-09-08).

    ŚWIECA NIEDOMKNIĘTA NIE WCHODZI. Jej „zamknięcie" to bieżąca cena, która
    jeszcze się zmieni — zapisana pod czasem końca świecy byłaby kursem
    z przyszłości, a przy `ON CONFLICT DO NOTHING` zostałaby tam na zawsze.
    Ten sam błąd, który potok danych ma opisany w
    tests/test_swieca_zamknieta.py.
    """
    import httpx

    teraz = teraz or datetime.now(timezone.utc)
    params = {"category": kategoria, "symbol": para, "interval": "D", "limit": 1000}
    if od is not None:
        params["start"] = int(od.timestamp() * 1000)

    async with httpx.AsyncClient(timeout=20.0) as klient:
        odp = await klient.get(f"{host}/v5/market/kline", params=params)
        odp.raise_for_status()
        dane = odp.json()

    if dane.get("retCode") != 0:
        # Wyjątek, a nie pusta lista: pusta lista jest nieodróżnialna od
        # „nic nowego nie ma", a to dwa różne stany.
        raise RuntimeError(f"{host} odmówił świec {para}: "
                           f"{dane.get('retCode')} {dane.get('retMsg')}")

    zrodlo = f"{_etykieta(host)}:{para}:{kategoria}:1d"
    out: list[Kurs] = []
    for wiersz in (dane.get("result") or {}).get("list") or []:
        poczatek = datetime.fromtimestamp(int(wiersz[0]) / 1000, tz=timezone.utc)
        koniec = poczatek + KROK
        if koniec > teraz:
            continue                      # świeca trwająca — patrz docstring
        out.append(Kurs(czas=koniec, kurs=Decimal(str(wiersz[4])), zrodlo=zrodlo))
    out.sort(key=lambda k: k.czas)
    return out


async def kurs_biezacy(adapter, para: str, kategoria: str = "spot",
                       teraz: datetime | None = None) -> Kurs | None:
    """Kurs „teraz" z ADAPTERA — `cena_odniesienia`, bez własnego HTTP.

    Ta metoda robi już dokładnie to, czego tu trzeba: czyta z hosta PODMIOTU
    (a nie globalnego), nie wymaga klucza, bo endpoint jest publiczny, i zwraca
    None, gdy odczyt się nie uda — z komentarzem „BRAK ODCZYTU TO LUKA, NIE
    ZGODA". Powielanie jej tutaj byłoby drugą kopią tej samej decyzji o hoście,
    rozjeżdżającą się przy pierwszej zmianie.

    None idzie dalej jako None. Kurs, którego nie odczytaliśmy, nie ma prawa
    stać się wierszem w bazie z jakąkolwiek wartością.
    """
    cena = await adapter.cena_odniesienia(para, kategoria)
    if cena is None:
        return None
    return Kurs(czas=teraz or datetime.now(timezone.utc), kurs=cena,
                zrodlo=f"{_etykieta(getattr(adapter, '_host', ''))}:{para}:"
                       f"{kategoria}:ticker")


# ------------------------------------------------------------------ baza danych

async def zapisz_kursy(execute, base_ccy: str, quote_ccy: str,
                       kursy: list[Kurs]) -> int:
    """Zapisuje kursy do market.fx_rate. Zwraca liczbę przekazanych wierszy.

    `ON CONFLICT DO NOTHING`, a NIE `DO UPDATE`. Kurs raz zapisany pod daną
    chwilą jest tym, co system wtedy wiedział, i nadpisanie go późniejszym
    odczytem skasowałoby jedyny ślad tego, na czym oparto raport z tamtego dnia.
    Bitemporalność bez niezmienności przeszłości nie jest bitemporalnością.
    """
    for k in kursy:
        await execute(
            "INSERT INTO market.fx_rate (base_ccy, quote_ccy, event_time, rate, source)"
            " VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (base_ccy, quote_ccy, k.czas, k.kurs, k.zrodlo))
    return len(kursy)


async def wczytaj_kursy(fetch_all, base_ccy: str, quote_ccy: str,
                        od: datetime | None = None) -> list[Kurs]:
    """Kursy z bazy, rosnąco po czasie.

    `od` bierze ZAPAS W TYŁ: pierwszy punkt krzywej potrzebuje kursu SPRZED
    siebie, więc odczyt zaczynający się dokładnie w tej samej chwili co krzywa
    zrobiłby z pierwszego punktu lukę, której nie ma.
    """
    if od is not None:
        rows = await fetch_all(
            "SELECT event_time, rate, source FROM market.fx_rate"
            " WHERE base_ccy = %s AND quote_ccy = %s AND event_time >= %s"
            " ORDER BY event_time", (base_ccy, quote_ccy, od - MAX_WIEK - KROK))
    else:
        rows = await fetch_all(
            "SELECT event_time, rate, source FROM market.fx_rate"
            " WHERE base_ccy = %s AND quote_ccy = %s"
            " ORDER BY event_time", (base_ccy, quote_ccy))
    return [Kurs(czas=r["event_time"], kurs=Decimal(str(r["rate"])),
                 zrodlo=r["source"]) for r in rows]


async def ustawienie(fetch_one) -> tuple[str, str]:
    """(waluta odniesienia, waluta pomiaru) z app.waluta_odniesienia.

    Brak wiersza NIE jest cichym EUR. Migracja 0036 go wstawia, więc jego brak
    znaczy „migracja nie przeszła" — a raport w wymyślonej walucie byłby wtedy
    gorszy niż jego brak.
    """
    r = await fetch_one("SELECT kod, kod_mierzony FROM app.waluta_odniesienia"
                        " WHERE jedyny LIMIT 1")
    if not r:
        raise BrakZrodlaKursu(
            "app.waluta_odniesienia jest pusta — migracja 0036 nie przeszła; "
            "nie zgaduję, w czym właściciel liczy")
    return r["kod"], r["kod_mierzony"]


async def wczytaj_krzywa(fetch_all, engine: str, mode: str, dni: int):
    """Pierwsza krzywa: (czas, equity) z exec.equity_point, rosnąco."""
    rows = await fetch_all(
        "SELECT event_time, equity FROM exec.equity_point"
        " WHERE engine::text = %s AND mode::text = %s"
        "   AND event_time >= now() - make_interval(days => %s)"
        " ORDER BY event_time", (engine, mode, dni))
    return [(r["event_time"], Decimal(str(r["equity"]))) for r in rows]


async def wczytaj_przeplywy(fetch_all, engine: str, mode: str):
    """(czas, kwota) z exec.cash_flow, rosnąco. Kwoty w walucie POMIARU."""
    rows = await fetch_all(
        "SELECT event_time, amount FROM exec.cash_flow"
        " WHERE engine::text = %s AND mode::text = %s"
        " ORDER BY event_time", (engine, mode))
    return [(r["event_time"], Decimal(str(r["amount"]))) for r in rows]


async def host_podmiotu(fetch_one) -> str:
    """Host podmiotu z ref.venue. Ta sama prawda co w uzgodnieniu, nie druga kopia.

    Świadomie `live_host`, także dla trybu demo: host demonstracyjny służy
    zapytaniom PODPISANYM, a kurs jest odczytem publicznym z księgi, w której
    właściciel naprawdę wymieni. Gdy tabela nic nie mówi, zostaje stała HOST_EU
    — i to jedyne miejsce, w którym ona działa.
    """
    r = await fetch_one("SELECT live_host FROM ref.venue WHERE code = 'bybit'")
    return (r or {}).get("live_host") or HOST_EU


# --------------------------------------------------------------------- raport

def wypisz(krzywa: list[Punkt], waluta: str, mierzona: str,
           wynik: dict | None = None, ile: int = 12) -> None:
    """Druga krzywa OBOK pierwszej, w jednej tabeli.

    Obok, a nie zamiast, i obok, a nie w osobnym raporcie: dwie liczby w dwóch
    miejscach porównuje się dopiero wtedy, gdy ktoś już podejrzewa problem —
    a tu chodzi o to, żeby zobaczył go, zanim zacznie podejrzewać.
    """
    print(f"  {'czas':16} {mierzona:>14} {'kurs':>10} {waluta:>16}")
    pokaz = krzywa if len(krzywa) <= ile else (
        krzywa[:ile // 2] + krzywa[-(ile - ile // 2):])
    for p in pokaz:
        if p.equity_odniesienia is None:
            # LUKA, NIE ZERO. Wypisana słowem, żeby nie dało się jej wziąć za
            # liczbę — pusta kolumna albo 0,00 wygląda jak wynik pomiaru.
            print(f"  {p.czas:%Y-%m-%d %H:%M} {p.equity:>14.2f} {'—':>10} "
                  f"{'nie przeliczono':>16}")
        else:
            print(f"  {p.czas:%Y-%m-%d %H:%M} {p.equity:>14.2f} "
                  f"{p.kurs:>10.4f} {p.equity_odniesienia:>16.2f}")
    if len(krzywa) > ile:
        print(f"  … pominięto {len(krzywa) - ile} punktów w środku")

    s = podsumowanie(krzywa)
    print(f"\n  punktów {s['punktow']}, przeliczonych {s['przeliczonych']}, "
          f"luk {s['luk']}")
    if s["luk"]:
        print(f"  powód pierwszej luki: {s['powod_luki']}")
    if s["roznica_pp"] is None:
        print("  ZMIANY NIE POLICZONO — brak dwóch przeliczonych punktów")
    else:
        print(f"  zmiana w {mierzona}: {s['zmiana_pomiaru_pct']:+.2f}%   "
              f"w {waluta}: {s['zmiana_odniesienia_pct']:+.2f}%   "
              f"różnica {s['roznica_pp']:+.2f} pp")

    if wynik is None:
        return
    print("\n  PYTANIE WŁAŚCICIELA — czy rachunek poszedł na minus")
    print(f"  wpłacone netto: {wynik['wplacone_pomiar']:.2f} {mierzona}")
    if wynik["wynik_odniesienia"] is None:
        print(f"  w {waluta}: NIE PRZELICZONO — {wynik['luka']}")
        return
    print(f"  wpłacone netto: {wynik['wplacone_odniesienia']:.2f} {waluta}")
    print(f"  wartość dziś:   {wynik['equity_odniesienia']:.2f} {waluta}")
    print(f"  wynik: {wynik['wynik_pomiar']:+.2f} {mierzona}   "
          f"{wynik['wynik_odniesienia']:+.2f} {waluta}")
    if wynik["wynik_pomiar"] >= 0 > wynik["wynik_odniesienia"]:
        print("  UWAGA: w walucie pomiaru rachunek jest na plusie, a w walucie "
              "właściciela NA MINUSIE. Dokładnie ten stan bezpiecznik "
              "przepuszcza jako zielony.")


# ------------------------------------------------------------------------ CLI

async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Druga krzywa kapitału w walucie odniesienia właściciela")
    ap.add_argument("--co", default="krzywa",
                    choices=("kurs", "historia", "krzywa"),
                    help="kurs: dopisz BIEŻĄCY kurs do market.fx_rate; "
                         "historia: uzupełnij historię ze świec dziennych; "
                         "krzywa: pokaż drugą krzywą obok pierwszej")
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--mode", default="demo")
    ap.add_argument("--dni", type=int, default=30)
    ap.add_argument("--waluta", default=None,
                    help="nadpisuje ustawienie z app.waluta_odniesienia; "
                         "do sprawdzenia, jak wyglądałaby krzywa w innej walucie "
                         "BEZ zmiany ustawienia właściciela")
    ap.add_argument("--sucho", action="store_true",
                    help="dla --co kurs/historia: pokaż, nie zapisuj")
    args = ap.parse_args()

    if not os.environ.get("DATABASE_URL"):
        sys.exit("DATABASE_URL nie jest ustawiony")

    from services.api.db import close_pool, execute, fetch_all, fetch_one, open_pool

    await open_pool()
    try:
        waluta, mierzona = await ustawienie(fetch_one)
        if args.waluta:
            waluta = args.waluta.upper()
        z = zrodlo_dla(waluta)
        host = await host_podmiotu(fetch_one)
        if z.zastrzezenie:
            print(f"UWAGA o źródle {waluta}: {z.zastrzezenie}")

        if args.co == "historia":
            od = datetime.now(timezone.utc) - timedelta(days=args.dni)
            kursy = await kursy_dzienne(host, z.para, z.kategoria, od=od)
            print(f"{z.para} z {host}: {len(kursy)} zamkniętych świec dziennych")
            if not kursy:
                print("NIC NIE POBRANO — to nie znaczy, że kurs się nie zmieniał")
                return 1
            print(f"  od {kursy[0].czas:%Y-%m-%d} ({kursy[0].kurs}) "
                  f"do {kursy[-1].czas:%Y-%m-%d} ({kursy[-1].kurs})")
            if args.sucho:
                return 0
            n = await zapisz_kursy(execute, mierzona, waluta, kursy)
            print(f"zapisano (bez nadpisywania istniejących): {n}")
            return 0

        if args.co == "kurs":
            from services.api.security import SecretBox
            from services.exchanges.bybit import BybitAdapter
            cred = await fetch_one(
                "SELECT c.key_enc, c.secret_enc FROM app.api_credential c"
                " JOIN ref.venue v ON v.id = c.venue_id"
                " WHERE v.code = 'bybit' AND c.mode::text = %s AND c.active"
                " ORDER BY c.id LIMIT 1", (args.mode,))
            if cred:
                box = SecretBox()
                adapter = BybitAdapter(api_key=box.decrypt(bytes(cred["key_enc"])),
                                       api_secret=box.decrypt(bytes(cred["secret_enc"])),
                                       mode=args.mode, host=host)
            else:
                # Endpoint jest PUBLICZNY, więc brak klucza nie jest powodem,
                # żeby nie znać kursu. Puste poświadczenia nie idą do żadnego
                # podpisu — `cena_odniesienia` woła z `private=False`.
                adapter = BybitAdapter(api_key="", api_secret="",
                                       mode=args.mode, host=host)
            try:
                k = await kurs_biezacy(adapter, z.para, z.kategoria)
            finally:
                await adapter.close()
            if k is None:
                print(f"kurs {mierzona}/{waluta}: NIE ODCZYTANO z {host} — "
                      f"to LUKA, nie brak zmiany")
                return 1
            print(f"kurs {mierzona}/{waluta} = {k.kurs} ({k.zrodlo})")
            if args.sucho:
                return 0
            await zapisz_kursy(execute, mierzona, waluta, [k])
            return 0

        # --co krzywa
        punkty = await wczytaj_krzywa(fetch_all, args.engine, args.mode, args.dni)
        if not punkty:
            print(f"krzywa {args.engine}/{args.mode}: brak punktów w oknie "
                  f"{args.dni} dni — nie ma czego przeliczać")
            return 0
        kursy = await wczytaj_kursy(fetch_all, mierzona, waluta, od=punkty[0][0])
        krzywa = krzywa_dwuwalutowa(punkty, kursy)
        przeplywy = await wczytaj_przeplywy(fetch_all, args.engine, args.mode)
        wynik = wynik_wlasciciela(przeplywy, punkty[-1][1], kursy,
                                  czas=punkty[-1][0])
        print(f"krzywa {args.engine}/{args.mode}, {args.dni} dni, "
              f"pomiar w {mierzona}, odniesienie {waluta}\n")
        wypisz(krzywa, waluta, mierzona, wynik)
        return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
