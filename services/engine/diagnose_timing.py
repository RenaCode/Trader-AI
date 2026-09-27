#!/usr/bin/env python3
"""Czy sygnal cokolwiek WYCZUWA W CZASIE - test permutacyjny.

PYTANIE, KTOREGO NIKT DOTAD NIE ZADAL

Projekt mierzy dwie rzeczy i zadna nie odpowiada na to pytanie:

  - ODSETEK DODATNICH OKIEN premiuje NIEUCZESTNICZENIE. Strategia trzymajaca
    mala ekspozycje na spadajacym rynku ma duzo okien dodatnich, bo rzadko
    traci - i rzadko cokolwiek wyczuwa.

  - POKONANIE KUP-I-TRZYMAJ porownuje z pelna ekspozycja. Strategia o dwuprocentowej
    ekspozycji pobije trzymanie na kazdym rynku spadajacym i przegra na kazdym
    rosnacym, niezaleznie od tego, czy jej wejscia sa trafione czy losowe.

Brakuje benchmarku z TA SAMA EKSPOZYCJA i LOSOWYM CZASEM. Jesli te same
transakcje - ta sama liczba, te same dlugosci, te same instrumenty - ustawione
w losowych momentach daja to samo, to sygnal nie wnosi nic poza profilem
ekspozycji.

DLACZEGO TO NIE JEST KOLEJNA HIPOTEZA

Model zerowy nie jest kandydatem do wyboru. Niczego nie optymalizujemy, nie
zmieniamy zadnych wag i nie zapisujemy nic do learn.hypothesis_trial. Liczymy
jedna liczbe: percentyl, na ktorym wypada wynik faktyczny wsrod wynikow
losowych.

CO ZACHOWUJEMY, A CO LOSUJEMY

Zachowane: liczba transakcji, dlugosc kazdej z nich, instrument, okno czasowe,
model kosztow. Losowany: WYLACZNIE moment wejscia.

Dzieki temu roznica moze pochodzic tylko z czasu - a nie z tego, ze strategia
handluje rzadziej, krocej albo na innych rynkach.

CZEGO TEN TEST NIE ROBI

Nie przepuszcza transakcji losowych przez risk kernel: nie sprawdza limitow
ekspozycji ani kill-switcha. Pytanie brzmi "czy moment wejscia cokolwiek
znaczy", a nie "czy losowa strategia bylaby wykonalna". Wynik nalezy czytac
jako test TRAFNOSCI CZASU, nie jako alternatywna strategie.

Uzycie:
    DATABASE_URL=... python services/engine/diagnose_timing.py --bar 4h
"""
from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg
from psycopg.rows import dict_row

from services.engine.horizons import DEFAULT_BAR, HORIZONS, Horizon, get as get_horizon
from services.engine.koszty import runda
from services.engine.portfolio import Egzekucja, precompute_panels, simulate_portfolio
from services.engine.risk_rules import MIN_CONFIDENCE
from services.engine.train import (
    DEFAULT_WEIGHTS, MIN_INSTRUMENTS, _bez_martwych, atr_threshold, ic_weights, load_panel,
    bramki_okna, martwe_sygnaly,
)
from services.engine.uniwersum import SQL_UNIWERSUM_DECYZYJNE, odcisk

# Ile losowych ukladow czasu na okno. 200 daje percentyl z dokladnoscia 0,5 pkt,
# co w zupelnosci wystarcza do odpowiedzi "srodek rozkladu czy ogon".
LOSOWAN = 200
SEED = 20260907


