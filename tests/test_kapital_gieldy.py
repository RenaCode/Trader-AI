"""Punkt krzywej kapitalu z ksiag gieldy - ta sama miara co krzywa krypto."""
from datetime import datetime, timezone
from decimal import Decimal as D

from services.engine.kapital_gieldy import zapisz_punkt
from tests.atrapa_bazy import Kursor

TERAZ = datetime(2026, 9, 22, 14, 30, 17, tzinfo=timezone.utc)


def test_punkt_z_konta_liczy_nav_i_realized_TA_SAMA_regula_co_krypto():
    # przeplywy: brak wierszy cash_flow -> start 100 000 jak seed_equity dla demo
    cur = Kursor([{"wplaty": 0, "units": 0}])
    w = zapisz_punkt(cur, "equity", "demo",
                     {"equity": D("100250.5"), "cash": D("60000"), "ekspozycja": D("40250.5")},
                     unrealized=D("250.5"), as_of=TERAZ)
    assert w["nav"] == D("100250.5") / D(100000)
    assert w["realized"] == D("0"), "equity - wplaty - unrealized = 0 przy samym niezrealizowanym"
    sql, params = next((s, p) for s, p in cur.sql if "INSERT INTO exec.equity_point" in s)
    assert params[2] == TERAZ.replace(second=0, microsecond=0), "siatka minutowa jak w silniku"
    assert params[3] == D("100250.5") and params[7] == D("40250.5")
    assert "ON CONFLICT (engine, mode, event_time) DO NOTHING" in sql


def test_tryb_live_bez_przeplywow_NIE_zgaduje_kapitalu():
    import pytest
    cur = Kursor([{"wplaty": 0, "units": 0}])
    with pytest.raises(RuntimeError):
        zapisz_punkt(cur, "equity", "live", {"equity": D(1), "cash": D(1), "ekspozycja": D(0)},
                     unrealized=D(0), as_of=TERAZ)
