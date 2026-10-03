"""Pył - reszta poniżej kroku ilości po sprzedaży - jako składnik karty kosztu.

Zmierzone 2026-10-02 na 14 pozycjach demo: 0,23-0,37 pb na rundę, około 1,1%
prowizji, a reszta wynosi średnio blisko połowy kroku. Symulacja liczy ilości
ciągłe i tej straty nie zna, więc składnik wchodzi do DOPŁATY spoza symulacji.

Kontrakt pilnowany tutaj:
  * wzór `krok * cena / (2 * notional)`, ŚREDNIA po uniwersum (do 2026-10-03
    maksimum - zawyżenie zmierzone, patrz `koszty.pyl_rundy`),
  * notional z mediany progów wejścia ostatniej wersji modelu,
  * model nie schodzi poniżej górnej granicy pomiaru demo per instrument,
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
    NIEZMIERZONE, ZALOZENIE, karta_rundy, koszty, pyl_per_instrument, pyl_rundy,
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


def test_agregacja_to_SREDNIA_po_uniwersum():
    kroki = {"ADA": 0.1, "SOL": 0.001}
    ceny = {"ADA": 0.25, "SOL": 200.0}
    # (0,025 + 0,2) / 1500 / 2 - średnia, nie maksimum
    assert pyl_rundy(kroki, ceny, 750.0) == pytest.approx(
        (0.1 * 0.25 + 0.001 * 200.0) / 1500.0 / 2)
    assert pyl_per_instrument(kroki, ceny, 750.0) == pytest.approx(
        {"ADA": 0.025 / 1500.0, "SOL": 0.2 / 1500.0})


# Pomiar demo 2026-10-02 (14 rund, `raw->>'resztka_nierozliczalna'`): najwyższa
# zmierzona strata per instrument w pb rundy. Krok z `ref.instrument`, cena
# maksimum okna 1400 dni (kopia lokalna bazy, 2026-10-02), notional 792 =
# 1 500 x udzial_budzetu(0,264), 0,264 = mediana progów crypto 4h.
DEMO_MAX_PB = {"SOLUSDC": (0.001, 285.74, 0.711), "BTCUSDC": (1e-6, 126101.0, 0.496),
               "NEARUSDC": (0.01, 8.125, 0.337), "ARBUSDC": (0.1, 2.381, 0.114),
               "DOGEUSDC": (0.1, 0.47568, 0.083), "ADAUSDC": (0.1, 1.298, 0.077)}


def test_model_NIE_schodzi_ponizej_gornej_granicy_demo_per_instrument():
    """Ograniczenie zmiany z 2026-10-03: bramka nie może zrobić się łagodniejsza
    bez pomiaru. Liczone dla wartości per instrument ORAZ dla liczby w karcie."""
    kroki = {s: k for s, (k, _, _) in DEMO_MAX_PB.items()}
    ceny = {s: c for s, (_, c, _) in DEMO_MAX_PB.items()}
    per = pyl_per_instrument(kroki, ceny, 792.0)
    for s, (_, _, demo) in DEMO_MAX_PB.items():
        assert per[s] * 1e4 >= demo, (s, per[s] * 1e4, demo)
    assert pyl_rundy(kroki, ceny, 792.0) * 1e4 >= max(d for _, _, d in DEMO_MAX_PB.values())


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
    def __init__(self, wiersze, progi=None):
        self.wiersze, self.sql, self.progi = wiersze, [], progi

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchall(self):
        return self.wiersze

    def fetchone(self):
        return {"progi": self.progi} if self.progi is not None else None


INSTR = [{"id": 1, "symbol": "ADAUSDC"}, {"id": 2, "symbol": "SOLUSDC"}]
OD = datetime(2026, 1, 1, tzinfo=timezone.utc)
DO = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_pyl_toru_bierze_krok_z_ref_instrument_i_budzet_z_reguly_symulacji():
    cur = _Kursor([{"symbol": "ADAUSDC", "lot_size": Decimal("0.1"),
                    "cena_max": Decimal("0.30")},
                   {"symbol": "SOLUSDC", "lot_size": Decimal("0.001"),
                    "cena_max": Decimal("250")}])
    pyl, pyl_odn, linie = trener.pyl_toru(cur, "spot", INSTR, H, OD, DO, None)
    baza = float(position_budget(DEFAULT_LIMITS, 2, Decimal(str(START_EQUITY))))
    notional = baza * 0.5            # udzial_budzetu(0,25) - bez nazwy modelu
    srednia = (0.1 * 0.30 + 0.001 * 250) / 2
    assert pyl == pytest.approx(srednia / (2 * notional))
    assert pyl_odn == pytest.approx(srednia / (2 * START_EQUITY / 2))
    assert "lot_size" in cur.sql[0][0] and cur.sql[0][1][0] == H.source_timeframe
    tekst = "\n".join(linie)
    assert "ADAUSDC" in tekst and "SOLUSDC" in tekst and "średnia" in tekst


def test_notional_z_MEDIANY_progow_ostatniej_wersji():
    cur = _Kursor([{"symbol": "ADAUSDC", "lot_size": Decimal("0.1"),
                    "cena_max": Decimal("0.30")},
                   {"symbol": "SOLUSDC", "lot_size": Decimal("0.001"),
                    "cena_max": Decimal("250")}],
                  progi={"trend_up": 0.2835, "trend_down": 0.2437,
                         "range": 0.3503, "high_vol": 0.2080})
    pyl, _, linie = trener.pyl_toru(cur, "spot", INSTR, H, OD, DO, None,
                                    "crypto_signal_weighting_4h")
    baza = float(position_budget(DEFAULT_LIMITS, 2, Decimal(str(START_EQUITY))))
    notional = baza * min(1.0, 2 * 0.2636)     # mediana = (0,2437+0,2835)/2
    assert pyl == pytest.approx((0.1 * 0.30 + 0.001 * 250) / 2 / (2 * notional))
    assert any("mediana 4 progów" in l for l in linie)
    assert cur.sql[1][1] == ("crypto_signal_weighting_4h",)


def test_pyl_toru_bez_kroku_jednego_instrumentu_daje_NIEZMIERZONE():
    cur = _Kursor([{"symbol": "ADAUSDC", "lot_size": None, "cena_max": Decimal("0.3")},
                   {"symbol": "SOLUSDC", "lot_size": Decimal("0.001"),
                    "cena_max": Decimal("250")}])
    pyl, odn, linie = trener.pyl_toru(cur, "spot", INSTR, H, OD, DO, None)
    assert (pyl, odn) == (None, None)
    assert "NIEZMIERZONY" in linie[0] and "ADAUSDC" in linie[0]


def test_pyl_toru_instrument_bez_wiersza_to_tez_dziura():
    cur = _Kursor([{"symbol": "SOLUSDC", "lot_size": Decimal("0.001"),
                    "cena_max": Decimal("250")}])
    assert trener.pyl_toru(cur, "spot", INSTR, H, OD, DO, None)[:2] == (None, None)


def test_pyl_toru_dla_kontraktu_nie_pyta_bazy():
    cur = _Kursor([])
    assert trener.pyl_toru(cur, "perp", INSTR, H, OD, DO, None) == (None, None, [])
    assert cur.sql == []


def test_wydruk_trenera_nazywa_pyl(monkeypatch):
    from tests.test_miary_po_doplacie_z_karty import _przebieg
    z = _przebieg(monkeypatch, 5.85)
    # Atrapa bazy nie zna kroku ilości - składnik ma to powiedzieć wprost.
    assert "pyl (NIEZMIERZONE)" in z
    assert "pyl: NIE ZMIERZONO" in z


# ------------------------------------------------ savepoint przy odczycie progów

class _Transakcja:
    def __init__(self, polaczenie):
        self.p = polaczenie

    def __enter__(self):
        self.p.zdarzenia.append("savepoint")

    def __exit__(self, typ, wartosc, tb):
        self.p.zdarzenia.append("rollback_do_savepointu" if typ else "release")
        return False


class _Polaczenie:
    def __init__(self):
        self.zdarzenia = []

    def transaction(self):
        return _Transakcja(self)


class _KursorPadajacy:
    def __init__(self):
        self.connection = _Polaczenie()

    def execute(self, sql, params=None):
        raise RuntimeError("relation learn.model_version does not exist")


def test_blad_odczytu_progow_cofa_SAVEPOINT_a_nie_cala_transakcje():
    cur = _KursorPadajacy()
    prog, skad = trener.prog_dla_pylu(cur, "crypto_signal_weighting_4h")
    assert prog == trener.PROG_WEJSCIA_POCZATKOWY and "początkowy" in skad
    assert cur.connection.zdarzenia == ["savepoint", "rollback_do_savepointu"]


import os  # noqa: E402

wymaga_bazy = pytest.mark.skipif(
    not os.environ.get("TEST_DB_URL"),
    reason="ustaw TEST_DB_URL na jednorazową bazę z zastosowanymi migracjami")


@wymaga_bazy
def test_PRAWDZIWA_BAZA_po_bledzie_odczytu_progow_transakcja_trenera_zyje(monkeypatch):
    """Na starym kodzie (`except` bez savepointu) następne zapytanie padało
    z InFailedSqlTransaction - „brak progów" przerywał cały przebieg."""
    import psycopg
    from psycopg.rows import dict_row
    monkeypatch.setattr(trener, "_SQL_PROGI_OSTATNIEJ_WERSJI",
                        "SELECT 1/0 AS progi WHERE %s IS NOT NULL")
    with psycopg.connect(os.environ["TEST_DB_URL"], row_factory=dict_row) as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 AS x")            # transakcja trenera już otwarta
        prog, _ = trener.prog_dla_pylu(cur, "crypto_signal_weighting_4h")
        assert prog == trener.PROG_WEJSCIA_POCZATKOWY
        cur.execute("SELECT 2 AS x")
        assert cur.fetchone()["x"] == 2
        conn.rollback()
