#!/usr/bin/env python3
"""Przepisuje wszystkie zaszyfrowane wartosci nowym kluczem SECRETS_KEY.

KIEDY

Po zmianie klucza szyfrujacego. Procedura w calosci:

    1. SECRETS_KEY_OLD = dotychczasowy klucz, SECRETS_KEY = nowy
    2. wdrozenie - nowe zapisy ida juz nowym kluczem, stare wartosci nadal
       sie odczytuja, bo SecretBox probuje obu kluczy
    3. ten skrypt - przepisuje wszystko, co zostalo na starym kluczu
    4. usuniecie SECRETS_KEY_OLD z sekretu i ponowne wdrozenie

Kroku 3 nie wolno pominac. Dopoki jakakolwiek wartosc jest zaszyfrowana starym
kluczem, usuniecie SECRETS_KEY_OLD czyni ja nieodczytywalna - i dowiemy sie
o tym dopiero przy probie uzycia, czyli w najgorszym momencie.

BEZPIECZENSTWO

Skrypt jest idempotentny: wartosci juz zaszyfrowane biezacym kluczem pomija.
Kazdy wiersz przepisuje w OSOBNEJ transakcji - przerwanie w polowie zostawia
czesc przepisana, a czesc nie, i oba stany sa odczytywalne, dopoki
SECRETS_KEY_OLD jest ustawiony.

Uzycie:
    DATABASE_URL=... SECRETS_KEY=nowy SECRETS_KEY_OLD=stary \\
        python services/rotate_secrets.py [--wykonaj]

Bez --wykonaj tylko raportuje, co by zrobil.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg
from psycopg.rows import dict_row

from services.api.security import SecretBox

# (tabela, kolumna klucza glownego, kolumny zaszyfrowane)
CELE = [
    ("app.totp_secret",   "user_id", ["secret_enc"]),
    ("app.api_credential", "id",     ["key_enc", "secret_enc", "passphrase_enc"]),
    ("app.secret",         "name",   ["value_enc"]),
    ("notify.channel",     "id",     ["config_enc"]),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wykonaj", action="store_true",
                    help="bez tej flagi skrypt tylko raportuje")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")
    if not os.environ.get("SECRETS_KEY_OLD"):
        print("UWAGA: SECRETS_KEY_OLD nie jest ustawiony. Skrypt przepisze tylko to,")
        print("co i tak da sie odczytac biezacym kluczem - czyli prawdopodobnie nic.\n")

    box = SecretBox()
    do_przepisania = przepisane = pominiete = bledy = 0

    with psycopg.connect(url, row_factory=dict_row) as conn:
        for tabela, pk, kolumny in CELE:
            lista = ", ".join(kolumny)
            with conn.cursor() as cur:
                cur.execute(f"SELECT {pk}, {lista} FROM {tabela}")
                wiersze = cur.fetchall()

            for w in wiersze:
                for kol in kolumny:
                    blob = w[kol]
                    if blob is None:
                        continue
                    blob = bytes(blob)
                    if box.zaszyfrowany_biezacym(blob):
                        pominiete += 1
                        continue
                    do_przepisania += 1
                    if not args.wykonaj:
                        print(f"  do przepisania: {tabela}.{kol} ({pk}={w[pk]})")
                        continue
                    try:
                        jawny = box.decrypt(blob)
                    except Exception as e:                       # noqa: BLE001
                        bledy += 1
                        print(f"  BLAD odczytu {tabela}.{kol} ({pk}={w[pk]}): {e}")
                        print("     -> wartosc nie da sie odczytac ZADNYM ze znanych kluczy")
                        continue
                    # Osobna transakcja per wiersz - patrz komentarz modulu.
                    with psycopg.connect(url) as tx:
                        tx.execute(
                            f"UPDATE {tabela} SET {kol} = %s WHERE {pk} = %s",
                            (box.encrypt(jawny), w[pk]))
                        tx.commit()
                    przepisane += 1
                    print(f"  przepisane: {tabela}.{kol} ({pk}={w[pk]})")

    print()
    print(f"  już na bieżącym kluczu: {pominiete}")
    if args.wykonaj:
        print(f"  przepisane:             {przepisane}")
        if bledy:
            print(f"  BŁĘDY:                  {bledy}  <- NIE usuwaj SECRETS_KEY_OLD")
            return 1
        if przepisane:
            print("\n  Teraz można usunąć SECRETS_KEY_OLD i wdrożyć ponownie.")
    else:
        print(f"  do przepisania:         {do_przepisania}")
        print("\n  To był podgląd. Uruchom z --wykonaj, żeby przepisać.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
