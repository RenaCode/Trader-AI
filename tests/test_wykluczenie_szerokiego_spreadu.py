"""Instrument o średnim spreadzie z 7 dni > 15 pb nie dostaje NOWYCH wejść.

CO BYŁO ZŁE (audyt Fable F-1.5, 2026-10-03)

Własna księga, 7 dni: NEARUSDC 21,2 pb średnio, DOGE 7,6, reszta ≤ 6 pb.
NEAR: 4 rundy giełdowe, −218 USD = 59% straty giełdowej. Spread płacony na
rundę był większy niż cała zmierzona przewaga strategii.

CO ZNACZY „WYPADA Z UNIWERSUM DECYZYJNEGO" — I CZEGO NIE ZNACZY

* Nowe wejście odpada: decyzja `hold` z powodem `szeroki_spread`.
* WYJŚCIE IDZIE DALEJ. Pozycja otwarta przed wykluczeniem musi mieć jak wyjść;
  blokada wyjścia trzymałaby ryzyko, którego reguła chce się pozbyć.
* Odcisk uniwersum (`uniwersum.odcisk`) i przekrój `rel_strength` ZOSTAJĄ na
  pełnym zbiorze. Zmiana zbioru zmieniłaby nazwę cechy przekrojowej, a sygnał
  zniknąłby bez śladu do czasu backfillu (docstring `uniwersum.py`).
* Brak pomiaru to NIE wykluczenie: instrument z garstką świec arkusza zostaje.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.engine.uniwersum import (
    MIN_SWIEC_SPREADU, OKNO_SPREADU_DNI, PROG_SPREADU_PB, opis_wykluczen,
    za_szeroki_spread)

KORZEN = Path(__file__).resolve().parents[1]


def test_prog_i_okno_z_audytu():
    assert PROG_SPREADU_PB == 15.0
    assert OKNO_SPREADU_DNI == 7


def test_near_wypada_doge_zostaje():
    srednie = {1: (21.15, 41), 2: (7.63, 41), 3: (0.73, 41)}
    assert za_szeroki_spread(srednie) == {1: 21.15}


def test_brak_pomiaru_to_nie_wykluczenie():
    """Garstka świec mówi o chwili pomiaru, nie o rynku."""
    assert za_szeroki_spread({1: (40.0, MIN_SWIEC_SPREADU - 1)}) == {}


def test_prog_ostro_wiekszy():
    assert za_szeroki_spread({1: (PROG_SPREADU_PB, 41)}) == {}


def test_opis_podaje_symbol_i_spread():
    linie = opis_wykluczen({1: 21.15}, {1: "NEARUSDC"})
    assert any("NEARUSDC" in w and "21.15" in w for w in linie)
    assert opis_wykluczen({}, {}) == []


def test_trener_wypisuje_wykluczone():
    zrodlo = (KORZEN / "services/engine/train.py").read_text(encoding="utf-8")
    assert "opis_wykluczen(" in zrodlo


def test_migracja_dopuszcza_powod():
    sql = (KORZEN / "db/migrations/0075_powod_holdu_szeroki_spread.sql").read_text(
        encoding="utf-8")
    assert "'szeroki_spread'" in sql
    assert "lock_timeout" in sql


DB = os.environ.get("TEST_DB_URL")


@pytest.mark.skipif(not DB, reason="ustaw TEST_DB_URL na jednorazowa baze")
def test_srednie_spready_z_bazy():
    import psycopg
    from psycopg.rows import dict_row

    from services.engine.uniwersum import srednie_spready

    teraz = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    nazwa = f"book_spread_bps__4h_test_{uuid.uuid4().hex[:6]}"
    with psycopg.connect(DB, autocommit=True, row_factory=dict_row) as c:
        venue = c.execute("SELECT id FROM ref.venue WHERE engine = 'crypto'"
                          " ORDER BY id LIMIT 1").fetchone()["id"]
        ids = [c.execute(
            "INSERT INTO ref.instrument (venue_id, symbol, engine, quote_ccy, kind)"
            " VALUES (%s, %s, 'crypto', 'USDC', 'spot') RETURNING id",
            (venue, f"T{uuid.uuid4().hex[:6].upper()}USDC")).fetchone()["id"]
            for _ in range(2)]
        fd = c.execute(
            "INSERT INTO feat.feature_def (name, params, code_ref)"
            " VALUES (%s, '{\"role\": \"book_spread_bps\", \"bar\": \"4h\"}', 'test')"
            " RETURNING id", (nazwa,)).fetchone()["id"]
        try:
            for k in range(30):
                t = teraz - timedelta(hours=4 * (k + 1))
                c.execute("INSERT INTO feat.feature_value (feature_def_id,"
                          " instrument_id, event_time, value) VALUES"
                          " (%s,%s,%s,21.0), (%s,%s,%s,3.0)",
                          (fd, ids[0], t, fd, ids[1], t))
            # starsze niż okno: nie wchodzą do średniej
            c.execute("INSERT INTO feat.feature_value (feature_def_id,"
                      " instrument_id, event_time, value) VALUES (%s,%s,%s,900.0)",
                      (fd, ids[1], teraz - timedelta(days=OKNO_SPREADU_DNI + 1)))
            with c.cursor() as cur:
                # zegar teraźniejszy, nie pełna godzina: `ingested_at <= do`
                # odcina wiersze zapisane po chwili pomiaru (punkt w czasie)
                wynik = srednie_spready(cur, ids, "4h", datetime.now(timezone.utc))
            assert wynik[ids[0]] == (pytest.approx(21.0), 30)
            assert wynik[ids[1]] == (pytest.approx(3.0), 30)
            assert set(za_szeroki_spread(wynik)) == {ids[0]}
        finally:
            c.execute("DELETE FROM feat.feature_value WHERE feature_def_id = %s", (fd,))
            c.execute("DELETE FROM feat.feature_def WHERE id = %s", (fd,))
            c.execute("DELETE FROM ref.instrument WHERE id = ANY(%s)", (ids,))
