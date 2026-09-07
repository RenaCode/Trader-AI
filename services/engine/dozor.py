#!/usr/bin/env python3
"""Czuwanie nad stanem nauki: odzywa sie WYLACZNIE, gdy cos sie zmienilo.

DLACZEGO TO NIE JEST ZADANIE CYKLICZNE OD POWIADOMIEN

Kusi, zeby po kazdym nocnym treningu wysylac podsumowanie. To bylby najgorszy
mozliwy projekt: powiadomienie, ktore przychodzi zawsze, przestaje cokolwiek
znaczyc, a odbiorca uczy sie je pomijac - wlasnie wtedy, gdy przyjdzie to jedno
wazne.

Ten projekt zmierzyl u siebie, jak wyglada szum: cztery kolejne przebiegi tego
samego kodu, rozniace sie wylacznie przesunieciem okna o dzien, daly

    52,5%  ->  57,5%  ->  60,0%  ->  62,5%

czyli dziesiec punktow bez zadnej zmiany. Dobowa roznica odsetka NIE JEST wiec
zdarzeniem. Zdarzeniem jest dopiero wyjscie poza przedzial ufnosci poprzedniego
przebiegu - i to jest tu progiem, a nie liczba dobrana na oko.

CO UZNAJEMY ZA ZMIANE ISTOTNA

  1. zmiana statusu (learning / validated / degraded) - zawsze,
  2. odsetek dodatnich okien POZA przedzialem ufnosci poprzedniego przebiegu,
  3. ubytek okien informatywnych o cwierc lub wiecej - to ubytek DOWODOW,
     zwykle oznaczajacy, ze kalibracja znalazla uklad, ktory prawie nie handluje,
  4. zmiana znaku mediany Sharpe,
  5. dryf cech powyzej progu, gdy jednoczesnie urosl.

Wszystko inne to cisza - wypisana w logu razem z powodem, zeby dalo sie
sprawdzic, ze dozor dziala, a nie zamilkl.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Prog dryfu. Powyzej niego rozklad cech przestaje przypominac ten, na ktorym
# model byl uczony - patrz learn.drift_metric.
PSI_PROG = 1.0
# Ubytek okien informatywnych, ktory traktujemy jako utrate dowodow.
UBYTEK_OKIEN = 0.25


def zmiany(poprzedni: dict | None, biezacy: dict) -> list[str]:
    """Lista powodow, dla ktorych warto sie odezwac. Pusta = cisza.

    Funkcja jest czysta i to jest celowe: caly werdykt da sie przetestowac bez
    bazy, gieldy i sieci.
    """
    if biezacy["windows_total"] == 0:
        return ["przebieg nie dal ani jednego okna informatywnego"]

    if poprzedni is None:
        return ["pierwszy zapisany przebieg — nie ma z czym porownac"]

    powody: list[str] = []

    if poprzedni["status"] != biezacy["status"]:
        powody.append(f"status: {poprzedni['status']} → {biezacy['status']}")

    # Prog z pomiaru, nie z przeczucia: dobowa roznica odsetka miesci sie
    # w szumie, wyjscie poza przedzial poprzedniego przebiegu - nie.
    lo, hi = poprzedni.get("positive_ci_low"), poprzedni.get("positive_ci_high")
    odsetek = biezacy["windows_positive"] / biezacy["windows_total"]
    if lo is not None and hi is not None and not (lo <= odsetek <= hi):
        powody.append(
            f"odsetek dodatnich okien {odsetek:.1%} poza przedzialem poprzedniego "
            f"przebiegu ({lo:.1%}–{hi:.1%})")

    poprz_okien = poprzedni["windows_total"]
    if poprz_okien and biezacy["windows_total"] < poprz_okien * (1 - UBYTEK_OKIEN):
        powody.append(
            f"ubylo okien informatywnych: {poprz_okien} → {biezacy['windows_total']} "
            f"— to ubytek dowodow, nie wyniku")

    sp, sb = poprzedni.get("median_oos_sharpe"), biezacy.get("median_oos_sharpe")
    if sp is not None and sb is not None and (sp > 0) != (sb > 0):
        powody.append(f"mediana Sharpe zmienila znak: {sp:+.3f} → {sb:+.3f}")

    pp, pb = poprzedni.get("max_psi") or 0.0, biezacy.get("max_psi") or 0.0
    if pb > PSI_PROG and pb > pp:
        powody.append(f"dryf cech {pb:.2f} powyzej progu {PSI_PROG} i rosnie")

    return powody


# Ile moze minac od ostatniego uderzenia serca, zanim uznamy silnik za martwy.
#
# Petla decyzyjna budzi sie co 300 s, wiec trzy pominiete cykle to sygnal,
# a nie wahanie. Zapas jest istotny: po wdrozeniu pod startuje z `sleep 90`
# i kontrola petli, wiec pierwsze uderzenie potrafi spoznic sie o kilka minut.
MARTWY_PO_MIN = 15


async def sprawdz_zywotnosc(fetch_all, notify, sucho: bool = False,
                            prog_min: int | None = None) -> int:
    """Czy silniki, ktore MAJA dzialac, faktycznie bija.

    DLACZEGO TO JEST OSOBNY PROCES

    Martwy proces nie zglosi wlasnej smierci. Petla decyzyjna zapisuje
    `risk.engine_heartbeat` przy kazdym cyklu, a dashboard to pokazuje - ale
    dashboard trzeba ogladac. Typ zdarzenia `engine.down` istnial w bazie od
    migracji 0007, oznaczony jako krytyczny i zawsze wlaczony, i NIKT GO NIGDY
    NIE WYSYLAL.

    STATUS ODROZNIA ZATRZYMANIE OD AWARII

    Silnik `equity` ma status `halted` i uderzenie sprzed dziesieciu dni, bo nie
    zostal jeszcze zbudowany. To nie jest awaria i nie ma o czym powiadamiac.
    Sprawdzamy wylacznie te, ktore twierdza, ze dzialaja.

    TLUMIENIE BEZ DODATKOWEJ KOLUMNY

    Powiadomienie o zdarzeniu `always_on` omija zwykly cooldown, wiec bez
    tlumienia czujka krzyczalaby przy kazdym przebiegu. Zamiast trzymac stan
    w osobnej kolumnie sprawdzamy, czy powiadomienie zostalo juz wyslane PO
    ostatnim uderzeniu serca: jesli tak, ta awaria jest juz zgloszona. Gdy silnik
    wroci, `last_beat_at` przesunie sie i nastepna awaria bedzie nowa.
    """
    # Prog jako parametr, zeby dalo sie wywolac SCIEZKE ALARMOWA na zadanie.
    # Bez tego jedyna droga sprawdzenia, czy alarm w ogole dziala, bylo zabicie
    # silnika na produkcji - czyli sciezka, ktora ma zadzialac raz na rok
    # w najgorszym momencie, nie bylaby przetestowana nigdy.
    prog = MARTWY_PO_MIN if prog_min is None else prog_min
    # DWA ZRODLA PULSU, bo nadzorca tez moze umrzec.
    #
    # Dozorca ryzyka (services/engine/dozorca.py) ocenia limity konta niezaleznie
    # od silnika - po to, zeby kill-switch dzialal takze wtedy, gdy silnik zyje
    # i nic nie robi. Ale nadzorca, ktorego nikt nie nadzoruje, jest tylko
    # przesunieciem tego samego problemu o jeden poziom: martwy dozorca wyglada
    # dokladnie jak dozorca, ktory nie ma co zglaszac.
    #
    # Status `running` na sztywno: dozorca nie ma stanu posredniego. Albo bije,
    # albo go nie ma.
    silniki = await fetch_all(
        "SELECT engine::text AS engine, mode::text AS mode, status, last_beat_at,"
        " round(extract(epoch from (now() - last_beat_at))/60) AS wiek_min"
        " FROM risk.engine_heartbeat"
        " UNION ALL"
        " SELECT nazwa AS engine, 'nadzor' AS mode, 'running' AS status, last_beat_at,"
        " round(extract(epoch from (now() - last_beat_at))/60) AS wiek_min"
        " FROM risk.dozorca_heartbeat"
        " ORDER BY engine")
    if not silniki:
        print("brak wpisow o zywotnosci silnikow")
        return 0

    martwe = []
    for s in silniki:
        opis = f"{s['engine']}/{s['mode']}: {s['status']}, ostatnie uderzenie {s['wiek_min']:.0f} min temu"
        if s["status"] != "running":
            print(f"  pomijam  {opis} — zatrzymany świadomie")
            continue
        if s["wiek_min"] > prog:
            martwe.append(s)
            print(f"  MARTWY   {opis}")
        else:
            print(f"  żyje     {opis}")

    if not martwe:
        return 0

    if sucho:
        print("Tryb suchy — powiadomienie nie wysyłane.")
        return 1

    odbiorcy = await fetch_all('SELECT id FROM app."user" WHERE is_active')
    for s in martwe:
        juz = await fetch_all(
            "SELECT 1 FROM notify.notification WHERE event_code = 'engine.down'"
            " AND engine::text = %s AND created_at > %s LIMIT 1",
            (s["engine"], s["last_beat_at"]))
        if juz:
            print(f"  (awaria {s['engine']} juz zgloszona — cisza)")
            continue
        tresc = (f"Ostatnie uderzenie serca {s['wiek_min']:.0f} minut temu "
                 f"({s['last_beat_at']:%Y-%m-%d %H:%M} UTC), próg to {prog} min.\n\n"
                 f"Sprawdź: kubectl logs deploy/trader-engine --tail=50")
        for u in odbiorcy:
            await notify(u["id"], "engine.down",
                         f"Silnik {s['engine']} przestał odpowiadać", tresc,
                         engine=s["engine"], severity="critical",
                         data={"wiek_min": float(s["wiek_min"]), "status": s["status"]})
        print(f"  powiadomiono o {s['engine']}: {len(odbiorcy)} odbiorców")

    # KOD WYJSCIA NIEZEROWY, choc powiadomienie juz poszlo.
    #
    # To jest DRUGI, niezalezny sygnal: nieudane zadanie widac w `kubectl get
    # jobs` nawet wtedy, gdy kanal powiadomien zawiodl. Czujka, ktora polega na
    # jednej drodze zgloszenia, ma ten sam problem co system, ktory pilnuje.
    return 1


async def sprawdz_ryzyko(fetch_all, notify, sucho: bool = False) -> int:
    """Czy kill-switch zadzialal i nikt sie o tym nie dowiedzial.

    Kill-switch to moment, w ktorym system SAM SIEBIE zatrzymuje - po
    przekroczeniu limitu straty dziennej albo obsuniecia kapitalu. Wymaga
    RECZNEGO odblokowania (patrz RiskKernel._trigger_killswitch), wiec dopoki
    nikt go nie zwolni, silnik nie wejdzie w zadna pozycje.

    Typ zdarzenia `killswitch.triggered` jest w bazie od migracji 0007,
    oznaczony jako krytyczny i zawsze wlaczony. Komentarz przy tabeli
    heartbeatu mowi wprost: "Brak sygnalu => wstrzymanie I ALERT". Wstrzymanie
    zbudowano, alertu nie.

    Bez powiadomienia system po zadzialaniu kill-switcha wyglada dokladnie tak
    samo jak system, ktory nie ma sygnalu do wejscia: zero decyzji `enter`,
    zadnego bledu w logu, heartbeat bije dalej. Roznica - ze przestal handlowac
    NA STALE - byla widoczna wylacznie dla kogos, kto zajrzalby do
    risk.killswitch_event.

    Tlumienie jak przy zywotnosci: sprawdzamy, czy powiadomienie poszlo juz po
    `triggered_at`. Zdarzenie zwolnione (released_at) pomijamy - problem zostal
    obsluzony przez czlowieka.
    """
    zdarzenia = await fetch_all(
        "SELECT k.id, k.engine::text AS engine, k.mode::text AS mode, k.reason,"
        "       k.triggered_at,"
        "       round(extract(epoch from (now() - k.triggered_at))/60) AS wiek_min"
        " FROM risk.killswitch_event k"
        " WHERE k.released_at IS NULL"
        " ORDER BY k.triggered_at")
    if not zdarzenia:
        print("kill-switch: brak aktywnych zdarzeń")
        return 0

    odbiorcy = await fetch_all('SELECT id FROM app."user" WHERE is_active')
    nowe = 0
    for z in zdarzenia:
        print(f"  KILL-SWITCH {z['engine']}/{z['mode']}: {z['reason']} "
              f"({z['wiek_min']:.0f} min temu)")
        juz = await fetch_all(
            "SELECT 1 FROM notify.notification WHERE event_code = 'killswitch.triggered'"
            " AND engine::text = %s AND created_at > %s LIMIT 1",
            (z["engine"], z["triggered_at"]))
        if juz:
            print("    (już zgłoszone — cisza)")
            continue
        nowe += 1
        if sucho:
            continue
        tresc = (f"{z['reason']}\n\nSilnik NIE WEJDZIE w żadną pozycję do czasu "
                 f"ręcznego zwolnienia — kill-switch nie wygasa sam, bo automatyczne "
                 f"wznowienie po obsunięciu to najprostsza droga do powtórzenia tej "
                 f"samej straty.\n\nZwolnienie: zakładka Ryzyko w interfejsie.")
        for u in odbiorcy:
            await notify(u["id"], "killswitch.triggered",
                         f"Kill-switch zadziałał ({z['engine']})", tresc,
                         engine=z["engine"], severity="critical",
                         data={"reason": z["reason"], "event_id": z["id"]})
        print(f"    powiadomiono: {len(odbiorcy)} odbiorców")

    if sucho and nowe:
        print("Tryb suchy — powiadomienie nie wysyłane.")
    # Kod niezerowy tylko przy NOWYM zdarzeniu. Kill-switch czekajacy na
    # zwolnienie jest stanem znanym, a nie powtarzajaca sie awaria zadania.
    return 1 if nowe else 0


# Ile moze minac od ostatniej udanej kopii, zanim uznamy to za problem.
#
# Zadanie chodzi raz na dobe o 03:17 UTC, wiec 26 godzin daje dwie godziny
# zapasu na dlugi zrzut i na przesuniecie startu przez zajety wezel. Krotszy
# prog zglaszalby falszywy alarm przy kazdym wolniejszym przebiegu.
KOPIA_STARA_PO_H = 26

# O ile kopia moze byc mniejsza od poprzedniej, zanim uznamy to za utrate danych.
#
# Baza rosnie, wiec zrzut powinien rosnac albo stac. Skurczenie o wiecej niz
# polowe oznacza, ze zrzut nie objal czesci danych - a taki plik NADAL przechodzi
# pg_restore --list, bo jest poprawnym archiwum, tyle ze prawie pustym.
KOPIA_SKURCZ = 0.5


def ocen_kopie(biezaca: dict, poprzednia: dict | None,
               prog_h: float = KOPIA_STARA_PO_H,
               skurcz: float = KOPIA_SKURCZ) -> list[tuple[str, str]]:
    """Werdykt o kopiach jako CZYSTA funkcja - lista (tytul, tresc).

    Wydzielone z tego samego powodu co `zmiany()` przy stanie nauki: galaz
    "kopia skurczyla sie" nie dala sie wywolac przeciwko produkcji, bo wymaga
    DWOCH wierszy, a po wdrozeniu byl jeden. Logika alarmu, ktora da sie
    sprawdzic tylko wtedy, gdy dane produkcyjne akurat sie ulozą, jest logika
    niesprawdzona.
    """
    powody: list[tuple[str, str]] = []

    if float(biezaca["wiek_h"]) > prog_h:
        powody.append((
            "Brak świeżej kopii zapasowej",
            f"Ostatnia udana kopia {biezaca['wiek_h']} godzin temu "
            f"({biezaca['finished_at']:%Y-%m-%d %H:%M} UTC), próg to {prog_h} h.\n\n"
            f"Sprawdź: kubectl get cronjob trader-backup; "
            f"kubectl get jobs | grep backup"))

    if poprzednia and biezaca["bajtow"] < poprzednia["bajtow"] * skurcz:
        powody.append((
            "Kopia zapasowa skurczyła się o ponad połowę",
            f"Ostatnia: {biezaca['bajtow'] / 1e6:.1f} MB, "
            f"poprzednia: {poprzednia['bajtow'] / 1e6:.1f} MB.\n\n"
            f"Taki plik nadal przechodzi pg_restore --list, bo jest poprawnym "
            f"archiwum — po prostu prawie pustym."))

    return powody


async def sprawdz_kopie(fetch_all, notify, sucho: bool = False) -> int:
    """Czy istnieje swieza, zweryfikowana kopia zapasowa.

    DLACZEGO NIE WYSTARCZY, ZE ZADANIE CYKLICZNE ISTNIEJE

    Zadanie zakonczone bledem widac w `kubectl get jobs`. Zawieszonego
    harmonogramu nie widac nigdzie. Pelny wolumen, padniety wezel i skasowany
    CronJob objawiaja sie tak samo - cisza.

    Kopia zapasowa, o ktorej braku dowiadujemy sie w dniu odtwarzania, jest
    gorsza niz jej brak: przez caly ten czas daje falszywe poczucie
    zabezpieczenia.

    Sprawdzamy STAN (czy jest swiezy wiersz), a nie zdarzenie (czy zadanie sie
    uruchomilo) - stan jest odporny na kazdy sposob, w jaki kopia moze nie
    powstac.
    """
    wiersze = await fetch_all(
        "SELECT plik, bajtow, finished_at,"
        " round(extract(epoch from (now() - finished_at))/3600, 1) AS wiek_h"
        " FROM app.backup_run ORDER BY finished_at DESC LIMIT 2")
    odbiorcy = await fetch_all('SELECT id FROM app."user" WHERE is_active')

    async def zglos(tytul: str, tresc: str) -> None:
        print(f"  {tytul}: {tresc.splitlines()[0]}")
        if sucho:
            return
        for u in odbiorcy:
            await notify(u["id"], "backup.missing", tytul, tresc, severity="critical")

    if not wiersze:
        # Brak WSZYSTKICH wpisow to nie to samo co brak swiezego: tuz po
        # wdrozeniu tej zmiany dziennik jest pusty, bo zadanie jeszcze nie
        # przebieglo. Mowimy o tym, ale nie jako o awarii.
        print("kopie: dziennik pusty — pierwsze zadanie jeszcze nie przebiegło")
        return 0

    biezaca = wiersze[0]
    print(f"kopie: ostatnia {biezaca['finished_at']:%Y-%m-%d %H:%M} "
          f"({biezaca['wiek_h']} h temu), {biezaca['bajtow'] / 1e6:.1f} MB")

    powody = ocen_kopie(biezaca, wiersze[1] if len(wiersze) > 1 else None)
    for tytul, tresc in powody:
        await zglos(tytul, tresc)

    if not powody:
        print("  bez zastrzeżeń")
    return 1 if powody else 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--co", default="stan-nauki",
                    choices=("stan-nauki", "zywotnosc", "ryzyko", "kopie"),
                    help="stan-nauki: czy wynik treningu sie zmienil; "
                         "zywotnosc: czy silniki bija; "
                         "ryzyko: czy kill-switch zadzialal; "
                         "kopie: czy jest swieza kopia zapasowa")
    ap.add_argument("--prog-min", type=int, default=None, dest="prog_min",
                    help="prog martwoty w minutach; do wywolania sciezki alarmowej "
                         "na zadanie. Domyslnie stala MARTWY_PO_MIN")
    ap.add_argument("--engine", default="crypto")
    # Domyslnie horyzont, ktorym handluje silnik. Tory porownawcze (np. 1h) maja
    # wlasny stan, ale NIE podnosza alarmu - inaczej powiadomienie mowiloby
    # o modelu, ktorym nikt nie handluje.
    ap.add_argument("--bar", default="4h")
    ap.add_argument("--sucho", action="store_true",
                    help="wypisz werdykt, nie wysylaj powiadomienia")
    ap.add_argument("--kod-trenera", type=int, default=0, dest="kod_trenera",
                    help="kod wyjscia trenera; niezerowy zawsze wywoluje "
                         "powiadomienie")
    args = ap.parse_args()

    if not os.environ.get("DATABASE_URL"):
        sys.exit("DATABASE_URL nie jest ustawiony")

    from services.api.db import close_pool, fetch_all, open_pool
    from services.api.notifications import notify

    await open_pool()
    try:
        if args.co == "zywotnosc":
            return await sprawdz_zywotnosc(fetch_all, notify, sucho=args.sucho,
                                           prog_min=args.prog_min)
        if args.co == "ryzyko":
            return await sprawdz_ryzyko(fetch_all, notify, sucho=args.sucho)
        if args.co == "kopie":
            return await sprawdz_kopie(fetch_all, notify, sucho=args.sucho)

        wiersze = await fetch_all(
            "SELECT status, windows_total, windows_positive, positive_ci_low,"
            " positive_ci_high, median_oos_sharpe, max_psi, trials_run, run_at"
            " FROM learn.learning_state_log WHERE engine = %s AND bar = %s"
            " ORDER BY run_at DESC LIMIT 2", (args.engine, args.bar))
        if not wiersze:
            print(f"brak zapisanych przebiegow dla {args.engine}/{args.bar}"
                  " — dozor nie ma czego porownac")
            return 0

        biezacy = wiersze[0]
        poprzedni = wiersze[1] if len(wiersze) > 1 else None
        powody = zmiany(poprzedni, biezacy)

        # AWARIA TRENERA MUSI SIE ODEZWAC, choc nie jest zmiana stanu.
        #
        # Bez tego przebieg zakonczony bledem nie dopisuje wiersza do dziennika,
        # wiec dozor porownuje dwa STARE wpisy, nie widzi roznicy i milczy -
        # awaria wygladalaby dokladnie jak spokoj. To najgorszy mozliwy tryb
        # awarii dla czegokolwiek, co ma czuwac.
        if args.kod_trenera:
            powody.insert(0, f"trener zakonczyl sie bledem (kod {args.kod_trenera}) "
                             f"— ponizsze liczby pochodza z POPRZEDNIEGO przebiegu")

        odsetek = (biezacy["windows_positive"] / biezacy["windows_total"]
                   if biezacy["windows_total"] else 0.0)
        print(f"Stan: {biezacy['status']}, {biezacy['windows_positive']}"
              f"/{biezacy['windows_total']} okien ({odsetek:.1%})")

        if not powody:
            # Cisza jest wynikiem, wiec musi byc widoczna w logu. Inaczej nie da
            # sie odroznic dozoru, ktory nic nie znalazl, od dozoru, ktory padl.
            # Ten sam powod, dla ktorego awaria trenera wyzej lamie cisze.
            print("Bez zmiany istotnej — cisza. Sprawdzone: status, przedzial "
                  "ufnosci, liczba okien, znak mediany, dryf.")
            return 0

        print("Zmiana istotna:")
        for p in powody:
            print(f"  • {p}")

        if args.sucho:
            print("Tryb suchy — powiadomienie nie wysylane.")
            return 0

        odbiorcy = await fetch_all('SELECT id FROM app."user" WHERE is_active')
        if not odbiorcy:
            print("Brak odbiorcow — powiadomienie nie ma do kogo pojsc.")
            return 0

        tytul = (f"Trener zawiódł ({args.engine}/{args.bar})" if args.kod_trenera
                 else f"Stan nauki ({args.engine}/{args.bar}): {biezacy['status']}")
        tresc = (f"{biezacy['windows_positive']}/{biezacy['windows_total']} okien "
                 f"dodatnich ({odsetek:.0%}), mediana Sharpe "
                 f"{biezacy['median_oos_sharpe']:+.3f}.\n\n"
                 + "\n".join(f"• {p}" for p in powody))
        for u in odbiorcy:
            await notify(u["id"], "learning.state_changed", tytul, tresc,
                         engine=args.engine,
                         severity="critical" if args.kod_trenera else None,
                         data={"powody": powody, "odsetek": round(odsetek, 4),
                               "kod_trenera": args.kod_trenera})
        print(f"Powiadomiono odbiorcow: {len(odbiorcy)}")
    finally:
        await close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
