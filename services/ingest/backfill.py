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

# Zabezpieczenie przed nieskonczona petla, gdyby endpoint zwracal same pustki.
# Kazda proba przeskakuje 1000 swiec, wiec dla 1h to ponad 500 lat.
MAX_EMPTY_PROBES = 5000


# Ile ostatnich swiec pobieramy ponownie przy kazdym przebiegu, zeby poprawic
# ewentualne bledy. Trzy wystarczaja: swieca zamyka sie raz i wiecej sie nie
# zmienia, wiec jedyne, co wymaga poprawki, to koncowka poprzedniego przebiegu.
ZAKLADKA = 3


def batched_insert(cur, rows: list) -> int:
    """Zapis swiec z POPRAWIANIEM tego, co juz jest.

    DLACZEGO NIE "DO NOTHING"

    Do 2026-09-06 zapis pomijal konflikt, a wznawianie zaczynalo od najnowszej
    posiadanej swiecy - wiec swieca raz zapisana nie byla juz nigdy odpytywana.
    W polaczeniu z blednym filtrem (ponizej) oznaczalo to, ze swieca zapisana
    W TRAKCIE swojej godziny zostawala w bazie na zawsze z zakresem pierwszych
    dwoch minut.

    Zmierzone na BTCUSDC: zamkniecie zgodne z faktycznym w 9 godzinach na 55,
    a zapisany zakres high-low stanowil 39% prawdziwego.

    Swieca ZAMKNIETA jest niezmienna, wiec ponowne pobranie daje te same
    wartosci i nadpisanie jest bezpieczne. Warunek WHERE pilnuje, zeby
    `ingested_at` nie zmienialo sie przy kazdym przebiegu bez powodu - inaczej
    stracilibysmy informacje o tym, kiedy naprawde poznalismy te swiece.
    """
    sql = ("INSERT INTO market.candle (instrument_id, timeframe, event_time, open, high, low,"
           " close, volume, trade_count, ingested_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
           " ON CONFLICT (instrument_id, timeframe, event_time) DO UPDATE SET"
           " open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,"
           " close = EXCLUDED.close, volume = EXCLUDED.volume,"
           " trade_count = EXCLUDED.trade_count, ingested_at = EXCLUDED.ingested_at"
           " WHERE (market.candle.open, market.candle.high, market.candle.low,"
           "        market.candle.close, market.candle.volume)"
           "    IS DISTINCT FROM"
           "       (EXCLUDED.open, EXCLUDED.high, EXCLUDED.low,"
           "        EXCLUDED.close, EXCLUDED.volume)")
    for i in range(0, len(rows), BATCH):
        cur.executemany(sql, rows[i:i + BATCH])
    return len(rows)


async def backfill_symbol(adapter, conn, instrument_id: int, symbol: str,
                          timeframe: str, since: datetime,
                          napraw: bool = False) -> int:
    step = timedelta(minutes=TF_MINUTES[timeframe])
    cur = conn.cursor()

    # Nie sciagamy tego, co juz mamy - ale sprawdzamy OBA brzegi. Wznawianie
    # wylacznie od najnowszej posiadanej swiecy nie potrafi dociagnac historii
    # starszej niz to, co juz jest w bazie: zadanie "daj cztery lata" widzi
    # "mam dane az do teraz" i konczy sie zerem, nie zglaszajac bledu.
    cur.execute(
        "SELECT min(event_time), max(event_time) FROM market.candle"
        " WHERE instrument_id = %s AND timeframe = %s",
        (instrument_id, timeframe),
    )
    have_from, have_until = cur.fetchone()
    now = datetime.now(timezone.utc)

    ranges: list[tuple[datetime, datetime]] = []
    if napraw:
        # TRYB NAPRAWCZY: pobierz okno PONOWNIE, niezaleznie od tego, co juz mamy.
        #
        # Zwykla sciezka z zalozenia nie odpytuje swiec, ktore posiada - i to
        # jest wlasciwe, dopoki zapisane swiece sa poprawne. Gdy nie sa (np.
        # zapisano je w trakcie ich trwania, patrz filtr w _fetch_range),
        # zakladka trzech swiec nie siega wstecz wystarczajaco daleko.
        print(f"  {symbol:10s} tryb naprawczy: ponowne pobranie od {since:%Y-%m-%d %H:%M}")
        ranges.append((since, now))
    elif have_until is None:
        ranges.append((since, now))
    else:
        if since < have_from:
            ranges.append((since, have_from))
        # ZAKLADKA: cofamy sie o kilka swiec zamiast zaczynac od nastepnej.
        #
        # Bez niej swieca raz zapisana nigdy nie jest odpytywana ponownie, wiec
        # jakikolwiek blad w niej - w szczegolnosci zapis w trakcie jej trwania -
        # zostaje na zawsze. Z zakladka i zapisem poprawiajacym potok leczy sie
        # sam przy nastepnym przebiegu.
        if have_until + step < now:
            ranges.append((have_until - ZAKLADKA * step, now))
    if not ranges:
        print(f"  {symbol:10s} już aktualne")
        return 0

    total = 0
    for range_start, range_stop in ranges:
        total += await _fetch_range(adapter, conn, cur, instrument_id, symbol,
                                    timeframe, step, range_start, range_stop, now)

    print(f"\r  {symbol:10s} {total:>7d} swiec{' ' * 30}" if sys.stdout.isatty()
          else f"  {symbol:10s} {total:>7d} swiec")
    return total


