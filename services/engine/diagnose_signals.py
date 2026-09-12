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
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg
from psycopg.rows import dict_row

from services.engine.horizons import DEFAULT_BAR, HORIZONS, get as get_horizon
from services.engine.statystyka import spearman
from services.engine.uniwersum import SQL_UNIWERSUM_DECYZYJNE, odcisk
from services.engine.portfolio import precompute_panels
from services.engine.train import atr_threshold, load_panel

# Na ile swiec do przodu mierzymy zwrot. Wejscie zawsze po cenie NASTEPNEJ
# swiecy, wiec zwrot liczymy od niej - inaczej mierzylibysmy ruch, ktorego
# nie dalo sie zlapac.
HORYZONTY = (1, 3, 6, 12)

# Cechy zbierane, ale NIEUZYWANE przez zaden sygnal.
#
# Mikrostruktura arkusza zlecen jest liczona od 2026-09-04, a sygnal na niej
# oparty celowo jeszcze nie istnieje: kalibrowanie go na kilku dniach dodaloby
# stopien swobody i nic nie powiedzialoby o rynku (patrz komentarz przy
# compute_orderbook).
#
# ALE MIERZENIE TO NIE BUDOWANIE. IC jest statystyka opisowa - nie wybiera
# niczego, nie wchodzi do licznika hipotez i nie zmienia zadnych wag. Odpowiada
# na pytanie, ktore trzeba zadac ZANIM sygnal powstanie: czy w tych danych
# w ogole cokolwiek jest. Jesli po kilku miesiacach IC bedzie zerowe, sygnal nie
# powstanie wcale i zaoszczedzimy sobie stopnia swobody.
CECHY_BEZ_SYGNALU = ("book_imbalance", "book_spread_bps")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bar", default=DEFAULT_BAR, choices=sorted(HORIZONS))
    ap.add_argument("--days", type=int, default=1400)
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--instrumenty", default="",
                    help="lista symboli po przecinku - zawezenie do zbioru. "
                         "Do porownania IC MIEDZY uniwersami: gdy dwie grupy maja "
                         "osobno przewage, a razem nie, przyczyna musi byc widoczna "
                         "w tym, ze ten sam sygnal ma w nich rozny znak. Puste = "
                         "uniwersum decyzyjne (active AND tradeable), czyli to, "
                         "co obstawia produkcja")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    h = get_horizon(args.bar)
    now = datetime.now(timezone.utc)

    with psycopg.connect(url, row_factory=dict_row) as conn:
        cur = conn.cursor()
        tylko = [x.strip().upper() for x in args.instrumenty.split(",") if x.strip()]
        if tylko:
            # ZBIOR PODANY WPROST - do porownania uniwersum kandydujacego
            # z obowiazujacym. Odcisk liczy sie wtedy z DOKLADNIE tej listy,
            # tak samo jak przy `features.py --instrumenty`.
            cur.execute("SELECT id, symbol FROM ref.instrument WHERE engine=%s AND active"
                        " AND kind <> 'perp' AND symbol = ANY(%s)"
                        " ORDER BY symbol", (args.engine, tylko))
        else:
            # UNIWERSUM DECYZYJNE, NIE „WSZYSTKIE AKTYWNE".
            #
            # Do 2026-09-12 staly tu `active AND kind <> 'perp'` bez `tradeable`
            # i wynikaly z tego dwie rzeczy naraz, obie ciche:
            #
            #   * odcisk policzony z tego zbioru nie pasowal do ZADNEJ definicji
            #     w `feat.feature_def` (potok cech zapisuje je pod odciskiem
            #     uniwersum decyzyjnego), wiec `load_panel` nie oddawal ani
            #     jednego wiersza `rel_strength`. `cross_section()` zwracal None,
            #     a tabela IC drukowala sie bez tego wiersza - czyli akurat bez
            #     liczby, po ktora sie tu przychodzi;
            #   * od migracji 0023 ten sam aktyw jest w bazie dwa razy (*USDT
            #     i *USDC), wiec proba IC liczyla BTC podwojnie. `n` roslo
            #     dwukrotnie bez jednej nowej obserwacji, a blad standardowy
            #     wypisany nizej malal przez to o czynnik do sqrt(2).
            cur.execute(SQL_UNIWERSUM_DECYZYJNE, (args.engine,))
        instrumenty = cur.fetchall()
        brak = sorted(set(tylko) - {i["symbol"] for i in instrumenty})
        if brak:
            sys.exit(f"nie ma w bazie albo nieaktywne: {', '.join(brak)}")
        if not instrumenty:
            sys.exit("uniwersum decyzyjne jest puste — nie ma czego mierzyć")

        # ODCISK UNIWERSUM dla cechy przekrojowej. Bez niego sila wzgledna byla by
        # martwa w calej diagnostyce, a jej IC - jedynego sygnalu patrzacego na
        # instrument PRZEZ PRYZMAT pozostalych - nie dalo by sie porownac miedzy
        # zbiorami. Czyli akurat tej liczby, po ktora sie tu przychodzi.
        fp = odcisk([i["symbol"] for i in instrumenty])
        print(f"Uniwersum: {len(instrumenty)} instrumentów, odcisk {fp}")

        # {sygnal: {horyzont: ([wyniki], [zwroty])}}
        zebrane: dict[str, dict[int, tuple[list, list]]] = {}
        cechy: dict[str, dict[int, tuple[list, list]]] = {}
        MIN_PANEL = 500
        pominiete: list[tuple[str, int]] = []
        for inst in instrumenty:
            panel = load_panel(cur, inst["id"], h,
                               now - timedelta(days=args.days), now, fp=fp)
            if len(panel) < MIN_PANEL:
                # POMINIECIE JEST WYNIKIEM, NIE MILCZENIEM.
                #
                # Przy zbyt krotkim `--days` odpadaja WSZYSTKIE instrumenty
                # i skrypt wypisywal pusta tabele, konczac sie zerem. Wygladalo
                # to jak "zaden sygnal nic nie przewiduje", a znaczylo "nie bylo
                # czego liczyc" - dwie rzeczy nie do odroznienia z wydruku.
                pominiete.append((inst["symbol"], len(panel)))
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

                    # Surowe cechy, ktorych zaden sygnal nie konsumuje. Tu nie ma
                    # progu "sygnal milczy" - wartosc cechy zawsze cos znaczy.
                    # Cechy siedza pod kluczem "f", nie na wierzchu wiersza -
                    # panel to {t, price, high, low, f:{rola: wartosc}}.
                    for rola in CECHY_BEZ_SYGNALU:
                        wartosc = (panel[i].get("f") or {}).get(rola)
                        if wartosc is None:
                            continue
                        w = cechy.setdefault(rola, {}).setdefault(k, ([], []))
                        w[0].append(float(wartosc))
                        w[1].append(zwrot)
            print(f"  {inst['symbol']:10s} {len(panel):>6,} świec", flush=True)

    if pominiete:
        print(f"\n  pominięte (panel < {MIN_PANEL} świec): "
              + ", ".join(f"{s} ({n})" for s, n in pominiete[:8])
              + (f" i {len(pominiete) - 8} więcej" if len(pominiete) > 8 else ""))
    if not zebrane:
        print(f"\nPRZERWANO: żaden instrument nie ma panelu dłuższego niż {MIN_PANEL} świec.")
        print(f"  --days {args.days} daje przy świecy {h.bar} około "
              f"{args.days * 1440 // h.bar_minutes} świec na instrument.")
        print(f"  Potrzeba co najmniej {MIN_PANEL}, czyli --days "
              f"{MIN_PANEL * h.bar_minutes // 1440 + 1} lub więcej.")
        return 2

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

    if cechy:
        print()
        print("CECHY ZBIERANE, KTÓRYCH ŻADEN SYGNAŁ JESZCZE NIE UŻYWA")
        print(naglowek.replace("sygnał", "cecha "))
        print("-" * len(naglowek))
        for name in sorted(cechy):
            wiersz = name.ljust(20)
            n = 0
            for k in HORYZONTY:
                xs, ys = cechy[name].get(k, ([], []))
                ic = spearman(xs, ys)
                n = max(n, len(xs))
                wiersz += (f"{ic:+.4f}".rjust(11) if ic is not None else "—".rjust(11))
            print(f"{wiersz}   {n:,}")
        print()
        print("Sygnał oparty na tych cechach CELOWO jeszcze nie istnieje —")
        print("historia mikrostruktury jest za krótka. To jest pomiar, nie budowa:")
        print("nie wybiera niczego i nie wchodzi do licznika hipotez.")

    print()
    print("BŁĄD STANDARDOWY IC — z poprawką na nakładanie się obserwacji")
    print()
    print("  Zwrot z k świec liczony przy KAŻDEJ świecy nakłada się na k-1")
    print("  sąsiednich pomiarów: te obserwacje nie są niezależne. Naiwne")
    print("  1/√n zaniża więc błąd tym mocniej, im dłuższy horyzont — czyli")
    print("  dokładnie tam, gdzie IC zwykle wygląda najlepiej.")
    print("  Efektywna liczba obserwacji to w przybliżeniu n/k.")
    print()
    naglowek2 = "seria".ljust(20) + "".join(f"{k:>4} św.".rjust(11) for k in HORYZONTY)
    print(naglowek2)
    print("-" * len(naglowek2))
    for zbior, etykieta in ((zebrane, ""), (cechy, " (cecha)")):
        for name in sorted(zbior):
            wiersz = (name + etykieta).ljust(20)
            for k in HORYZONTY:
                xs, _ = zbior[name].get(k, ([], []))
                n_ef = len(xs) / k if xs else 0
                se = (1 / n_ef ** 0.5) if n_ef else float("inf")
                ic = spearman(*zbior[name].get(k, ([], [])))
                krotnosc = (abs(ic) / se) if (ic is not None and se) else 0.0
                wiersz += f"±{se:.4f}({krotnosc:.1f})".rjust(11)
            print(wiersz)
    print()
    print("  W nawiasie: |IC| wyrażone w błędach standardowych. Poniżej 2 to")
    print("  wynik nieodróżnialny od zera niezależnie od tego, jak duży wygląda.")

    print()
    print("IC to korelacja rangowa między wynikiem sygnału a zwrotem PO nim.")
    print("Odniesienie: |IC| < 0,01 to szum; ~0,02 bywa użyteczne przy wielu")
    print("transakcjach; > 0,05 to dużo jak na dane rzadsze niż minutowe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
