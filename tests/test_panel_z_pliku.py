"""Harness offline: `train(..., panel=plik)` bez bazy (audyt Fable T3 / F-6.2).

ROWNOWAZNOSC Z BAZA NA PRAWDZIWYCH DANYCH sprawdzona 2026-10-03 na kopii
produkcji (`trader_prod_kopia`, dump 20261003T043403Z): `train.py --sucho
--days 1400 --kind spot_eu --limit` z bazy i z pliku `eksport_paneli.py
--do <ta sama chwila>` daly identyczna linie PODSUMOWANIE_JSON i identyczny
wydruk poza jedna linia logu (wykluczenia spreadem z 7 dni - tylko z bazy).
Tutaj: format pliku, odmowy i przebieg z pliku na danych syntetycznych - bez
zadnego polaczenia (`conn=None`; kazde uzycie bazy wywrociloby test).
"""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from services.engine import panel_plik
from services.engine import train as tr
from services.engine.horizons import get as get_horizon
from services.engine.portfolio import Egzekucja

H4 = get_horizon("4h")
KONIEC = datetime(2026, 10, 3, 4, 34, 3, tzinfo=timezone.utc)
DNI = 300
# `train.MIN_INSTRUMENTS` - mniej instrumentow i okno odpada w calosci.
SYMBOLE = ("BTCUSDC", "ETHUSDC", "SOLUSDC", "ADAUSDC", "XRPUSDC", "DOGEUSDC",
           "LINKUSDC", "SUIUSDC")


def _panel_i_swiece(seed: int):
    rng = random.Random(seed)
    start = (KONIEC - timedelta(days=DNI)).replace(minute=0, second=0, microsecond=0)
    start -= timedelta(hours=start.hour % 4)
    swiece, p = [], 100.0
    for k in range(DNI * 24):
        otw, p = p, p * math.exp(rng.gauss(0.0001, 0.006))
        swiece.append({"t": start + timedelta(hours=k), "open": otw, "close": p,
                       "low": min(otw, p) * (1 - abs(rng.gauss(0, 0.002))),
                       "high": max(otw, p) * (1 + abs(rng.gauss(0, 0.002)))})
    panel = []
    for i in range(0, len(swiece) - 4, 4):
        g = swiece[i:i + 4]
        zmiana = g[-1]["close"] / swiece[max(0, i - 24)]["close"] - 1
        panel.append({
            "t": g[0]["t"], "price": g[-1]["close"],
            "low": min(s["low"] for s in g), "high": max(s["high"] for s in g),
            "rewizje": 0,
            "f": {"atr_pct": 1.0 + rng.random(), "ema_ratio": 1 + zmiana / 4,
                  "rsi": 50 + 200 * zmiana, "range_pos": rng.random(),
                  "ret_short": 100 * zmiana, "ret_mid": 100 * zmiana * 2,
                  "ret_long": 100 * zmiana * 3, "volume_z": rng.gauss(0, 1),
                  "news_sent": 0.0, "funding_rate": 0.0, "funding_z": 0.0,
                  "rel_strength": rng.random(), "book_spread_bps": 2.0}})
    return panel, swiece


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
        "dsr": {"konfiguracje": [], "trials": 0, "odchylenie": [None, 0]},
    }


def _przez_plik(tmp_path, dane: dict) -> dict:
    sciezka = tmp_path / "panel.json.gz"
    panel_plik.zapisz(sciezka, dane)
    return panel_plik.wczytaj(sciezka)


def test_plik_oddaje_panel_co_do_wartosci_i_strefy_czasu(tmp_path):
    dane = _plik()
    wczytane = _przez_plik(tmp_path, json.loads(json.dumps(dane, default=str)) | {
        "panele": dane["panele"], "swiece_1h": dane["swiece_1h"],
        "od": dane["od"], "do": dane["do"], "ostatnia_swieca": dane["ostatnia_swieca"]})
    assert wczytane["panele"] == dane["panele"]
    assert wczytane["swiece_1h"] == dane["swiece_1h"]
    assert wczytane["do"] == KONIEC and wczytane["do"].tzinfo is not None
    assert wczytane["spread"]["per_instrument"] == ((1, 1.0, 100), (2, 3.0, 100))


def test_plik_w_innej_wersji_formatu_odmawia(tmp_path):
    sciezka = tmp_path / "p.json.gz"
    panel_plik.zapisz(sciezka, {"panele": {}})
    import gzip
    with gzip.open(sciezka, "rt") as f:
        d = json.load(f)
    d["wersja"] = 999
    with gzip.open(sciezka, "wt") as f:
        json.dump(d, f)
    with pytest.raises(ValueError, match="wersji"):
        panel_plik.wczytaj(sciezka)


def test_przebieg_z_pliku_jest_zawsze_suchy(tmp_path):
    with pytest.raises(ValueError, match="suchy"):
        tr.train(None, "crypto", DNI, 2, H4, "spot_eu", sucho=False,
                 panel=_przez_plik(tmp_path, _plik()))


def test_plik_innego_toru_albo_za_krotki_odmawia(tmp_path):
    plik = _przez_plik(tmp_path, _plik())
    with pytest.raises(SystemExit, match="1h"):
        tr.train(None, "crypto", DNI, 2, get_horizon("1h"), "spot_eu", sucho=True, panel=plik)
    with pytest.raises(SystemExit, match="poza plik"):
        tr.train(None, "crypto", DNI + 1, 2, H4, "spot_eu", sucho=True, panel=plik)


def test_n_selekcji_z_pliku_liczy_ten_przebieg_raz():
    p = {"bar": "4h", "kind": "spot_eu"}
    assert tr.n_konfiguracji_z_listy([], p) == 1
    assert tr.n_konfiguracji_z_listy([panel_plik.kanoniczne_params(p)], p) == 1
    assert tr.n_konfiguracji_z_listy(['{"bar":"1h"}'], p) == 2


@pytest.mark.parametrize("egz", [
    Egzekucja(limit=True),
    Egzekucja(limit=True, opoznienie_min=20, pesymistyczna=True),
], ids=["dzis", "pesymistyczna"])
def test_przebieg_z_pliku_bez_bazy_jest_powtarzalny(tmp_path, capsys, egz):
    plik = _przez_plik(tmp_path, _plik())
    raz = tr.train(None, "crypto", DNI, 2, H4, "spot_eu", egz, sucho=True,
                   wybor="ic", panel=plik)
    dwa = tr.train(None, "crypto", DNI, 2, H4, "spot_eu", egz, sucho=True,
                   wybor="ic", panel=plik)
    assert raz == dwa
    # Symulacja OOS zlozyla zlecenia (dane syntetyczne nie musza dac okien
    # informatywnych - test dotyczy sciezki bez bazy, nie wyniku).
    assert raz["wypelnialnosc"] is not None
    assert raz["pesymistyczna"] is egz.pesymistyczna
    if egz.pesymistyczna:
        assert raz["wypelnialnosc"] is not None and raz["wypelnialnosc"] < 1.0
    assert "PODSUMOWANIE_JSON" in capsys.readouterr().out
