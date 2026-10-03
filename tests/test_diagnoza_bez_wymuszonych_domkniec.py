"""Test permutacyjny porownuje TE SAME transakcje po obu stronach - bez domkniec koncem okna.

Pozycja otwarta na koncu okna zamyka sie w `simulate_portfolio` po cenie
OSTATNIEJ swiecy, rynkowo, z poslizgiem i stawka taker. `zwrot_losowej` przy
limicie wychodzi swiece wczesniej i placi makera. Do 2026-10-03
`diagnose_timing.main` wpuszczal takie transakcje do obu stron: na panelu
`_panel(20)` z limitem faktyczny +0,0553 wobec losowego +0,0343 z tej samej
swiecy decyzji i dlugosci (reprodukcja audytu 2, R-d).

Wlasnosc: przy permutacji TOZSAMOSCIOWEJ (kazda transakcja dostaje wlasna
swiece i dlugosc) srednia losowych MUSI byc srednia faktycznych. Koszty
wyzerowane (`stock`, `poslizg=0`), zeby porownywac ceny.
"""
from __future__ import annotations

import statistics

import pytest

from services.engine.diagnose_timing import transakcje_do_porownania, zwrot_losowej
from services.engine.portfolio import Egzekucja, simulate_portfolio
from services.engine.signals import DEFAULT_WEIGHTS
from tests.test_zwrot_losowej_jak_symulacja import H, LIMITY, _panel


def _symuluj(n, limit):
    panel = _panel(n)
    res = simulate_portfolio({"AAA": panel}, DEFAULT_WEIGHTS, {"AAA": 2.0},
                             {"AAA": (0.05, 0.0225)}, H, 0.05, LIMITY,
                             kind="stock", egzekucja=Egzekucja(limit=limit),
                             poslizg=0.0)
    return panel, res


@pytest.mark.parametrize("limit", [False, True])
def test_permutacja_tozsamosciowa_daje_wynik_faktyczny(limit):
    sprawdzonych_z_wymuszonym = 0
    for n in range(20, 80):
        panel, res = _symuluj(n, limit)
        koniec = panel[-1]["t"]
        wymuszone = [t for t in res.trades if t["closed_at"] == koniec]
        zwroty, wzorce = transakcje_do_porownania(res.trades, {"AAA": panel})
        if not wzorce:
            continue
        assert len(zwroty) == len(wzorce) == len(res.trades) - len(wymuszone)
        losowe = [zwrot_losowej(panel, i0, dl, 0.0, limit=limit)
                  for _, i0, dl in wzorce]
        assert statistics.fmean(losowe) == pytest.approx(
            statistics.fmean(zwroty), abs=1e-12), f"n={n}"
        sprawdzonych_z_wymuszonym += bool(wymuszone)
    # Kanarek: test ma przejsc przez panele, na ktorych domkniecie wymuszone
    # NAPRAWDE wystapilo - inaczej nie sprawdzalby niczego.
    assert sprawdzonych_z_wymuszonym >= 3


def test_wymuszone_domkniecie_z_limitem_rozjezdza_sie_ze_zwrotem_losowym():
    """Dowod, ze wykluczenie jest potrzebne: na tym samym panelu wymuszona
    transakcja przeliczona przez `zwrot_losowej` daje INNY zwrot."""
    panel, res = _symuluj(20, True)
    czasy = [r["t"] for r in panel]
    t = next(t for t in res.trades if t["closed_at"] == czasy[-1])
    i0, i1 = czasy.index(t["opened_at"]), czasy.index(t["closed_at"])
    assert zwrot_losowej(panel, i0, i1 - i0, 0.0, limit=True) != pytest.approx(
        t["ret"], abs=1e-6)
