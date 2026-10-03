"""Ekspozycja strategii liczy kazdy niesiony przedzial RAZ - takze ostatni.

CO BYLO ZLE (audyt 2 R-d, audyt Fable F-3.5): `avg_exposure` mierzy portfel po
wyjsciach i PRZED wejsciami swiecy. Pozycja otwarta w swiecy `a` i domknieta
koncem okna w `b` niesie cene przez `b - a` przedzialow, a do sredniej weszla
`b - a - 1` razy. Koszyk kup-i-trzymaj niesie cene przez kazdy przedzial okna,
wiec „koszyk przy tej samej ekspozycji" byl mnozony przez za mala liczbe -
obciazenie na korzysc strategii. Porownania ida po `ekspozycja_przenoszona`.
"""
from datetime import timedelta

import pytest

from services.engine.portfolio import (
    Egzekucja, kup_i_trzymaj, metrics, simulate_portfolio,
)
from services.engine.risk_rules import START_EQUITY
from services.engine.signals import DEFAULT_WEIGHTS
from tests.test_portfolio import H, LIMITY_LUZNE, T0, cechy_kupuj

N = 30


def _plaski():
    return [{"t": T0 + timedelta(hours=4 * i), "f": cechy_kupuj(), "price": 100.0,
             "low": 99.0, "high": 101.0} for i in range(N)]


def _jedna_pozycja_do_konca():
    res = simulate_portfolio({"AAA": _plaski()}, DEFAULT_WEIGHTS, {"AAA": 2.0},
                             {"AAA": (0.05, 0.02)}, H, 0.05, LIMITY_LUZNE,
                             kind="spot", egzekucja=Egzekucja(limit=False))
    assert len(res.trades) == 1, "test zaklada jedna pozycje trzymana do konca okna"
    return res


def test_ekspozycja_przenoszona_liczy_kazdy_przedzial_raz():
    res = _jedna_pozycja_do_konca()
    notional = res.trades[0]["notional"]
    # Wejscie w pierwszej swiecy, domkniecie koncem okna: N-1 przedzialow,
    # kazdy z pozycja. Plaska cena - notional staly (z dokladnoscia do
    # poslizgu wejscia: pierwszy przedzial po cenie wejscia, reszta po 100).
    assert res.avg_exposure_przenoszona == pytest.approx(notional, rel=1e-3)


def test_stara_miara_gubi_jeden_przedzial_na_transakcje():
    res = _jedna_pozycja_do_konca()
    notional = res.trades[0]["notional"]
    assert res.avg_exposure == pytest.approx(notional * (N - 2) / (N - 1), rel=1e-3)
    assert res.avg_exposure < res.avg_exposure_przenoszona


def test_metryki_niosa_obie_miary():
    m = metrics(_jedna_pozycja_do_konca(), 1)
    assert m["ekspozycja_przenoszona"] > m["avg_exposure"] > 0
    assert m["ekspozycja_przenoszona"] <= 1.0
    assert START_EQUITY > 0


def test_koszyk_liczy_ostatnia_swiece_raz():
    """Kup-i-trzymaj: zwrot od pierwszej do ostatniej ceny okna, jedna runda -
    dolozenie swiecy o tej samej cenie co ostatnia niczego nie zmienia."""
    rows = [{"t": T0 + timedelta(hours=4 * i), "price": 100.0 + i} for i in range(N)]
    dluzszy = rows + [{"t": rows[-1]["t"] + timedelta(hours=4), "price": rows[-1]["price"]}]
    assert kup_i_trzymaj({"AAA": rows}, "spot") == pytest.approx(
        kup_i_trzymaj({"AAA": dluzszy}, "spot"))
