"""Deterministyczny panel syntetyczny dla testow trenera (bez bazy).

Uzywany przez `test_trener_rownowaznosc.py` (zrzut referencyjny) i testy siatki
okien / progow. ZMIANA TEGO GENERATORA zmienia zrzut referencyjny - wtedy
przegenerowac go swiadomie (`tests/test_trener_rownowaznosc.py --zapisz`).
"""
from __future__ import annotations

import copy
import functools
import math
import random
from datetime import datetime, timedelta, timezone

KONIEC = datetime(2026, 10, 3, 4, 0, tzinfo=timezone.utc)
DNI = 400
SYMBOLE = ("BTCUSDC", "ETHUSDC", "SOLUSDC", "ADAUSDC", "XRPUSDC", "DOGEUSDC",
           "LINKUSDC", "SUIUSDC")


def _panel_i_swiece(seed: int):
    """Szereg z lekka zaleznoscia zwrotu od cech - zeby okna mialy transakcje
    i rozne rezimy, a nie same zera."""
    rng = random.Random(seed)
    start = KONIEC - timedelta(days=DNI)
    swiece, p = [], 100.0 * (1 + seed)
    for k in range(DNI * 24):
        dryf = 0.0004 * math.sin(k / (24 * 20) + seed)
        otw, p = p, p * math.exp(rng.gauss(dryf, 0.006 + 0.004 * (k // 2000 % 2)))
        swiece.append({"t": start + timedelta(hours=k), "open": otw, "close": p,
                       "low": min(otw, p) * (1 - abs(rng.gauss(0, 0.002))),
                       "high": max(otw, p) * (1 + abs(rng.gauss(0, 0.002)))})
    panel = []
    for i in range(0, len(swiece) - 4, 4):
        g = swiece[i:i + 4]
        zmiana = g[-1]["close"] / swiece[max(0, i - 24)]["close"] - 1
        dlugo = g[-1]["close"] / swiece[max(0, i - 168)]["close"] - 1
        panel.append({
            "t": g[0]["t"], "price": g[-1]["close"],
            "low": min(s["low"] for s in g), "high": max(s["high"] for s in g),
            "rewizje": int(rng.random() < 0.01),
            "f": {"atr_pct": 0.5 + abs(zmiana) * 40 + rng.random() * 0.5,
                  "ema_ratio": 1 + dlugo / 4,
                  "rsi": max(1.0, min(99.0, 50 + 300 * zmiana)),
                  "range_pos": rng.random(),
                  "ret_short": 100 * zmiana, "ret_mid": 100 * dlugo,
                  "ret_long": 100 * dlugo * 1.5, "volume_z": rng.gauss(0, 1),
                  "news_sent": 0.0, "funding_rate": rng.gauss(0, 1e-4),
                  "funding_z": rng.gauss(0, 1),
                  "rel_strength": rng.random(), "book_spread_bps": 2.0}})
    return panel, swiece


def plik_syntetyczny() -> dict:
    """Swieza kopia (wolajacy moze ja zmieniac)."""
    return copy.deepcopy(_plik())


@functools.lru_cache(maxsize=1)
def _plik() -> dict:
    instrumenty = [{"id": n + 1, "symbol": s, "quote_ccy": "USDC"}
                   for n, s in enumerate(SYMBOLE)]
    panele, swiece = {}, {}
    for n, i in enumerate(instrumenty):
        panele[i["symbol"]], swiece[i["symbol"]] = _panel_i_swiece(n)
    return {
        "engine": "crypto", "bar": "4h", "dni": DNI, "od": KONIEC - timedelta(days=DNI),
        "do": KONIEC, "odcisk": "test", "instrumenty": instrumenty, "panele": panele,
        "swiece_1h": swiece,
        "spread": {"bps": 2.0, "nasza_ksiega": True, "n": 200, "instrumentow": 2,
                   "pominietych": 0, "od": KONIEC - timedelta(days=DNI), "do": KONIEC,
                   "rozklad": {"mediana": 2.0, "q1": 1.0, "q3": 3.0, "najwiekszy": 3.0},
                   "per_instrument": [(1, 1.0, 100), (2, 3.0, 100)]},
        "kroki": dict.fromkeys(SYMBOLE, 1e-4),
        "prog_pylu": [0.26, "test"], "ostatnia_swieca": swiece["BTCUSDC"][-1]["t"],
        "przekroj_policzony": True,
        "dsr": {"konfiguracje": ['{"bar":"4h","wybor":"argmax"}'], "trials": 120,
                "odchylenie": [None, 0]},
    }
