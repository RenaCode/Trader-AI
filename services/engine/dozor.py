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


# Ile swiec wolno sie spoznic, zanim uznamy dane za nieswieze. Prog wyprowadzony
# Z DLUGOSCI SWIECY, nie z liczby dobranej na oko: stala liczba minut bylaby albo
# krzykliwa przy dlugiej swiecy, albo bezuzyteczna przy krotkiej.
#
# DLACZEGO TRZY, A NIE DWIE. `event_time` to czas OTWARCIA swiecy, a zapisujemy
# wylacznie swiece KOMPLETNE. Najnowszy wiersz ma wiec naturalnie od jednej do
# dwoch dlugosci swiecy - o 15:50 najnowsza swieca 1h ma znacznik 14:00, bo ta
# z 15:00 jeszcze trwa. Do tego dochodzi odstep pobierania (20 min).
#
# Prog dwoch swiec lezalby dokladnie na gornej granicy NORMY i migalby kilka razy
# dziennie. Czujka, ktora miga, jest tak samo bezuzyteczna jak czujka falszywa:
# obie ucza odbiorce, ze mozna je pomijac. Trzy swiece daja przy 1h czterdziesci
# minut zapasu ponad zmierzone maksimum.
SWIEC_ZWLOKI = 3

# Dlugosc timeframe'u w minutach. Nieznany timeframe NIE dostaje wartosci
# domyslnej: prog wyliczony ze zgadnietej dlugosci albo krzyczy bez powodu,
# albo milczy, gdy powinien krzyknac - a oba psuja czujke.
MINUT_TIMEFRAME = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}


def ocen_swiezosc(wiersze: list[dict], swiec_zwloki: int = SWIEC_ZWLOKI) -> list[tuple[str, str]]:
    """Ktore zrodla danych sa nieswieze. CZYSTA FUNKCJA.

    Zwraca listę (tytuł, treść) — pusta znaczy: wszystko świeże.
    """
    powody = []
    for w in wiersze:
        prog_min = w["minut_swiecy"] * swiec_zwloki
        if w["wiek_min"] is None:
            powody.append((
                f"Brak danych: {w['co']}",
                f"Tabela {w['co']} nie ma ANI JEDNEGO wiersza. Silnik nie ma na "
                f"czym podejmować decyzji."))
            continue
        if w["wiek_min"] > prog_min:
            powody.append((
                f"Dane nieświeże: {w['co']}",
                f"Najnowszy wpis sprzed {w['wiek_min']:.0f} min, próg to "
                f"{prog_min:.0f} min ({swiec_zwloki} świece po "
                f"{w['minut_swiecy']:.0f} min).\n\n"
                f"Pętla decyzyjna NIE ZGŁASZA tego sama: bije dalej i podejmuje "
                f"decyzje na tym, co ma. Stare cechy nie wyglądają jak awaria — "
                f"wyglądają jak rynek, który przestał dawać sygnały."))
    return powody


