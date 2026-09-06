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
    silniki = await fetch_all(
        "SELECT engine::text AS engine, mode::text AS mode, status, last_beat_at,"
        " round(extract(epoch from (now() - last_beat_at))/60) AS wiek_min"
        " FROM risk.engine_heartbeat ORDER BY engine")
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


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--co", default="stan-nauki",
                    choices=("stan-nauki", "zywotnosc"),
                    help="stan-nauki: czy wynik treningu sie zmienil; "
                         "zywotnosc: czy silniki bija")
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
