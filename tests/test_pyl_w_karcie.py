"""Pył - reszta poniżej kroku ilości po sprzedaży - jako składnik karty kosztu.

Zmierzone 2026-10-02 na 14 pozycjach demo: 0,23-0,37 pb na rundę, około 1,1%
prowizji, a reszta wynosi średnio blisko połowy kroku. Symulacja liczy ilości
ciągłe i tej straty nie zna, więc składnik wchodzi do DOPŁATY spoza symulacji.

Kontrakt pilnowany tutaj:
  * wzór `krok * cena / (2 * notional)`, maksimum po uniwersum,
  * brak kroku albo ceny DOWOLNEGO instrumentu -> NIEZMIERZONE, nie zero,
  * składnik istnieje tylko tam, gdzie prowizja od kupna idzie w walucie
    bazowej (spot Bybita) - nie na kontrakcie i nie na akcjach,
  * opis dopłaty nazywa go z nazwy.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from services.engine import train as trener
from services.engine.horizons import get as get_horizon
from services.engine.koszty import (
    NIEZMIERZONE, ZALOZENIE, karta_rundy, koszty, pyl_rundy,
)
from services.engine.risk_rules import DEFAULT_LIMITS, START_EQUITY, position_budget

H = get_horizon("4h")


def _pyl(karta):
    return next((s for s in karta.skladniki if s.nazwa == "pyl"), None)


# ------------------------------------------------------------------ wzór

def test_wzor_to_polowa_kroku_wzgledem_notionalu():
    # ADAUSDC: krok 0,1 przy cenie 0,25 i notionalu 750 -> 0,0125/750
    assert pyl_rundy({"ADA": 0.1}, {"ADA": 0.25}, 750.0) == pytest.approx(
        0.1 * 0.25 / (2 * 750.0))


def test_agregacja_to_MAKSIMUM_po_uniwersum():
    kroki = {"ADA": 0.1, "SOL": 0.001}
    ceny = {"ADA": 0.25, "SOL": 200.0}
    # SOL: 0,2/1500 > ADA: 0,025/1500
    assert pyl_rundy(kroki, ceny, 750.0) == pytest.approx(0.001 * 200.0 / 1500.0)


@pytest.mark.parametrize("kroki,ceny", [
    ({"ADA": 0.1, "SOL": None}, {"ADA": 0.25, "SOL": 200.0}),
    ({"ADA": 0.1, "SOL": 0.001}, {"ADA": 0.25}),
    ({}, {}),
])
def test_dziura_w_danych_to_NIEZMIERZONE_a_nie_zero(kroki, ceny):
    assert pyl_rundy(kroki, ceny, 750.0) is None


# ------------------------------------------------------------------ karta

def test_karta_spot_ma_pyl_w_DOPLACIE():
    k = karta_rundy("spot", spread_bps=5.85, pyl=3e-5)
    s = _pyl(k)
    assert s is not None and s.w_symulacji is False and s.zrodlo == ZALOZENIE
    assert k.doplata_poza_symulacja == pytest.approx(5.85e-4 + 3e-5)
    assert "pyl" not in k.nieznane


def test_karta_spot_bez_kroku_ma_pyl_NIEZMIERZONY():
    k = karta_rundy("spot_eu", True, spread_bps=5.85)
    s = _pyl(k)
    assert s is not None and s.wartosc is None and s.zrodlo == NIEZMIERZONE
    assert "pyl" in k.nieznane and not k.pelna
    assert k.doplata_poza_symulacja == pytest.approx(5.85e-4)


@pytest.mark.parametrize("kind", ["perp", "stock", "etf"])
def test_bez_prowizji_w_walucie_bazowej_NIE_MA_skladnika(kind):
    assert not koszty(kind).prowizja_w_bazowej
    assert _pyl(karta_rundy(kind, pyl=3e-5)) is None


def test_opis_doplaty_NAZYWA_pyl():
    z = trener.opis_doplaty(karta_rundy("spot", spread_bps=5.85, pyl=3e-5))
    assert "pyl 0.30 pb" in z and "spread 5.85 pb" in z
    bez = trener.opis_doplaty(karta_rundy("spot", spread_bps=5.85))
    assert "pyl (NIEZMIERZONE)" in bez


# ------------------------------------------------------------------ trener

class _Kursor:
    def __init__(self, wiersze):
        self.wiersze, self.sql = wiersze, []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchall(self):
        return self.wiersze


INSTR = [{"id": 1, "symbol": "ADAUSDC"}, {"id": 2, "symbol": "SOLUSDC"}]
OD = datetime(2026, 1, 1, tzinfo=timezone.utc)
DO = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_pyl_toru_bierze_krok_z_ref_instrument_i_budzet_z_reguly_symulacji():
    cur = _Kursor([{"symbol": "ADAUSDC", "lot_size": Decimal("0.1"),
                    "cena_max": Decimal("0.30")},
                   {"symbol": "SOLUSDC", "lot_size": Decimal("0.001"),
                    "cena_max": Decimal("250")}])
    pyl, pyl_odn = trener.pyl_toru(cur, "spot", INSTR, H, OD, DO, None)
    baza = float(position_budget(DEFAULT_LIMITS, 2, Decimal(str(START_EQUITY))))
    notional = baza * 0.5            # udzial_budzetu(0,25)
    assert pyl == pytest.approx(0.001 * 250 / (2 * notional))
    assert pyl_odn == pytest.approx(0.001 * 250 / (2 * START_EQUITY / 2))
    assert "lot_size" in cur.sql[0][0] and cur.sql[0][1][0] == H.source_timeframe


def test_pyl_toru_bez_kroku_jednego_instrumentu_daje_NIEZMIERZONE():
    cur = _Kursor([{"symbol": "ADAUSDC", "lot_size": None, "cena_max": Decimal("0.3")},
                   {"symbol": "SOLUSDC", "lot_size": Decimal("0.001"),
                    "cena_max": Decimal("250")}])
    assert trener.pyl_toru(cur, "spot", INSTR, H, OD, DO, None) == (None, None)


def test_pyl_toru_instrument_bez_wiersza_to_tez_dziura():
    cur = _Kursor([{"symbol": "SOLUSDC", "lot_size": Decimal("0.001"),
                    "cena_max": Decimal("250")}])
    assert trener.pyl_toru(cur, "spot", INSTR, H, OD, DO, None) == (None, None)


def test_pyl_toru_dla_kontraktu_nie_pyta_bazy():
    cur = _Kursor([])
    assert trener.pyl_toru(cur, "perp", INSTR, H, OD, DO, None) == (None, None)
    assert cur.sql == []


def test_wydruk_trenera_nazywa_pyl(monkeypatch):
    from tests.test_miary_po_doplacie_z_karty import _przebieg
    z = _przebieg(monkeypatch, 5.85)
    # Atrapa bazy nie zna kroku ilości - składnik ma to powiedzieć wprost.
    assert "pyl (NIEZMIERZONE)" in z
    assert "pyl: NIE ZMIERZONO" in z
