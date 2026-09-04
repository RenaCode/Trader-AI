#!/usr/bin/env python3
"""Rejestracja instrumentow krypto do obserwacji.

DLACZEGO WIECEJ NIZ TRZY

Na swiecy 4-godzinnej strategia wchodzi w rynek 4-8 razy na instrument
miesiecznie. Przy trzech instrumentach okno testowe dlugosci 30 dni zbiera
5 transakcji - za malo, zeby cokolwiek zmierzyc, wiec walk-forward odrzucal
39 z 40 okien jako nieinformatywne.

Sa trzy sposoby, zeby dostac wiecej transakcji w oknie, i tylko jeden jest
uczciwy:

  1. obnizyc prog wejscia  -> wiecej transakcji, ale slabszych; przy koszcie
     rzedu 22-48% mediany ruchu to powrot do handlowania szumem,
  2. wydluzyc okno testowe -> wiecej transakcji na okno, ale mniej okien;
     przy 4 latach danych schodzimy ponizej progu wiarygodnosci walidacji,
  3. obserwowac wiecej instrumentow -> wiecej transakcji BEZ zmiany strategii
     i bez skracania oceny.

Wybrana jest trzecia. Instrumenty dobrane po plynnosci na Bybit spot, nie po
tym, ktore historycznie rosly - dobor po wyniku bylby selekcja przezywalnosci
i unieważnilby caly backtest.

ILE INSTRUMENTOW - Z POMIARU, NIE Z PRZECZUCIA

Przy dwunastu instrumentach mediana wynosi 10 transakcji na okno testowe
(30 dni), przy progu informatywnosci 12. Polowa okien wypada wiec ponizej progu
i walk-forward konczy sie na 19 okienach z wymaganych 20.

Sprawdzilem, czy nie wystarczy wydluzyc okna testowego. NIE wystarczy - i to jest
wynik przeciwny do intuicji. Dluzsze okno ma wiecej transakcji, ale okien jest
odpowiednio mniej, a ubywa ich szybciej, niz przybywa informatywnych:

    30 dni -> 40 okien, 19 informatywnych   (stan zmierzony)
    45 dni -> 26 okien, ~16 informatywnych
    60 dni -> 20 okien, ~14 informatywnych
    90 dni -> 13 okien, ~10 informatywnych

30 dni jest wiec juz optimum tego kompromisu. Jedyna pozostala dzwignia, ktora
nie oslabia samej strategii, to szerszy zbior instrumentow: dwa razy wiecej
rynkow to dwa razy wiecej okazji przy NIEZMIENIONYCH progach wejscia i przy tej
samej lacznej ekspozycji, bo budzet na pozycje to limit/liczba_instrumentow.

ZASTRZEZENIE: instrumenty krypto sa silnie skorelowane, wiec podwojenie ich
liczby NIE podwaja liczby niezaleznych obserwacji. Podnosi moc statystyczna
mniej, niz sugeruje sam licznik transakcji - i tak nalezy czytac kazdy wynik
policzony na tym zbiorze.

Uzycie:
    DATABASE_URL=... python services/ingest/instruments.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg
from psycopg.rows import dict_row

# (symbol, waluta bazowa). Kryterium: obecnosc na Bybit spot i plynnosc,
# nie historyczna stopa zwrotu.
CRYPTO = [
    ("BTCUSDT", "BTC"), ("ETHUSDT", "ETH"), ("SOLUSDT", "SOL"),
    ("XRPUSDT", "XRP"), ("BNBUSDT", "BNB"), ("DOGEUSDT", "DOGE"),
    ("ADAUSDT", "ADA"), ("LINKUSDT", "LINK"), ("AVAXUSDT", "AVAX"),
    ("DOTUSDT", "DOT"), ("LTCUSDT", "LTC"), ("TRXUSDT", "TRX"),
    # Rozszerzenie do 24 - uzasadnienie w akapicie "ILE INSTRUMENTOW".
    ("BCHUSDT", "BCH"), ("ATOMUSDT", "ATOM"), ("FILUSDT", "FIL"),
    ("NEARUSDT", "NEAR"), ("APTUSDT", "APT"), ("ARBUSDT", "ARB"),
    ("OPUSDT", "OP"), ("INJUSDT", "INJ"), ("SUIUSDT", "SUI"),
    ("SEIUSDT", "SEI"), ("TIAUSDT", "TIA"), ("UNIUSDT", "UNI"),
]

# PARY USDC - to, co gielda z kontem faktycznie oferuje.
#
# Podmiot unijny ma 4 pary *USDT i 113 par *USDC; cala jego plynnosc siedzi na
# USDC (patrz migracja 0018). Handel idzie wiec parami USDC, a historia, na
# ktorej model byl trenowany, jest w parach USDT.
#
# DLACZEGO NIE WYSTARCZY MAPOWANIE SYMBOLI
#
# Kusi, zeby trenowac na glebszym szeregu USDT i podmieniac symbol dopiero przy
# skladaniu zlecenia. Korelacja zwrotow 4h miedzy para USDT i USDC tego samego
# aktywa wynosi 0,973-0,9997, wiec wyglada to na to samo. Nie jest.
#
# Zmierzona MEDIANA BAZY miedzy tymi szeregami (2026-09-04, 1000 swiec 4h):
#
#     BTC 0,053%   ETH 0,054%   SOL 0,058%   XRP 0,053%   ADA 0,061%
#     DOGE 0,064%  SUI 0,065%   ARB 0,078%   LINK 0,096%  NEAR 0,163%
#
# Runda kosztowa z limitem to 0,200%. Baza jest wiec rzedu jednej czwartej
# calego budzetu kosztowego - czyli tego samego rzedu co przewaga, ktorej
# szukamy. Trenowanie na jednym szeregu i handlowanie drugim wstrzykuje szum
# wielkosci mierzonego efektu.
#
# Pary USDT ZOSTAJA aktywne: maja glebsza historie i to na nich stoi dotychczasowy
# pomiar. Rozstrzygnie porownanie na tych samych oknach, nie deklaracja.
CRYPTO_USDC = [
    ("BTCUSDC", "BTC"), ("ETHUSDC", "ETH"), ("SOLUSDC", "SOL"),
    ("XRPUSDC", "XRP"), ("ADAUSDC", "ADA"), ("DOGEUSDC", "DOGE"),
    ("LINKUSDC", "LINK"), ("ARBUSDC", "ARB"), ("SUIUSDC", "SUI"),
    ("NEARUSDC", "NEAR"),
]


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    with psycopg.connect(url, row_factory=dict_row) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM ref.venue WHERE code = 'bybit'")
        row = cur.fetchone()
        if not row:
            sys.exit("brak giełdy 'bybit' w ref.venue")
        venue_id = row["id"]

        added = 0
        # `tradeable` ustawiamy WYLACZNIE przy wstawianiu i nigdy przy konflikcie:
        # uniwersum decyzyjne zmienia sie migracja albo swiadoma decyzja, a nie
        # przy okazji ponownego uruchomienia rejestracji instrumentow (0020).
        #
        # Pary USDC wchodza jako NIEHANDLOWALNE. Kolejnosc jest tu istotna:
        # najpierw historia i cechy, dopiero potem wejscie do uniwersum. Odwrotna
        # dalaby silnikowi instrumenty bez danych, czyli okna bez transakcji.
        for symbol, base, quote, tradeable in (
            [(s, b, "USDT", True) for s, b in CRYPTO]
            + [(s, b, "USDC", False) for s, b in CRYPTO_USDC]
        ):
            cur.execute(
                "INSERT INTO ref.instrument (venue_id, symbol, engine, base_ccy, quote_ccy,"
                " kind, active, tradeable) VALUES (%s,%s,'crypto',%s,%s,'spot',true,%s)"
                " ON CONFLICT (venue_id, symbol) DO UPDATE SET active = true"
                " RETURNING (xmax = 0) AS inserted",
                (venue_id, symbol, base, quote, tradeable),
            )
            if cur.fetchone()["inserted"]:
                added += 1
        conn.commit()

        cur.execute("SELECT count(*) FILTER (WHERE active) AS akt,"
                    " count(*) FILTER (WHERE active AND tradeable) AS handl"
                    " FROM ref.instrument WHERE engine='crypto' AND kind='spot'")
        r = cur.fetchone()
        print(f"Dodano {added} nowych. Aktywnych (zbieramy historię): {r['akt']}, "
              f"w uniwersum decyzyjnym: {r['handl']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
