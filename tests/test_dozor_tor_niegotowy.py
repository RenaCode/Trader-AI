"""Kod trenera 3 (tor niegotowy) trwajacy dluzej niz N dni daje OSTRZEZENIE z rytmem.

Do 2026-10-03 trainer-cron zapisywal kod 3 wylacznie w echu Joba, a dozor
dostawal `--kod-trenera 0`: tor equity stal z 0/20 znacznikow korekty podzialow
i jedynym sladem byl log. Gdyby chart przekazal 3 bez tej czujki, dozor
powiedzialby „trener zakonczyl sie bledem" krytycznie co noc - albo, przy torze
bez ani jednego wiersza w dzienniku, zamilkl na „brak zapisanych przebiegow".
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.engine import dozor
from services.engine.dozor import (KOD_TOR_NIEGOTOWY, TOR_NIEGOTOWY_DNI,
                                   TOR_NIEGOTOWY_PRZYPOMNIENIE_DNI,
                                   ocen_tor_niegotowy)

T = datetime(2026, 10, 3, 3, 0, tzinfo=timezone.utc)
KORZEN = Path(__file__).resolve().parents[1]


def test_kod_jest_tym_samym_co_w_trenerze():
    from services.engine.train import KOD_TOR_NIEGOTOWY as Z_TRENERA
    assert KOD_TOR_NIEGOTOWY == Z_TRENERA == 3


def test_krotko_niegotowy_to_cisza_dlugo_to_ostrzezenie():
    assert ocen_tor_niegotowy(T - timedelta(days=TOR_NIEGOTOWY_DNI - 1), None, T) is None
    p = ocen_tor_niegotowy(T - timedelta(days=TOR_NIEGOTOWY_DNI + 1), None, T)
    assert p and "kod 3" in p


def test_tor_bez_zadnego_przebiegu_ostrzega():
    p = ocen_tor_niegotowy(None, None, T)
    assert p and "ani jednego" in p


def test_tygodniowy_rytm():
    stary = T - timedelta(days=30)
    assert ocen_tor_niegotowy(stary, T - timedelta(days=2), T) is None
    assert ocen_tor_niegotowy(
        stary, T - timedelta(days=TOR_NIEGOTOWY_PRZYPOMNIENIE_DNI, hours=1), T)


def _atrapy(ostatni_run, zgloszenie):
    wyslane = []

    async def fetch_all(sql, params=None):
        if "learning_state_log" in sql:
            return [{"run_at": ostatni_run}]
        if "notify.notification" in sql:
            assert "tor_niegotowy" in sql and params == ("equity", "1h")
            return [{"created_at": zgloszenie}] if zgloszenie else []
        if 'app."user"' in sql:
            return [{"id": 1}]
        raise AssertionError(sql)

    async def notify(uid, code, tytul, tresc, **kw):
        wyslane.append((code, kw))
    return fetch_all, notify, wyslane


def test_sprawdz_wysyla_ostrzezenie_nie_krytyczne():
    fetch_all, notify, wyslane = _atrapy(None, None)
    kod = asyncio.run(dozor.sprawdz_tor_niegotowy(
        fetch_all, notify, engine="equity", bar="1h", teraz=T))
    assert kod == 0
    assert len(wyslane) == 1
    code, kw = wyslane[0]
    assert code == "learning.state_changed"
    assert kw["severity"] == "warning"
    assert kw["data"]["powod"] == "tor_niegotowy" and kw["data"]["bar"] == "1h"


def test_sprawdz_w_tlumieniu_milczy():
    fetch_all, notify, wyslane = _atrapy(None, T - timedelta(days=1))
    asyncio.run(dozor.sprawdz_tor_niegotowy(
        fetch_all, notify, engine="equity", bar="1h", teraz=T))
    assert wyslane == []


def test_main_obsluguje_kod_3_przed_odczytem_dziennika():
    """Tor equity nie ma wierszy - galaz „brak zapisanych przebiegow" nie moze
    byc pierwsza. I kod 3 nie moze trafic do „trener zakonczyl sie bledem"."""
    zrodlo = (KORZEN / "services/engine/dozor.py").read_text(encoding="utf-8")
    main = zrodlo[zrodlo.index("async def main()"):]
    assert main.index("args.kod_trenera == KOD_TOR_NIEGOTOWY") < main.index(
        "await fetch_all(SQL_STAN_NAUKI")