async def sprawdz_swiezosc(fetch_all, notify, sucho: bool = False,
                           bar: str = "4h") -> int:
    """Czy dane, na ktorych silnik podejmuje decyzje, sa swieze.

    DLACZEGO PULS SILNIKA TU NIE WYSTARCZA

    `sprawdz_zywotnosc` pilnuje, czy silnik zyje. Silnik moze jednak zyc
    doskonale i handlowac na cechach sprzed trzech dni - bo to nie on je liczy.
    Pobieranie swiec, finansowanie i cechy chodza w OSOBNYCH podach, a te
    do niedawna mialy w komendzie `|| true`, ktore polykalo kazda awarie.
    Martwe pobieranie wygladalo wiec dokladnie tak samo jak rynek bez zmian.

    PYTAMY O TIMEFRAME ZRODLOWY, NIE O SWIECE DOCELOWA

    Swieca 4h nie istnieje w `market.candle` - jest skladana ze swiec 1h.
    Pierwsza wersja tego sprawdzenia pytala o timeframe "4h" i dostala "brak
    danych" przy w pelni zdrowym potoku. Falszywy alarm psuje czujke
    SKUTECZNIEJ niz jej brak: uczy odbiorce, ze mozna ja pomijac.

    Prog liczy sie tez od swiecy ZRODLOWEJ. Przy swiecach 1h sprzed trzech
    godzin swieca 4h jest juz niepelna, choc osiem godzin nie minelo - a to
    swieca zrodlowa decyduje, czy da sie ja zlozyc.

    SPRAWDZAMY STAN, NIE ZDARZENIE - tak samo jak przy kopii zapasowej. Stan
    jest odporny na kazdy sposob, w jaki dane moga sie nie pojawic: padniety
    pod, zawieszony harmonogram, blad polaczenia z gielda, skasowany Deployment.
    """
    from services.engine.horizons import get as get_horizon

    h = get_horizon(bar)
    minut_zrodla = MINUT_TIMEFRAME.get(h.source_timeframe)
    if minut_zrodla is None:
        print(f"świeżość: nieznany timeframe źródłowy {h.source_timeframe!r} — "
              f"nie ma z czym porównać wieku danych")
        return 0

    wiersze = await fetch_all(
        "SELECT 'market.candle (' || %(tf)s || ', źródło świecy ' || %(bar)s || ')' AS co,"
        "       %(min)s::numeric AS minut_swiecy,"
        "       round(extract(epoch from (now() - max(event_time)))/60) AS wiek_min"
        "  FROM market.candle WHERE timeframe::text = %(tf)s"
        " UNION ALL"
        " SELECT 'feat.feature_value', %(min_bar)s::numeric,"
        "        round(extract(epoch from (now() - max(event_time)))/60)"
        "   FROM feat.feature_value",
        {"tf": h.source_timeframe, "bar": bar, "min": minut_zrodla,
         "min_bar": h.bar_minutes})

    for w in wiersze:
        wiek = "brak danych" if w["wiek_min"] is None else f"{w['wiek_min']:.0f} min temu"
        print(f"świeżość {w['co']}: {wiek}")

    powody = ocen_swiezosc(wiersze)
    if not powody:
        print("  bez zastrzeżeń")
        return 0

    odbiorcy = await fetch_all('SELECT id FROM app."user" WHERE is_active')
    for tytul, tresc in powody:
        print(f"  {tytul}")
        if sucho:
            continue
        for u in odbiorcy:
            await notify(u["id"], "engine.down", tytul, tresc, severity="critical")
    return len(powody)


def ocen_egzekucje(stan: dict) -> list[tuple[str, str, str]]:
    """Czy zlecenia w ogole dochodza do gieldy. CZYSTA FUNKCJA.

    Zwraca listę (kod_zdarzenia, tytuł, treść).

    JEDNO ODRZUCENIE TO NIE AWARIA. Zlecenie poniżej minimalnej wartości albo
    chwilowy limit konta zdarzają się i nie ma po co nikogo budzić o trzeciej
    w nocy. Awarią jest stan, w którym NIC NIE PRZECHODZI — bo wtedy system
    stoi, a wygląda dokładnie jak system bez sygnałów.

    Dlatego progiem nie jest liczba odrzuceń, tylko ich stosunek do przyjęć.

    ZLECENIA O NIEZNANYM STANIE SA GROZNIEJSZE NIZ ODRZUCONE. Odrzucone na pewno
    nie weszło. „Nieznane" znaczy, że odpowiedź nie dotarła — a zlecenie mogło
    wejść. Każde takie, które przeleżało dłużej niż jedną świecę, jest kandydatem
    na pozycję-widmo i wymaga sprawdzenia, nie czekania.
    """
    powody = []
    przyjete, odrzucone = stan["przyjete"], stan["odrzucone"]

    if odrzucone and not przyjete:
        powody.append((
            "order.rejected",
            f"Żadne zlecenie nie przeszło ({odrzucone} odrzuconych)",
            f"W ostatnim oknie giełda nie przyjęła ANI JEDNEGO zlecenia, "
            f"a odrzuciła {odrzucone}.\n\n"
            f"Powody, po jednym na rodzaj:\n" + "\n".join(
                f"  • {p}" for p in stan["powody"]) + "\n\n"
            f"System nie handluje i NIE WYGLĄDA to na awarię: silnik bije, dane "
            f"są świeże, kill-switch milczy. Odrzucone zlecenie w logu wygląda "
            f"tak samo jak brak sygnału."))
    elif odrzucone and przyjete and odrzucone >= przyjete:
        powody.append((
            "order.rejected",
            f"Więcej zleceń odrzuconych ({odrzucone}) niż przyjętych ({przyjete})",
            "Przechodzi mniej niż połowa zleceń. Powody:\n" + "\n".join(
                f"  • {p}" for p in stan["powody"])))

    if stan["nieznane"]:
        powody.append((
            "order.rejected",
            f"Zlecenia o NIEZNANYM stanie: {stan['nieznane']}",
            f"Dla {stan['nieznane']} zleceń odpowiedź giełdy nie dotarła i nikt "
            f"nie ustalił, co się z nimi stało. Odrzucone zlecenie na pewno nie "
            f"weszło — nieznane MOGŁO WEJŚĆ.\n\n"
            f"Każde takie jest kandydatem na pozycję-widmo: risk kernel liczyłby "
            f"z niej ekspozycję, więc blokowałaby wejścia, których nic nie "
            f"blokuje.\n\nIdentyfikatory: " + ", ".join(stan["id_nieznanych"])))

    if stan["zawieszone_plany"]:
        powody.append((
            "engine.down",
            f"Plany bez rozstrzygnięcia: {stan['zawieszone_plany']}",
            f"{stan['zawieszone_plany']} planów przekroczyło termin swojej świecy "
            f"i nadal ma status `pending`. Znaczy to, że rozstrzyganie planów "
            f"przestało działać — a nierozstrzygnięty plan wejścia blokuje "
            f"kolejną decyzję dla tego instrumentu."))
    return powody


