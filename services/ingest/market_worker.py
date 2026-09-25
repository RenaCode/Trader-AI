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

SYMBOL, KTOREGO GIELDA NIE LISTUJE, TO NIE JEST AWARIA - ALE TEZ NIE ZERO (2026-09-25)

Zdanie wyzej („instrument, ktorego gielda nie zna, moglby milczec miesiacami")
bylo sluszne dopoki „gielda nie zna" i „odczyt sie nie udal" byly nierozroznialne.
Od 24.09 przestaly byc, i to kosztowalo 242 restarty: `fetch_order_book` przeszlo
na ksiege podmiotu unijnego, ktory jako podmiot MiCA nie listuje ANI JEDNEJ pary
*USDT, a ten worker zbiera arkusz dla wszystkich instrumentow `active` - 10 par
USDC i 24 pary USDT. Dwadziescia twardych bledow na cykl, piec cykli, koniec
procesu; i tak w kolko, bo restart niczego tu nie naprawia.

Rozroznienie idzie wiec przez typ wyjatku, nie przez wylaczenie licznika:

    SymbolNieListowany  -> stan TRWALY i ZNANY. Cykl konczy sie POWODZENIEM,
                           a liczba takich symboli idzie do `detail` pulsu jako
                           `bez_arkusza`.
    ExchangeError       -> odczyt sie NIE UDAL (siec, klucz, limit, nieznany kod).
                           Bez zmian: awaria cyklu, licznik, piec i koniec.

DLACZEGO `bez_arkusza` MUSI ISC DO BAZY, A NIE TYLKO DO LOGU. Bo inaczej ta
poprawka zamienia awarie w CISZE, czyli w gorszy stan: log ginie przy restarcie,
a liczba rosnaca albo utrzymujaca sie ma byc odczytywalna bez zagladania do poda -
`SELECT detail->>'bez_arkusza' FROM risk.worker_heartbeat WHERE worker='market'`.

KTORA GIELDE PYTAMY O ARKUSZ: `ref.instrument.tradeable` (migracja 0020), czyli
ta sama flaga, ktora wyznacza uniwersum decyzyjne.

    tradeable=true  -> ksiega EGZEKUCJI. Arkusz zasila rachunek kosztu
                       (`koszty.karta_rundy`), a kosztu nie opisuje ksiega,
                       w ktorej nasze zlecenie nigdy nie stanie.
    tradeable=false -> host globalny, czyli zachowanie sprzed cd77710. To nie
                       jest ustepstwo: wartosci `book_spread_bps` tych
                       instrumentow sprzed 24.09 tez pochodza stamtad, wiec
                       szereg zostaje ciagly.

Proweniencja nie potrzebuje kolumny w `market.book_snapshot`: wynika z flagi,
ktora o niej zdecydowala i ktora stoi w bazie obok instrumentu.

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

from services.exchanges.base import ExchangeError, SymbolNieListowany
from services.exchanges.fabryka import adapter_danych
from services.ingest import puls

# Ile cykli pod rzad zakonczonych bledem konczy proces - ta sama stala co
# w history_worker, features i news_worker. Rozna liczba w kazdym module
# znaczylaby, ze „piec awarii" jest wynikiem przypadku, a nie decyzji.
POD_RZAD_MAX = 5

# Nazwa w `risk.worker_heartbeat` - ROLA w potoku, nie egzemplarz procesu.
PULS = "market"

# CO ILE SEKUND CZYTAMY LISTE INSTRUMENTOW OD NOWA.
#
# Do 2026-09-25 lista byla czytana RAZ, przed petla `while True`, wiec instrument
# wylaczony w bazie byl odpytywany az do restartu poda - a pod procesu tla nie
# restartuje sie sam. To dotyczy obu kierunkow zmiany i drugi jest gorszy:
# instrument DOPISANY do bazy nie zbieral danych, dopoki ktos nie zauwazyl, ze
# ich nie ma.
#
# 900 s, a nie `interval` (60 s) ani doba:
#   * lista zmienia sie migracja albo przebiegiem `instrumenty.py`, czyli
#     w skali godzin - czytanie jej co cykl byloby 1440 zapytaniami na dobe
#     o odpowiedz, ktora sie nie zmienia,
#   * kwadrans to gorna granica rozjazdu, ktora da sie przezyc: wylaczony
#     instrument jest odpytywany najwyzej 15 zapytan dluzej niz trzeba,
#   * ta sama liczba co `workers.features` w values.yaml i `ODSWIEZANIE` nie
#     jest wiec trzecia niezalezna stala do rozjechania - rytm zmian danych
#     referencyjnych jest w tym projekcie jeden.
ODSWIEZANIE_LISTY_S = 900


def czytaj_instrumenty(cur, venue_code: str) -> list[dict]:
    """Instrumenty tej gieldy, dla ktorych zbieramy dane.

    `tradeable` wchodzi do SELECT-a, bo wyznacza KSIEGE, ktorej pytamy o arkusz
    (patrz naglowek). Filtrem nadal jest `active`: historie zbieramy dla
    wszystkiego, co aktywne, takze dla par nieobjetych uniwersum decyzyjnym.

    OSOBNA FUNKCJA, bo zapytanie jest wolane w DWOCH miejscach - raz przed petla
    i potem co `ODSWIEZANIE_LISTY_S`. Dwie kopie tego samego SELECT-a rozjechalyby
    sie cicho: po odswiezeniu lista mialaby inny sklad niz przy starcie, a jedyny
    objaw bylby taki, ze arkusz idzie z innej ksiegi niz poprzednio.
    """
    cur.execute(
        "SELECT i.id, i.symbol, i.tradeable FROM ref.instrument i"
        " JOIN ref.venue v ON v.id=i.venue_id"
        " WHERE v.code = %s AND i.active ORDER BY i.symbol", (venue_code,))
    return cur.fetchall()


async def cycle(conn, adapter, instruments: list[dict], book_depth: int) -> dict:
    cur = conn.cursor()
    # `bez_arkusza` JEST OSOBNA KOLUMNA TEGO RACHUNKU, nie czescia `bledy`.
    # Zlepienie ich konczy proces (patrz naglowek), rozdzielenie bez licznika
    # zamienia to w cisze - stad trzecia liczba, ktora idzie do pulsu.
    stats = {"swiece": 0, "orderbooki": 0, "bledy": 0, "bez_arkusza": 0}
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
            # KTORA KSIEGA - decyduje `tradeable`, nie sufiks symbolu. Zgadywanie
            # po „USDC" dzialaloby do pierwszego listingu pary USDC, ktorej
            # jeszcze nie handlujemy, i do pierwszej giedly z inna waluta
            # rozliczeniowa - a mylilo by sie cicho, bo arkusz i tak by przyszedl.
            book = await adapter.fetch_order_book(
                inst["symbol"], book_depth,
                ksiega_egzekucji=bool(inst["tradeable"]))
        except SymbolNieListowany as e:
            # NIE JEST TO AWARIA CYKLU: ta gielda tego symbolu nie prowadzi
            # i nie bedzie prowadzic ani za minute, ani za miesiac. Liczy sie
            # jednak do `bez_arkusza`, ktore idzie do pulsu - inaczej trwaly
            # brak danych stalby sie niewidoczny.
            stats["bez_arkusza"] += 1
            print(f"  {inst['symbol']:10s} orderbook: {e}")
            continue
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
        # LICZYMY ZAPISANE WIERSZE, NIE UDANE ODPYTANIA.
        #
        # Do 2026-09-25 stalo tu `+= 1` bezwarunkowo, czyli licznik rosl takze
        # wtedy, gdy `ON CONFLICT DO NOTHING` nie wstawil niczego. Klucz glowny
        # `market.book_snapshot` to (instrument_id, event_time) - migracja 0002 -
        # a `event_time` bierze sie ze ZNACZNIKA OFERTY, nie z chwili odpytania.
        # Gielda, ktora oddaje te sama oferte co minute (poza sesja Alpaca oddaje
        # ostatnia znana), dawala wiec log meldujacy "orderbooki 21" przy ZERU
        # zapisanych wierszy - i ta sama liczba szla do pulsu, czyli do bazy.
        #
        # To jest ten sam ksztalt wady, ktory ten plik tropi w naglowku: melduje
        # postep, ktorego nie ma. Swiece byly liczone poprawnie (`cur.rowcount`)
        # juz wyzej, wiec rozjazd byl miedzy dwiema liniami w jednej funkcji.
        #
        # `if > 0`, bo `rowcount` przy niektorych sterownikach potrafi byc -1
        # dla polecenia, ktore nie raportuje liczby wierszy - ta sama ostroznosc
        # co przy swiecach.
        stats["orderbooki"] += cur.rowcount if cur.rowcount > 0 else 0
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
            instruments = czytaj_instrumenty(cur, adapter.venue_code)
            print(f"Obserwowane instrumenty: {', '.join(i['symbol'] for i in instruments)}")

            pod_rzad = 0
            lista_z = datetime.now(timezone.utc)
            while True:
                t0 = datetime.now(timezone.utc)
                blad: str | None = None
                try:
                    # LISTA INSTRUMENTOW ODSWIEZANA W TRAKCIE PRACY.
                    #
                    # Do 2026-09-25 czytala sie RAZ, przed ta petla, wiec zmiana
                    # w `ref.instrument` dzialala dopiero po restarcie poda -
                    # a pod procesu tla nie restartuje sie sam. Instrument
                    # wylaczony byl odpytywany dalej, a instrument DOPISANY nie
                    # zbieral danych, dopoki ktos nie zauwazyl, ze ich nie ma.
                    #
                    # ODCZYT JEST W TYM SAMYM `try`, co cykl, i to nie jest
                    # przypadek: zapytanie do bazy moze sie wywrocic, a awaria
                    # odswiezenia listy jest awaria cyklu - inaczej proces
                    # chodzilby na starej liscie, milczac o tym, ze nie umie jej
                    # odczytac.
                    if (t0 - lista_z).total_seconds() >= ODSWIEZANIE_LISTY_S:
                        nowa = czytaj_instrumenty(cur, adapter.venue_code)
                        lista_z = t0
                        stare = {i["symbol"] for i in instruments}
                        nowe = {i["symbol"] for i in nowa}
                        # ROZNICE WYPISUJEMY Z NAZWY. Sama zmiana liczby nie mowi,
                        # ktory instrument przyszedl, a ktory odszedl - a to jest
                        # jedyny slad, po ktorym da sie potem odpowiedziec, od
                        # kiedy dany szereg sie konczy albo zaczyna.
                        if nowe - stare:
                            print(f"[{t0:%H:%M:%S}] doszły: "
                                  f"{', '.join(sorted(nowe - stare))}")
                        if stare - nowe:
                            print(f"[{t0:%H:%M:%S}] odeszły: "
                                  f"{', '.join(sorted(stare - nowe))}")
                        # Podmiana TAKZE gdy zbior symboli sie nie zmienil:
                        # `tradeable` moglo sie przestawic przy tym samym
                        # skladzie, a ta flaga wyznacza ksiege, ktorej pytamy
                        # o arkusz.
                        instruments = nowa
                    s = await cycle(conn, adapter, instruments, args.depth)
                    print(f"[{t0:%H:%M:%S}] świece +{s['swiece']}, orderbooki {s['orderbooki']}, "
                          f"bez arkusza {s['bez_arkusza']}, błędy {s['bledy']}")
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
                        #
                        # `bez_arkusza` JEST TU CZESCIA PULSU, A NIE OSOBNYM
                        # WPISEM. Cykl policzyl sie jako udany, wiec bez tej
                        # liczby baza twierdzilaby, ze zebrano wszystko - a to
                        # zamienia awarie w cisze. Log nie wystarcza: ginie przy
                        # restarcie, a ta liczba ma byc odczytywalna bez poda.
                        puls.bij(conn, puls_nazwa, args.interval,
                                 {"instrumenty": len(instruments),
                                  "swiece": s["swiece"],
                                  "orderbooki": s["orderbooki"],
                                  "bez_arkusza": s["bez_arkusza"],
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