def zwrot_losowej(panel: list[dict], start: int, dlugosc: int,
                  koszt_rundy: float) -> float | None:
    """Zwrot transakcji o zadanej dlugosci, wchodzacej w swiecy `start`.

    Wejscie po cenie swiecy start+1 i wyjscie po start+1+dlugosc - dokladnie tak,
    jak liczy to symulacja portfelowa. Koszt rundy odejmowany raz, bo dotyczy
    obu nog razem.
    """
    a, b = start + 1, start + 1 + dlugosc
    if b >= len(panel):
        return None
    p0, p1 = panel[a]["price"], panel[b]["price"]
    if not p0:
        return None
    # Pozycja dluga - na spocie innych nie ma (patrz komentarz przy `wzorce`).
    return (p1 / p0 - 1) - koszt_rundy


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bar", default=DEFAULT_BAR, choices=sorted(HORIZONS))
    ap.add_argument("--days", type=int, default=1400)
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--kind", default="spot_eu", choices=("spot", "spot_eu", "perp"))
    ap.add_argument("--limit", action=argparse.BooleanOptionalAction, default=True,
                    help="zlecenia z limitem (domyślnie); --no-limit dla rynkowych")
    ap.add_argument("--losowan", type=int, default=LOSOWAN)
    # KTORA STRATEGIA JEST TESTOWANA. `bazowe` = DEFAULT_WEIGHTS - wariant BEZ
    # zmierzonej przewagi (docs/03: 20/40 okien, Sharpe +0,008). Produkcja
    # i +6,8% pochodza z wag `ic`. Pomiar z 22.09.2026 na `bazowe` dal
    # percentyl ~50 i zostal odczytany jako "brak timingu" - a mowil tylko,
    # ze strategia bez przewagi nie ma timingu. Domysl zostaje `bazowe` dla
    # porownywalnosci z 07.09; wlasciwe pytanie zadaje `--wagi ic`.
    ap.add_argument("--wagi", default="bazowe", choices=("bazowe", "ic"))
    # CO JEST LOSOWANE. `czas`: wejscie w losowej swiecy przy zachowanej
    # dlugosci (pytanie: czy strategia wyczuwa MOMENT). `dlugosc`: wejscie
    # w faktycznej swiecy, a dlugosci trzymania POTASOWANE miedzy transakcjami
    # okna (ten sam rozklad; pytanie: czy przewaga siedzi w REGULE WYJSCIA).
    # Hipoteza stratega z 22.09.2026: trafnosc ~53% w kazdym rezimie przy
    # profit factor 1,76 znaczy, ze o wyniku decyduje wielkosc wygranych
    # i przegranych - a te ustala wyjscie, nie wejscie. Test `czas` tego
    # wymiaru z konstrukcji nie widzi, bo trzyma dlugosci stale.
    ap.add_argument("--losuj", default="czas", choices=("czas", "dlugosc"))
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    h = get_horizon(args.bar)
    now = datetime.now(timezone.utc)
    rng = random.Random(SEED)
    koszt = runda(args.kind, args.limit)
    egz = Egzekucja(limit=args.limit)

    print(f"Test permutacyjny ({'czasu wejścia' if args.losuj == 'czas' else 'długości trzymania'}, wagi {args.wagi}) — świeca {h.bar}, {args.losowan} losowań na okno")
    print(f"Egzekucja: {args.kind}, koszt rundy {koszt * 100:.3f}%\n")

    with psycopg.connect(url, row_factory=dict_row) as conn:
        cur = conn.cursor()
        cur.execute(SQL_UNIWERSUM_DECYZYJNE, (args.engine,))
        instrumenty = cur.fetchall()
        inst_ids = {i["symbol"]: i["id"] for i in instrumenty}
        if not instrumenty:
            print("PRZERWANO: uniwersum decyzyjne jest puste")
            return 2

        # ODCISK UNIWERSUM. Bez niego `load_panel` odsiewa cechy przekrojowe
        # warunkiem `params->>'uniwersum' = NULL`, ktory jest falszem dla kazdego
        # wiersza - `rel_strength` wypadalo wiec z panelu, `cross_section()`
        # zwracalo None, a `combine()` renormalizowalo wynik po pozostalych
        # SIEDMIU sygnalach. Test permutacyjny mierzyl przez to inna strategie
        # niz ta, ktora prowadzi produkcja, i nie mial jak tego powiedziec:
        # brakujacej cechy nie widac w wydruku niczym poza samym wynikiem.
        fp = odcisk([i["symbol"] for i in instrumenty])
        print(f"Uniwersum: {len(instrumenty)} instrumentów, odcisk {fp}")

        panels: dict[str, list[dict]] = {}
        for inst in instrumenty:
            p = load_panel(cur, inst["id"], h, now - timedelta(days=args.days), now,
                           fp=fp)
            if len(p) > 500:
                panels[inst["symbol"]] = p
        if len(panels) < MIN_INSTRUMENTS:
            print(f"PRZERWANO: {len(panels)} instrumentów z panelem, próg to {MIN_INSTRUMENTS}")
            return 2
        print(f"instrumentów: {len(panels)}\n")

    percentyle: list[float] = []
    okna = 0
    start = now - timedelta(days=args.days)
    while start + timedelta(days=h.train_days + h.test_days) <= now:
        train_to = start + timedelta(days=h.train_days)
        test_to = train_to + timedelta(days=h.test_days)
        test = {s: [r for r in panels[s] if train_to <= r["t"] < test_to] for s in panels}
        test = {s: v for s, v in test.items() if len(v) > 20}
        if len(test) < MIN_INSTRUMENTS:
            start += timedelta(days=h.step_days)
            continue

        trening = {s: [r for r in panels[s] if start <= r["t"] < train_to] for s in test}
        prog = {s: atr_threshold(trening[s]) for s in test}
        cache_tr = precompute_panels(trening, prog, h)
        # DOKLADNIE TA SAMA SCIEZKA CO W TRENERZE (train.py, petla okien):
        # martwe sygnaly poza kalibracja, bramki z wag bazowych po
        # wyzerowaniu martwych, handel wagami `ic` z okna treningowego.
        # Rozjazd w ktorymkolwiek z tych trzech krokow mierzylby inna
        # strategie niz ta, ktora chodzi na produkcji.
        martwe = martwe_sygnaly(cache_tr)
        bazowe = _bez_martwych(DEFAULT_WEIGHTS, martwe)
        wagi = ic_weights(trening, cache_tr, martwe) if args.wagi == "ic" else bazowe
        # PROGI PER REZIM, z tej samej funkcji, ktorej uzywa trener - inaczej
        # diagnostyka mierzylaby prog wspolny, ktorego produkcja od 2026-09-27
        # nie stosuje.
        gates = bramki_okna({s: trening[s] for s in test}, bazowe, prog, h,
                            {s: cache_tr[s] for s in test})
        cache_te = precompute_panels(test, prog, h)

        res = simulate_portfolio(test, wagi, prog, gates, h, MIN_CONFIDENCE,
                                 instrument_ids=inst_ids, cache=cache_te,
                                 kind=args.kind, egzekucja=egz)
        if len(res.trades) < h.min_trades:
            start += timedelta(days=h.step_days)
            continue

        faktyczny = statistics.fmean(t["ret"] for t in res.trades)

        # Dlugosc kazdej transakcji w swiecach - odtwarzana z panelu instrumentu.
        wzorce = []
        for t in res.trades:
            seria = test[t["symbol"]]
            czasy = [r["t"] for r in seria]
            try:
                i0, i1 = czasy.index(t["opened_at"]), czasy.index(t["closed_at"])
            except ValueError:
                continue
            # KIERUNEK JEST ZAWSZE DLUGI i to nie jest uproszczenie tego testu.
            #
            # Na spocie nie da sie zajac krotkiej pozycji bez pozyczki marzowej,
            # wiec simulate_portfolio ich nie otwiera (koszty.krotka_pozycja).
            # Zapisuje to jawnie, zamiast wyliczac kierunek ze znaku zwrotu -
            # taki wzor wygladalby na obsluge shortow, a faktycznie zwracalby
            # jedynke zawsze.
            wzorce.append((t["symbol"], i0, max(1, i1 - i0)))
        if len(wzorce) < h.min_trades:
            start += timedelta(days=h.step_days)
            continue

        losowe = []
        dlugosci = [d for _, _, d in wzorce]
        for _ in range(args.losowan):
            zwroty = []
            if args.losuj == "dlugosc":
                # Tasowanie dlugosci MIEDZY transakcjami okna: kazda transakcja
                # dostaje dlugosc innej, wejscie zostaje w swojej swiecy.
                # `zwrot_losowej` wchodzi w swiecy start+1, wiec start = i0-1.
                rng.shuffle(dlugosci)
                for (sym, i0, _), dl in zip(wzorce, dlugosci):
                    z = zwrot_losowej(test[sym], i0 - 1, dl, koszt) if i0 >= 1 else None
                    if z is not None:
                        zwroty.append(z)
            else:
                for sym, _, dlugosc in wzorce:
                    seria = test[sym]
                    if len(seria) - dlugosc - 2 <= 0:
                        continue
                    i = rng.randrange(0, len(seria) - dlugosc - 2)
                    z = zwrot_losowej(seria, i, dlugosc, koszt)
                    if z is not None:
                        zwroty.append(z)
            if zwroty:
                losowe.append(statistics.fmean(zwroty))
        if len(losowe) < args.losowan // 2:
            start += timedelta(days=h.step_days)
            continue

        gorszych = sum(1 for x in losowe if x < faktyczny)
        pct = gorszych / len(losowe) * 100
        percentyle.append(pct)
        okna += 1
        print(f"  okno {okna:>2}  {train_to:%m-%d}–{test_to:%m-%d}  "
              f"transakcji={len(wzorce):>3}  faktyczny={faktyczny * 100:+.3f}%  "
              f"mediana losowych={statistics.median(losowe) * 100:+.3f}%  "
              f"percentyl={pct:.0f}")
        start += timedelta(days=h.step_days)

    if not percentyle:
        print("\nPRZERWANO: żadne okno nie dało dość transakcji do testu.")
        return 2

    sredni = statistics.fmean(percentyle)
    powyzej = sum(1 for p in percentyle if p > 50)

    # ISTOTNOSC LICZONA TUTAJ, NIE ZOSTAWIONA CZYTELNIKOWI.
    #
    # Przy braku trafnosci czasu percentyl kazdego okna ma rozklad jednostajny
    # na (0, 100): srednia 50, odchylenie 100/sqrt(12) = 28,9. Blad standardowy
    # sredniej z `okna` okien to 28,9/sqrt(okna).
    #
    # Bez tej linii liczba w rodzaju "43,3" zacheca do wniosku "gorzej niz
    # losowo", ktory przy trzydziestu kilku oknach zwykle nie jest uprawniony.
    se_sredniej = 28.87 / okna ** 0.5
    odchylenie = (sredni - 50) / se_sredniej
    # Liczba okien powyzej mediany: dwumian o p=0,5.
    se_licznika = (okna * 0.25) ** 0.5
    odchylenie_licznika = (powyzej - okna / 2) / se_licznika if se_licznika else 0.0

    print(f"\nOkien: {okna}")
    print(f"Średni percentyl wyniku faktycznego wśród losowych: {sredni:.1f}"
          f"   (oczekiwane 50, błąd std. ±{se_sredniej:.1f}, odchylenie {odchylenie:+.1f})")
    print(f"Okien powyżej mediany losowej: {powyzej}/{okna} ({powyzej / okna * 100:.0f}%)"
          f"   (oczekiwane {okna / 2:.1f}, odchylenie {odchylenie_licznika:+.1f})")

    if abs(odchylenie) < 2:
        print()
        print("WERDYKT: nieodróżnialne od losowego. Oba odchylenia poniżej dwóch")
        print("błędów standardowych — sygnał nie wykazuje mierzalnej trafności")
        print("czasu wejścia, ale nie da się też stwierdzić, że szkodzi.")
    elif odchylenie >= 2:
        print()
        print("WERDYKT: moment wejścia coś wnosi — wynik istotnie powyżej losowego.")
    else:
        print()
        print("WERDYKT: moment wejścia wnosi NA NIEKORZYŚĆ — wynik istotnie poniżej")
        print("losowego. Strategia trafiałaby lepiej, wchodząc w przypadkowych chwilach.")
    print()
    print("JAK TO CZYTAĆ")
    print("  Percentyl 50 znaczy, że wejścia w losowych momentach dałyby to samo —")
    print("  czyli sygnał nie wyczuwa czasu i cały wynik pochodzi z profilu")
    print("  ekspozycji. Wartość wyraźnie powyżej 50 znaczy, że moment wejścia")
    print("  coś wnosi; poniżej — że wnosi na niekorzyść.")
    print()
    print("  Test zachowuje liczbę transakcji, ich długości i instrumenty.")
    print("  Losowany jest WYŁĄCZNIE moment wejścia.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
