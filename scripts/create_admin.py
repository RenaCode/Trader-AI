#!/usr/bin/env python3
"""Zaklada lub resetuje konto (domyslnie wlasciciela, `--rola viewer` - podglad).

Haslo NIE jest argumentem wiersza polecen (trafiloby do historii powloki i do
listy procesow) - jest czytane z STDIN albo generowane losowo.

Uzycie:
    DATABASE_URL=... SECRETS_KEY=... python scripts/create_admin.py admin
    DATABASE_URL=... python scripts/create_admin.py admin --stdin  < plik_z_haslem
    DATABASE_URL=... python scripts/create_admin.py <login> --rola viewer

`viewer` widzi wszystko i niczego nie zmienia (`wymagaj_wlasciciela` w API).
Reset hasla istniejacego konta nie rusza jego roli; `--rola` inna niz obecna
przerywa bez zmian - zmiana roli to swiadome UPDATE, nie skutek uboczny
resetu hasla.
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg
from services.api.security import hash_password


ROLE = ("owner", "viewer")


def rola_z_argumentow(argv: list[str]) -> str | None:
    """Wartosc `--rola` albo None, gdy nie podano (nowe konto: owner)."""
    if "--rola" not in argv:
        return None
    i = argv.index("--rola")
    if i + 1 >= len(argv) or argv[i + 1] not in ROLE:
        sys.exit(f"--rola: jedna z {', '.join(ROLE)}")
    return argv[i + 1]


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1].startswith("-"):
        sys.exit("uzycie: create_admin.py <login> [--stdin] [--rola owner|viewer]")
    login = sys.argv[1].strip()
    rola = rola_z_argumentow(sys.argv)
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    if "--stdin" in sys.argv:
        password = sys.stdin.read().strip()
        if len(password) < 12:
            sys.exit("haslo musi miec co najmniej 12 znakow")
        generated = False
    else:
        password = secrets.token_urlsafe(18)
        generated = True

    with psycopg.connect(url, autocommit=True) as conn:
        obecna = conn.execute("SELECT role FROM app.user WHERE login = %s",
                              (login,)).fetchone()
        if obecna and rola and obecna[0] != rola:
            sys.exit(f"konto {login!r} ma role {obecna[0]!r}, nie {rola!r} - nic nie zmieniono "
                     "(zmiana roli: UPDATE app.user SET role = ... WHERE login = ...)")
        conn.execute(
            """
            INSERT INTO app.user (login, password_hash, role, is_active)
            VALUES (%s, %s, %s, true)
            ON CONFLICT (login) DO UPDATE
              SET password_hash = EXCLUDED.password_hash,
                  is_active = true, failed_logins = 0, locked_until = NULL
            """,
            (login, hash_password(password), rola or "owner"),
        )
        # Reset hasla uniewaznia wszystkie sesje tego konta.
        conn.execute(
            "UPDATE app.session s SET revoked_at = now()"
            " FROM app.user u WHERE u.id = s.user_id AND u.login = %s AND s.revoked_at IS NULL",
            (login,),
        )

    print(f"konto: {login} (rola: {obecna[0] if obecna else rola or 'owner'})")
    if generated:
        print(f"haslo: {password}")
        print("\nZapisz je teraz - nie da sie go odczytac pozniej (w bazie jest tylko hash Argon2id).")
    else:
        print("haslo: ustawione z STDIN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
