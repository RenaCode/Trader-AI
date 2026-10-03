"""Refaktor `train.train` na czyste funkcje nie zmienia wyniku (audyt Fable T2).

ZRZUT REFERENCYJNY (`tests/fixtures/trener_rownowaznosc.json`) zostal
wygenerowany PRZED refaktorem, na kodzie 4358d81, z deterministycznego panelu
syntetycznego ponizej (stale ziarno, zero polaczen z baza - `train(None, ...,
panel=plik)`). Test porownuje z nim CALY wydruk trenera i slownik
`PODSUMOWANIE_JSON` dla kilku wariantow, ktore przechodza przez rozne galezie
`train()`: wybor `ic`, `argmax` (losowanie z ziarnem), `bazowe`, prog wspolny
i model wypelnien pesymistyczny.

Zmiana wyniku zamierzona (nowa miara, poprawka) oznacza swiadome
przegenerowanie zrzutu:

    .venv/bin/python -B tests/test_trener_rownowaznosc.py --zapisz

i opis w commicie, CO sie zmienilo i dlaczego. Zrzut przegenerowany "zeby
test przeszedl" przestaje cokolwiek chronic.
"""
from __future__ import annotations

import contextlib
import io
import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.engine import panel_plik  # noqa: E402
from services.engine import train as tr  # noqa: E402
from services.engine.horizons import get as get_horizon  # noqa: E402
from services.engine.portfolio import Egzekucja  # noqa: E402

ZRZUT = Path(__file__).parent / "fixtures" / "trener_rownowaznosc.json"
H4 = get_horizon("4h")
KONIEC = datetime(2026, 10, 3, 4, 0, tzinfo=timezone.utc)
DNI = 400
SYMBOLE = ("BTCUSDC", "ETHUSDC", "SOLUSDC", "ADAUSDC", "XRPUSDC", "DOGEUSDC",
           "LINKUSDC", "SUIUSDC")

# nazwa -> argumenty `train()` poza panelem
WARIANTY = {
    "ic": dict(egzekucja=Egzekucja(limit=True), wybor="ic"),
    "argmax": dict(egzekucja=Egzekucja(limit=True), wybor="argmax", trials=3),
    "bazowe": dict(egzekucja=Egzekucja(), wybor="bazowe"),
    "ic_wspolny": dict(egzekucja=Egzekucja(limit=True), wybor="ic",
                       progi_per_rezim=False),
    "ic_pesymistyczna": dict(egzekucja=Egzekucja(limit=True, opoznienie_min=20,
                                                 pesymistyczna=True), wybor="ic"),
}


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


def przebieg(plik: dict, wariant: str) -> dict:
    a = dict(WARIANTY[wariant])
    trials = a.pop("trials", 2)
    wydruk = io.StringIO()
    with contextlib.redirect_stdout(wydruk):
        wynik = tr.train(None, "crypto", DNI, trials, H4, "spot_eu", sucho=True,
                         panel=plik, **a)
    # Przez JSON: krotki -> listy, tak jak w zrzucie.
    return {"json": json.loads(json.dumps(wynik)), "stdout": wydruk.getvalue()}


@pytest.fixture(scope="module")
def plik(tmp_path_factory):
    sciezka = tmp_path_factory.mktemp("rownowaznosc") / "panel.json.gz"
    panel_plik.zapisz(sciezka, plik_syntetyczny())
    return panel_plik.wczytaj(sciezka)


@pytest.fixture(scope="module")
def zrzut():
    return json.loads(ZRZUT.read_text(encoding="utf-8"))


@pytest.mark.parametrize("wariant", sorted(WARIANTY))
def test_wynik_identyczny_z_referencja(plik, zrzut, wariant):
    teraz = przebieg(plik, wariant)
    ref = zrzut[wariant]
    assert teraz["json"] == ref["json"]
    assert teraz["stdout"].splitlines() == ref["stdout"].splitlines()


def test_referencja_ma_okna_i_transakcje(zrzut):
    """Zrzut, ktory nie ma okien, niczego by nie chronil."""
    for wariant, ref in zrzut.items():
        assert ref["json"]["okna"] >= 4, wariant
        assert ref["json"]["transakcji"] >= 100, wariant


if __name__ == "__main__":
    if "--zapisz" not in sys.argv:
        sys.exit("uzycie: tests/test_trener_rownowaznosc.py --zapisz")
    import tempfile
    with tempfile.TemporaryDirectory() as kat:
        sciezka = Path(kat) / "panel.json.gz"
        panel_plik.zapisz(sciezka, plik_syntetyczny())
        p = panel_plik.wczytaj(sciezka)
    ZRZUT.parent.mkdir(exist_ok=True)
    wynik = {w: przebieg(p, w) for w in sorted(WARIANTY)}
    ZRZUT.write_text(json.dumps(wynik, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                     encoding="utf-8")
    for w, r in wynik.items():
        print(w, r["json"]["okna"], r["json"]["transakcji"], r["json"]["status"])
