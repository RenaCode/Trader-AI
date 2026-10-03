"""`ema_ratio` zamkniętej świecy nie zależy od tego, skąd zaczął się przebieg (audyt 3 T6).

Do 2026-10-03 rozgrzewka `compute_prices` brała `h.ema_slow`, a bufor EMA to
`4 * ema_slow`. Dla 1h: rozgrzewka 205 świec, bufor 288 — więc przy oknie
przebiegu 24 h każda świeca dostawała EMA z bufora krótszego niż stały, i to
tym krótszego, im dalej `since` przesunęło się za nią. Na produkcji: 748 rewizji
`ema_ratio__1h` na dobę, średnio 12,6 h po świecy, przy niezmienionym `rsi`
i `ret_*` tych samych świec. Silnik decydował na innej liczbie niż ta, na której
uczy się trener.

Atrapa `load_bars` oddaje panel tak jak SQL: `rozgrzewka` świec przed `since`
plus okno — czyli długość historii zależy od `since`, dokładnie jak w bazie.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from services.engine.horizons import H1H, H4H
from services.ingest import features

IDS = {rola: i for i, rola in enumerate(features.PRICE_ROLES, start=1)}
START = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _panel(h, teraz):
    krok = timedelta(minutes=h.bar_minutes)
    out, t, i = [], START, 0
    while t + krok <= teraz:
        c = 100.0 + 10 * math.sin(i / 17.0) + 0.01 * i
        out.append({"t": t, "open": c, "high": c + 1, "low": c - 1, "close": c,
                    "volume": 10.0 + i % 13, "n": h.swiec_zrodlowych_na_swiece})
        t, i = t + krok, i + 1
    return out


def _cechy(monkeypatch, h, teraz, since) -> dict:
    panel = _panel(h, teraz)

    def load_bars(cur, iid, h_, since_, until, *, rozgrzewka=0):
        przed = [b for b in panel if b["t"] < since_][-rozgrzewka:] if rozgrzewka else []
        return przed + [b for b in panel if since_ <= b["t"] < until]

    zapis: list[tuple] = []
    monkeypatch.setattr(features, "load_bars", load_bars)
    monkeypatch.setattr(features, "insert_values",
                        lambda cur, rows: zapis.extend(rows) or len(rows))
    features.compute_prices(None, 1, h, IDS, since, teraz)
    return {(r[0], r[2]): r[3] for r in zapis}


@pytest.mark.parametrize("h", [H1H, H4H], ids=["1h", "4h"])
def test_ta_sama_swieca_z_oknem_1_i_30_dni_ma_to_samo_ema_ratio(monkeypatch, h):
    teraz = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    krotkie = _cechy(monkeypatch, h, teraz, teraz - timedelta(days=1))
    dlugie = _cechy(monkeypatch, h, teraz, teraz - timedelta(days=30))
    fid = IDS["ema_ratio"]
    wspolne = [k for k in krotkie if k[0] == fid and k in dlugie]
    assert wspolne, "brak wspólnych świec - test nie mierzy niczego"
    rozne = [k for k in wspolne if krotkie[k] != dlugie[k]]
    assert not rozne, (
        f"{len(rozne)} z {len(wspolne)} świec {h.bar} ma inne `ema_ratio` zależnie "
        f"od okna przebiegu - wartość zależy od długości wczytanej historii")


def test_przesuniecie_since_o_godzine_nie_rewiduje_starszych_swiec(monkeypatch):
    """Dokładnie to, co robi worker co przebieg."""
    teraz = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    a = _cechy(monkeypatch, H1H, teraz, teraz - timedelta(days=1))
    b = _cechy(monkeypatch, H1H, teraz + timedelta(hours=1),
               teraz + timedelta(hours=1) - timedelta(days=1))
    fid = IDS["ema_ratio"]
    rewizje = [k for k in a if k[0] == fid and k in b and a[k] != b[k]]
    assert not rewizje


def test_rozgrzewka_obejmuje_bufor_ema():
    for h in (H1H, H4H):
        assert features.bufor_ema(h) == h.ema_slow * features.EMA_BUFFER