async def sprawdz_egzekucje(fetch_all, notify, sucho: bool = False,
                            okno_h: int = 24) -> int:
    """Czy sciezka egzekucji dziala. Stan, nie zdarzenie.

    DLACZEGO TO NIE WYSTARCZA, ZE ZLECENIA SA ZAPISYWANE

    `exec.order` ma kolumne `reject_reason` od migracji 0005 i typ zdarzenia
    `order.rejected` istnieje w `notify.event_type` rownie dlugo - z etykieta,
    opisem i cooldownem. NIKT GO NIGDY NIE WYSLAL. Zapis bez czujki znaczy
    tyle, ze przyczyna bedzie znana, gdy juz ktos zauwazy skutek.
    """
    st = await fetch_all(
        "SELECT count(*) FILTER (WHERE status NOT IN ('rejected','unknown')) AS przyjete,"
        "       count(*) FILTER (WHERE status = 'rejected') AS odrzucone,"
        "       count(*) FILTER (WHERE status = 'unknown') AS nieznane"
        "  FROM exec.order WHERE submitted_at > now() - make_interval(hours => %(h)s)",
        {"h": okno_h})
    powody_rows = await fetch_all(
        "SELECT DISTINCT left(reject_reason, 160) AS powod FROM exec.order"
        " WHERE status = 'rejected' AND reject_reason IS NOT NULL"
        "   AND submitted_at > now() - make_interval(hours => %(h)s) LIMIT 5",
        {"h": okno_h})
    nieznane_rows = await fetch_all(
        "SELECT client_order_id FROM exec.order WHERE status = 'unknown'"
        "   AND submitted_at < now() - make_interval(hours => 4) LIMIT 10")
    zawieszone = await fetch_all(
        "SELECT count(*) AS n FROM journal.planned_order"
        " WHERE status = 'pending' AND valid_until < now() - make_interval(hours => 4)")

    stan = {
        "przyjete": st[0]["przyjete"] if st else 0,
        "odrzucone": st[0]["odrzucone"] if st else 0,
        # NIEZNANE LICZYMY TYLKO TE STARE. Zlecenie zlozone minute temu moze byc
        # nieznane najzupelniej poprawnie - odpowiedz jeszcze nie wrocila.
        "nieznane": len(nieznane_rows),
        "id_nieznanych": [r["client_order_id"] or "?" for r in nieznane_rows],
        "powody": [r["powod"] for r in powody_rows],
        "zawieszone_plany": zawieszone[0]["n"] if zawieszone else 0,
    }
    print(f"egzekucja ({okno_h} h): przyjętych {stan['przyjete']}, "
          f"odrzuconych {stan['odrzucone']}, o nieznanym stanie {stan['nieznane']}, "
          f"planów bez rozstrzygnięcia {stan['zawieszone_plany']}")

    powody = ocen_egzekucje(stan)
    if not powody:
        print("  bez zastrzeżeń")
        return 0

    odbiorcy = await fetch_all('SELECT id FROM app."user" WHERE is_active')
    for kod, tytul, tresc in powody:
        print(f"  {tytul}")
        if sucho:
            continue
        for u in odbiorcy:
            await notify(u["id"], kod, tytul, tresc, severity="critical")
    return len(powody)


