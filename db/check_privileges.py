#!/usr/bin/env python3
"""Sprawdza, czy rola aplikacji ma dokladnie te uprawnienia, ktore ma miec.

DLACZEGO SKRYPT, A NIE JEDNORAZOWE SPRAWDZENIE

Migracja 0013 odebrala aplikacji prawa superuzytkownika. Kazda NASTEPNA migracja
moze te ochrone cicho zniesc - wystarczy `GRANT ALL`, `ALTER TABLE ... OWNER TO`
albo nowa tabela z domyslnymi uprawnieniami dobranymi zbyt hojnie. Nic tego nie
zglosi, bo aplikacja nadal bedzie dzialac; straci sie tylko gwarancja.

Skrypt sprawdza OBIE strony: ze rola potrafi to, czego potrzebuje, i ze NIE
potrafi tego, przed czym ma chronic. Sam test pozytywny bylby bezwartosciowy -
przeszedlby takze dla superuzytkownika.

Uzycie:
    APP_DB_URL=postgresql://trader_app:...@host/trader python db/check_privileges.py
"""
from __future__ import annotations

import os
import sys

import psycopg

# (opis, SQL, czy_ma_sie_udac)
KONTROLE = [
    # --- to musi dzialac ---
    ("SELECT z market.candle", "SELECT count(*) FROM market.candle", True),
    ("SELECT z journal.decision", "SELECT count(*) FROM journal.decision", True),
    ("SELECT z learn.model_version", "SELECT count(*) FROM learn.model_version", True),
    ("EXECUTE feat.as_of", "SELECT * FROM feat.as_of(1, now(), NULL, interval '1 day')", True),
    ("INSERT do risk.engine_heartbeat",
     "INSERT INTO risk.engine_heartbeat (engine, mode, last_beat_at, status, detail)"
     " VALUES ('crypto','demo',now(),'running','{}')"
     " ON CONFLICT (engine) DO UPDATE SET last_beat_at = now()", True),
    ("UPDATE learn.learning_state",
     "UPDATE learn.learning_state SET updated_at = now() WHERE engine = 'crypto'", True),
    ("INSERT do news.api_spend",
     "INSERT INTO news.api_spend (model, purpose, cost_usd) VALUES ('test','test',0)", True),
    ("DELETE z app.secret", "DELETE FROM app.secret WHERE name = '__nieistnieje__'", True),
    ("DELETE z notify.channel", "DELETE FROM notify.channel WHERE id = -1", True),

    # --- to NIE MOZE dzialac ---
    ("DELETE z journal.decision", "DELETE FROM journal.decision WHERE id = -1", False),
    ("TRUNCATE journal.decision", "TRUNCATE journal.decision CASCADE", False),
    ("ALTER TABLE ... DISABLE TRIGGER",
     "ALTER TABLE journal.decision DISABLE TRIGGER decision_immutable", False),
    ("DROP TABLE market.candle", "DROP TABLE market.candle", False),
    ("TRUNCATE market.candle", "TRUNCATE market.candle", False),
    ("CREATE TABLE w schemacie app", "CREATE TABLE app.wlam (x int)", False),
    ("DELETE z market.candle", "DELETE FROM market.candle WHERE false", False),
    ("DELETE z feat.feature_value", "DELETE FROM feat.feature_value WHERE false", False),
    ("DELETE z exec.fill", "DELETE FROM exec.fill WHERE false", False),
]


def main() -> int:
    url = os.environ.get("APP_DB_URL") or os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("Ustaw APP_DB_URL (połączenie ROLĄ APLIKACJI, nie właściciela)")

    bledy = []
    print("Uprawnienia roli aplikacji\n")
    for opis, sql, ma_sie_udac in KONTROLE:
        try:
            # Kazda kontrola we WLASNEJ transakcji, wycofywanej po sprawdzeniu -
            # inaczej udany INSERT zostawilby smieci, a nieudany zablokowal reszte.
            with psycopg.connect(url) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
                conn.rollback()
            wynik, udalo = "wykonane", True
        except psycopg.errors.InsufficientPrivilege:
            wynik, udalo = "brak uprawnień", False
        except Exception as e:                                   # noqa: BLE001
            wynik, udalo = f"{type(e).__name__}", False

        zgodne = udalo == ma_sie_udac
        znak = "ok  " if zgodne else "BŁĄD"
        oczek = "ma działać" if ma_sie_udac else "ma być zablokowane"
        print(f"  [{znak}] {opis:38s} {oczek:20s} → {wynik}")
        if not zgodne:
            bledy.append(opis)

    with psycopg.connect(url) as conn:
        su = conn.execute(
            "SELECT usesuper FROM pg_user WHERE usename = current_user").fetchone()[0]
    print(f"  [{'ok  ' if not su else 'BŁĄD'}] {'superużytkownik':38s} "
          f"{'ma być NIE':20s} → {'TAK' if su else 'nie'}")
    if su:
        bledy.append("rola jest superużytkownikiem")

    print()
    if bledy:
        print(f"NIEZGODNOŚCI: {len(bledy)}")
        for b in bledy:
            print(f"  - {b}")
        return 1
    print(f"Wszystkie {len(KONTROLE) + 1} kontroli zgodne z oczekiwaniem.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