async def _fetch_range(adapter, conn, cur, instrument_id: int, symbol: str,
                       timeframe: str, step, start: datetime, stop: datetime,
                       now: datetime) -> int:
    total = 0
    empty_rounds = 0
    while start < stop:
        try:
            candles = await adapter.fetch_candles(symbol, timeframe, limit=1000, start=start)
        except ExchangeError as e:
            if not e.retryable:
                raise
            print(f"  {symbol:10s} {e} - ponawiam za 5 s")
            await asyncio.sleep(5)
            continue

        if not candles:
            # Pusta odpowiedz NIE oznacza konca historii. Oznacza ja dopiero
            # dojscie do terazniejszosci. Instrument notowany od pozniejszej daty
            # niz `since` zwraca pustki na poczatku - wczesniejsza wersja tego
            # kodu konczyla wtedy po dwoch probach i sciagala zero swiec,
            # milczaco udajac sukces. Przeskakujemy dziure i idziemy dalej.
            empty_rounds += 1
            start += step * 1000
            if start >= stop or empty_rounds > MAX_EMPTY_PROBES:
                break
            continue
        empty_rounds = 0

        # WYLACZNIE SWIECE ZAMKNIETE.
        #
        # `event_time` to POCZATEK swiecy, wiec warunek `event_time < now`
        # przepuszczal swiece trwajaca: o 09:01 swieca 09:00 spelniala go
        # i ladowala w bazie z zakresem jednej minuty. Domkniecie wymaga
        # `event_time + step <= now`.
        #
        # Zmierzony skutek starego warunku (BTCUSDC, 55 godzin z kompletem
        # danych minutowych): zamkniecie zgodne w 9 przypadkach, zapisany
        # zakres high-low rowny 39% faktycznego. Kazda cecha liczona z tych
        # swiec - ATR, polozenie w zakresie, rezim zmiennosci - byla zanizona.
        rows = [
            (instrument_id, timeframe, c.event_time, c.open, c.high, c.low, c.close,
             c.volume, c.trades, now)
            for c in candles
            if c.event_time < stop and c.event_time + step <= now
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

    return total


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--timeframe", default="1m", choices=sorted(TF_MINUTES))
    ap.add_argument("--symbol", help="tylko ten instrument")
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--napraw", action="store_true",
                    help="pobierz okno PONOWNIE i popraw to, co juz jest w bazie. "
                         "Do usuwania skutkow blednego zapisu; zwykly przebieg "
                         "pomija swiece, ktore posiada")
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
                grand += await backfill_symbol(adapter, conn, iid, symbol, args.timeframe,
                                               since, napraw=args.napraw)
        finally:
            await adapter.close()

    print(f"\nRazem: {grand} swiec")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
