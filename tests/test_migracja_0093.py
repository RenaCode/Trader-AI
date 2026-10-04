"""Migracja 0093: typy `killswitch.released` i `kapital.przeplyw` (przegląd 2026-10-04).

Oba typy są `always_on` i `critical`, czyli idą przez bramkę na telefon
niezależnie od preferencji. Plik ma własny `BEGIN/COMMIT` z `lock_timeout`
(konwencja od 0070) i musi być idempotentny: migrator zapisuje plik i wpis
w `schema_migrations` w osobnych transakcjach.
"""
from __future__ import annotations

import os

import pytest

from tests.atrapa_bazy import KORZEN

PLIK = KORZEN / "db/migrations/0093_killswitch_released_i_kapital.sql"
TEST_DB_URL = os.environ.get("TEST_DB_URL")


def test_ma_lock_timeout_i_wlasna_transakcje():
    tresc = PLIK.read_text(encoding="utf-8")
    assert "SET LOCAL lock_timeout" in tresc
    assert tresc.strip().startswith("--") and "\nBEGIN;" in tresc
    assert tresc.rstrip().endswith("COMMIT;")
    assert tresc.count("ON CONFLICT (code) DO NOTHING") == 2


@pytest.mark.skipif(not TEST_DB_URL, reason="ustaw TEST_DB_URL na jednorazową bazę")
def test_BAZA_dwukrotne_zastosowanie_daje_ten_sam_stan():
    import psycopg
    from psycopg.rows import dict_row
    tresc = PLIK.read_text(encoding="utf-8")
    with psycopg.connect(TEST_DB_URL, row_factory=dict_row, autocommit=True) as conn:
        conn.execute(tresc)
        conn.execute(tresc)
        wiersze = conn.execute(
            "SELECT code, default_severity::text AS sev, always_on, default_cooldown_s"
            "  FROM notify.event_type"
            " WHERE code IN ('killswitch.released', 'kapital.przeplyw')"
            " ORDER BY code").fetchall()
        opis = conn.execute(
            "SELECT description FROM notify.event_type"
            " WHERE code = 'killswitch.triggered'").fetchone()["description"]
    assert [(w["code"], w["sev"], w["always_on"], w["default_cooldown_s"])
            for w in wiersze] == [("kapital.przeplyw", "critical", True, 0),
                                  ("killswitch.released", "critical", True, 0)]
    assert "risk.limit_def.wznowienie" in opis
