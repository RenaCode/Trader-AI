#!/usr/bin/env python3
"""Dociaganie biezacych danych rynkowych: swiece 1m i snapshoty orderbooka.

Swiece pobieramy z zakladka (ostatnie ~200 zamiast ostatniej), zeby przerwa
w dzialaniu workera zalatala sie sama przy nastepnym cyklu. ON CONFLICT
DO NOTHING sprawia, ze powtorki nic nie kosztuja.

Uzycie:
    DATABASE_URL=... python services/ingest/market_worker.py --interval 60
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg
from psycopg.rows import dict_row

from services.exchanges.base import ExchangeError
from services.exchanges.bybit import BybitAdapter


async def cycle(conn, adapter, instruments: list[dict], book_depth: int) -> dict:
    cur = conn.cursor()
    stats = {"swiece": 0, "orderbooki": 0, "bledy": 0}
    now = datetime.now(timezone.utc)

    for inst in instruments:
        try:
            candles = await adapter.fetch_candles(inst["symbol"], "1m", limit=200)
        except ExchangeError as e:
            stats["bledy"] += 1
            print(f"  {inst['symbol']:10s} świece: {e}")
            continue

        rows = [(inst["id"], "1m", c.event_time, c.open, c.high, c.low, c.close,
                 c.volume, c.trades, now)
                for c in candles if c.event_time < now.replace(second=0, microsecond=0)]
        if rows:
            cur.executemany(
                "INSERT INTO market.candle (instrument_id, timeframe, event_time, open, high,"
                " low, close, volume, trade_count, ingested_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", rows)
            stats["swiece"] += cur.rowcount if cur.rowcount > 0 else 0

        try:
            book = await adapter.fetch_order_book(inst["symbol"], book_depth)
        except ExchangeError as e:
            stats["bledy"] += 1
            print(f"  {inst['symbol']:10s} orderbook: {e}")
            continue

        cur.execute(
            "INSERT INTO market.book_snapshot (instrument_id, event_time, mid, spread,"
            " bid_volume, ask_volume, imbalance, levels, ingested_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now()) ON CONFLICT DO NOTHING",
            (inst["id"], book.event_time, book.mid, book.spread,
             sum(l.qty for l in book.bids), sum(l.qty for l in book.asks),
             book.imbalance,
             json.dumps({"bids": [[str(l.price), str(l.qty)] for l in book.bids[:10]],
                         "asks": [[str(l.price), str(l.qty)] for l in book.asks[:10]]})))
        stats["orderbooki"] += 1
        conn.commit()

    return stats


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--depth", type=int, default=25)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    adapter = BybitAdapter(api_key="", api_secret="", mode="demo")
    try:
        with psycopg.connect(url, row_factory=dict_row) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT i.id, i.symbol FROM ref.instrument i JOIN ref.venue v ON v.id=i.venue_id"
                " WHERE v.code='bybit' AND i.active ORDER BY i.symbol")
            instruments = cur.fetchall()
            print(f"Obserwowane instrumenty: {', '.join(i['symbol'] for i in instruments)}")

            while True:
                t0 = datetime.now(timezone.utc)
                try:
                    s = await cycle(conn, adapter, instruments, args.depth)
                    print(f"[{t0:%H:%M:%S}] świece +{s['swiece']}, orderbooki {s['orderbooki']}, "
                          f"błędy {s['bledy']}")
                except Exception as e:                        # noqa: BLE001
                    conn.rollback()
                    print(f"[{t0:%H:%M:%S}] cykl przerwany: {type(e).__name__}: {e}")
                if args.once:
                    return 0
                await asyncio.sleep(args.interval)
    finally:
        await adapter.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
