#!/usr/bin/env python3
"""Zaklada lub resetuje konto (domyslnie wlasciciela, `--rola viewer` - podglad).

Haslo NIE jest argumentem wiersza polecen (trafiloby do historii powloki i do
listy procesow) - jest czytane z STDIN albo generowane losowo.

Uzycie:
    DATABASE_URL=... python scripts/create_admin.py <login> --rola owner
    DATABASE_URL=... python scripts/create_admin.py <login> --rola viewer --stdin < plik_z_haslem
    DATABASE_URL=... python scripts/create_admin.py <login>          # reset hasla istniejacego

Nowe konto wymaga jawnego `--rola` (bez domyslnej): literowka w nazwie opcji
nie moze po cichu zalozyc konta `owner` komus, kto mial tylko patrzec.
Nieznany argument = odmowa. Login jak w Authelii (`[a-z0-9._-]`, jak
`sso._LOGIN`) - inaczej SSO odpowiadaloby temu kontu zawsze 400.

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
from services.api.sso import _LOGIN


ROLE = ("owner", "viewer")
USAGE = "uzycie: create_admin.py <login> [--rola owner|viewer] [--stdin]"


def argumenty(argv: list[str]) -> tuple[str, str | None, bool]:
    """(login, rola albo None, czy --stdin). Kazdy inny argument = odmowa."""
    if not argv or argv[0].startswith("-"):
        sys.exit(USAGE)
    login, reszta, rola, stdin = argv[0].strip(), argv[1:], None, False
    if not _LOGIN.fullmatch(login):
        sys.exit(f"login {login!r}: tylko a-z, 0-9, '.', '_', '-' (jak w Authelii)")
    while reszta:
        a = reszta.pop(0)
        if a == "--stdin" and not stdin:
            stdin = True
        elif a == "--rola" and rola is None and reszta and reszta[0] in ROLE:
            rola = reszta.pop(0)
        else:
            sys.exit(f"nieznany albo powtorzony argument {a!r}\n{USAGE}")
    return login, rola, stdin


def main() -> int:
    login, rola, stdin = argumenty(sys.argv[1:])
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    if stdin:
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
        if obecna is None and rola is None:
            sys.exit(f"konta {login!r} nie ma - nowe konto wymaga --rola owner|viewer")
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
            # Istniejace konto: rola z bazy (ON CONFLICT i tak jej nie zmienia,
            # ale wiersz INSERT musi spelnic NOT NULL, zanim dojdzie do konfliktu).
            (login, hash_password(password), rola or obecna[0]),
        )
        # Reset hasla uniewaznia wszystkie sesje tego konta.
        conn.execute(
            "UPDATE app.session s SET revoked_at = now()"
            " FROM app.user u WHERE u.id = s.user_id AND u.login = %s AND s.revoked_at IS NULL",
            (login,),
        )

    print(f"konto: {login} (rola: {obecna[0] if obecna else rola})")
    if generated:
        print(f"haslo: {password}")
        print("\nZapisz je teraz - nie da sie go odczytac pozniej (w bazie jest tylko hash Argon2id).")
    else:
        print("haslo: ustawione z STDIN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
