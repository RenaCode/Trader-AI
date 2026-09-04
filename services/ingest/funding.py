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


def ensure_perp(cur, spot: dict) -> tuple[int, str]:
    """Odnajduje perpetual, z ktorego bierzemy finansowanie dla tego aktywa.

    FINANSOWANIE JEST WLASNOSCIA AKTYWA, NIE WALUTY KWOTOWANEJ

    Poprzednia wersja doklejala `.P` do symbolu spotowego, wiec dla BTCUSDT
    wychodzil BTCUSDT.P - poprawnie - ale dla BTCUSDC wychodzil BTCUSDC.P,
    ktorego na gieldzie nie ma. Zapytanie konczylo sie bledem "Symbol Invalid"
    i WYWRACALO CALY PRZEBIEG, bo wyjatek szedl w gore petli: jeden nowy
    instrument zatrzymywal pobieranie stawek dla wszystkich pozostalych.

    Perpetual wskazujemy wiec po AKTYWIE i kwotujemy w USDT - tam ta stawka
    powstaje i tam mamy jej historie. Wszystkie instrumenty spotowe tego samego
    aktywa, niezaleznie od waluty kwotowanej, dziedzicza to samo zrodlo
    (patrz migracja 0021).
    """
    perp_symbol = f"{spot['base_ccy']}USDT.P"
    cur.execute(
        "INSERT INTO ref.instrument (venue_id, symbol, engine, base_ccy, quote_ccy, kind, active)"
        " VALUES (%s,%s,%s,%s,'USDT','perp',false)"
        " ON CONFLICT (venue_id, symbol) DO UPDATE SET kind='perp'"
        " RETURNING id",
        (spot["venue_id"], perp_symbol, spot["engine"], spot["base_ccy"]),
    )
    perp_id = cur.fetchone()["id"]
    meta = {"funding_from": perp_id}
    if spot["quote_ccy"] != "USDT":
        # Zapisane wprost, bo to PRZYBLIZENIE: stawka pochodzi z perpetuala
        # kwotowanego inaczej niz instrument, ktorym handlujemy.
        meta["funding_proxy"] = perp_symbol
    cur.execute(
        "UPDATE ref.instrument SET meta = meta || %s::jsonb WHERE id = %s",
        (json.dumps(meta), spot["id"]),
    )
    return perp_id, perp_symbol[:-2]


def missing_ranges(lo: datetime | None, hi: datetime | None, since: datetime,
                   now: datetime, step: timedelta) -> list[tuple[datetime, datetime]]:
    """Przedzialy, ktorych brakuje - takze PRZED tym, co juz mamy.

    Wznawianie wylacznie od najnowszego posiadanego rekordu wyglada rozsadnie
    i jest pulapka: wystarczy, ze inny proces dociagnie swiezy ogonek, a zadanie
    "sciagnij cztery lata" widzi "mam dane az do teraz" i konczy sie zerem, nie
    zglaszajac bledu. Dokladnie to sie tu wydarzylo - worker biezacy wstawil
    27 najnowszych stawek, po czym pelny backfill nie zrobil nic.

    Dlatego liczymy oba brzegi: dziure przed najstarszym rekordem i ogon po
    najnowszym.
    """
    if lo is None or hi is None:
        return [(since, now)]
    out = []
    if since < lo:
        out.append((since, lo))
    if hi + step < now:
        out.append((hi + step, now))
    return out


async def backfill(adapter, conn, perp_id: int, symbol: str, since: datetime) -> int:
    cur = conn.cursor()
    cur.execute("SELECT min(event_time) AS lo, max(event_time) AS hi"
                " FROM market.funding_rate WHERE instrument_id=%s", (perp_id,))
    row = cur.fetchone()
    now = datetime.now(timezone.utc)
    step = timedelta(hours=INTERVAL_H)
    total = 0

    for start, stop in missing_ranges(row["lo"], row["hi"], since, now, step):
        total += await _fetch_range(adapter, conn, cur, perp_id, symbol, start, stop, now)

    print(f"  {symbol:12s} {total:>6,} stawek")
    return total


async def _fetch_range(adapter, conn, cur, perp_id: int, symbol: str,
                       start: datetime, stop: datetime, now: datetime) -> int:
    total = 0
    while start < stop:
        end = min(start + timedelta(hours=INTERVAL_H * PAGE), stop)
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
        bledy: list[str] = []
        # Po aktywie, nie po instrumencie: BTCUSDT i BTCUSDC dziela jedno zrodlo
        # finansowania, wiec pobieranie go dwa razy byloby ta sama praca dwukrotnie
        # i drugim zapisem tej samej prawdy.
        zrobione: set[str] = set()
        try:
            for spot in spots:
                perp_id, rynek = ensure_perp(cur, spot)
                conn.commit()
                if rynek in zrobione:
                    continue
                zrobione.add(rynek)
                # Blad JEDNEGO rynku nie moze zatrzymac pozostalych. Poprzednia
                # wersja przepuszczala wyjatek w gore i jeden nowy instrument
                # kasowal caly przebieg.
                try:
                    grand += await backfill(adapter, conn, perp_id, rynek, since)
                except ExchangeError as e:
                    bledy.append(f"{rynek}: {e}")
        finally:
            await adapter.close()

    print(f"\nRazem: {grand:,} stawek z {len(zrobione)} rynków")
    if bledy:
        print(f"Nieudane ({len(bledy)}): " + "; ".join(bledy))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