async def sprawdz_poslizg(fetch_all, *, engine: str = "crypto",
                          mode: str = "demo", okno_h: int | None = None) -> int:
    """Ile naprawde kosztuje egzekucja. RAPORT, NIE CZUJKA.

    DLACZEGO TO TU SIEDZI

    `POSLIZG_TAKER = 0.0004` i `POSLIZG_MAKER = 0.0` sa zalozeniami, a runda
    kosztuje 0,200% przy medianie zwrotu rzedu dziesiatych procenta - blad
    o cztery punkty bazowe przesuwa wynik o tyle, ile cala zmierzona przewaga.
    Liczba, ktora da sie policzyc, ale ktorej nikt nie widzi, nie zmieni zadnej
    decyzji; dlatego pomiar wchodzi tam, gdzie i tak codziennie zaglada
    zadanie cykliczne.

    DLACZEGO NIE WYSYLA POWIADOMIENIA

    Reszta tego pliku odzywa sie o STANACH, ktore trzeba naprawic teraz: martwy
    silnik, brak kopii, zlecenia, ktore nie przechodza. Poslizg wiekszy od
    zalozonego nie jest awaria do naprawienia o trzeciej w nocy - jest liczba
    do wpisania w wycene strategii. Alarm zaprojektowany zanim padla pierwsza
    obserwacja mialby i tak prog wziety z sufitu, a alarm, ktorego nie da sie
    uzasadnic, uczy odbiorce pomijac wszystkie pozostale.

    Zwraca zawsze 0 z tego samego powodu: niezerowy kod zapalilby zadanie
    cykliczne na czerwono, czyli podnioslby alarm bez tresci.
    """
    from services.engine.poslizg import opis, pobierz, pomiar

    wiersze = await pobierz(fetch_all, engine=engine, mode=mode, okno_h=okno_h)
    okno = "od poczatku" if okno_h is None else f"{okno_h} h"
    print(f"poślizg ({engine}/{mode}, {okno}), "
          f"wypełnień w rejestrze: {len(wiersze)}")
    for linia in opis(pomiar(wiersze)):
        print(f"  {linia}")
    return 0


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


# --- czujka dostepnosci instrumentow ----------------------------------------
#
# Ile moze minac od ostatniego POTWIERDZENIA statusu u gieldy.
#
# Zadanie `services/ingest/instrumenty.py` chodzi raz na dobe, wiec 26 godzin
# daje dwie godziny zapasu - tak samo jak przy kopiach zapasowych. Krotszy prog
# zglaszalby falszywy alarm przy kazdym przesunietym przebiegu, dluzszy pozwala
# przespac cala dobe delistingu.
PRZEGLAD_STATUSU_H = 26


