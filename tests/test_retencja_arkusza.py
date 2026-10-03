"""Retencja `market.book_snapshot` (audyt Fable U-14 - KOREKTA).

Audyt podał „385 MB bez polityki retencji". Polityka JEST od migracji 0002
(`add_retention_policy(..., 90 days)`) i chodzi na produkcji (job 1005,
`drop_after: 90 days`, ostatni przebieg 03.10 Success) - nic jeszcze nie
skasowała, bo snapshoty zaczynają się 27.08. Cechy `book_*` są liczone ze
snapshotów na bieżąco i zostają w `feat.feature_value` bez retencji
(`features.compute_orderbook`), więc 90 dni surowych danych wystarcza.
Ten test pilnuje, żeby polityka nie zniknęła po cichu.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

KORZEN = Path(__file__).resolve().parents[1]


def test_polityka_retencji_w_migracjach():
    sql = (KORZEN / "db/migrations/0002_market_data.sql").read_text(encoding="utf-8")
    assert "add_retention_policy('market.book_snapshot', interval '90 days')" in sql


@pytest.mark.skipif(not os.environ.get("TEST_DB_URL"), reason="ustaw TEST_DB_URL")
def test_polityka_retencji_w_bazie():
    import psycopg

    with psycopg.connect(os.environ["TEST_DB_URL"]) as c:
        w = c.execute(
            "SELECT config->>'drop_after' FROM timescaledb_information.jobs"
            " WHERE proc_name = 'policy_retention'"
            "   AND hypertable_schema = 'market' AND hypertable_name = 'book_snapshot'"
        ).fetchall()
    assert w == [("90 days",)]
