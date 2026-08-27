#!/usr/bin/env python3
"""Runner migracji SQL dla Trader-AI.

Celowo bez Alembica: schemat opiera sie mocno na DDL specyficznym dla TimescaleDB
(hypertables, continuous aggregates, polityki kompresji), ktore w Alembicu i tak
laduja w op.execute() jako surowy SQL. Numerowane pliki .sql sa czytelniejsze
i latwiejsze do audytu.

Kazda migracja idzie w jednej transakcji i jest zapisywana w schema_migrations
razem z hashem tresci - probna zmiana juz zastosowanego pliku jest wykrywana.
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version      text        PRIMARY KEY,
    checksum     bytea       NOT NULL,
    applied_at   timestamptz NOT NULL DEFAULT now(),
    duration_ms  integer     NOT NULL
);
"""


def dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")
    return url


def main() -> int:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        sys.exit(f"Brak migracji w {MIGRATIONS_DIR}")

    with psycopg.connect(dsn(), autocommit=True) as conn:
        conn.execute(BOOTSTRAP)
        applied = {
            row[0]: bytes(row[1])
            for row in conn.execute("SELECT version, checksum FROM schema_migrations")
        }

        pending = []
        for path in files:
            version = path.stem
            checksum = hashlib.sha256(path.read_bytes()).digest()
            if version in applied:
                if applied[version] != checksum:
                    sys.exit(
                        f"Migracja {version} zostala zmieniona po zastosowaniu.\n"
                        "Migracje sa niemutowalne - dodaj nowy plik zamiast edytowac stary."
                    )
                continue
            pending.append((version, path, checksum))

        if not pending:
            print(f"Baza aktualna ({len(applied)} migracji).")
            return 0

        for version, path, checksum in pending:
            print(f"-> {version} ...", end=" ", flush=True)
            started = time.monotonic()
            # Jedna migracja = jedna transakcja. Blad cofa calosc.
            with psycopg.connect(dsn()) as tx:
                with tx.cursor() as cur:
                    cur.execute(path.read_text())
                    duration = int((time.monotonic() - started) * 1000)
                    cur.execute(
                        "INSERT INTO schema_migrations (version, checksum, duration_ms)"
                        " VALUES (%s, %s, %s)",
                        (version, checksum, duration),
                    )
                tx.commit()
            print(f"ok ({duration} ms)")

        print(f"Zastosowano {len(pending)} migracji.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
