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
        for symbol, base in CRYPTO:
            cur.execute(
                "INSERT INTO ref.instrument (venue_id, symbol, engine, base_ccy, quote_ccy,"
                " kind, active) VALUES (%s,%s,'crypto',%s,'USDT','spot',true)"
                " ON CONFLICT (venue_id, symbol) DO UPDATE SET active = true"
                " RETURNING (xmax = 0) AS inserted",
                (venue_id, symbol, base),
            )
            if cur.fetchone()["inserted"]:
                added += 1
        conn.commit()

        cur.execute("SELECT count(*) AS n FROM ref.instrument"
                    " WHERE engine='crypto' AND active AND kind='spot'")
        print(f"Dodano {added} nowych, aktywnych instrumentów krypto: {cur.fetchone()['n']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
