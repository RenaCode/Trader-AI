#!/usr/bin/env python3
"""Backfill stawek finansowania perpetuali.

Instrumenty, ktorymi handlujemy, sa spotowe - a funding istnieje tylko na
perpetualach. Nie jest to jednak dana "nie z tego rynku": stawka finansowania
opisuje, jak mocno lewarowany tlum jest wychylony w jedna strone na TEJ SAMEJ
parze. To jedyne zrodlo w calym zestawie, ktore nie jest przetworzona cena -
i dlatego jako jedyne moze wniesc informacje, ktorej w cenie jeszcze nie ma.

Perpetual zakladamy jako osobny, NIEAKTYWNY instrument (kind='perp'), a
instrument spotowy dostaje w `meta.funding_from` jego id. Dzieki temu klucz obcy
w market.funding_rate wskazuje na to, czego stawka naprawde dotyczy, zamiast
udawac, ze spot ma funding.

Uzycie:
    DATABASE_URL=... python services/ingest/funding.py --days 1460
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg
from psycopg.rows import dict_row

from services.exchanges.base import ExchangeError
from services.exchanges.bybit import BybitAdapter

# Bybit oglasza funding co 8 h i oddaje maksymalnie 200 rekordow na zapytanie.
INTERVAL_H = 8
PAGE = 200


def ensure_perp(cur, spot: dict) -> int:
    """Tworzy (lub odnajduje) instrument perpetualowy i podpina go pod spot."""
    perp_symbol = f"{spot['symbol']}.P"
    cur.execute(
        "INSERT INTO ref.instrument (venue_id, symbol, engine, base_ccy, quote_ccy, kind, active)"
        " VALUES (%s,%s,%s,%s,%s,'perp',false)"
        " ON CONFLICT (venue_id, symbol) DO UPDATE SET kind='perp'"
        " RETURNING id",
        (spot["venue_id"], perp_symbol, spot["engine"], spot["base_ccy"], spot["quote_ccy"]),
    )
    perp_id = cur.fetchone()["id"]
    cur.execute(
        "UPDATE ref.instrument SET meta = meta || %s::jsonb WHERE id = %s",
        (json.dumps({"funding_from": perp_id}), spot["id"]),
    )
    return perp_id


async def backfill(adapter, conn, perp_id: int, symbol: str, since: datetime) -> int:
    cur = conn.cursor()
    cur.execute("SELECT max(event_time) AS m FROM market.funding_rate WHERE instrument_id=%s",
                (perp_id,))
    have = cur.fetchone()["m"]
    start = max(since, have + timedelta(hours=INTERVAL_H)) if have else since
    now = datetime.now(timezone.utc)
    total = 0

    while start < now:
        end = min(start + timedelta(hours=INTERVAL_H * PAGE), now)
        try:
            rows = await adapter.fetch_funding(symbol, start, end)
        except ExchangeError as e:
            if not e.retryable:
                raise
            print(f"  {symbol:12s} {e} — ponawiam za 5 s")
            await asyncio.sleep(5)
            continue

        if rows:
            cur.executemany(
                "INSERT INTO market.funding_rate (instrument_id, event_time, rate, ingested_at)"
                " VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                [(perp_id, t, r, now) for t, r in rows],
            )
            conn.commit()
            total += len(rows)
            # Idziemy od ostatniej otrzymanej stawki, a nie od konca okna -
            # inaczej przy niepelnej stronie przeskoczylibysmy dziure.
            start = rows[-1][0] + timedelta(hours=INTERVAL_H)
        else:
            start = end
        await asyncio.sleep(0.05)

    print(f"  {symbol:12s} {total:>6,} stawek")
    return total


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1460)
    ap.add_argument("--engine", default="crypto")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    print(f"Funding od {since:%Y-%m-%d}\n")

    with psycopg.connect(url, row_factory=dict_row) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT i.id, i.venue_id, i.symbol, i.engine, i.base_ccy, i.quote_ccy"
            " FROM ref.instrument i JOIN ref.venue v ON v.id=i.venue_id"
            " WHERE i.engine=%s AND i.active AND i.kind='spot' AND v.code='bybit'"
            " ORDER BY i.symbol", (args.engine,))
        spots = cur.fetchall()

        adapter = BybitAdapter(api_key="", api_secret="", mode="demo", category="linear")
        grand = 0
        try:
            for spot in spots:
                perp_id = ensure_perp(cur, spot)
                conn.commit()
                grand += await backfill(adapter, conn, perp_id, spot["symbol"], since)
        finally:
            await adapter.close()

    print(f"\nRazem: {grand:,} stawek")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