def ocen_instrumenty(wiersze: list[dict], prog_h: int = PRZEGLAD_STATUSU_H
                     ) -> list[tuple[str, str, str]]:
    """Czy uniwersum nadal istnieje u giełdy. CZYSTA FUNKCJA.

    Zwraca listę (kod_zdarzenia, tytuł, treść).

    DLACZEGO DOZOR PYTA O TO DRUGI RAZ

    Status pobiera i zapisuje `services/ingest/instrumenty.py` — i on też zdejmuje
    `tradeable`, i on też wysyła alarm o ZMIANIE. Ta czujka pilnuje trzech rzeczy,
    których tamten proces z definicji nie dopilnuje:

      1. że w ogóle chodzi. Zadanie, które przestało się uruchamiać, zostawia
         kolumnę statusu zamrożoną na „Trading" — i od tej chwili baza codziennie
         zapewnia, że wszystko handluje. BRAK ODCZYTU TO NIE JEST ZGODNOŚĆ,
         a milczące zero mówi „sprawdzone i w porządku" o czymś, czego nikt nie
         sprawdził;
      2. że reakcja faktycznie nastąpiła — instrument poza handlem, który wciąż
         ma `tradeable`, znaczy, że bramka się nie zamknęła;
      3. że o ZAMROŻONYM KAPITALE ktoś przypomina. Wycofanie trwa miesiącami,
         a alarm o zmianie stanu idzie raz. Pozycja na wycofywanym instrumencie
         jest problemem codziennie, aż do zamknięcia albo konwersji.

    Słownik statusów jest importowany, nie przepisany: druga lista słów
    rozjechałaby się przy pierwszym nowym statusie giełdy i to właśnie ta czujka
    twierdziłaby wtedy, że jest spokojnie.
    """
    from services.ingest.instrumenty import klasyfikuj_status

    powody: list[tuple[str, str, str]] = []

    nieswiezy = [r for r in wiersze
                 if r["sprawdzony_h"] is None or r["sprawdzony_h"] > prog_h]
    if nieswiezy:
        opis = ", ".join(
            f"{r['symbol']} ({'nigdy' if r['sprawdzony_h'] is None else str(r['sprawdzony_h']) + ' h temu'})"
            for r in nieswiezy[:12])
        powody.append((
            "instrument.suspended",
            f"Status {len(nieswiezy)} instrumentów nie był sprawdzany u giełdy",
            f"Próg to {prog_h} h (zadanie chodzi raz na dobę).\n\n{opis}\n\n"
            f"Kolumna `status_gieldy` pokazuje wtedy ostatnią znaną wartość i "
            f"wygląda dokładnie tak, jakby giełda właśnie potwierdziła handel. "
            f"Delisting w tym stanie przechodzi niezauważony aż do chwili, gdy "
            f"zlecenie zostanie odrzucone — a odrzucenie w logu wygląda jak brak "
            f"sygnału.\n\nSprawdź zadanie services/ingest/instrumenty.py."))

    for r in wiersze:
        # NIGDY NIESPRAWDZONY NIE JEST WYCOFANY - jest NIEZNANY. Zarzut wobec
        # niego postawiono wyzej ("nikt nie pytal") i drugi, o delisting, byly
        # falszywy: tuz po migracji 0033 zadna data sprawdzenia jeszcze nie
        # istnieje, wiec cale uniwersum krzyczaloby o wycofaniu.
        if r["sprawdzony_h"] is None:
            continue
        # NULL przy niepustej dacie sprawdzenia znaczy juz co innego: pytalismy,
        # a gielda tego instrumentu NIE ZNA.
        obecny = r["status_gieldy"] is not None
        klasa = klasyfikuj_status(r["status_gieldy"], obecny_u_gieldy=obecny)
        if klasa == "handel":
            continue
        czym = (f"status „{r['status_gieldy']}”" if obecny
                else "giełda nie zna tego instrumentu")
        pozycji, wartosc = r["pozycji"], float(r["wartosc"] or 0)

        if pozycji:
            powody.append((
                "instrument.delisted" if klasa == "wycofanie" else "instrument.suspended",
                f"{r['symbol']}: otwarta pozycja na instrumencie poza handlem"
                f" ({klasa})",
                f"{czym}. Otwartych pozycji: {pozycji}, wartość wejścia ≈{wartosc:.2f}.\n\n"
                f"Kapitał jest ZAMROŻONY: wyjścia nie da się złożyć, dopóki giełda "
                f"nie prowadzi handlu, a przy wycofaniu niewycofane środki są po "
                f"zamknięciu wypłat konwertowane po ówczesnej cenie rynkowej. Limity "
                f"ryzyka liczą tę pozycję jak każdą inną, więc blokuje ona budżet "
                f"kolejnym wejściom.\n\nZamknięcie jest DECYZJĄ HANDLOWĄ i żaden "
                f"automat go za nikogo nie podejmie."))
        elif r["tradeable"]:
            powody.append((
                "instrument.delisted" if klasa == "wycofanie" else "instrument.suspended",
                f"{r['symbol']}: poza handlem u giełdy, a wciąż w uniwersum",
                f"{czym}, a instrument ma nadal `tradeable = true`. Bramka, która "
                f"miała go zdjąć, nie zadziałała — silnik może wystawić zlecenie na "
                f"rynek, którego nie ma.\n\nSprawdź ostatni przebieg "
                f"services/ingest/instrumenty.py."))
    return powody


