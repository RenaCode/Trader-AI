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
from psycopg import sql

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


# Rola bazodanowa -> zmienna srodowiskowa z jej haslem.
#
# Kazdy proces ma WLASNA role (migracja 0034), bo wspolna rola znaczyla, ze
# silnik moze zwolnic sobie kill-switcha - nie robil tego, bo nie mial takiego
# kodu, a nie dlatego, ze nie mogl. Rozdzial uprawnien jest jednak wart tyle,
# ile rozdzial POSWIADCZEN: jedno haslo do wszystkich rol oznaczaloby, ze proces
# o wezszych nadaniach po prostu laczy sie ta rola, ktora ma szersze.
ROLE_HASLA = {
    "trader_app": "APP_DB_PASSWORD",
    "trader_silnik": "SILNIK_DB_PASSWORD",
    "trader_dozorca": "DOZORCA_DB_PASSWORD",
    "trader_czujka": "CZUJKA_DB_PASSWORD",
    "trader_proba": "PROBA_DB_PASSWORD",
}


def ustaw_hasla_rol(conn) -> None:
    """Ustawia hasla rol procesow ze zmiennych srodowiskowych.

    Robione POZA plikami migracji swiadomie. Haslo w pliku .sql trafiloby do
    repozytorium i do sumy kontrolnej migracji - a wtedy jego rotacja wymagalaby
    edycji zastosowanego juz pliku, czego migrator slusznie zabrania.

    Krok jest idempotentny: przy kazdym uruchomieniu ustawia to, co jest
    w zmiennych srodowiskowych. Dzieki temu rotacja hasla sprowadza sie do
    zmiany sekretu i restartu - bez dotykania migracji.

    Rola, ktorej hasla nie podano, ZOSTAJE BEZ HASLA i jej pod sie nie polaczy.
    To jest zamierzone: cicha praca procesu na cudzych poswiadczeniach byla by
    gorsza niz pod, ktory nie wstal - ten drugi widac w `kubectl get pods`.
    """
    istniejace = {
        r[0] for r in conn.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
            (list(ROLE_HASLA),))
    }
    for rola, zmienna in ROLE_HASLA.items():
        if rola not in istniejace:
            print(f"Rola {rola} jeszcze nie istnieje - pomijam.")
            continue
        haslo = os.environ.get(zmienna)
        if not haslo:
            print(f"{zmienna} nie ustawione - rola {rola} zostaje bez hasla "
                  f"i jej proces sie nie zaloguje.")
            continue
        conn.execute(
            sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(
                sql.Identifier(rola), sql.Literal(haslo)))
        print(f"Haslo roli {rola} ustawione.")


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
            # Hasla ustawiamy TAKZE gdy nie ma nic do zastosowania. Wczesniej ten
            # krok byl za `return 0`, wiec rotacja hasla wymagala, zeby akurat
            # czekala jakas migracja - a bez niej nowy sekret byl w podzie
            # i nie byl w bazie. Roznica objawia sie dopiero przy restarcie,
            # czyli najpozniej jak sie da.
            ustaw_hasla_rol(conn)
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
        ustaw_hasla_rol(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
