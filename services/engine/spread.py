"""Spread arkusza ZMIERZONY Z BAZY, per silnik - zamiast stalej w charcie.

DLACZEGO TEN MODUL ISTNIEJE

Do 2026-09-27 koszt spreadu wchodzil do trenera jako `trainerCron.spreadBps:
5.85` - jedna liczba wpisana w chart 2026-09-25. Stala byla POMIAREM, ale
migawkowym: nikt nie zapisal zapytania, ktore ja dalo, wiec nie dalo sie jej
ani odtworzyc, ani zauwazyc, ze zgnila. A zgnije sama: uniwersum decyzyjne
zmienia sie migracja, cennik i plynnosc gieldy zmieniaja sie bez migracji,
a spread zalezy od obu.

Od tej daty spread liczy sie Z BAZY, na TYM SAMYM uniwersum i w TYM SAMYM
oknie, ktore liczy trening - patrz `train.train`, ktore podaje tu wprost
`instrument_ids`, `since` i `until` swojego przebiegu. Agregat policzony na
innym zbiorze albo innym okresie opisywalby inny rynek niz ten, ktory
symulujemy, i roznicy nie bylo by z czego zobaczyc.

CZYM JEST `book_spread_bps` - Z DOWODU W KODZIE, NIE Z NAZWY

`services/exchanges/base.OrderBook.spread` to `asks[0].price - bids[0].price`,
czyli PELNY spread w jednostkach ceny. `market_worker` zapisuje go do
`market.book_snapshot.spread` bez przeliczania, a `features.compute_orderbook`
agreguje `avg(spread / NULLIF(mid, 0)) * 10000`, czyli PELNY spread jako czesc
ceny, w punktach bazowych, sredni po swiecy.

RUNDA PLACI DOKLADNIE TYLE, ILE WYNOSI TA LICZBA. Wejscie placi pol spreadu
(od mida do aska), wyjscie drugie pol (od mida do bidu) - razem jeden pelny
spread. "Polowa spreadu na noge razy dwie nogi" z `koszty.karta_rundy` jest
wiec JUZ W SRODKU tej cechy i nie wolno jej mnozyc przez dwa. Podwojenie
zawyzyloby doplate dwukrotnie.

KTORA STATYSTYKA - I DLACZEGO NIE MEDIANA

Koszt placi kazda TRANSAKCJA, nie kazda swieca, wiec szukana wielkosc to
WARTOSC OCZEKIWANA spreadu w chwilach, w ktorych skladamy zlecenia. Wartosc
oczekiwana jest liniowa, wiec jej estymatorem jest SREDNIA - nie mediana.

Rozklad jest skosny i roznica nie jest kosmetyczna. Zmierzone 2026-09-27 na
uniwersum decyzyjnym crypto (10 par USDC, swieca 4h, 1370 obserwacji):

    mediana 4,46 pb | srednia 8,62 pb | q3 9,59 pb | p90 22,98 pb | p99 67,38 pb

Mediana zanizylaby koszt rundy o czynnik 1,93 - i to jest ten kierunek bledu,
ktory czyni strategie lepsza, niz jest.

USREDNIAMY NAJPIERW PO INSTRUMENCIE, POTEM PO INSTRUMENTACH. Powod jest
mierzalny: rozrzut MIEDZY instrumentami przewyzsza rozrzut w czasie o trzy
rzedy wielkosci (mediana BTCUSDC 0,013 pb, NEARUSDC 34,21 pb). Instrument
o krotszym pokryciu - swiezy listing albo dziura w arkuszu - wchodzilby wtedy
do zlaczonej sredniej z waga swojej liczby swiec, czyli o jego wplywie na
koszt decydowalaby kompletnosc danych, a nie rynek.

CZEGO TA LICZBA NADAL NIE JEST: sredniej WAZONEJ TRANSAKCJAMI. Rownomierna
waga po instrumentach jest zalozeniem, ze transakcje rozkladaja sie po
uniwersum rowno - a nie rozkladaja. Kierunek tego bledu jest ZMIERZONY:
15 pozycji USDC z `exec.position` (2026-09-27) daje sredni spread wazony
faktycznym skladem 11,22 pb wobec 8,62 pb przy wadze rownomiernej, bo
strategia weszla m.in. w ARB i NEAR - najszersze pary uniwersum. Przy n = 15
nie jest to estymator, ale jest dowodem, ze rownomierna waga daje DOLNE
oszacowanie. Karta kosztu i tak zostaje niepelna, wiec ten brak jest opisany
tam, gdzie powinien.

BRAMKA SPOJNOSCI - PO CO ODRZUCAMY WLASNY POMIAR

Cecha moze byc policzona poprawnie z arkusza, ktory nie jest arkuszem
wypelniajacym nasze zlecenia. `services/exchanges/alpaca.fetch_order_book`
czyta `/v2/stocks/quotes/latest` z feedu IEX - JEDNEJ gieldy z kilkunastu -
a zlecenie Alpaca kieruje na caly rynek. Zmierzone 2026-09-27 na 20 spolkach
uniwersum equity: mediana `book_spread_bps` to 41 pb, a dla JNJ 479 pb, czyli
4,8% ceny. Podstawienie tego do karty kosztu zabiloby kazda strategie equity
na artefakcie danych.

Pytanie, ktore ta bramka zadaje, nie wymaga wiedzy o gieldzie: czy kwotowany
spread jest porownywalny z CALYM zakresem ruchu ceny w tej samej swiecy. Jesli
jest - kwotowanie bracketuje kazda transakcje wydrukowana w tym oknie, wiec nie
jest cena, po ktorej ktokolwiek handluje. Kontrola na tych samych danych:

    crypto (tradeable, 1h, 5480 par): mediana ilorazu 0,033 - 50 par ponad zakres
    equity (tradeable, 1h,  560 par): mediana ilorazu 0,843 - 257 par ponad zakres

Prog 0,5 lezy 15x nad crypto i 1,7x pod equity, wiec nie jest dobrany tak, zeby
przestawic werdykt - obie strony sa od niego daleko.

BRAK POMIARU ZOSTAJE BRAKIEM POMIARU. Kazda odmowa oddaje `bps = None`
i `powod` do wypisania. Wtedy `karta_rundy` zostawia skladnik "spread" jako
NIE ZMIERZONO - dokladnie ten stan, ktory obowiazywal dla equity, gdy klucz
`trainerCron.equity.spreadBps` byl zakomentowany. Zadna sciezka w tym module
nie podstawia liczby z drugiego silnika.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

from services.engine.statystyka import rozklad

# Od tej chwili `BybitAdapter.fetch_order_book` bierze arkusz z `self._host`,
# czyli z gieldy, na ktora ida zlecenia (opisane w migracji 0018 i w
# `tests/test_arkusz_per_listing.py`). Wczesniej arkusz KAZDEGO instrumentu
# szedl z hosta globalnego, wiec kazda wartosc `book_spread_bps` z data
# wczesniejsza jest pomiarem INNEJ ksiegi.
#
# Data, nie kolumna w bazie: `market.book_snapshot` nie ma kolumny zrodla,
# a te wiersze juz istnieja. Rozstrzyga wiec to, co o zrodle zdecydowalo -
# kod z tej daty oraz `ref.instrument.tradeable` (od 2026-09-25 ksiegi
# egzekucji pytamy WYLACZNIE o instrumenty tradeable).
DATA_WLASNEJ_KSIEGI = datetime(2026, 9, 24, tzinfo=timezone.utc)

# Instrument o krotszym pokryciu niz to NIE WCHODZI do agregatu. Srednia z pieciu
# swiec mowi o momencie pomiaru, nie o rynku - ta sama zasada, ktora w
# `features.compute_orderbook` odrzuca swiece o garstce snapshotow.
MIN_SWIEC_NA_INSTRUMENT = 30

# Ponizej tylu instrumentow agregat opisuje kilka wybranych par, a nie
# uniwersum, ktorego koszt liczymy.
MIN_INSTRUMENTOW = 3

# Jaka czesc obserwacji musi dac sie zestawic ze swieca, zeby bramka spojnosci
# byla sprawdzona. Nizej oddajemy BRAK POMIARU, a nie pomiar niesprawdzony:
# kontrola, ktorej nie dalo sie wykonac, nie jest kontrola zaliczona.
MIN_POKRYCIE_SPOJNOSCI = 0.5

# Mediana ilorazu (spread swiecy / zakres high-low tej swiecy), powyzej ktorej
# odrzucamy caly pomiar. Uzasadnienie i zmierzone wartosci - w naglowku modulu.
PROG_SPOJNOSCI = 0.5

_SQL = """
WITH okno AS (
    SELECT fv.instrument_id, fv.event_time, fv.value AS spread_bps, i.tradeable
    FROM feat.feature_value fv
    JOIN feat.feature_def fd ON fd.id = fv.feature_def_id
    JOIN ref.instrument   i  ON i.id  = fv.instrument_id
    WHERE fd.active
      AND fd.params->>'role' = 'book_spread_bps'
      AND fd.params->>'bar'  = %(bar)s
      AND fv.instrument_id = ANY(%(ids)s)
      AND fv.event_time >= %(od)s AND fv.event_time < %(do)s
      AND fv.ingested_at <= %(do)s
      AND fv.value IS NOT NULL
), swiece AS (
    SELECT instrument_id,
           time_bucket(%(interwal)s::interval, event_time) AS t,
           max(high) AS gora, min(low) AS dol
    FROM market.candle
    WHERE instrument_id = ANY(%(ids)s)
      AND timeframe = %(zrodlo)s
      AND event_time >= %(od)s AND event_time < %(do)s
    GROUP BY 1, 2
)
SELECT o.instrument_id, o.event_time, o.spread_bps, o.tradeable,
       CASE WHEN s.gora + s.dol > 0
            THEN (s.gora - s.dol) / ((s.gora + s.dol) / 2) * 10000
       END AS zakres_bps
