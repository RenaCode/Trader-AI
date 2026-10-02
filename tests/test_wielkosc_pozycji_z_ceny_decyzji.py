"""Symulacja liczy liczbę sztuk z TEJ SAMEJ ceny, z której liczy ją produkcja.

Pętla decyzyjna (`loop.py`, gałąź `enter_*`) robi `qty = notional / price`,
gdzie `price` to zamknięcie świecy decyzyjnej - innej ceny w chwili decyzji nie
zna. Do 2026-10-02 `simulate_portfolio` dzieliła ten sam notional przez cenę
NASTĘPNEJ świecy. Po luce w górę kupowała więc mniej sztuk niż produkcja, po
luce w dół więcej - rozjazd systematycznie skorelowany z ruchem, który jest
przedmiotem pomiaru.

Test wymusza lukę 2x między świecą decyzji a świecą wykonania i patrzy na
EKSPOZYCJĘ (sztuki razy cena rynkowa) - wielkość, którą widzi risk kernel,
a nie na pole dodane razem z poprawką.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services.engine.horizons import get as get_horizon
from services.engine.portfolio import simulate_portfolio
from services.engine.signals import DEFAULT_WEIGHTS

H = get_horizon("4h")
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

LIMITY = {
    "max_daily_loss":        {"value": Decimal("99"),        "action": "killswitch"},
    "max_drawdown":          {"value": Decimal("99"),        "action": "killswitch"},
    "max_orders_per_hour":   {"value": Decimal("1000"),      "action": "block"},
    # Żaden limit nie może tu wiązać - inaczej mierzymy obcięcie, nie sizing.
    "max_position_notional": {"value": Decimal("999999999"), "action": "downsize"},
    "max_total_exposure":    {"value": Decimal("1000"),      "action": "block"},
    "max_leverage":          {"value": Decimal("9999"),      "action": "block"},
}


def _cechy(kup: bool) -> dict:
    wspolne = {"atr_pct": 1.0, "rsi": 50.0, "range_pos": 0.5, "volume_z": 0.0,
               "news_sent": 0.0, "funding_rate": 0.0, "funding_z": 0.0}
    if kup:
        return wspolne | {"ema_ratio": 1.01, "ret_short": 20.0,
                          "ret_mid": 40.0, "ret_long": 80.0}
    return wspolne | {"ema_ratio": 1.0, "ret_short": 0.0,
                      "ret_mid": 0.0, "ret_long": 0.0}


def _panel(cena_po_luce: float) -> list[dict]:
    """Decyzja na świecy 0 (cena 100), od świecy 1 cena po luce.

    Sygnał kupna trzyma się do świecy 2, więc pozycja przeżywa pomiar
    ekspozycji na świecach 1-2 (wyjścia idą PRZED pomiarem - przy sygnale
    tylko na świecy 0 pozycja znikała przed nim i test porównywał zero
    z zerem). Potem sygnał neutralny i wyjście - jedna transakcja."""
    out = []
    for i in range(8):
        cena = 100.0 if i == 0 else cena_po_luce
        out.append({"t": T0 + timedelta(hours=4 * i), "f": _cechy(i <= 2),
                    "price": cena, "high": cena, "low": cena})
    return out


def _ekspozycja(cena_po_luce: float) -> float:
    res = simulate_portfolio({"AAA": _panel(cena_po_luce)}, DEFAULT_WEIGHTS,
                             {"AAA": 2.0}, {"AAA": (0.05, 0.0225)}, H, 0.05, LIMITY)
    assert len(res.trades) == 1, "kontrola: ma powstać dokładnie jedna transakcja"
    assert res.max_exposure > 0, "kontrola: pozycja ma trafić do pomiaru ekspozycji"
    return res.max_exposure


@pytest.mark.parametrize("luka", [2.0, 0.5])
def test_liczba_sztuk_z_ceny_DECYZJI_jak_w_petli_produkcyjnej(luka):
    """Bez luki ekspozycja to notional. Z luką `x` produkcja ma tyle samo sztuk
    (notional / 100), więc ekspozycja po cenie rynkowej to `x` razy notional.
    Stary kod dzielił przez cenę po luce i ekspozycja zostawała równa
    notionalowi - czyli symulował inną liczbę sztuk niż ta, którą kupi pętla."""
    bez_luki = _ekspozycja(100.0)
    z_luka = _ekspozycja(100.0 * luka)
    assert z_luka == pytest.approx(bez_luki * luka, rel=1e-9), (
        f"luka {luka}x: ekspozycja {z_luka:.2f} wobec {bez_luki:.2f} bez luki — "
        f"liczba sztuk policzona z ceny następnej świecy, nie z ceny decyzji")
