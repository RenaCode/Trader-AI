"""Próba kill-switcha nie twierdzi, że potwierdziła wstrzymanie wejść, gdy go nie zmierzyła (audyt 3 T5).

Zdarzenia 16–25 (24.09–03.10): dziesięć prób z rzędu bez zamiaru wejścia
w oknie, więc ogniwo 3 NIEROZSTRZYGNIĘTE. Log mimo to pisał „Od ostatniego
potwierdzenia: 1.0 doby (próg 7)”, bo licznik liczył każdą próbę, która wywołała
zdarzenie. Od migracji 0072 wynik próby siedzi w `risk.killswitch_event.
proba_wynik`, a komunikat podaje osobno ogniwa 1-2 i 4 oraz pełne potwierdzenie.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from tests.test_proba_ogniwo3 import (MOJE_POWIADOMIENIE, ZDARZENIE, _atrapa,
                                      bez_czekania)  # noqa: F401

KORZEN = Path(__file__).resolve().parents[1]


def _zwolnienie(wykonane):
    return [(s, p) for s, p in wykonane
            if "UPDATE risk.killswitch_event SET released_at" in s][-1]


def test_brak_zamiaru_wejscia_zapisuje_wynik_CZESCIOWY_i_mowi_to_wprost(
        bez_czekania, capsys):
    pk = bez_czekania
    fetch_one, fetch_all, execute, wykonane = _atrapa(
        zdarzenie=ZDARZENIE, powiadomienia=MOJE_POWIADOMIENIE, wstrzymane=0)
    assert asyncio.run(pk.uruchom(fetch_all, fetch_one, execute)) == 0
    err = capsys.readouterr().err

    sql, params = _zwolnienie(wykonane)
    assert "proba_wynik" in sql and "czesciowa" in params
    assert "Od ostatniego potwierdzenia" not in err, (
        "stary komunikat sugeruje potwierdzenie całego łańcucha")
    assert "ogniwami 1-2 i 4" in err
    assert "Pełnego potwierdzenia (z ogniwem 3" in err
    assert "NIE DA SIĘ potwierdzić bez zamiaru wejścia" in err


def test_zaliczona_zapisuje_wynik_ZALICZONA(bez_czekania):
    pk = bez_czekania
    fetch_one, fetch_all, execute, wykonane = _atrapa(
        zdarzenie=ZDARZENIE, powiadomienia=MOJE_POWIADOMIENIE, wstrzymane=2)
    assert asyncio.run(pk.uruchom(fetch_all, fetch_one, execute)) == 0
    # Pierwsze zwolnienie - drugie, w `finally`, ma `AND released_at IS NULL`
    # i na zwolnionym wierszu jest w bazie zapisem pustym.
    zwolnienia = [s for s, _ in wykonane
                  if "UPDATE risk.killswitch_event SET released_at" in s]
    assert "proba_wynik = 'zaliczona'" in zwolnienia[0]


def test_brak_powiadomienia_to_NIEDOKONCZONA_a_nie_czesciowa(bez_czekania):
    pk = bez_czekania
    fetch_one, fetch_all, execute, wykonane = _atrapa(
        zdarzenie=ZDARZENIE, powiadomienia=(), wstrzymane=0)
    assert asyncio.run(pk.uruchom(fetch_all, fetch_one, execute)) == 1
    _, params = _zwolnienie(wykonane)
    assert "niedokonczona" in params


def test_pelne_potwierdzenie_podane_z_wiekiem(bez_czekania, capsys):
    pk = bez_czekania
    fetch_one, fetch_all, execute, _ = _atrapa(
        zdarzenie=ZDARZENIE, powiadomienia=MOJE_POWIADOMIENIE, wstrzymane=0,
        dni_pelne=12.5)
    asyncio.run(pk.uruchom(fetch_all, fetch_one, execute))
    assert "12.5 doby temu" in capsys.readouterr().err


def test_niedokonczona_nie_odnawia_licznika_ciszy():
    zrodlo = (KORZEN / "services/engine/proba_killswitcha.py").read_text(encoding="utf-8")
    zwarty = " ".join(zrodlo.split())
    assert "proba_wynik IN ('zaliczona', 'czesciowa')" in zwarty


def test_migracja_daje_kolumne_i_nadanie_roli_proby():
    m = (KORZEN / "db/migrations/0072_wynik_proby_pozarowej.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS proba_wynik" in m
    assert "GRANT UPDATE (proba_wynik) ON risk.killswitch_event TO trader_proba" in m
