"""`train.py --sucho` nie zostawia w bazie ani jednego wiersza.

CO BYŁO ZŁE (audyt Fable F-2.4, 2026-10-03)

106 ze 169 wersji toru 4h miało status `training` — sieroty po `--sucho`
i po przebiegach zabitych. `--sucho` zakładało wiersz w `learn.model_version`
i `learn.training_run`, zatwierdzało go od razu, a na końcu oznaczało tylko
przebieg jako „tryb suchy" — wersja zostawała w `training` na zawsze, razem
z `drift_metric` i `calibration_bin` przypiętymi do niej. `n_konfiguracji` je
filtruje, ale rejestr modeli był przez to nieczytelny.

Teraz przebieg suchy liczy w jednej transakcji, która na końcu jest WYCOFANA:
zapisy potrzebne w trakcie (własna wersja w liczniku N) istnieją dla tego
połączenia, a po przebiegu nie zostaje nic.
"""
from __future__ import annotations

import os
import uuid

import pytest

DB = os.environ.get("TEST_DB_URL")


@pytest.mark.skipif(not DB, reason="ustaw TEST_DB_URL na jednorazowa baze")
def test_sucho_nie_zostawia_wersji_ani_przebiegu():
    import psycopg
    from psycopg.rows import dict_row

    from services.engine import train as tr
    from services.engine.horizons import get as get_horizon

    symbol = f"T{uuid.uuid4().hex[:6].upper()}USDC"
    with psycopg.connect(DB, autocommit=True) as c:
        venue = c.execute("SELECT id FROM ref.venue WHERE engine = 'crypto'"
                          " ORDER BY id LIMIT 1").fetchone()[0]
        iid = c.execute(
            "INSERT INTO ref.instrument (venue_id, symbol, engine, quote_ccy, kind,"
            " tradeable) VALUES (%s, %s, 'crypto', 'USDC', 'spot', true) RETURNING id",
            (venue, symbol)).fetchone()[0]
        przed = c.execute("SELECT count(*) FROM learn.model_version").fetchone()[0]
        przed_run = c.execute("SELECT count(*) FROM learn.training_run").fetchone()[0]
    try:
        with psycopg.connect(DB, row_factory=dict_row) as conn:
            # Pusta historia: przebieg dochodzi do zapisu wersji i pada na
            # braku paneli - tak jak przebieg zabity w połowie.
            with pytest.raises(SystemExit) as e:
                tr.train(conn, "crypto", 30, 1, get_horizon("4h"), "spot_eu",
                         sucho=True, wybor="ic", spread_bps=5.0)
            assert e.value.code == tr.KOD_NIC_NIE_POLICZONO
        with psycopg.connect(DB) as c:
            po = c.execute("SELECT count(*) FROM learn.model_version").fetchone()[0]
            po_run = c.execute("SELECT count(*) FROM learn.training_run").fetchone()[0]
        assert po == przed, "przebieg suchy zostawił wersję modelu w `training`"
        assert po_run == przed_run, "przebieg suchy zostawił wiersz training_run"
    finally:
        with psycopg.connect(DB, autocommit=True) as c:
            c.execute("DELETE FROM ref.instrument WHERE id = %s", (iid,))


def test_sucho_nie_zatwierdza_niczego_w_kodzie():
    """Na kodzie: każde `conn.commit()` w `train()` jest za warunkiem
    przebiegu nie-suchego albo idzie przez `_utrwal`, które przy `--sucho`
    wycofuje transakcję."""
    import inspect

    from services.engine import train as tr

    zrodlo = inspect.getsource(tr.train)
    gole = [w for w in zrodlo.splitlines()
            if w.strip() == "conn.commit()"]
    # zostają: gałąź nie-sucha w `_utrwal` oraz trzy gałęzie `if not sucho:`
    # - po zapisie wersji, w pętli WF i przy zapisie Sharpe OOS
    assert len(gole) == 4, (
        f"{len(gole)} bezwarunkowych `conn.commit()` w train() — przebieg suchy "
        f"zatwierdzi wiersze, których nie powinien zostawić")
