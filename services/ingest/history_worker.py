#!/usr/bin/env python3
"""Proces tla utrzymujacy biezaca historie: swiece godzinowe + stawki finansowania.

PETLA JEST TUTAJ, NIE W SHELLU.

Do 2026-09-07 pod `history` chodzil na komendzie
`sh -c "while true; do backfill.py || true; funding.py || true; sleep 1200; done"`.
Trzy skutki, kazdy zly i zaden widoczny:

  * `|| true` polykal KAZDY niezerowy wynik, wiec awaria pobierania nie
    restartowala kontenera ani nie podnosila licznika restartow. Pod stal
    "Running", k3s meldowal zdrowie, a historia mogla stac od dni,
  * proces startowal od nowa co cykl, wiec nie mial pojecia o awariach
    poprzednich - piec padnietych cykli z rzedu wygladalo tak samo jak piec
    udanych,
  * caly ksztalt byl "skryptem walajacym sie po VPS", tyle ze schowanym
    w charcie.

Teraz proces zyje wlasna petla, laczy sie z baza NA KAZDY CYKL i KONCZY SIE
po pieciu awariach pod rzad - bo restart poda widac, a wpis w logu nie.

Licznik awarii broni jednak wylacznie przed procesem, ktory PADA. Proces, ktory
STOI - zawieszony na gniezdzie bez limitu czasu albo na blokadzie w bazie - nie
konczy cyklu, wiec nie dolicza sie do zadnego licznika i nadal wyglada na
`Running`. Dlatego po kazdym UDANYM cyklu leci puls do `risk.worker_heartbeat`
(services/ingest/puls.py), a `dozor.py --co workery` porownuje jego wiek
z interwalem TEGO workera.

Dwa zadania w jednym procesie, bo sa jednym zadaniem: obraz historii, ktorego
potrzebuja cechy. Swiece 4h powstaja z agregacji 1h, wiec bez tego procesu
horyzont domyslny stanalby w miejscu; funding to jedyne zrodlo w zestawie,
ktore nie jest przetworzona cena.

Uzycie:
    DATABASE_URL=... python services/ingest/history_worker.py --interval 1200
    DATABASE_URL=... python services/ingest/history_worker.py --once
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
from psycopg.rows import dict_row

from services.ingest import backfill as swiece
from services.ingest import funding as stawki
from services.ingest import puls

# Nazwa w `risk.worker_heartbeat`. To ROLA w potoku, a nie egzemplarz procesu:
# `--engine` i `--timeframe` ida do `detail`, zeby zmiana argumentu w charcie
# nie zakladala nowego wiersza i nie zostawiala starego, ktory od tej chwili
# starzalby sie w nieskonczonosc jako „martwy worker".
PULS = "history"

# Ile awarii pod rzad konczy proces. Wypisanie bledu do logu NIE JEST
# widocznoscia - logu poda nikt nie czyta o trzeciej w nocy. Restart poda jest
# widoczny: rosnie licznik restartow, pod wpada w CrashLoopBackOff i widac to
# w `kubectl get pods` bez zagladania do czegokolwiek.
POD_RZAD_MAX = 5


async def cykl(url: str, *, engine: str, timeframe: str, days: int,
               verbose: bool, interwal_s: int, pod_rzad: int
               ) -> tuple[int, int, list[str]]:
    """Jeden cykl: swiece, potem funding, na SWIEZYM polaczeniu.

    POLACZENIE NA KAZDY CYKL, NIE NA CALY PROCES.

    Trzymane na caly proces po restarcie bazy zrywa sie raz i juz sie nie
    naprawia: kazdy kolejny cykl pada na tym samym martwym gniezdzie, a proces
    idzie dalej w nieskonczonosc - martwy i wygladajacy na zywy. Koszt jednego
    polaczenia na dwadziescia minut jest przy tym zaden.

    OKNO LICZONE TU, A NIE PRZY STARCIE: `since` wyliczone raz przed petla
    zamarzloby w chwili startu procesu i po tygodniu pracy proces dociagalby
    okno sprzed tygodnia.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    bledy: list[str] = []
    ile_swiec = ile_stawek = 0

    with psycopg.connect(url, row_factory=dict_row) as conn:
        # Kazde zadanie we wlasnym `try`: awaria fundingu nie moze skasowac
        # pobranych przed chwila swiec ani odwrotnie. To jedyne, co zostaje po
        # `|| true` - z ta roznica, ze blad nie znika, tylko wraca do licznika.
        try:
            ile_swiec = await swiece.przebieg(conn, engine=engine,
                                              timeframe=timeframe, since=since)
        except Exception as e:                                # noqa: BLE001
            conn.rollback()
            bledy.append(f"swiece {timeframe}: {type(e).__name__}: {e}")

        try:
            ile_stawek, rynkow, bledy_rynkow = await stawki.przebieg(
                conn, engine=engine, since=since)
            # Nieudany pojedynczy rynek tez jest awaria cyklu. Bez tego jeden
            # instrument, ktorego gielda nie zna, moglby milczec miesiacami.
            bledy += [f"funding {b}" for b in bledy_rynkow]
            if verbose:
                print(f"  funding: {rynkow} rynków")
        except Exception as e:                                # noqa: BLE001
            conn.rollback()
            bledy.append(f"funding: {type(e).__name__}: {e}")

        # PULS TUTAJ, A NIE W `main`: polaczenie zyje tylko w tym bloku, wiec
        # zapis pulsu z zewnatrz wymagalby drugiego polaczenia - czyli sprawdzal
        # baze, ktorej cykl juz nie uzywa.
        #
        # Warunek jest DOKLADNIE tym samym warunkiem, ktory nizej zeruje licznik
        # `pod_rzad`: pusta lista bledow. Druga definicja awarii „na potrzeby
        # pulsu" rozjechalaby czujke z licznikiem restartow tego samego procesu.
        if bledy:
            puls.zglos_awarie(conn, PULS, interwal_s, pod_rzad + 1, bledy[0])
        else:
            # Blad zapisu pulsu NIE JEST tu lapiany - patrz `puls.bij`. Wpadnie
            # do `except` w `main` i zostanie policzony jako awaria cyklu, bo
            # nim jest: worker, ktorego czujka nie widzi, jest dla nadzoru
            # nieodrozninalny od workera, ktorego nie ma.
            puls.bij(conn, PULS, interwal_s,
                     {"engine": engine, "timeframe": timeframe, "dni_okna": days,
                      "swiece": ile_swiec, "stawki": ile_stawek})

    return ile_swiec, ile_stawek, bledy


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=1200,
                    help="przerwa miedzy cyklami (s)")
    ap.add_argument("--once", action="store_true", help="jeden cykl zamiast petli")
    ap.add_argument("--days", type=int, default=3,
                    help="jak gleboko wstecz domykac dziury w kazdym cyklu")
    ap.add_argument("--timeframe", default="1h", choices=sorted(swiece.TF_MINUTES))
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--quiet", action="store_true",
                    help="wycisz rutynowy postep; OPISY BLEDOW ida zawsze")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    print(f"Historia: świece {args.timeframe} i funding, okno {args.days} dni, "
          f"co {args.interval} s")

    pod_rzad = 0
    while True:
        t0 = datetime.now(timezone.utc)
        try:
            ile_swiec, ile_stawek, bledy = await cykl(
                url, engine=args.engine, timeframe=args.timeframe,
                days=args.days, verbose=not args.quiet,
                interwal_s=args.interval, pod_rzad=pod_rzad)
        except Exception as e:                                # noqa: BLE001
            # Tu wpada juz tylko to, czego nie da sie przypisac do zadania -
            # przede wszystkim nieudane POLACZENIE z baza.
            ile_swiec = ile_stawek = 0
            bledy = [f"{type(e).__name__}: {e}"]

        if not args.quiet:
            print(f"[{t0:%H:%M:%S}] świece +{ile_swiec:,}, stawki +{ile_stawek:,}, "
                  f"błędów {len(bledy)}")
        # OPISY BLEDOW WYPISUJEMY ZAWSZE, TAKZE PRZY --quiet. `--quiet` istnieje
        # po to, zeby wyciszyc rutynowy postep, a nie awarie. Sam licznik bez
        # opisu juz raz ukryl serie odmow gieldy.
        for blad in dict.fromkeys(bledy):
            print(f"    BŁĄD {blad}")

        if bledy:
            pod_rzad += 1
            print(f"[{t0:%H:%M:%S}] cykl nieudany ({pod_rzad}/{POD_RZAD_MAX})")
            if pod_rzad >= POD_RZAD_MAX:
                print(f"{POD_RZAD_MAX} cykli pod rząd zakończonych błędem — "
                      f"kończę proces, żeby restart był WIDOCZNY zamiast cichy")
                return 1
        else:
            pod_rzad = 0

        if args.once:
            return 1 if bledy else 0
        await asyncio.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
