#!/usr/bin/env python3
"""Korekta swiec equity o podzialy akcji: PRZEPISANIE szeregu, potem znacznik.

PO CO TO ISTNIEJE

Do 2026-09-27 adapter Alpaki pobieral swiece z `adjustment=raw`, czyli BEZ
korekty o podzialy akcji. W `market.candle` siedza wiec trzy skoki x10
(NVDA 2024-06-10, AVGO 2024-07-15, NFLX 2025-11-17), ktore w szeregu zwrotow
wygladaja jak spadek o 90%. `train.py` odmawia z tego powodu treningu toru
equity, dopoki kazdy instrument uniwersum nie ma znacznika
`ZNACZNIK_KOREKTY_PODZIALOW` w `ref.instrument.meta`.

Ten skrypt spelnia warunek tej bramki - i jest OSOBNA sciezka od
`backfill.py`/`history_worker.py`, bo tamte UTRZYMUJA okno (`--days 3`),
a tu chodzi o PRZEPISANIE calego szeregu.

TRZY TRYBY, W TEJ KOLEJNOSCI

    --sucho      niczego nie zapisuje. Pobiera szereg skorygowany, zestawia go
                 z baza i mowi: ile swiec mamy, ile dostawca oddal, ILU NAM
                 BRAKUJE (to jest warunek pelnosci), ile wierszy zmieni wartosc
                 i jakim czynnikiem. Z `--korekta raw` daje punkt odniesienia:
                 jesli `raw` i `split` dadza TE SAMA liczbe zmienionych wierszy,
                 to parametr nie dochodzi do dostawcy i nic nie mierzymy.

    --przepisz   przepisuje szereg. JEDNA TRANSAKCJA NA INSTRUMENT: albo caly
                 szereg tego instrumentu jest skorygowany, albo nie zmienia sie
                 ani jeden jego wiersz. Znacznika NIE STAWIA.

    --znacznik   stawia znacznik - ale tylko instrumentom, ktore maja w
                 `market.korekta_swiec` udane przepisanie, i tylko wtedy, gdy
                 CECHY ZGADZAJA SIE ZE SWIECAMI (nizej).

DLACZEGO ZNACZNIK JEST W OSOBNYM TRYBIE, A NIE NA KONCU PRZEPISANIA

Bo miedzy jednym a drugim musi wejsc przeliczenie cech. Trener nie czyta swiec
jako predyktorow - czyta `feat.feature_value`, a tam leza wartosci policzone ze
swiec NIESKORYGOWANYCH. Poprawienie swiec ich nie rusza: `features-equity`
chodzi z `--okno-dni 7`, wiec okien z 2024 i 2025 nie dotknie NIGDY. Znacznik
postawiony zaraz po przepisaniu swiec mowilby prawde o `market.candle`
i zdejmowal bramke z danych, ktore trener faktycznie czyta i ktore nadal sa
zatrute. Dlatego `--znacznik` sam sprawdza zgodnosc: przelicza cechy kodem
`features.py` W TRANSAKCJI, KTORA WYCOFUJE, i porownuje z zapisanymi. Rozjazd
oznacza odmowe postawienia znacznika.

KOLEJNOSC CALEJ OPERACJI (te same polecenia w renacode-infra: docs/projekty/trader/08-przed-go-live.md)

    1. korekta_podzialow.py --sucho                (pomiar, nic nie pisze)
    2. korekta_podzialow.py --przepisz             (swiece, bez znacznika)
    3. features.py --bar 1h --engine equity --okno-dni 1400 --once   (cechy)
    4. korekta_podzialow.py --znacznik             (sprawdza cechy, stawia znacznik)
    5. train.py --bar 1h --engine equity ...       (bramka juz nie blokuje)

Uzycie:
    DATABASE_URL=... SECRETS_KEY=... python services/ingest/korekta_podzialow.py --sucho
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg
from psycopg.rows import dict_row

from services.engine import horizons
from services.engine.uniwersum import sql_uniwersum_decyzyjnego
from services.exchanges.alpaca import KOREKTA_SWIEC, ZNACZNIK_KOREKTY_PODZIALOW
from services.exchanges.base import ExchangeError
from services.exchanges.fabryka import adapter_danych
from services.ingest import features
from services.ingest.backfill import TF_MINUTES, batched_insert


@dataclass
class Raport:
    """Zestawienie szeregu z bazy z szeregiem od dostawcy. Bez zapisu."""
    symbol: str
    instrument_id: int
    w_bazie: int = 0
    pobranych: int = 0
    brakujacych: int = 0          # mamy w bazie, dostawca nie oddal
    nowych: int = 0               # dostawca oddal, nie mamy
    zmienionych: int = 0
    okno_od: datetime | None = None
    okno_do: datetime | None = None
    czynniki: list[Decimal] = field(default_factory=list)
    brakujace_przyklady: list[datetime] = field(default_factory=list)

    @property
    def czynnik(self) -> Decimal | None:
        """Mediana `nowa/stara` tam, gdzie cena sie zmienila. Dla podzialu 10:1
        wychodzi okolo 0,1 - i to jest liczba, ktora nazywa zdarzenie."""
        return statistics.median(sorted(self.czynniki)) if self.czynniki else None

    @property
    def pelny(self) -> bool:
        """Czy dostawca pokryl KAZDA swiece, ktora mamy.

        To jest cala definicja pelnosci. Szereg, w ktorym czesc swiec zostala
        nieskorygowana, jest GORSZY niz nieskorygowany caly: nieciaglosc wypada
        wtedy w srodku okna treningowego i nic o niej nie mowi.
        """
        return self.brakujacych == 0

    def opis(self) -> str:
        cz = f"x{self.czynnik:.4f}" if self.czynnik is not None else "bez zmian"
        return (f"{self.symbol:10s} baza {self.w_bazie:>6d}  dostawca {self.pobranych:>6d}"
                f"  brak {self.brakujacych:>4d}  nowych {self.nowych:>4d}"
                f"  zmienionych {self.zmienionych:>6d}  {cz}")


async def pobierz_serie(adapter, symbol: str, timeframe: str, od: datetime,
                        do: datetime, *, korekta: str | None = None) -> list:
    """Caly szereg [od, do) z dostawcy, rosnaco, bez swiecy trwajacej.

    Paginacja wlasna, bo `fetch_candles` zatrzymuje sie na `limit`, a tu chodzi
    o KOMPLET - urwanie na limicie dalo by szereg czesciowo skorygowany, czyli
    dokladnie to, czego ta operacja ma nie zrobic.
    """
    step = timedelta(minutes=TF_MINUTES[timeframe])
    kursor = od
    out: list = []
    widziane: set[datetime] = set()
    while kursor < do:
        # BEZ `end`. Gorna granice odcina filtr nizej, a `end` nie byl w tym
        # repozytorium nigdy wyslany do Alpaki (ani backfill, ani market_worker
        # go nie podaja), wiec operacja na produkcyjnych danych nie jest
        # miejscem na jego pierwszy raz.
        swiece = await _pobierz_cierpliwie(adapter, symbol, timeframe, kursor, korekta)
        if not swiece:
            break
        for c in swiece:
            # Swieca TRWAJACA nie jest obserwacja - zapisana ma zakres kilku
            # minut i zanizylaby kazda ceche liczona z zakresu (ten sam warunek
            # co w backfill.py).
            if c.event_time + step <= do and c.event_time not in widziane:
                widziane.add(c.event_time)
                out.append(c)
        ostatnia = swiece[-1].event_time
        if ostatnia + step <= kursor:
            break
        kursor = ostatnia + step
    out.sort(key=lambda c: c.event_time)
    return out


# Limit zapytan Alpaki jest minutowy, a ponowienia w adapterze trwaja razem
# ok. 3,5 s - za malo, gdy ten sam klucz zuzywaja rownolegle workery equity.
# Ta operacja nie ma terminu, wiec czeka do pelnej minuty, zamiast urywac
# pomiar w polowie (2026-10-03: krok 1b padl dwa razy na "too many requests").
CIERPLIWOSC_PROB = 6
CIERPLIWOSC_S = 30.0


async def _pobierz_cierpliwie(adapter, symbol, timeframe, kursor, korekta,
                              *, prob: int = CIERPLIWOSC_PROB,
                              pauza_s: float = CIERPLIWOSC_S):
    for proba in range(prob):
        try:
            return await adapter.fetch_candles(symbol, timeframe, limit=10_000,
                                               start=kursor, korekta=korekta)
        except ExchangeError as e:
            if not e.retryable or proba == prob - 1:
                raise
            print(f"  {symbol}: {e} - czekam {pauza_s:.0f} s (proba {proba + 1}/{prob})",
                  file=sys.stderr)
            await asyncio.sleep(pauza_s)
    raise AssertionError("nieosiagalne")


def stan_bazy(cur, instrument_id: int, timeframe: str) -> dict:
    """Caly zapisany szereg jako event_time -> wiersz."""
    cur.execute(
        "SELECT event_time, open, high, low, close, volume, trade_count"
        "  FROM market.candle WHERE instrument_id = %s AND timeframe = %s"
        " ORDER BY event_time", (instrument_id, timeframe))
    return dict((r["event_time"], r) for r in cur.fetchall())


def zestaw(symbol: str, instrument_id: int, w_bazie: dict, swiece: list) -> Raport:
    """Porownanie bez zapisu.

    `zmienionych` liczy TE SAMA rzecz, co warunek WHERE w `batched_insert` -
    inaczej raport mowilby o innej liczbie wierszy, niz przepisanie faktycznie
    ruszy, i nie bylo by po czym tego poznac.
    """
    r = Raport(symbol=symbol, instrument_id=instrument_id, w_bazie=len(w_bazie),
               pobranych=len(swiece))
    pobrane_czasy = set(c.event_time for c in swiece)
    brak = sorted(set(w_bazie) - pobrane_czasy)
    r.brakujacych = len(brak)
    r.brakujace_przyklady = brak[:5]
    for c in swiece:
        stara = w_bazie.get(c.event_time)
        if stara is None:
            r.nowych += 1
            continue
        stare = (stara["open"], stara["high"], stara["low"], stara["close"],
                 stara["volume"])
        nowe = (c.open, c.high, c.low, c.close, c.volume)
        if stare != nowe:
            r.zmienionych += 1
            if stara["close"]:
                r.czynniki.append(c.close / stara["close"])
            if r.okno_od is None:
                r.okno_od = c.event_time
            r.okno_do = c.event_time
    return r


def wiersze_do_zapisu(instrument_id: int, timeframe: str, swiece: list,
                      teraz: datetime) -> list:
    return [(instrument_id, timeframe, c.event_time, c.open, c.high, c.low,
             c.close, c.volume, c.trades, teraz) for c in swiece]


def zapisz_slad(cur, r: Raport, timeframe: str, zdarzenie: str, adjustment: str,
                szczegol: str | None = None) -> None:
    """Slad do `market.korekta_swiec` (migracja 0065).

    Nie jest ozdoba: sam znacznik w `meta` mowi tylko `true`, wiec bez tego
    wiersza "potwierdzone pomiarem" i "wpisane, zeby ruszylo" wygladaja
    w bazie identycznie.
    """
    cur.execute(
        "INSERT INTO market.korekta_swiec (instrument_id, timeframe, zdarzenie,"
        " adjustment, okno_od, okno_do, swiec_w_bazie, swiec_pobranych,"
        " swiec_zmienionych, wspolczynnik, szczegol)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (r.instrument_id, timeframe, zdarzenie, adjustment, r.okno_od, r.okno_do,
         r.w_bazie, r.pobranych, r.zmienionych, r.czynnik, szczegol))


# --------------------------------------------------------------- cechy vs swiece

# TOLERANCJA POROWNANIA CECH. Do 2026-10-03 stalo tu `przed[k] != v`, czyli
# rownosc co do bitu dwoch liczb double policzonych W ROZNYCH CHWILACH: zapisana
# wartosc i ta sama cecha przeliczona teraz. Ten sam kod na tych samych swiecach
# potrafi dac roznice w ostatnich bitach (inna kolejnosc sumowania okna
# kroczacego, inny poczatek zakresu `od`), a jedna taka roznica blokowala
# `--znacznik` przy POPRAWNYCH danych - czyli tor equity zostawal martwy z powodu
# szumu arytmetyki. Skala tego, co sprawdzenie ma wylapac, to skok x10 po
# podziale (90%), wiec 1e-9 wzglednie jest o osiem rzedow ponizej sygnalu
# i o kilka powyzej bledu zaokraglen double (~1e-16 na operacje). `abs_tol`
# dla cech bliskich zera (zwrot, z-score), przy ktorych wzgledna tolerancja
# traci sens.
TOLERANCJA_WZGL = 1e-9
TOLERANCJA_BEZWZGL = 1e-12


def rozne(a, b) -> bool:
    """Czy dwie wartosci cechy roznia sie ponad szum arytmetyki."""
    if a is None or b is None:
        return (a is None) != (b is None)
    return not math.isclose(float(a), float(b), rel_tol=TOLERANCJA_WZGL,
                            abs_tol=TOLERANCJA_BEZWZGL)


def policz_rozjazd(przed: dict, po: dict) -> tuple[int, int]:
    """(niezgodne, brakujace) dla dwoch odczytow tych samych kluczy."""
    niezgodne = sum(1 for k, v in po.items() if k in przed and rozne(przed[k], v))
    brakujace = sum(1 for k in po if k not in przed)
    return niezgodne, brakujace


def rozjazd_cech(conn, instrument_id: int, h, feat_ids: dict, od: datetime,
                 do: datetime) -> tuple[int, int]:
    """Ile ZAPISANYCH wartosci cech nie zgadza sie z policzonymi ze swiec.

    Liczy WLASNYM kodem potoku cech (`features.compute_prices`), a nie druga
    kopia wzoru - druga kopia rozjechalaby sie i zwracala zero z wlasnego
    powodu. Transakcja jest WYCOFYWANA, wiec sprawdzenie nie zapisuje niczego.

    Zwraca (niezgodne, brakujace): pierwsze to wartosci, ktore sa i sa inne,
    drugie - ktorych nie ma wcale.
    """
    cur = conn.cursor(row_factory=dict_row)
    ids = list(feat_ids.values())

    def wczytaj() -> dict:
        cur.execute(
            "SELECT feature_def_id, event_time, value FROM feat.feature_value"
            " WHERE instrument_id = %s AND feature_def_id = ANY(%s)"
            "   AND event_time >= %s AND event_time <= %s",
            (instrument_id, ids, od, do))
        return dict(((r["feature_def_id"], r["event_time"]), r["value"])
                    for r in cur.fetchall())

    przed = wczytaj()
    try:
        features.compute_prices(cur, instrument_id, h, feat_ids, od, do)
        po = wczytaj()
    finally:
        conn.rollback()
    return policz_rozjazd(przed, po)


def rozjazd_przekroju(conn, h, feat_ids: dict, engine: str, od: datetime,
                      do: datetime) -> tuple[int, int]:
    """To samo dla cechy PRZEKROJOWEJ.

    `rel_strength` jest wlasnoscia pary (instrument, uniwersum): zwrot -90%
    jednego instrumentu przestawia ranking WSZYSTKICH pozostalych w tej samej
    swiecy. Sprawdzenie per instrument tego nie zobaczy.
    """
    cur = conn.cursor(row_factory=dict_row)
    fid = feat_ids.get(h.name("rel_strength"))
    if fid is None:
        return 0, 0

    def wczytaj() -> dict:
        cur.execute(
            "SELECT instrument_id, event_time, value FROM feat.feature_value"
            " WHERE feature_def_id = %s AND event_time >= %s AND event_time <= %s",
            (fid, od, do))
        return dict(((r["instrument_id"], r["event_time"]), r["value"])
                    for r in cur.fetchall())

    przed = wczytaj()
    try:
        features.compute_cross_section(cur, h, feat_ids, od, do, None, engine=engine)
        po = wczytaj()
    finally:
        conn.rollback()
    return policz_rozjazd(przed, po)


# --------------------------------------------------------------------- tryby

def uniwersum(cur, engine: str, symbol: str | None) -> list[dict]:
    """DOKLADNIE ten zbior, o ktory pyta bramka w `train.py`.

    Wspolny warunek z `uniwersum.py`, nie wlasne zapytanie: znacznik postawiony
    innemu zbiorowi niz pytany przez bramke znaczy albo bramke, ktora nie puszcza
    nigdy, albo - gorzej - znacznik na instrumencie, ktorego nikt nie przepisal.
    """
    cur.execute(sql_uniwersum_decyzyjnego("id, symbol, meta"), (engine,))
    wiersze = cur.fetchall()
    if symbol:
        wiersze = [w for w in wiersze if w["symbol"] == symbol]
        if not wiersze:
            raise RuntimeError(f"{symbol} nie jest w uniwersum decyzyjnym {engine}")
    if not wiersze:
        raise RuntimeError(f"uniwersum decyzyjne {engine} jest puste")
    return wiersze


async def tryb_pomiaru(conn, adapter, wiersze: list[dict], timeframe: str,
                       korekta: str | None) -> list[Raport]:
    """Pomiar. Zero zapisow - takze zero `commit`, takze przy bledzie."""
    cur = conn.cursor(row_factory=dict_row)
    teraz = datetime.now(timezone.utc)
    raporty: list[Raport] = []
    print(f"POMIAR (adjustment={korekta or KOREKTA_SWIEC}) - nic nie zapisuje\n")
    for w in wiersze:
        baza = stan_bazy(cur, w["id"], timeframe)
        if not baza:
            print(f"  {w['symbol']:10s} brak swiec {timeframe} w bazie - pomijam")
            continue
        swiece = await pobierz_serie(adapter, w["symbol"], timeframe, min(baza),
                                     teraz, korekta=korekta)
        r = zestaw(w["symbol"], w["id"], baza, swiece)
        raporty.append(r)
        print("  " + r.opis())
        if not r.pelny:
            przyklady = ", ".join(str(t) for t in r.brakujace_przyklady)
            print(f"      NIEPELNE: dostawca nie oddal {r.brakujacych} swiec, "
                  f"ktore mamy (np. {przyklady})")
    return raporty


async def tryb_przepisania(conn, adapter, wiersze: list[dict], timeframe: str
                           ) -> tuple[list[Raport], list[str]]:
    """Przepisanie szeregu. JEDNA TRANSAKCJA NA INSTRUMENT.

    Swiece i slad ida tym samym commitem, wiec nie istnieje stan "przepisane,
    ale bez sladu" ani "slad bez przepisania". Wyjatek albo niepelne pokrycie
    konczy sie `rollback` i instrument zostaje NIETKNIETY - to jest cala
    gwarancja "albo caly szereg, albo nic".

    DLACZEGO NIE `backfill.przebieg(napraw=True)`, ktory juz umie pobrac okno
    ponownie: bo on zapisuje i COMMITUJE okno po oknie (`_fetch_range`), wiec
    przerwanie w polowie zostawia szereg czesciowo skorygowany - dokladnie ten
    stan, ktory jest gorszy od braku korekty, bo nieciaglosc wypada w srodku
    okna treningowego i nic o niej nie mowi.
    """
    cur = conn.cursor(row_factory=dict_row)
    teraz = datetime.now(timezone.utc)
    raporty: list[Raport] = []
    odrzucone: list[str] = []
    print(f"PRZEPISANIE szeregu {timeframe} (adjustment={KOREKTA_SWIEC})\n")
    for w in wiersze:
        baza = stan_bazy(cur, w["id"], timeframe)
        if not baza:
            odrzucone.append(f"{w['symbol']}: brak swiec {timeframe} w bazie")
            print(f"  {w['symbol']:10s} ODRZUCONY: brak swiec {timeframe} w bazie")
            continue
        try:
            swiece = await pobierz_serie(adapter, w["symbol"], timeframe,
                                         min(baza), teraz)
            r = zestaw(w["symbol"], w["id"], baza, swiece)
            if not r.pelny:
                raise RuntimeError(
                    f"dostawca nie oddal {r.brakujacych} swiec, ktore mamy "
                    f"(np. {r.brakujace_przyklady[0]}) - przepisanie czesciowe "
                    f"zostawiloby nieciaglosc w srodku okna treningowego")
            batched_insert(cur, wiersze_do_zapisu(w["id"], timeframe, swiece, teraz))
            zapisz_slad(cur, r, timeframe, "swiece_przepisane", KOREKTA_SWIEC)
            conn.commit()
        except (ExchangeError, RuntimeError, psycopg.Error) as e:
            conn.rollback()
            odrzucone.append(f"{w['symbol']}: {type(e).__name__}: {e}")
            print(f"  {w['symbol']:10s} ODRZUCONY, nic nie zapisano: {e}")
            continue
        raporty.append(r)
        print("  " + r.opis())
    return raporty, odrzucone


def tryb_znacznika(conn, wiersze: list[dict], timeframe: str, h, engine: str
                   ) -> tuple[list[str], list[str]]:
    """Znacznik - po instrumencie, i tylko gdy WSZYSTKO sie zgadza.

    Warunki; kazdy z osobna odmawia:
      * instrument ma w `market.korekta_swiec` udane `swiece_przepisane`
        z `adjustment` rownym temu, ktory stosuje dzis adapter,
      * przeliczenie cech ze swiec daje TE SAME wartosci, co zapisane,
      * to samo dla cechy przekrojowej - liczonej raz dla calego zbioru.
    """
    cur = conn.cursor(row_factory=dict_row)
    postawione: list[str] = []
    odmowy: list[str] = []
    ids = [w["id"] for w in wiersze]

    cur.execute(
        "SELECT instrument_id, max(wykonano_at) AS kiedy, min(okno_od) AS od"
        "  FROM market.korekta_swiec WHERE zdarzenie = 'swiece_przepisane'"
        "   AND timeframe = %s AND adjustment = %s AND instrument_id = ANY(%s)"
        " GROUP BY instrument_id", (timeframe, KOREKTA_SWIEC, ids))
    przepisane = dict((r["instrument_id"], r) for r in cur.fetchall())

    bez_sladu = sorted(w["symbol"] for w in wiersze if w["id"] not in przepisane)
    if bez_sladu:
        odmowy.append(
            f"brak sladu przepisania ({KOREKTA_SWIEC}, {timeframe}) dla: "
            f"{', '.join(bez_sladu)} - najpierw --przepisz")

    do_zbadania = [w for w in wiersze if w["id"] in przepisane]
    if not do_zbadania:
        return postawione, odmowy

    cur.execute(
        "SELECT min(event_time) AS od, max(event_time) AS do_ FROM market.candle"
        " WHERE timeframe = %s AND instrument_id = ANY(%s)",
        (timeframe, [w["id"] for w in do_zbadania]))
    okno = cur.fetchone()
    od, do = okno["od"], okno["do_"]

    feat_ids = features.ensure_definitions(cur, h, [w["symbol"] for w in wiersze])
    # COMMIT, NIE ROLLBACK - i to jest warunek poprawnosci sprawdzenia nizej.
    # `rozjazd_cech` wycofuje swoja transakcje, wiec wycofalaby TAKZE swiezo
    # powstale definicje, a `compute_prices` pisalby wartosci wskazujace na
    # `feature_def_id`, ktorego juz nie ma. Definicja cechy to zresztą katalog,
    # nie dane: `features.py` zapisuje ja tak samo w kazdym cyklu.
    conn.commit()

    for w in do_zbadania:
        niezgodne, brakujace = rozjazd_cech(conn, w["id"], h, feat_ids, od, do)
        stan = "zgodne" if niezgodne == 0 else f"NIEZGODNE: {niezgodne}"
        brak = f", brakujacych {brakujace}" if brakujace else ""
        print(f"  {w['symbol']:10s} cechy vs swiece: {stan}{brak}")
        if niezgodne:
            odmowy.append(f"{w['symbol']}: {niezgodne} zapisanych wartosci cech "
                          f"nie zgadza sie ze swiecami")

    p_niezgodne, p_brak = rozjazd_przekroju(conn, h, feat_ids, engine, od, do)
    stan = "zgodny" if p_niezgodne == 0 else f"NIEZGODNY: {p_niezgodne}"
    brak = f", brakujacych {p_brak}" if p_brak else ""
    print(f"  {'przekroj':10s} rel_strength: {stan}{brak}")
    if p_niezgodne:
        odmowy.append(f"cecha przekrojowa rel_strength: {p_niezgodne} zapisanych "
                      f"wartosci nie zgadza sie ze swiecami (CALY zbior)")

    if odmowy:
        return postawione, odmowy

    for w in do_zbadania:
        r = Raport(symbol=w["symbol"], instrument_id=w["id"])
        r.okno_od, r.okno_do = przepisane[w["id"]]["od"], do
        cur.execute(
            "UPDATE ref.instrument SET meta = coalesce(meta, jsonb_build_object())"
            " || jsonb_build_object(%s::text, true) WHERE id = %s",
            (ZNACZNIK_KOREKTY_PODZIALOW, w["id"]))
        zapisz_slad(cur, r, timeframe, "znacznik", KOREKTA_SWIEC,
                    szczegol="cechy zgodne ze swiecami po przepisaniu")
        conn.commit()
        postawione.append(w["symbol"])
    return postawione, odmowy


async def main() -> int:
    ap = argparse.ArgumentParser(description="Korekta swiec o podzialy akcji")
    ap.add_argument("--engine", default="equity")
    ap.add_argument("--timeframe", default="1h", choices=sorted(TF_MINUTES))
    ap.add_argument("--bar", default="1h",
                    help="horyzont, ktorego cechy sprawdza --znacznik")
    ap.add_argument("--symbol", help="tylko ten instrument")
    ap.add_argument("--sucho", action="store_true",
                    help="pomiar bez zapisu: ile swiec, ile brakuje, ile sie zmieni")
    ap.add_argument("--przepisz", action="store_true",
                    help="przepisz szereg (jedna transakcja na instrument)")
    ap.add_argument("--znacznik", action="store_true",
                    help="postaw znacznik korekty - po sprawdzeniu cech")
    ap.add_argument("--korekta", choices=("raw", "split", "dividend", "all"),
                    help="TYLKO z --sucho: o jaka korekte zapytac dostawce. "
                         "Sluzy sprawdzeniu, czy parametr w ogole dochodzi "
                         "(raw i split musza dac ROZNE liczby)")
    args = ap.parse_args()

    tryby = [t for t in ("sucho", "przepisz", "znacznik") if getattr(args, t)]
    if len(tryby) != 1:
        sys.exit("podaj DOKLADNIE jeden tryb: --sucho, --przepisz albo --znacznik")
    if args.korekta and not args.sucho:
        # Zapis MUSI isc ta korekta, ktora zadeklarowal adapter - inaczej
        # znacznik mowilby o czym innym, niz jest w swiecach.
        sys.exit("--korekta jest dopuszczalna WYLACZNIE z --sucho")

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")
    h = horizons.get(args.bar)
    if h.source_timeframe != args.timeframe:
        sys.exit(f"horyzont {args.bar} liczy sie ze swiec {h.source_timeframe}, "
                 f"a --timeframe to {args.timeframe}")
    return await przebieg(url, args, h)


async def przebieg(url: str, args, h) -> int:
    """Jeden przebieg na WLASNYM polaczeniu. Wydzielone z `main`, zeby test
    mogl wolac to samo, co terminal, bez parsowania argumentow."""
    with psycopg.connect(url, row_factory=dict_row) as conn:
        cur = conn.cursor(row_factory=dict_row)
        try:
            wiersze = uniwersum(cur, args.engine, args.symbol)
        except RuntimeError as e:
            sys.exit(str(e))
        print(f"uniwersum decyzyjne {args.engine}: {len(wiersze)} instrumentow\n")

        if args.znacznik:
            postawione, odmowy = tryb_znacznika(conn, wiersze, args.timeframe,
                                                h, args.engine)
            for o in odmowy:
                print(f"\nODMOWA: {o}")
            ile = f"{len(postawione)} z {len(wiersze)}"
            kto = f" ({', '.join(postawione)})" if postawione else ""
            print(f"\nznacznik postawiony: {ile}{kto}")
            return 1 if odmowy else 0

        adapter = adapter_danych(conn, args.engine)
        try:
            if args.sucho:
                raporty = await tryb_pomiaru(conn, adapter, wiersze,
                                             args.timeframe, args.korekta)
                niepelne = [r.symbol for r in raporty if not r.pelny]
                print(f"\nRAZEM zmieni wartosc: "
                      f"{sum(r.zmienionych for r in raporty)} wierszy "
                      f"market.candle w {len(raporty)} szeregach")
                if niepelne:
                    print(f"NIEPELNE POKRYCIE: {', '.join(niepelne)} - dla tych "
                          f"instrumentow --przepisz ODMOWI zapisu")
                return 0
            raporty, odrzucone = await tryb_przepisania(conn, adapter, wiersze,
                                                        args.timeframe)
            print(f"\nprzepisane szeregi: {len(raporty)} z {len(wiersze)}; "
                  f"wierszy zmienionych: {sum(r.zmienionych for r in raporty)}")
            for o in odrzucone:
                print(f"ODRZUCONY: {o}")
            print("znacznika NIE postawiono - to robi osobny przebieg "
                  "`--znacznik`, po przeliczeniu cech")
            return 1 if odrzucone else 0
        finally:
            await adapter.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
