#!/usr/bin/env python3
"""Backfill historycznych swiec z gieldy do market.candle.

Idempotentny: ON CONFLICT DO NOTHING plus wznawianie od najnowszej swiecy, ktora
juz mamy. Przerwany w polowie i uruchomiony ponownie dokonczy robote, a nie
zacznie od zera.

Uzycie:
    DATABASE_URL=... python services/ingest/backfill.py --days 90 --timeframe 1m
    DATABASE_URL=... python services/ingest/backfill.py --days 90 --symbol BTCUSDT
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg

from services.exchanges.base import ExchangeError
from services.exchanges.bybit import BybitAdapter

# Ile minut trwa jedna swieca danego timeframe'u - do przesuwania okna paginacji.
TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}

BATCH = 2000


def batched_insert(cur, rows: list) -> int:
    sql = ("INSERT INTO market.candle (instrument_id, timeframe, event_time, open, high, low,"
           " close, volume, trade_count, ingested_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
           " ON CONFLICT DO NOTHING")
    for i in range(0, len(rows), BATCH):
        cur.executemany(sql, rows[i:i + BATCH])
    return len(rows)


async def backfill_symbol(adapter, conn, instrument_id: int, symbol: str,
                          timeframe: str, since: datetime) -> int:
    step = timedelta(minutes=TF_MINUTES[timeframe])
    cur = conn.cursor()

    # Wznow od miejsca, w ktorym skonczylismy - nie sciagamy tego, co juz mamy.
    cur.execute(
        "SELECT max(event_time) FROM market.candle WHERE instrument_id = %s AND timeframe = %s",
        (instrument_id, timeframe),
    )
    have_until = cur.fetchone()[0]
    start = max(since, have_until + step) if have_until else since
    now = datetime.now(timezone.utc)
    if start >= now:
        print(f"  {symbol:10s} juz aktualne")
        return 0

    total = 0
    empty_rounds = 0
    while start < now:
        try:
            candles = await adapter.fetch_candles(symbol, timeframe, limit=1000, start=start)
        except ExchangeError as e:
            if not e.retryable:
                raise
            print(f"  {symbol:10s} {e} - ponawiam za 5 s")
            await asyncio.sleep(5)
            continue

        if not candles:
            # Dwie puste odpowiedzi z rzedu oznaczaja koniec dostepnej historii,
            # a nie chwilowa dziure - inaczej petla krecilaby sie w nieskonczonosc.
            empty_rounds += 1
            if empty_rounds >= 2:
                break
            start += step * 1000
            continue
        empty_rounds = 0

        rows = [
            (instrument_id, timeframe, c.event_time, c.open, c.high, c.low, c.close,
             c.volume, c.trades, now)
            for c in candles if c.event_time < now
        ]
        if rows:
            batched_insert(cur, rows)
            conn.commit()
            total += len(rows)

        last = candles[-1].event_time
        if last <= start:      # brak postepu - zabezpieczenie przed petla
            break
        start = last + step
        # Postep w jednej linii tylko na terminalu. W przekierowanym logu \r nie
        # dziala i produkuje jedna wielometrowa linie, ktorej nie da sie czytac.
        if sys.stdout.isatty():
            print(f"\r  {symbol:10s} {total:>7d} swiec, do {last:%Y-%m-%d %H:%M}",
                  end="", flush=True)
        # Limit Bybit to 120 zapytan / 5 s; 50 ms przerwy trzyma nas daleko ponizej.
        await asyncio.sleep(0.05)

    print(f"\r  {symbol:10s} {total:>7d} swiec{' ' * 30}" if sys.stdout.isatty()
          else f"  {symbol:10s} {total:>7d} swiec")
    return total


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--timeframe", default="1m", choices=sorted(TF_MINUTES))
    ap.add_argument("--symbol", help="tylko ten instrument")
    ap.add_argument("--engine", default="crypto")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    print(f"Backfill {args.timeframe} od {since:%Y-%m-%d} ({args.days} dni)\n")

    with psycopg.connect(url) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT i.id, i.symbol, v.code
            FROM ref.instrument i JOIN ref.venue v ON v.id = i.venue_id
            WHERE i.engine = %s AND i.active AND (%s::text IS NULL OR i.symbol = %s)
            ORDER BY i.symbol
            """,
            (args.engine, args.symbol, args.symbol),
        )
        instruments = cur.fetchall()
        if not instruments:
            sys.exit("brak pasujacych instrumentow")

        adapter = BybitAdapter(api_key="", api_secret="", mode="demo")
        grand = 0
        try:
            for iid, symbol, venue in instruments:
                if venue != "bybit":
                    print(f"  {symbol:10s} pominiete ({venue} nieobslugiwany w backfillu)")
                    continue
                grand += await backfill_symbol(adapter, conn, iid, symbol, args.timeframe, since)
        finally:
            await adapter.close()

    print(f"\nRazem: {grand} swiec")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
