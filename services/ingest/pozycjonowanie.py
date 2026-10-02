#!/usr/bin/env python3
"""Pobieranie pozycjonowania tłumu: udział kont długich i otwarte pozycje.

SKĄD TO SIĘ WZIĘŁO. Pytanie brzmiało „czy kopiować ruchy najlepszych traderów".
Kopiować się nie da: Bybit nie wystawia copy tradingu w API v5, jego wewnętrzny
ranking odpowiada 403, a podmiot unijny, na którym handlujemy, nie ma
perpetuali w ogóle. Nie warto też: ranking „najlepszych" to prawy ogon rozkładu
i przy dziesiątkach tysięcy uczestników czołówka jest zdominowana przez
szczęście pomnożone przez dźwignię. Wybór po fakcie, bez próby out-of-sample,
to dokładnie ten błąd, przeciwko któremu zbudowany jest cały ten system.

Wartościowa jest ZAGREGOWANA informacja o ustawieniu tłumu — bez wybierania
zwycięzców. To ta sama rodzina co `market.funding_rate` i z tego samego powodu:
jest to jedno z niewielu źródeł, które nie jest przetworzoną ceną, więc jako
jedno z niewielu może wnieść informację, której w cenie jeszcze nie ma.

CO TO ZNACZY, A CZEGO NIE ZNACZY. `long_ratio` to udział KONT, nie kapitału —
jeden duży gracz waży tyle samo co jeden mały. Otwarte pozycje dotyczą
perpetuala kwotowanego w USDT, a my handlujemy spotem w USDC: to jest sygnał
o TYM SAMYM aktywie z innego rynku, nie pozycja do odwzorowania.

TO NIE JEST DECYZJA HANDLOWA. Ten moduł wyłącznie zbiera. Czy z tych liczb
płynie przewaga, rozstrzyga trener walk-forward na danych spoza próby — i do
tego czasu nic tu nie wpływa na ani jedno zlecenie.

Użycie:
    DATABASE_URL=... python services/ingest/pozycjonowanie.py --days 600
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

from pathlib import Path

import psycopg
from psycopg.rows import dict_row

# Korzen repozytorium na sciezce importu. Uruchomienie `python
# services/ingest/pozycjonowanie.py` ustawia sys.path[0] na katalog SKRYPTU,
# nie na korzen - bez tego `import services.*` konczy sie
# ModuleNotFoundError dopiero przy uruchomieniu, mimo ze testy przechodza.
# Tak samo robia funding.py, instrumenty.py i history_worker.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.exchanges.base import ExchangeError  # noqa: E402
from services.exchanges.bybit import BybitAdapter  # noqa: E402
from services.ingest.funding import (  # noqa: E402
    ensure_perp, missing_ranges, odczekaj_albo_przerwij,
)

# Siatka godzinowa - taka, jaką oddaje giełda dla obu tych wielkości.
INTERVAL_H = 1
# Ile godzin na jedno zapytanie. `open-interest` przyjmuje najwyżej 200 wierszy,
# więc to ono wyznacza krok - pytanie o więcej dałoby ciszę w połowie okna.
PAGE = 200


async def _pobierz_okno(adapter, conn, cur, perp_id: int, symbol: str,
                        start: datetime, stop: datetime, now: datetime) -> int:
    total = 0
    proby = 0
    while start < stop:
        end = min(start + timedelta(hours=INTERVAL_H * PAGE), stop)
        try:
            rows = await adapter.fetch_positioning(symbol, start, end)
        except ExchangeError as e:
            if not e.retryable:
                raise
            # Z licznikiem - patrz `funding.odczekaj_albo_przerwij`.
            proby += 1
            await odczekaj_albo_przerwij(e, symbol, proby, start, "pozycjonowania")
            continue
        proby = 0

        if rows:
            # ON CONFLICT DO UPDATE, a nie DO NOTHING: dwa źródła mogą przyjść
            # w różnych przebiegach (jedno zawiodło, drugie nie), więc drugi
            # przebieg ma UZUPEŁNIĆ brakującą kolumnę. `coalesce` z EXCLUDED
            # pilnuje, żeby uzupełnianie nigdy nie skasowało tego, co już jest.
            cur.executemany(
                "INSERT INTO market.positioning"
                " (instrument_id, event_time, long_ratio, open_interest, ingested_at)"
                " VALUES (%s,%s,%s,%s,%s)"
                " ON CONFLICT (instrument_id, event_time) DO UPDATE SET"
                "   long_ratio = coalesce(EXCLUDED.long_ratio,"
                "                         market.positioning.long_ratio),"
                "   open_interest = coalesce(EXCLUDED.open_interest,"
                "                            market.positioning.open_interest)",
                [(perp_id, t, u, oi, now) for t, u, oi in rows],
            )
            conn.commit()
            total += len(rows)
            # Od ostatniego OTRZYMANEGO znacznika, nie od końca okna: przy
            # niepełnej stronie przeskoczylibyśmy dziurę (ta sama pułapka co
            # w `funding._fetch_range`).
            start = rows[-1][0] + timedelta(hours=INTERVAL_H)
        else:
            start = end
        await asyncio.sleep(0.05)

    return total


async def backfill(adapter, conn, perp_id: int, symbol: str,
                   since: datetime) -> int:
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT min(event_time) AS lo, max(event_time) AS hi"
                " FROM market.positioning WHERE instrument_id=%s", (perp_id,))
    row = cur.fetchone()
    now = datetime.now(timezone.utc)
    step = timedelta(hours=INTERVAL_H)
    total = 0

    # `missing_ranges` liczy OBA brzegi - dziurę przed najstarszym rekordem
    # i ogon po najnowszym. Wznawianie wyłącznie od najnowszego wygląda
    # rozsądnie i jest pułapką, opisaną przy finansowaniu: świeży ogonek
    # wstawiony przez inny proces sprawia, że pełny backfill kończy się zerem.
    for start, stop in missing_ranges(row["lo"], row["hi"], since, now, step):
        total += await _pobierz_okno(adapter, conn, cur, perp_id, symbol,
                                     start, stop, now)

    print(f"  {symbol:12s} {total:>7,} godzin")
    return total


async def przebieg(conn, *, engine: str, since: datetime) -> tuple[int, int, list[str]]:
    """Jeden pełny przebieg na GOTOWYM połączeniu.

    Zwraca (wiersze, liczba rynków, opisy błędów). Błędy WRACAJĄ do wołającego,
    a nie są tylko wypisane: proces tła musi umieć policzyć nieudane cykle pod
    rząd, a licznik zbudowany na treści logu nie istnieje.
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
    # Po AKTYWIE, nie po instrumencie: BTCUSDT i BTCUSDC dzielą jeden perpetual,
    # więc pobieranie dwa razy byłoby tą samą pracą i drugim zapisem tej samej
    # prawdy.
    zrobione: set[str] = set()
    try:
        for spot in spots:
            perp_id, rynek = ensure_perp(cur, spot)
            conn.commit()
            if rynek in zrobione:
                continue
            zrobione.add(rynek)
            # Błąd JEDNEGO rynku nie może zatrzymać pozostałych.
            try:
                grand += await backfill(adapter, conn, perp_id, rynek, since)
            except ExchangeError as e:
                bledy.append(f"{rynek}: {e}")
    finally:
        await adapter.close()

    return grand, len(zrobione), bledy


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=600)
    ap.add_argument("--engine", default="crypto")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    print(f"Pozycjonowanie od {since:%Y-%m-%d}\n")

    with psycopg.connect(url, row_factory=dict_row) as conn:
        grand, rynkow, bledy = await przebieg(conn, engine=args.engine, since=since)

    print(f"\nRazem: {grand:,} godzin z {rynkow} rynków")
    if bledy:
        print(f"Nieudane ({len(bledy)}): " + "; ".join(bledy))
    # NIEUDANY RYNEK MUSI DAC NIEZEROWY KOD WYJSCIA - inaczej wołający shell
    # widzi powodzenie po przebiegu, w którym nie udało się nic.
    return 1 if bledy else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
