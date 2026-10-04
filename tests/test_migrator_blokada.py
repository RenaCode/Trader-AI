"""Migrator pod blokadą doradczą i z `lock_timeout` (przegląd 2026-10-04, T-14).

Dwa kontenery `migrate` naraz (dwa szybkie synci, stary pod kasowany po
terminie) czytały tę samą listę oczekujących i stosowały ją oba. Plik bez
własnego `lock_timeout` czekał za odczytem trenera bez końca.
"""
from __future__ import annotations

import os
import sys

import pytest

from db import migrate


class Polaczenie:
    def __init__(self, dziennik, kw):
        self.dziennik, self.kw = dziennik, kw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.dziennik.append(("execute", " ".join(sql.split()), params))
        return []

    def cursor(self):
        return self

    def commit(self):
        self.dziennik.append(("commit", None, None))


def test_blokada_doradcza_wokol_CALEGO_przebiegu_i_lock_timeout_pliku(monkeypatch, tmp_path):
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    dziennik, polaczenia = [], []

    def connect(dsn, **kw):
        polaczenia.append(kw)
        return Polaczenie(dziennik, kw)

    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path)
    monkeypatch.setattr(migrate.psycopg, "connect", connect)
    monkeypatch.setattr(migrate, "ustaw_hasla_rol", lambda conn: None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x@y/z")
    monkeypatch.setattr(sys, "argv", ["migrate.py"])
    assert migrate.main() == 0

    sql = [s for rodzaj, s, _ in dziennik if rodzaj == "execute"]
    blokada = sql.index("SELECT pg_advisory_lock(%s)")
    odczyt = sql.index("SELECT version, checksum FROM schema_migrations")
    plik = sql.index("SELECT 1;")
    zwolnienie = sql.index("SELECT pg_advisory_unlock(%s)")
    assert blokada < odczyt < plik < zwolnienie, "lista oczekujących czytana poza blokadą"
    assert [p for r, s, p in dziennik if s == "SELECT pg_advisory_lock(%s)"] == \
        [(migrate.BLOKADA_MIGRATORA,)]
    assert polaczenia[0] == {"autocommit": True}
    assert polaczenia[1] == {"options": f"-c lock_timeout={migrate.LOCK_TIMEOUT}"}


@pytest.mark.skipif(not os.environ.get("TEST_DB_URL"), reason="ustaw TEST_DB_URL")
def test_BAZA_drugi_przebieg_na_aktualnej_bazie(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DB_URL"])
    monkeypatch.setattr(sys, "argv", ["migrate.py"])
    assert migrate.main() == 0
