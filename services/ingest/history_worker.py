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
from services.ingest import pozycjonowanie as tlum
from services.ingest import puls

# Nazwa w `risk.worker_heartbeat`. To ROLA w potoku, a nie egzemplarz procesu:
# `--engine` i `--timeframe` ida do `detail`, zeby zmiana argumentu w charcie
# nie zakladala nowego wiersza i nie zostawiala starego, ktory od tej chwili
# starzalby sie w nieskonczonosc jako „martwy worker".
PULS = "history"


def _puls(engine: str) -> str:
    """Nazwa pulsu: `history` dla krypto (zgodnosc z czujka), `history-<silnik>` dalej."""
    return PULS if engine == "crypto" else f"{PULS}-{engine}"


class _Pominiete(Exception):
    """Krok, ktory dla tego silnika nie istnieje (funding poza krypto)."""

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

        # FUNDING I POZYCJONOWANIE TO ZJAWISKA PERPETUALI. Akcje nie maja
        # stawki finansowania ani long/short ratio, wiec dla `equity` oba kroki
        # sa POMIJANE JAWNIE - a nie wykonywane z zerowym wynikiem, ktory
        # wygladalby jak "pobrano 0 rynkow" i mylil czujke pulsu.
        krypto = engine == "crypto"
        ile_tlumu = rynkow = rynkow_t = 0
        try:
            if not krypto:
                raise _Pominiete
            ile_stawek, rynkow, bledy_rynkow = await stawki.przebieg(
                conn, engine=engine, since=since)
            # Nieudany pojedynczy rynek tez jest awaria cyklu. Bez tego jeden
            # instrument, ktorego gielda nie zna, moglby milczec miesiacami.
            bledy += [f"funding {b}" for b in bledy_rynkow]
            if verbose:
                print(f"  funding: {rynkow} rynków")
        except _Pominiete:
            pass
        except Exception as e:                                # noqa: BLE001
            conn.rollback()
            bledy.append(f"funding: {type(e).__name__}: {e}")

        # POZYCJONOWANIE TLUMU - trzecie zadanie w tym samym cyklu i z tego
        # samego powodu co funding: opisuje ustawienie innych uczestnikow, wiec
        # nie jest przetworzona cena. Wlasny `try`, bo awaria tego zrodla nie
        # moze skasowac swiec ani stawek pobranych przed chwila.
        #
        # NIE WPLYWA NA ANI JEDNO ZLECENIE. To jest zbieranie danych; czy z tych
        # liczb plynie przewaga, rozstrzyga trener walk-forward.
        try:
            if not krypto:
                raise _Pominiete
            ile_tlumu, rynkow_t, bledy_t = await tlum.przebieg(
                conn, engine=engine, since=since)
            bledy += [f"pozycjonowanie {b}" for b in bledy_t]
            if verbose:
                print(f"  pozycjonowanie: {rynkow_t} rynków, {ile_tlumu} godzin")
        except _Pominiete:
            pass
        except Exception as e:                                # noqa: BLE001
            conn.rollback()
            bledy.append(f"pozycjonowanie: {type(e).__name__}: {e}")

        # PULS TUTAJ, A NIE W `main`: polaczenie zyje tylko w tym bloku, wiec
        # zapis pulsu z zewnatrz wymagalby drugiego polaczenia - czyli sprawdzal
        # baze, ktorej cykl juz nie uzywa.
        #
        # Warunek jest DOKLADNIE tym samym warunkiem, ktory nizej zeruje licznik
        # `pod_rzad`: pusta lista bledow. Druga definicja awarii „na potrzeby
        # pulsu" rozjechalaby czujke z licznikiem restartow tego samego procesu.
        if bledy:
            puls.zglos_awarie(conn, _puls(engine), interwal_s, pod_rzad + 1, bledy[0])
        else:
            # Blad zapisu pulsu NIE JEST tu lapiany - patrz `puls.bij`. Wpadnie
            # do `except` w `main` i zostanie policzony jako awaria cyklu, bo
            # nim jest: worker, ktorego czujka nie widzi, jest dla nadzoru
            # nieodrozninalny od workera, ktorego nie ma.
            puls.bij(conn, _puls(engine), interwal_s,
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
    # LIMIT CZASU CALEGO CYKLU - tak samo jak w `news_worker` (--limit-cyklu).
    #
    # Licznik `POD_RZAD_MAX` broni wylacznie przed procesem, ktory PADA. Proces,
    # ktory STOI - zawieszony na gniezdzie, na blokadzie w bazie albo (do
    # 2026-09-23) w petli ponowien `backfill._fetch_range` bez wyjscia - nigdy
    # nie konczy cyklu, wiec nie dolicza sie do zadnego licznika i NIE BIJE ANI
    # NIE ZGLASZA PULSU. Nadzor widzi „Running" i nic wiecej. Docstring tego
    # modulu nazywa ten stan wprost, a kod go dopuszczal.
    #
    # 3600 s, a nie „dwa interwaly": cykl po awarii jest dluzszy od cyklu przy
    # czystym starcie (dziura do domkniecia rosnie z czasem postoju), wiec limit
    # liczony dla zdrowego przebiegu ucinalby wlasnie ten cykl, ktory nadrabia.
    # Zdrowy cykl `--days 3` na swiecach 1h to sekundy - godzina jest o dwa rzedy
    # wielkosci za duza na prace i o rzad za mala na zawieszenie.
    ap.add_argument("--limit-cyklu", type=int, default=3600,
                    help="twardy limit czasu jednego cyklu (s)")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    print(f"Historia: świece {args.timeframe} i funding, okno {args.days} dni, "
          f"co {args.interval} s")

    pod_rzad = 0
    puls.zyje(args.interval)
    while True:
        t0 = datetime.now(timezone.utc)
        try:
            # Limit obejmuje CALY cykl, nie pojedyncze zapytanie do gieldy.
            # Timeout na kliencie HTTP chroni przed jednym zawieszonym zadaniem;
            # ten limit chroni przed wszystkim innym, co moze stanac - takze
            # przed kodem, ktorego jeszcze nie ma. `TimeoutError` wpada do
            # `except` nizej i staje sie zwykla awaria cyklu: idzie do pulsu,
            # do licznika `pod_rzad` i do logu.
            async with asyncio.timeout(args.limit_cyklu):
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

        # Obrot petli, takze nieudany - sonda zywotnosci (`puls.zyje`).
        puls.zyje(args.interval)
        if args.once:
            return 1 if bledy else 0
        await asyncio.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
