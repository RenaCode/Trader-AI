#!/usr/bin/env python3
"""Test w przód „czy trening się uczy" (H-4, docs/hipotezy.md) — SZKIELET.

    python services/engine/w_przod.py [--engine crypto] [--bar 4h]

PO CO. Trener porównuje się sam ze sobą na walk-forwardzie, którego okna
nakładają się w 29/30 dni, a zmiana miary 02-03.10 wyzerowała porównania
przebiegów (n = 1). Pytanie „czy wersja k jest lepsza od k−1" ma jedną uczciwą
odpowiedź: obie wersje zapisane i zamrożone PRZED oknem, porównane na oknie,
które zamknęło się PO zapisaniu obu — i liczba prób równa liczbie takich okien,
nie transakcji.

CO TEN PLIK ROBI DZIŚ
  * `pary_okien` — dla każdego pełnego tygodnia kalendarzowego (pon 00:00 UTC)
    wybiera wersję najnowszą przed jego początkiem i wersję, która była
    najnowsza tydzień wcześniej. Okna są rozłączne z konstrukcji; tydzień bez
    nowej wersji (trener padł, nic nie zapisał) nie daje pary.
  * `werdykt` — kryterium H-4 zapisane przed pomiarem: test znaków po oknach,
    rozstrzyga WYŁĄCZNIE pierwszych `N_OKIEN` okien z różnicą ≠ 0.
  * wypis par i okien zamkniętych z `learn.model_version` (tylko odczyt).

CZEGO JESZCZE NIE ROBI — `miara_okna`. Zwrot reguły ensemble z `params` danej
wersji na jednym tygodniu, po kosztach, przy tej samej ekspozycji, wymaga
panelu z bazy obciętego do okna i symulacji z `train` (`precompute_panels`,
`simulate_portfolio`, karta `spot_eu`). To osobna zmiana; do tego czasu
`miara_okna` mówi to wprost zamiast zwracać zero.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

TYDZIEN = timedelta(days=7)
# Kryterium H-4. Zmiana którejkolwiek z tych liczb to nowa hipoteza.
N_OKIEN = 12        # okien z różnicą ≠ 0, po których zapada werdykt
PROG_LEPSZYCH = 10  # v_k lepsza w co najmniej tylu z N_OKIEN (p ≈ 0,019)
# Pierwszy tydzień PO zapisaniu H-4 (05.10, poniedziałek, w trakcie dnia).
# Okna wcześniejsze dało się już obejrzeć, więc nie są testem w przód.
POCZATEK = datetime(2026, 10, 12, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Para:
    od: datetime
    do: datetime
    nowa: int         # numer wersji najnowszej przed `od`
    poprzednia: int   # numer wersji najnowszej przed `od - TYDZIEN`


def poczatek_tygodnia(t: datetime) -> datetime:
    t = t.astimezone(timezone.utc)
    return datetime(t.year, t.month, t.day, tzinfo=timezone.utc) - timedelta(days=t.weekday())


def _najnowsza_przed(wersje: list[tuple[int, datetime]], chwila: datetime) -> int | None:
    przed = [v for v, kiedy in wersje if kiedy < chwila]
    return przed[-1] if przed else None


def pary_okien(wersje: list[tuple[int, datetime]], teraz: datetime,
               od: datetime = POCZATEK) -> list[Para]:
    """Pary (v_k, v_{k−1}) na oknach ZAMKNIĘTYCH przed `teraz`. CZYSTA FUNKCJA.

    `wersje`: (numer, created_at). Wersja zapisana w trakcie tygodnia W jest
    oceniana dopiero na W+1 — jej prognozy nie mogą dotknąć okna, w którym
    jeszcze jej nie było.
    """
    wersje = sorted(wersje, key=lambda w: w[1])
    if not wersje:
        return []
    pary = []
    okno = max(poczatek_tygodnia(wersje[0][1]) + TYDZIEN, poczatek_tygodnia(od))
    while okno + TYDZIEN <= teraz:
        nowa = _najnowsza_przed(wersje, okno)
        poprzednia = _najnowsza_przed(wersje, okno - TYDZIEN)
        if poprzednia is not None and nowa != poprzednia:
            pary.append(Para(okno, okno + TYDZIEN, nowa, poprzednia))
        okno += TYDZIEN
    return pary


def _ogon_dwumianowy(k: int, n: int) -> float:
    """P(X >= k) dla X ~ Bin(n, 1/2)."""
    return sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n


def werdykt(roznice: list[float]) -> tuple[str, str]:
    """('w toku' | 'UCZY SIĘ' | 'NIE WYKAZANO', opis). CZYSTA FUNKCJA.

    `roznice`: miara(v_k) − miara(v_{k−1}) po oknach, w kolejności kalendarza.
    Remis (obie wersje bez transakcji albo identyczne) nie jest próbą.
    Werdykt bierze PIERWSZE `N_OKIEN` rozstrzygniętych okien i ani jednego
    więcej: dokładanie okien do chwili, w której wynik „wyjdzie", to
    zatrzymanie warunkowe, nie test.
    """
    rozstrzygniete = [d for d in roznice if d != 0]
    remisy = len(roznice) - len(rozstrzygniete)
    if len(rozstrzygniete) < N_OKIEN:
        return "w toku", (f"{len(rozstrzygniete)}/{N_OKIEN} okien rozstrzygniętych"
                          f" (remisów {remisy}) — werdykt dopiero po {N_OKIEN}")
    lepsza = sum(d > 0 for d in rozstrzygniete[:N_OKIEN])
    p = _ogon_dwumianowy(lepsza, N_OKIEN)
    opis = (f"v_k lepsza w {lepsza}/{N_OKIEN} okien (p = {p:.3f}, próg "
            f"{PROG_LEPSZYCH}/{N_OKIEN}); remisów {remisy}")
    return ("UCZY SIĘ" if lepsza >= PROG_LEPSZYCH else "NIE WYKAZANO"), opis


def miara_okna(cur, params: dict, para: Para):
    raise NotImplementedError(
        "miara okna H-4 niezaimplementowana: zwrot reguły ensemble z `params` "
        "wersji na oknie [od, do) po kosztach spot_eu przy tej samej ekspozycji "
        "— patrz docs/hipotezy.md, H-4, „Co dalej”")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--bar", default="4h")
    args = ap.parse_args(argv)

    import psycopg
    from psycopg.rows import dict_row

    nazwa = f"{args.engine}_signal_weighting_{args.bar}"
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as c:
        wiersze = c.execute(
            "SELECT v.version, v.created_at FROM learn.model_version v"
            " JOIN learn.model m ON m.id = v.model_id"
            " WHERE m.name = %s AND v.status <> 'failed' ORDER BY v.created_at",
            (nazwa,)).fetchall()
    pary = pary_okien([(w["version"], w["created_at"]) for w in wiersze],
                      datetime.now(timezone.utc))
    print(f"H-4 {nazwa}: wersji {len(wiersze)}, okien zamkniętych z parą {len(pary)}")
    for p in pary:
        print(f"  {p.od:%Y-%m-%d} – {p.do:%Y-%m-%d}: v{p.nowa} wobec v{p.poprzednia}")
    print("  miara okna: NIEZAIMPLEMENTOWANA (szkielet) — werdyktu nie ma z czego "
          "policzyć; docs/hipotezy.md, H-4")
    return 0


if __name__ == "__main__":
    sys.exit(main())
