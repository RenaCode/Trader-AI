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
    # `tradeable` PODANE JAWNIE, a nie zdane na DEFAULT.
    #
    # Kolumna ma `DEFAULT true` (migracja 0020), a ten INSERT jej nie wymieniał -
    # czyli każdy NOWY perpetual wchodził do bazy z flagą uniwersum decyzyjnego
    # WŁĄCZONĄ. Że dziś to nie boli, wynika wyłącznie z tego, że od migracji 0020
    # nie powstał ani jeden taki wiersz: wszystkie perpetuale w bazie są starsze
    # i mają `tradeable = false` po migracji. To zbieg okoliczności, nie
    # zabezpieczenie - wystarczy jeden nowy instrument spotowy z aktywem bez
    # perpetuala w bazie, żeby wiersz powstał.
    #
    # Czym by to groziło: perpetual jest tu WYŁĄCZNIE źródłem stawki
    # finansowania (`meta.funding_from`) i nikt go nie obstawia. Z `tradeable =
    # true` wchodziłby do uniwersum decyzyjnego, którym silnik handluje i po
    # którym liczy się odcisk cechy przekrojowej - a jego zwrot jest niemal
    # identyczny ze zwrotem pary spotowej tego samego aktywa, więc liczony
    # podwójnie przesuwałby percentyle całego zbioru.
    #
    # `active` zostaje `false` z tego samego powodu i było podane jawnie od
    # początku; obie flagi opisują teraz jedną decyzję, nie jedną decyzję i jedno
    # przemilczenie.
    #
    # NIE ZMIENIA ISTNIEJĄCYCH WIERSZY: gałąź `DO UPDATE` dotyka wyłącznie
    # `kind`, tak jak dotąd. Przestawianie `tradeable` istniejącemu instrumentowi
    # byłoby zmianą uniwersum decyzyjnego przez potok danych, a to należy do
    # migracji.
    cur.execute(
        "INSERT INTO ref.instrument (venue_id, symbol, engine, base_ccy, quote_ccy,"
        " kind, active, tradeable)"
        " VALUES (%s,%s,%s,%s,'USDT','perp',false,false)"
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
    # Kursor z wlasnym ksztaltem wiersza: ponizej czytamy po nazwie kolumny,
    # a polaczenie moze przyjsc od wywolujacego (history_worker) z dowolna
    # fabryka. Odziedziczenie krotek zamienialoby ten odczyt w TypeError
    # dopiero na produkcji.
    cur = conn.cursor(row_factory=dict_row)
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


async def przebieg(conn, *, engine: str, since: datetime) -> tuple[int, int, list[str]]:
    """Jeden pelny przebieg pobierania stawek na GOTOWYM polaczeniu.

    Zwraca (stawki, liczba rynkow, opisy bledow). Bledy WRACAJA do
    wywolujacego zamiast byc tylko wypisane: proces tla musi umiec policzyc
    nieudane cykle pod rzad, a licznik zbudowany na tresci logu nie istnieje.
    """
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        "SELECT i.id, i.venue_id, i.symbol, i.engine, i.base_ccy, i.quote_ccy"
        " FROM ref.instrument i JOIN ref.venue v ON v.id=i.venue_id"
        " WHERE i.engine=%s AND i.active AND i.kind='spot' AND v.code='bybit'"
        " ORDER BY i.symbol", (engine,))
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

    return grand, len(zrobione), bledy


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
        grand, rynkow, bledy = await przebieg(conn, engine=args.engine, since=since)

    print(f"\nRazem: {grand:,} stawek z {rynkow} rynków")
    if bledy:
        print(f"Nieudane ({len(bledy)}): " + "; ".join(bledy))
    # NIEUDANY RYNEK MUSI DAC NIEZEROWY KOD WYJSCIA. Do 2026-09-07 przebieg,
    # w ktorym kazdy rynek zawiodl, konczyl sie zerem - a wolajacy go shell
    # i tak polykal wynik przez `|| true`. Teraz kod wyjscia cos znaczy.
    return 1 if bledy else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
