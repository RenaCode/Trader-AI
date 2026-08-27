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
