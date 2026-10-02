"""Transakcja losowa w teście permutacyjnym trzyma cenę TYLE SAMO świec co faktyczna.

`diagnose_timing.main` odtwarza długość transakcji jako `i1 - i0`, gdzie `i0`
to indeks `opened_at` (świeca DECYZJI), a `i1` indeks `closed_at`. Symulacja
wchodzi po cenie `i0+1` i wychodzi po cenie `i1` (zlecenie rynkowe) albo po
cenach `i0` i `i1-1` (limit) - w obu przypadkach cena jest trzymana przez
`i1-i0-1` świec. Do 2026-10-02 `zwrot_losowej` brała `start+1 .. start+1+dlugosc`,
czyli trzymała JEDNĄ ŚWIECĘ DŁUŻEJ, a w trybie `--losuj dlugosc` wchodziła
dodatkowo o świecę wcześniej niż zlecenie rynkowe.

Test wprost: transakcje z `simulate_portfolio` przeliczone przez `zwrot_losowej`
z ich własną świecą decyzji i długością MUSZĄ dać ten sam zwrot. Koszty
wyzerowane (`stock`: prowizja 0, `poslizg=0`), żeby porównywać same ceny.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services.engine.diagnose_timing import zwrot_losowej
from services.engine.horizons import get as get_horizon
from services.engine.portfolio import Egzekucja, simulate_portfolio
from services.engine.signals import DEFAULT_WEIGHTS

H = get_horizon("4h")
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

LIMITY = {
    "max_daily_loss":        {"value": Decimal("99"),        "action": "killswitch"},
    "max_drawdown":          {"value": Decimal("99"),        "action": "killswitch"},
    "max_orders_per_hour":   {"value": Decimal("1000"),      "action": "block"},
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


def _panel(n: int = 60) -> list[dict]:
    """Cena falująca (każda świeca inna), sygnał kupna migający co 4 świece.

    Zakres świecy szeroki, żeby limit wypełniał się zawsze - porównujemy ceny,
    nie model wypełnień."""
    out = []
    for i in range(n):
        cena = 100.0 * (1 + 0.03 * math.sin(i * 0.7) + 0.002 * i)
        out.append({"t": T0 + timedelta(hours=4 * i), "f": _cechy((i // 4) % 2 == 0),
                    "price": cena, "high": cena * 2, "low": cena / 2})
    return out


@pytest.mark.parametrize("limit", [False, True])
def test_zwrot_losowej_odtwarza_transakcje_faktyczna_co_do_swiecy(limit):
    panel = _panel()
    res = simulate_portfolio({"AAA": panel}, DEFAULT_WEIGHTS, {"AAA": 2.0},
                             {"AAA": (0.05, 0.0225)}, H, 0.05, LIMITY,
                             kind="stock", egzekucja=Egzekucja(limit=limit),
                             poslizg=0.0)
    czasy = [r["t"] for r in panel]
    # Domknięcie na końcu okna idzie zawsze po rynku z poślizgiem taker -
    # to inna ścieżka niż ta, którą odtwarza test permutacyjny.
    zwykle = [t for t in res.trades if t["closed_at"] != czasy[-1]]
    assert len(zwykle) >= 3, "kontrola: mają powstać transakcje do porównania"
    for t in zwykle:
        i0, i1 = czasy.index(t["opened_at"]), czasy.index(t["closed_at"])
        z = zwrot_losowej(panel, i0, i1 - i0, 0.0, limit=limit)
        assert z == pytest.approx(t["ret"], abs=1e-12), (
            f"transakcja {i0}->{i1}: losowa {z:+.6f} wobec faktycznej "
            f"{t['ret']:+.6f} — inne świece niż w symulacji")


def test_dlugosc_jeden_to_zero_ruchu_a_poza_panelem_None():
    panel = _panel(10)
    assert zwrot_losowej(panel, 3, 1, 0.001) == pytest.approx(-0.001)
    assert zwrot_losowej(panel, 8, 3, 0.0) is None