async def sprawdz_instrumenty(fetch_all, notify, sucho: bool = False, *,
                              engine: str = "crypto", mode: str = "demo",
                              prog_h: int | None = None) -> int:
    """Czy któryś instrument uniwersum przestał istnieć u giełdy.

    ZAKRES: instrument w uniwersum decyzyjnym ALBO z otwartą pozycją. Migracja
    0020 wyłączyła czternaście par USDT, których podmiot unijny nie oferuje —
    alarm o nich przy każdym przebiegu nauczyłby czytać tę czujkę nieuważnie
    akurat wtedy, gdy dotyczy pary, którą naprawdę handlujemy.
    """
    prog = PRZEGLAD_STATUSU_H if prog_h is None else prog_h
    wiersze = await fetch_all(
        "SELECT i.symbol, i.tradeable, i.status_gieldy,"
        "       round(extract(epoch from (now() - i.status_sprawdzony_at))/3600)::int"
        "         AS sprawdzony_h,"
        "       count(p.id) AS pozycji,"
        "       coalesce(sum(p.qty * p.avg_price), 0) AS wartosc"
        "  FROM ref.instrument i"
        "  LEFT JOIN exec.position p ON p.instrument_id = i.id"
        "       AND p.closed_at IS NULL AND p.mode::text = %(mode)s"
        " WHERE i.active AND i.engine::text = %(engine)s"
        " GROUP BY i.id, i.symbol, i.tradeable, i.status_gieldy, i.status_sprawdzony_at"
        " HAVING i.tradeable OR count(p.id) > 0"
        " ORDER BY i.symbol",
        {"engine": engine, "mode": mode})
    print(f"instrumenty ({engine}/{mode}): {len(wiersze)} w uniwersum lub z pozycją")

    powody = ocen_instrumenty(wiersze, prog)
    if not powody:
        print("  bez zastrzeżeń")
        return 0

    # TLUMIENIE DOBOWE. `instrument.delisted` jest always_on, wiec omija zwykly
    # cooldown - a wycofanie trwa miesiacami. Bez wlasnego tlumienia czujka
    # krzyczalaby przy kazdym przebiegu i nauczylaby ignorowac wlasnie ten alarm.
    juz = await fetch_all(
        "SELECT 1 FROM notify.notification WHERE event_code = 'instrument.delisted'"
        "   AND created_at > now() - make_interval(hours => 24) LIMIT 1")

    odbiorcy = await fetch_all('SELECT id FROM app."user" WHERE is_active')
    for kod, tytul, tresc in powody:
        print(f"  {tytul}")
        if sucho:
            continue
        if kod == "instrument.delisted" and juz:
            print("    (zgłoszone w ciągu ostatniej doby — cisza)")
            continue
        sev = "critical" if kod == "instrument.delisted" else "warning"
        for u in odbiorcy:
            await notify(u["id"], kod, tytul, tresc, engine=engine, severity=sev)
    if sucho:
        print("Tryb suchy — powiadomienie nie wysyłane.")
    return len(powody)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--co", default="stan-nauki",
                    choices=("stan-nauki", "zywotnosc", "ryzyko", "kopie",
                             "swiezosc", "egzekucja", "poslizg", "instrumenty"),
                    help="stan-nauki: czy wynik treningu sie zmienil; "
                         "zywotnosc: czy silniki bija; "
                         "ryzyko: czy kill-switch zadzialal; "
                         "kopie: czy jest swieza kopia zapasowa; "
                         "swiezosc: czy dane, na ktorych silnik decyduje, "
                         "nie sa przestarzale; "
                         "egzekucja: czy zlecenia dochodza do gieldy; "
                         "poslizg: ile NAPRAWDE kosztuje egzekucja - raport, "
                         "nie czujka; "
                         "instrumenty: czy uniwersum nadal istnieje u gieldy "
                         "(wycofanie, zawieszenie, nieaktualny odczyt statusu)")
    ap.add_argument("--prog-statusu-h", type=int, default=None, dest="prog_statusu_h",
                    help="prog nieswiezosci odczytu statusu w godzinach dla "
                         "--co instrumenty; do wywolania sciezki alarmowej na "
                         "zadanie. Domyslnie stala PRZEGLAD_STATUSU_H")
    ap.add_argument("--prog-min", type=int, default=None, dest="prog_min",
                    help="prog martwoty w minutach; do wywolania sciezki alarmowej "
                         "na zadanie. Domyslnie stala MARTWY_PO_MIN")
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--mode", default="demo",
                    help="tryb rachunku; dotyczy sprawdzen czytajacych egzekucje")
    ap.add_argument("--okno-h", type=int, default=None, dest="okno_h",
                    help="okno w godzinach dla --co poslizg; domyslnie od poczatku, "
                         "bo przy probie rzedu kilku wypelnien kazde okno wyrzuca "
                         "czesc jedynych obserwacji, jakie sa")
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
        if args.co == "swiezosc":
            return await sprawdz_swiezosc(fetch_all, notify, sucho=args.sucho,
                                          bar=args.bar)
        if args.co == "egzekucja":
            return await sprawdz_egzekucje(fetch_all, notify, sucho=args.sucho)
        if args.co == "poslizg":
            return await sprawdz_poslizg(fetch_all, engine=args.engine,
                                         mode=args.mode, okno_h=args.okno_h)
        if args.co == "instrumenty":
            return await sprawdz_instrumenty(fetch_all, notify, sucho=args.sucho,
                                             engine=args.engine, mode=args.mode,
                                             prog_h=args.prog_statusu_h)

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
