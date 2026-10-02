"""Ostatnia ZAMKNIĘTA świeca dostaje cechy od razu. Do 02.10.2026 nie dostawała.

CO BYŁO ZŁE

`compute_prices` pomijało ostatnią świecę panelu bezwarunkowo, z komentarzem
„jest jeszcze otwarta". To przestało być prawdą 06.09.2026, kiedy `backfill.py`
zaczął zapisywać wyłącznie świece domknięte (`event_time + step <= now`).
Od tego dnia pomijana była świeca ZAMKNIĘTA, więc cecha świecy B powstawała
dopiero wtedy, gdy w bazie była świeca po niej:

  * krypto 4h (agregat z 1h): kubeł B liczył się dopiero, gdy kubeł B+4h dostał
    pierwszą świecę 1h — decyzja ~80–95 min po zamknięciu zamiast 20–35,
    a limit ważny do końca świecy T+1 wisiał krócej, niż zakłada symulacja;
  * equity 1h: cecha ostatniej świecy sesji dopiero przy następnym notowaniu.

CZEGO NIE WOLNO ZEPSUĆ PRZY NAPRAWIE

Kubeł 4h, który ma dopiero JEDNĄ świecę 1h z czterech, nadal nie może dostać
cech — z kawałka świecy, zapisanego z `ingested_at` końca świecy, pętla
zdecydowałaby, zanim następny przebieg wiersz poprawi.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.engine.horizons import H1H, H4H
from services.ingest import features

IDS = {rola: i for i, rola in enumerate(features.PRICE_ROLES, start=1)}
START = datetime(2026, 8, 1, tzinfo=timezone.utc)


def swiece_1h(teraz: datetime) -> list[datetime]:
    """Tylko świece zamknięte — tak, jak zapisuje je backfill."""
    out, t = [], START
    while t + timedelta(hours=1) <= teraz:
        out.append(t)
        t += timedelta(hours=1)
    return out


def bar(t: datetime, i: int, **extra) -> dict:
    return {"t": t, "open": 100.0 + i % 7, "high": 102.0 + i % 5,
            "low": 98.0 - i % 3, "close": 100.0 + i % 11,
            "volume": 10.0 + i % 13, **extra}


def panel_1h(teraz):
    return [bar(t, i) for i, t in enumerate(swiece_1h(teraz))]


def panel_4h(teraz):
    """Agregat tak, jak oddaje go `load_bars`: z liczbą świec źródłowych `n`."""
    kubly: dict[datetime, int] = {}
    for t in swiece_1h(teraz):
        k = t.replace(hour=t.hour // 4 * 4)
        kubly[k] = kubly.get(k, 0) + 1
    return [bar(k, i, n=n) for i, (k, n) in enumerate(sorted(kubly.items()))]


def najnowsza_cecha(monkeypatch, h, panel, teraz) -> datetime:
    zapis: list[tuple] = []
    monkeypatch.setattr(features, "load_bars", lambda *a, **k: panel)
    monkeypatch.setattr(features, "insert_values",
                        lambda cur, rows: zapis.extend(rows) or len(rows))
    features.compute_prices(None, 1, h, IDS, teraz - timedelta(days=1), teraz)
    assert zapis, "nie policzono żadnej cechy — test nie mierzy niczego"
    return max(r[2] for r in zapis)


def test_zamknieta_swieca_1h_dostaje_cechy_w_tym_samym_cyklu(monkeypatch):
    teraz = datetime(2026, 10, 1, 12, 20, tzinfo=timezone.utc)
    panel = panel_1h(teraz)
    assert panel[-1]["t"] == datetime(2026, 10, 1, 11, tzinfo=timezone.utc)
    assert najnowsza_cecha(monkeypatch, H1H, panel, teraz) == panel[-1]["t"], (
        "zamknięta świeca 11:00 nie dostała cech o 12:20 — czeka na świecę po niej")


def test_kompletny_kubel_4h_dostaje_cechy_zaraz_po_zamknieciu(monkeypatch):
    teraz = datetime(2026, 10, 1, 12, 20, tzinfo=timezone.utc)
    panel = panel_4h(teraz)
    assert panel[-1]["t"].hour == 8 and panel[-1]["n"] == 4
    assert najnowsza_cecha(monkeypatch, H4H, panel, teraz) == panel[-1]["t"], (
        "kubeł 08:00 ma komplet czterech świec 1h, a cechy dostaje dopiero "
        "po pojawieniu się kubła 12:00 — decyzja spóźnia się o godzinę")


def test_NIEKOMPLETNY_kubel_4h_nie_dostaje_cech(monkeypatch):
    """Druga strona: kubeł 12:00 o 13:20 to jedna świeca 1h z czterech."""
    teraz = datetime(2026, 10, 1, 13, 20, tzinfo=timezone.utc)
    panel = panel_4h(teraz)
    assert panel[-1]["t"].hour == 12 and panel[-1]["n"] == 1
    assert najnowsza_cecha(monkeypatch, H4H, panel, teraz) == panel[-2]["t"]


def test_kubel_z_kompletem_ale_przed_koncem_okna_nie_liczy_sie(monkeypatch):
    """Zegar rozstrzyga niezależnie od `n` — świeca, której koniec wypada po
    `until`, nie jest zamknięta, ile by źródeł nie miała."""
    teraz = datetime(2026, 10, 1, 11, 59, tzinfo=timezone.utc)
    panel = panel_4h(teraz)
    panel[-1]["n"] = 4
    assert najnowsza_cecha(monkeypatch, H4H, panel, teraz) == panel[-2]["t"]


def test_swieca_wciaz_otwarta_1h_nie_liczy_sie(monkeypatch):
    teraz = datetime(2026, 10, 1, 12, 20, tzinfo=timezone.utc)
    panel = panel_1h(teraz) + [bar(datetime(2026, 10, 1, 12, tzinfo=timezone.utc), 0)]
    assert najnowsza_cecha(monkeypatch, H1H, panel, teraz) == panel[-2]["t"]


def test_agregat_bez_liczby_zrodel_jest_pomijany_jak_dotad(monkeypatch):
    """Brak `n` to „nie wiadomo" — zachowanie sprzed poprawki, nie zgadywanie."""
    teraz = datetime(2026, 10, 1, 12, 20, tzinfo=timezone.utc)
    panel = [{k: v for k, v in b.items() if k != "n"} for b in panel_4h(teraz)]
    assert najnowsza_cecha(monkeypatch, H4H, panel, teraz) == panel[-2]["t"]


class _Kursor:
    def __init__(self):
        self.sql = ""

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchall(self):
        return [{"t": START, "open": 1, "high": 1, "low": 1, "close": 1,
                 "volume": 1, "n": 3}]


@pytest.mark.parametrize("h,ma_n", [(H4H, True), (H1H, False)])
def test_load_bars_oddaje_liczbe_swiec_zrodlowych_przy_agregacji(h, ma_n):
    k = _Kursor()
    wynik = features.load_bars(k, 1, h, START, START + timedelta(days=1))
    if ma_n:
        assert "count(*) AS n" in k.sql
        assert wynik[0]["n"] == 3
    else:
        assert "count(*)" not in k.sql