FROM okno o
LEFT JOIN swiece s ON s.instrument_id = o.instrument_id AND s.t = o.event_time
ORDER BY o.instrument_id, o.event_time
"""


@dataclass(frozen=True)
class Pomiar:
    """Wynik pomiaru spreadu razem z tym, czego o nim nie wiadomo.

    `bps` to PELNY spread w punktach bazowych, czyli koszt CALEJ RUNDY - patrz
    naglowek modulu. None znaczy: pomiaru nie ma, a `powod` mowi dlaczego.
    Zero w tym miejscu twierdziloby, ze spread nie istnieje.
    """
    bps: float | None
    nasza_ksiega: bool
    n: int = 0
    instrumentow: int = 0
    pominietych: int = 0
    od: datetime | None = None
    do: datetime | None = None
    spojnosc: float | None = None
    rozklad: dict = field(default_factory=dict)
    per_instrument: tuple[tuple[int, float, int], ...] = ()
    powod: str = ""


def _bez_wartosci(powod: str, **kw) -> Pomiar:
    # `nasza_ksiega=False` przy braku wartosci nie jest twierdzeniem o zrodle -
    # nie ma o czym twierdzic. `karta_rundy` i tak tej flagi nie czyta, gdy
    # `spread_bps is None`.
    return Pomiar(bps=None, nasza_ksiega=False, powod=powod, **kw)


def zmierz_spread(cur, engine: str, bar: str, source_timeframe: str,
                  bar_minutes: int, instrument_ids: list[int],
                  since: datetime, until: datetime) -> Pomiar:
    """Spread rundy z bazy, dla PODANEGO uniwersum i okna. Jedno zapytanie.

    `engine` nie wchodzi do zapytania - uniwersum przychodzi juz rozstrzygniete
    jako `instrument_ids`, dokladnie to, ktore trenuje `train()`. Parametr
    zostaje, bo decyduje o tym, czy feed arkusza JEST ksiega egzekucji:
    u Bybita od `DATA_WLASNEJ_KSIEGI` jest, u Alpaki (feed IEX, jedna gielda
    z kilkunastu) nie jest nigdy.
    """
    if not instrument_ids:
        return _bez_wartosci("puste uniwersum - nie ma czego mierzyc")
    cur.execute(_SQL, {"bar": bar, "ids": list(instrument_ids),
                       "od": since, "do": until,
                       "interwal": f"{bar_minutes} minutes",
                       "zrodlo": source_timeframe})
    wiersze = cur.fetchall()
    if not wiersze:
        return _bez_wartosci(
            f"cecha book_spread_bps nie ma ani jednej wartosci dla swiecy {bar} "
            f"na tym uniwersum w oknie {since:%Y-%m-%d}..{until:%Y-%m-%d}")

    wg_instrumentu: dict[int, list[float]] = {}
    tradeable: dict[int, bool] = {}
    for r in wiersze:
        wg_instrumentu.setdefault(r["instrument_id"], []).append(float(r["spread_bps"]))
        tradeable[r["instrument_id"]] = bool(r["tradeable"])

    dosc = {i: v for i, v in wg_instrumentu.items() if len(v) >= MIN_SWIEC_NA_INSTRUMENT}
    pominietych = len(wg_instrumentu) - len(dosc)
    if len(dosc) < MIN_INSTRUMENTOW:
        return _bez_wartosci(
            f"tylko {len(dosc)} instrumentow ma co najmniej "
            f"{MIN_SWIEC_NA_INSTRUMENT} swiec z arkuszem (wymagane "
            f"{MIN_INSTRUMENTOW}); pomiar opisywalby kilka par, nie uniwersum",
            n=sum(len(v) for v in wg_instrumentu.values()),
            instrumentow=len(wg_instrumentu), pominietych=pominietych)

    brane = [r for r in wiersze if r["instrument_id"] in dosc]
    czasy = [r["event_time"] for r in brane]
    wszystkie = [float(r["spread_bps"]) for r in brane]
    wspolne = dict(n=len(wszystkie), instrumentow=len(dosc),
                   pominietych=pominietych, od=min(czasy), do=max(czasy),
                   rozklad=rozklad(wszystkie))

    # --- bramka spojnosci: czy to jest cena, po ktorej ktos handluje ---------
    ilorazy = [float(r["spread_bps"]) / float(r["zakres_bps"]) for r in brane
               if r["zakres_bps"] is not None and float(r["zakres_bps"]) > 0]
    if len(ilorazy) < MIN_POKRYCIE_SPOJNOSCI * len(brane):
        return _bez_wartosci(
            f"nie da sie sprawdzic spojnosci: tylko {len(ilorazy)} z "
            f"{len(brane)} obserwacji ma swiece {source_timeframe} o dodatnim "
            f"zakresie, a kontrola niewykonana nie jest kontrola zaliczona",
            **wspolne)
    spojnosc = statistics.median(ilorazy)
    wspolne["spojnosc"] = spojnosc
    if spojnosc > PROG_SPOJNOSCI:
        return _bez_wartosci(
            f"ODRZUCONY: kwotowany spread to mediana {spojnosc:.2f} CALEGO "
            f"zakresu high-low tej samej swiecy (prog {PROG_SPOJNOSCI:.2f}) - "
            f"kwotowanie bracketuje kazda transakcje wydrukowana w tym oknie, "
            f"wiec nie jest cena, po ktorej ktokolwiek handluje. Dla silnika "
            f"equity zrodlem jest feed IEX (jedna gielda z kilkunastu), a "
            f"zlecenie Alpaca kieruje na caly rynek",
            **wspolne)

    per = sorted(((i, statistics.fmean(v), len(v)) for i, v in dosc.items()),
                 key=lambda t: t[1])
    bps = statistics.fmean([sr for _, sr, _ in per])

    # --- czyja to ksiega ----------------------------------------------------
    # Dwa warunki i oba musza zachodzic. Okno siegajace przed
    # DATA_WLASNEJ_KSIEGI zawiera wiersze z hosta globalnego; instrument
    # `tradeable = false` ma arkusz z hosta globalnego NADAL i celowo.
    nasza = (engine == "crypto"
             and min(czasy) >= DATA_WLASNEJ_KSIEGI
             and all(tradeable[i] for i in dosc))
    return Pomiar(bps=bps, nasza_ksiega=nasza,
                  per_instrument=tuple(per), **wspolne)


def opis_pomiaru(p: Pomiar, symbole: dict[int, str] | None = None) -> list[str]:
    """Pomiar wypisany Z ROZKLADEM, nie jedna liczba.

    Jednej liczby nie da sie sprawdzic wzrokiem: 8,62 pb przy medianie 4,46 pb
    i p90 22,98 pb to inny stan rynku niz 8,62 pb przy medianie 8,5 pb, a karta
    kosztu pokazuje w obu wypadkach to samo.
    """
    if p.bps is None:
        return [f"Spread z bazy: NIE ZMIERZONO — {p.powod}"]
    r = p.rozklad
    linie = [
        f"Spread z bazy: {p.bps:.3f} pb na rundę (średnia ze średnich per "
        f"instrument, {p.instrumentow} instrumentów, {p.n} świec)",
        f"  okno pomiaru: {p.od:%Y-%m-%d %H:%M}..{p.do:%Y-%m-%d %H:%M} UTC"
        + (f", pominięto {p.pominietych} instrumentów poniżej "
           f"{MIN_SWIEC_NA_INSTRUMENT} świec" if p.pominietych else ""),
        f"  rozkład po świecach: mediana {r['mediana']:.3f} | q1 "
        f"{r['q1']:.3f} | q3 {r['q3']:.3f} | max {r['najwiekszy']:.3f} pb",
    ]
    if p.spojnosc is not None:
        linie.append(f"  spójność: kwotowanie to mediana {p.spojnosc:.3f} "
                     f"zakresu high-low świecy (próg {PROG_SPOJNOSCI:.2f})")
    if p.per_instrument:
        naz = symbole or {}
        skraj = [p.per_instrument[0], p.per_instrument[-1]]
        linie.append("  skrajne instrumenty: " + ", ".join(
            f"{naz.get(i, i)} {sr:.3f} pb" for i, sr, _ in skraj))
    if not p.nasza_ksiega:
        linie.append("  UWAGA: to NIE jest księga, w której staje nasze "
                     "zlecenie — liczba wchodzi do karty jako OSZACOWANIE, "
                     "karta ZOSTAJE NIEPEŁNA")
    return linie
