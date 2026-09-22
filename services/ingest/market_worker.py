#!/usr/bin/env python3
"""Dociaganie biezacych danych rynkowych: swiece 1m i snapshoty orderbooka.

Swiece pobieramy z zakladka (ostatnie ~200 zamiast ostatniej), zeby przerwa
w dzialaniu workera zalatala sie sama przy nastepnym cyklu. ON CONFLICT
DO NOTHING sprawia, ze powtorki nic nie kosztuja.

DWA SYGNALY O TYM, ZE PROCES PRZESTAL PRACOWAC

Ten modul jako jedyny z czterech workerow nie mial ZADNEGO z nich:

  * LICZNIK AWARII konczy proces po pieciu nieudanych cyklach pod rzad. Wpis
    w logu nie jest widocznoscia - logu poda nikt nie czyta o trzeciej w nocy,
    a po restarcie kontenera nie ma go juz wcale. Restart JEST widoczny: rosnie
    licznik restartow i pod wpada w CrashLoopBackOff. Nieudane pobranie
    pojedynczego instrumentu tez liczy sie jako awaria cyklu - tak samo jak
    w history_worker, bo inaczej instrument, ktorego gielda nie zna, moglby
    milczec miesiacami przy zerowym liczniku restartow.
  * PULS po kazdym UDANYM cyklu (`risk.worker_heartbeat`). Licznik broni przed
    procesem, ktory PADA; puls przed procesem, ktory STOI - zawieszony na
    gniezdzie bez limitu czasu nie konczy cyklu, wiec nie dolicza sie do zadnego
    licznika i w `kubectl get pods` widnieje jako `Running` bez konca.

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
from services.exchanges.fabryka import adapter_danych
from services.ingest import puls

# Ile cykli pod rzad zakonczonych bledem konczy proces - ta sama stala co
# w history_worker, features i news_worker. Rozna liczba w kazdym module
# znaczylaby, ze „piec awarii" jest wynikiem przypadku, a nie decyzji.
POD_RZAD_MAX = 5

# Nazwa w `risk.worker_heartbeat` - ROLA w potoku, nie egzemplarz procesu.
PULS = "market"


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
    # Silnik wybiera gielde (przez ref.venue i fabryke adapterow) ORAZ nazwe
    # pulsu: dwa workery bijace w ten sam wiersz `risk.worker_heartbeat`
    # zaslanialyby sobie nawzajem awarie.
    ap.add_argument("--engine", default="crypto")
    args = ap.parse_args()
    puls_nazwa = PULS if args.engine == "crypto" else f"{PULS}-{args.engine}"

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    with psycopg.connect(url, row_factory=dict_row) as conn:
        adapter = adapter_danych(conn, args.engine)
    try:
        with psycopg.connect(url, row_factory=dict_row) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT i.id, i.symbol FROM ref.instrument i JOIN ref.venue v ON v.id=i.venue_id"
                " WHERE v.code = %s AND i.active ORDER BY i.symbol", (adapter.venue_code,))
            instruments = cur.fetchall()
            print(f"Obserwowane instrumenty: {', '.join(i['symbol'] for i in instruments)}")

            pod_rzad = 0
            while True:
                t0 = datetime.now(timezone.utc)
                blad: str | None = None
                try:
                    s = await cycle(conn, adapter, instruments, args.depth)
                    print(f"[{t0:%H:%M:%S}] świece +{s['swiece']}, orderbooki {s['orderbooki']}, "
                          f"błędy {s['bledy']}")
                    if s["bledy"]:
                        # JEDNO NIEUDANE POBRANIE TO AWARIA CYKLU. Opisy
                        # poszczegolnych bledow poszly juz wyzej, do logu;
                        # tutaj liczy sie sam fakt, ze cykl nie zebral
                        # wszystkiego, co mial zebrac.
                        blad = (f"{s['bledy']} nieudanych pobrań na "
                                f"{len(instruments)} instrumentów")
                    else:
                        # PULS DOPIERO TU, po CALYM udanym cyklu. Bicie przed
                        # petla albo po pierwszym instrumencie meldowaloby
                        # zdrowie procesu, ktory zawiesil sie na drugim.
                        #
                        # Blad zapisu pulsu celowo NIE jest lapiany osobno
                        # (patrz `puls.bij`) - wpadnie do `except` nizej
                        # i policzy sie jako awaria cyklu, bo nia jest.
                        puls.bij(conn, puls_nazwa, args.interval,
                                 {"instrumenty": len(instruments),
                                  "swiece": s["swiece"],
                                  "orderbooki": s["orderbooki"],
                                  "glebokosc": args.depth})
                except Exception as e:                        # noqa: BLE001
                    conn.rollback()
                    blad = f"{type(e).__name__}: {e}"
                    print(f"[{t0:%H:%M:%S}] cykl przerwany: {blad}")

                if blad:
                    pod_rzad += 1
                    # `last_beat_at` NIE RUSZA SIE - rosna tylko kolumny awarii.
                    # Puls „zyje" przy pieciu awariach pod rzad jest gorszy niz
                    # jego brak: mowi czujce dokladnie to, co czujka chce
                    # uslyszec, w chwili gdy jest najbardziej nieprawdziwe.
                    puls.zglos_awarie(conn, puls_nazwa, args.interval, pod_rzad, blad)
                    print(f"[{t0:%H:%M:%S}] cykl nieudany ({pod_rzad}/{POD_RZAD_MAX})")
                    if pod_rzad >= POD_RZAD_MAX:
                        print(f"{POD_RZAD_MAX} cykli pod rząd zakończonych błędem — "
                              f"kończę proces, żeby restart był WIDOCZNY zamiast cichy")
                        return 1
                else:
                    pod_rzad = 0

                if args.once:
                    return 1 if blad else 0
                await asyncio.sleep(args.interval)
    finally:
        await adapter.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
