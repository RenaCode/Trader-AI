#!/usr/bin/env python3
"""Wspolczynnik informacji (IC) dla kazdego sygnalu z osobna.

PYTANIE, NA KTORE ODPOWIADA

Walk-forward mowi, czy CALOSC ma przewage. Nie mowi, czy problem jest
w skladnikach, czy w przepisie. Jesli zaden pojedynczy sygnal nie koreluje
z przyszlym zwrotem, zadne wazenie tego nie naprawi - i dalsze dobieranie wag
jest strata czasu. Jesli ktorys koreluje, a calosc nie, to znaczy, ze psuje ja
sposob laczenia albo koszty.

CO TO JEST IC

Korelacja rangowa (Spearman) miedzy wynikiem sygnalu w chwili T a zwrotem
zrealizowanym PO tej chwili. Rangowa, a nie liniowa, bo interesuje nas
uporzadkowanie sytuacji od najlepszej do najgorszej, a nie skala.

Wartosci odniesienia z literatury dla strategii systematycznych:
    |IC| < 0,01   szum
    |IC| ~ 0,02   slabe, ale uzyteczne przy duzej liczbie transakcji
    |IC| > 0,05   mocne jak na dane dzienne i rzadsze

TO NIE JEST DOPASOWANIE. Skrypt niczego nie optymalizuje i nie zmienia zadnych
wag - liczy jedna statystyke opisowa na calej dostepnej historii. Wynik nie
wchodzi do licznika hipotez, bo nie ma tu czego wybierac.

Uzycie:
    DATABASE_URL=... python services/engine/diagnose_signals.py --bar 4h
"""
from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg
from psycopg.rows import dict_row

from services.engine.horizons import DEFAULT_BAR, HORIZONS, get as get_horizon
from services.engine.portfolio import precompute_panels
from services.engine.train import atr_threshold, load_panel

# Na ile swiec do przodu mierzymy zwrot. Wejscie zawsze po cenie NASTEPNEJ
# swiecy, wiec zwrot liczymy od niej - inaczej mierzylibysmy ruch, ktorego
# nie dalo sie zlapac.
HORYZONTY = (1, 3, 6, 12)


def rangi(xs: list[float]) -> list[float]:
    """Rangi z usrednianiem remisow."""
    posortowane = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(posortowane):
        j = i
        while j + 1 < len(posortowane) and xs[posortowane[j + 1]] == xs[posortowane[i]]:
            j += 1
        srednia = (i + j) / 2 + 1
        for k in range(i, j + 1):
            out[posortowane[k]] = srednia
        i = j + 1
    return out


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 30:
        return None
    rx, ry = rangi(xs), rangi(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    licz = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return licz / (sx * sy) if sx > 0 and sy > 0 else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bar", default=DEFAULT_BAR, choices=sorted(HORIZONS))
    ap.add_argument("--days", type=int, default=1400)
    ap.add_argument("--engine", default="crypto")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    h = get_horizon(args.bar)
    now = datetime.now(timezone.utc)

    with psycopg.connect(url, row_factory=dict_row) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, symbol FROM ref.instrument WHERE engine=%s AND active"
                    " AND kind <> 'perp' ORDER BY symbol", (args.engine,))
        instrumenty = cur.fetchall()

        # {sygnal: {horyzont: ([wyniki], [zwroty])}}
        zebrane: dict[str, dict[int, tuple[list, list]]] = {}
        for inst in instrumenty:
            panel = load_panel(cur, inst["id"], h, now - timedelta(days=args.days), now)
            if len(panel) < 500:
                continue
            cache = precompute_panels({inst["symbol"]: panel},
                                      {inst["symbol"]: atr_threshold(panel)},
                                      h)[inst["symbol"]]
            ceny = [r["price"] for r in panel]
            for i, (_regime, sygnaly) in enumerate(cache):
                for k in HORYZONTY:
                    # Wejscie po cenie swiecy i+1, wyjscie po cenie i+1+k.
                    if i + 1 + k >= len(ceny):
                        continue
                    zwrot = ceny[i + 1 + k] / ceny[i + 1] - 1
                    for name, out in sygnaly.items():
                        if out.score == 0.0:
                            continue        # sygnal milczy - nie jest predykcja
                        w = zebrane.setdefault(name, {}).setdefault(k, ([], []))
                        w[0].append(out.score)
                        w[1].append(zwrot)
            print(f"  {inst['symbol']:10s} {len(panel):>6,} świec", flush=True)

    print()
    naglowek = "sygnał".ljust(20) + "".join(f"{k:>4} św.".rjust(11) for k in HORYZONTY) + "   n"
    print(naglowek)
    print("-" * len(naglowek))
    for name in sorted(zebrane):
        wiersz = name.ljust(20)
        n = 0
        for k in HORYZONTY:
            xs, ys = zebrane[name].get(k, ([], []))
            ic = spearman(xs, ys)
            n = max(n, len(xs))
            wiersz += (f"{ic:+.4f}".rjust(11) if ic is not None else "—".rjust(11))
        print(f"{wiersz}   {n:,}")

    print()
    print("IC to korelacja rangowa między wynikiem sygnału a zwrotem PO nim.")
    print("Odniesienie: |IC| < 0,01 to szum; ~0,02 bywa użyteczne przy wielu")
    print("transakcjach; > 0,05 to dużo jak na dane rzadsze niż minutowe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
