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
    """Na kodzie: każde `conn.commit()` trenera jest za warunkiem przebiegu
    nie-suchego albo idzie przez `_utrwal`, które przy `--sucho` wycofuje
    transakcję. Od refaktoru T2 zapisy są w osobnych funkcjach."""
    import inspect

    from services.engine import train as tr

    def gole(f):
        return [w for w in inspect.getsource(f).splitlines()
                if w.strip() == "conn.commit()"]

    # `train()` samo nie zatwierdza niczego - robią to funkcje zapisu.
    assert gole(tr.train) == []
    # `_utrwal`: gałąź nie-sucha; po zapisie wersji i przy Sharpe OOS: `if not sucho`
    for f in (tr._utrwal, tr._zaloz_wersje, tr._zapisz_oos):
        zrodlo = inspect.getsource(f)
        assert len(gole(f)) == 1, f.__name__
        assert "if not sucho" in zrodlo or "if sucho:" in zrodlo, f.__name__
    # zapis okna jest wołany wyłącznie za `if not sucho:`
    assert len(gole(tr.zapisz_okno)) == 1
    zrodlo_train = inspect.getsource(tr.train)
    assert zrodlo_train.count("zapisz_okno(") == 1
    assert "if not sucho:\n            zapisz_okno(" in zrodlo_train
    # żadna inna funkcja modułu nie zatwierdza transakcji
    reszta = [n for n, f in inspect.getmembers(tr, inspect.isfunction)
              if f.__module__ == tr.__name__ and gole(f)
              and n not in {"_utrwal", "_zaloz_wersje", "_zapisz_oos", "zapisz_okno"}]
    assert reszta == [], f"`conn.commit()` poza funkcjami zapisu: {reszta}"
