"""Odniesienia B0-B3 (`services/engine/baseline.py`, audyt Fable T5 / R3).

B2 to odniesienie, ktore rozstrzyga STOP/GO 1: ensemble musi je pobic przy tej
samej ekspozycji w >= 60% okien. Odniesienie, ktore wchodzi w spadku albo
siedzi w gotowce w trendzie, przesuwaloby te poprzeczke bez slowa.
"""
from datetime import datetime, timedelta, timezone

import pytest

from services.engine import baseline as b
from services.engine.risk_rules import START_EQUITY

T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
BAR = 240


def _panel(dni: int, dzienny: float, start: float = 100.0) -> list[dict]:
    """Swiece 4h, cena zmienia sie geometrycznie o `dzienny` na dobe."""
    krok = (1 + dzienny) ** (1 / 6)
    return [{"t": T0 + timedelta(hours=4 * i), "price": start * krok ** i}
            for i in range(dni * 6)]


def _okno(historia, dni_okna=60):
    koniec = max(r["t"] for r in historia["BTCUSDC"]) + timedelta(hours=4)
    return koniec - timedelta(days=dni_okna), koniec


def test_b2_w_trendzie_wchodzi_w_najsilniejsze():
    historia = {"BTCUSDC": _panel(200, 0.004), "ETHUSDC": _panel(200, 0.006),
                "SOLUSDC": _panel(200, 0.008), "ADAUSDC": _panel(200, 0.002),
                "XRPUSDC": _panel(200, -0.001)}
    od, do = _okno(historia)
    odn = b.b2_momentum(historia, od, do, BAR, "spot_eu", 0.0)
    assert odn.rund > 0
    assert odn.ekspozycja > 0.5          # top-3 po 1/3 kapitalu, prawie cale okno
    assert odn.zwrot > 0


def test_b2_w_spadku_siedzi_w_gotowce():
    """BTC pod EMA 50 d: filtr trendu trzyma B2 w gotowce, niezaleznie od tego,
    ze cos w uniwersum rosnie."""
    historia = {"BTCUSDC": _panel(200, -0.004), "ETHUSDC": _panel(200, -0.003),
                "SOLUSDC": _panel(200, 0.004)}
    od, do = _okno(historia)
    odn = b.b2_momentum(historia, od, do, BAR, "spot_eu", 0.0)
    assert odn.ekspozycja == 0.0
    assert odn.zwrot == 0.0
    assert odn.rund == 0


def test_b2_bez_btc_nie_ma_porownania():
    assert b.b2_momentum({"ETHUSDC": _panel(200, 0.004)}, T0, T0 + timedelta(days=60),
                         BAR, "spot_eu", 0.0) is None


def test_b2_placi_koszty_rynkowe():
    """Ten sam rynek, wyzsza doplata -> nizszy zwrot o doplate na runde."""
    historia = {"BTCUSDC": _panel(200, 0.004), "ETHUSDC": _panel(200, 0.006),
                "SOLUSDC": _panel(200, 0.008)}
    od, do = _okno(historia)
    tanio = b.b2_momentum(historia, od, do, BAR, "spot_eu", 0.0)
    drogo = b.b2_momentum(historia, od, do, BAR, "spot_eu", 0.01)
    assert drogo.rund == tanio.rund
    assert drogo.zwrot < tanio.zwrot


def test_b3_trend_btc_eth_stala_czesc_kapitalu():
    historia = {"BTCUSDC": _panel(200, 0.004), "ETHUSDC": _panel(200, -0.004)}
    od, do = _okno(historia)
    odn = b.b3_trend(historia, od, do, BAR, "spot_eu", 0.0)
    # Tylko BTC nad EMA: jedna noga po 5% kapitalu przez prawie cale okno.
    assert 0.04 < odn.ekspozycja <= b.PARAMETRY_B3["udzial_na_instrument"]
    assert odn.zwrot > 0


def test_b0_gotowka_4_procent_w_skali_roku():
    odn = b.b0_gotowka(T0, T0 + timedelta(days=365), 0.04)
    assert odn.zwrot == pytest.approx(4.0)


def test_bije_przy_tej_samej_ekspozycji():
    odn = b.Odniesienie("B2", zwrot=10.0, ekspozycja=0.5)   # 20% na jednostke
    assert b.bije(2.5, 0.1, odn) is True      # 25% na jednostke
    assert b.bije(1.5, 0.1, odn) is False     # 15% na jednostke
    assert b.bije(1.0, 0.0, odn) is None      # strategia bez ekspozycji
    assert b.bije(1.0, 0.1, None) is None     # brak odniesienia


def test_odniesienie_w_gotowce_jest_pobite_kazdym_dodatnim_zwrotem():
    gotowka = b.Odniesienie("B2", zwrot=0.0, ekspozycja=0.0)
    assert b.bije(0.01, 0.1, gotowka) is True
    assert b.bije(-0.01, 0.1, gotowka) is False


def test_sygnal_b2_nie_zaglada_w_przyszlosc():
    """Zmiana cen PO oknie nie zmienia wyniku B2 w oknie."""
    historia = {"BTCUSDC": _panel(200, 0.004), "ETHUSDC": _panel(200, 0.006),
                "SOLUSDC": _panel(200, 0.008), "ADAUSDC": _panel(200, 0.002)}
    od, do = _okno(historia, 60)
    od, do = od - timedelta(days=20), do - timedelta(days=20)
    przed = b.b2_momentum(historia, od, do, BAR, "spot_eu", 0.0)
    zmieniona = {s: [r | {"price": r["price"] * (3.0 if r["t"] >= do else 1.0)}
                     for r in rows] for s, rows in historia.items()}
    po = b.b2_momentum(zmieniona, od, do, BAR, "spot_eu", 0.0)
    assert po == przed


def test_parametry_b2_zgodne_z_rejestrem_hipotez():
    """Parametry B2 zapisane w docs/hipotezy.md (H-B2) przed pierwszym
    przebiegiem - zmiana w kodzie bez wpisu to strojenie odniesienia."""
    from pathlib import Path

    rejestr = (Path(__file__).resolve().parents[1] / "docs" / "hipotezy.md").read_text(
        encoding="utf-8")
    assert "T−30 d do T−1 d" in rejestr and b.PARAMETRY_B2["okno_momentum_dni"] == 30
    assert "top 3" in rejestr and b.PARAMETRY_B2["top"] == 3
    assert "co 7 dni" in rejestr and b.PARAMETRY_B2["rebalans_dni"] == 7
    assert "EMA 50 dni" in rejestr and b.PARAMETRY_B2["ema_filtra_dni"] == 50
    assert START_EQUITY > 0
