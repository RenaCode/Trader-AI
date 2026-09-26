#!/usr/bin/env python3
"""Trener walk-forward: kalibracja wag sygnalow per rezim.

Protokol jest jedyny dopuszczalny w tym projekcie i celowo niewygodny:

  okno 1:  [--- trening 14 dni ---][test 2 dni]
  okno 2:      [--- trening 14 dni ---][test 2 dni]
  ...

Wagi dobierane sa WYLACZNIE na oknie treningowym, a wynik raportowany WYLACZNIE
z okna testowego, ktorego optymalizacja nigdy nie widziala. Zwykla walidacja
krzyzowa jest tu niewazna, bo tasowanie szeregu czasowego wpuszcza przyszlosc
do treningu.

DEFLOWANY SHARPE jest od 2026-09-26 warunkiem bramki promocji - patrz
`status_walidacji` i `werdykt_dsr`. Liczy sie PO koszcie, ktorego symulacja nie
nalicza (`Karta.doplata_poza_symulacja`), a jego N to liczba ROZROZNIALNYCH
KONFIGURACJI z `learn.model_version` - NIE licznik `learn.hypothesis_trial`,
mimo ze migracja 0006 tak go zapowiadala. Uzasadnienie przy `n_konfiguracji`:
kandydaci z tego licznika byli oceniani in-sample, a walk-forward juz za nich
zaplacil, liczac wynik poza oknem wyboru.

Kazda sprawdzona konfiguracja nadal laduje w learn.hypothesis_trial i licznik
jest podawany w raporcie jako KONTEKST. Im wiecej konfiguracji sie przetestuje,
tym latwiej trafic dobry wynik czystym przypadkiem, wiec sam Sharpe bez liczby
konfiguracji obok nie mowi o przewadze nic.

Uzycie:
    DATABASE_URL=... python services/engine/train.py --days 90 --trials 40

    # porownanie wariantow doboru wag, bez dotykania bazy:
    ... python services/engine/train.py --sucho --wybor ic
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
from decimal import Decimal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg
from psycopg.rows import dict_row

from services.engine.risk_rules import (
    DEFAULT_LIMITS, MIN_CONFIDENCE, START_EQUITY, wartosc_limitu,
)
from services.engine.horizons import DEFAULT_BAR, HORIZONS, Horizon, get as get_horizon
from services.engine.portfolio import (
    Egzekucja, kup_i_trzymaj, metrics as portfolio_metrics, precompute_panels,
    simulate_portfolio,
)
from services.engine.koszty import Karta, karta_rundy, opis_karty
from services.engine.statystyka import (
    PROG_DSR, deflowany_sharpe, profit_factor, rozklad,
    sharpe as sharpe_proby, spearman, wilson,
)
from services.engine.uniwersum import odcisk
from services.engine.signals import (
    DEFAULT_WEIGHTS, REGIMES, SIGNALS, classify_regime, combine, evaluate,
    wlaczone_eksperymenty,
)
from services.engine.wersja import sha_kodu

# Prog wejscia NIE jest stala. Jest percentylem rozkladu |wyniku| policzonego
# na oknie TRENINGOWYM - a wiec dobieranym pod zadana CZESTOTLIWOSC handlu,
# nigdy pod zwrot. To rozroznienie jest tu najwazniejsze: dobieranie progu az
# wynik wyjdzie dodatni to definicja przeuczenia, natomiast powiedzenie
# "chce handlowac w 12% najsilniejszych sytuacji" jest decyzja podjeta przed
# zobaczeniem jakiegokolwiek wyniku.
#
# Stala 0,25 dobrana byla pod rozklad wynikow ze swiecy 5-minutowej. Po zmianie
# normalizacji na bezwymiarowa rozklad sie przesunal i ta sama stala dawala
# 2 transakcje na okno testowe - za malo, by cokolwiek zmierzyc. Percentyl
# przenosi sie miedzy horyzontami sam.
ENTER_PCTL = 0.88
EXIT_RATIO = 0.45          # wyjscie przy spadku sily sygnalu do 45% progu wejscia
# Prog pewnosci przychodzi z risk_rules - ta sama liczba, ktora stosuje petla
# decyzyjna. Zdefiniowany tu osobno oznaczalby, ze trener kalibruje wagi przy
# innym progu, niz produkcja potem stosuje.
MIN_CONF = MIN_CONFIDENCE

# Minimum informatywnych okien, ponizej ktorego NIE WOLNO orzec "zwalidowane".
# Bez tego jedno dodatnie okno przy 40 pominietych dawalo 100% skutecznosci
# i status otwierajacy droge do handlu na zywo.
MIN_WINDOWS_FOR_VALIDATION = 20

# ----------------------------------------------------------- kody wyjscia
#
# DWIE ROZNE ODPOWIEDZI, KTORE DO 2026-09-25 MIALY TEN SAM KOD 2.
#
# ZMIERZONE NA PRODUKCJI 2026-09-25: Job `trader-trainer` liczy dwa tory w jednym
# skrypcie (charts/trader/templates/trainer-cron.yaml). Tor crypto konczyl sie
# wynikiem ("Stan: learning, 26/38 okien"), a nastepnie tor equity przerywal na
# pustym uniwersum (`tradeable = false` na wszystkich 21 akcjach, ustawione
# SWIADOMIE migracja 0047) i wypisywal kod 2. Skrypt zbieral kod do `RC`, wiec
# CALY Job konczyl sie bledem, ArgoCD stalo trwale `Degraded`, a padniete
# zadanie wracalo kazdej nocy.
#
# To jest dokladnie ten wzorzec, ktory `services/engine/dozor.py` opisuje jako
# zmierzony 2026-09-12: kill-switch aktywny przez poltorej doby dal 144 padniete
# Joby na dobe i "nauczyl przewijac cala kategorie". Sygnal powtarzany codziennie
# o stanie, ktory sie nie zmienia i o ktorym ktos juz zdecydowal, nie odroznia
# sie od sygnalu o awarii - i zabiera wiarygodnosc temu drugiemu.
#
# DLACZEGO OSOBNY KOD, A NIE OSOBNY JOB ANI "STATUS CZESCIOWEGO POWODZENIA"
#
#   * OSOBNE JOBY dla crypto i equity rozwiazuja to samo, ale za cene drugiej
#     kopii harmonogramu, drugiego obrazu do podbicia i drugiej sciezki, ktora
#     moze cicho zniknac - powod podany wprost w trainer-cron.yaml przy zbieraniu
#     kodu do wspolnego `RC`. Rozdzielenie Jobow przenosi tez problem, a nie
#     usuwa go: Job equity nadal padalby co noc z tego samego powodu.
#   * "STATUS CZESCIOWEGO POWODZENIA" (np. kod 0 z ostrzezeniem) kasuje
#     rozroznienie w miejscu, w ktorym jest ono potrzebne: wolajacy nie moglby
#     odroznic toru nieuruchomionego od toru, ktory policzyl wynik. Skoro
#     `main()` obsluguje JEDEN tor na wywolanie, informacja "ktory tor i dlaczego"
#     musi wyjsc kodem wyjscia, bo innego kanalu do wolajacego nie ma.
#   * OSOBNY KOD nie zmienia bramki: `train()` dalej odmawia, dalej wypisuje
#     PRZERWANO i dalej konczy sie niezerowo. Zmienia sie wylacznie to, ze
#     wolajacy MOZE odroznic te dwie sytuacje - i dopiero to pozwala mu nie
#     zabijac wyniku drugiego toru.
#
# KOD_NIC_NIE_POLICZONO: przebieg MIAL policzyc i nie policzyl. Cos jest zepsute
# (stanal potok cech, panel jest za krotki, cechy przestaly przychodzic) i ktos
# musi to naprawic. Job ma byc CZERWONY.
KOD_NIC_NIE_POLICZONO = 2
# KOD_TOR_NIEGOTOWY: przebieg nie wystartowal, bo NIE WOLNO mu bylo - warunek
# wstepny nazwany w kodzie nie jest spelniony, a spelnic go moze wylacznie
# czlowiek (migracja wlaczajaca uniwersum, backfill danych skorygowanych
# o podzialy). Nic nie policzono I NIC NIE ZEPSUTO, a stan jest DZISIAJ
# OCZEKIWANY. Job nie ma sie od tego zapalac; powod idzie do logu przy kazdym
# przebiegu i do podsumowania na koncu skryptu zadania.
#
# Uwaga na rownowaznosc obu bramek uzywajacych tego kodu (puste uniwersum
# i brak potwierdzenia korekty o podzialy): obie sa GLOSNE i obie odmawiaja
# treningu. Rozni je tylko to, czego brakuje, i oba komunikaty podaja, kto ma to
# uzupelnic.
KOD_TOR_NIEGOTOWY = 3

# OKNO POMIARU DRYFU CECH - WLASNY PARAMETR, NIE POCHODNA `--days`.
#
# Do 2026-09-23 okno liczylo sie jako `days // 3` wobec reszty `days`, czyli
# przy produkcyjnym `--days 1400` (values.yaml) porownywalo ostatnie 466 dni
# z poprzedzajacymi 934. Dwa sasiednie nocne odczyty dzielily wtedy ~99,8%
# danych, a caly mechanizm trwalosci w `dozor.ocen_dryf_trwaly` stoi na
# zalozeniu, ze kolejne odczyty sa NIEZALEZNE: „trzy kolejne przebiegi powyzej
# progu" byly jednym dowodem policzonym trzy razy.
#
# TRZYDZIESCI DNI WOBEC POPRZEDZAJACYCH DZIEWIECDZIESIECIU. Trzy liczby
# musialy sie tu zgodzic:
#
#   * OKNO NOWE ma byc porownywalne z RYTMEM REAKCJI. Dozor wymaga trzech
#     kolejnych przebiegow w rozstepie co najmniej 48 h, czyli reaguje w ciagu
#     doby-dwoch; okno, ktore w tym czasie wymienia znikoma czesc danych, nie
#     ma szansy tego zobaczyc. Przy 30 dniach doba to 1/30 okna, czyli kolejny
#     odczyt niesie realnie nowa informacje.
#   * OKNO NOWE ma tez byc dosc dlugie, zeby `stddev_pop` cokolwiek znaczylo.
#     Przy swiecy 4h trzydziesci dni to ~180 obserwacji na instrument.
#   * OKNO STARE ma byc ODNIESIENIEM, nie drugim pomiarem biezacego stanu -
#     stad trzykrotnosc, ta sama proporcja, ktora byla tu wczesniej. Kwartal
#     obejmuje wiecej niz jeden rezim rynku, wiec „historyczne odchylenie"
#     w mianowniku nie jest odchyleniem jednego nastroju.
#
# Swiadomie BEZ odczytu z argumentow i ze srodowiska: okno dobrane pod wynik
# przestaje byc pomiarem. To ta sama zasada co przy `MAX_DNI_BEZ_POTWIERDZENIA`
# w probie pozarowej.
DRYF_OKNO_NOWE_DNI = 30
DRYF_OKNO_STARE_DNI = 90

# Klucze z `portfolio.metrics`, z ktorych sklada sie rozklad sredniej ekspozycji.
# Kolejnosc jest kolejnoscia czynnikow w iloczynie.
KLUCZE_ROZKLADU = ("w_rynku", "pozycji_w_rynku", "notional_na_pozycje",
                   "budzet_na_pozycje")


def _limit_ekspozycji_pct() -> float:
    """Limit lacznej ekspozycji jako UDZIAL kapitalu, niezaleznie od jednostki.

    Do 0028 limit byl kwota i wydruk dzielil go przez START_EQUITY. Po zmianie
    jednostki na `pct_equity` ta sama linia pokazywala "limit laczny: 15
    z 100000, czyli 0%" - liczba prawdziwa, zdanie falszywe. Ten sam ksztalt
    bledu co przy rozkladzie ekspozycji: wydruk, ktory przestal opisywac to,
    co opisuje.
    """
    lim = DEFAULT_LIMITS["max_total_exposure"]
    return float(wartosc_limitu(lim, Decimal(str(START_EQUITY)))) / START_EQUITY


def probka_rozkladu(m: dict, instrumentow: int) -> dict:
    """Czynniki ekspozycji z jednego okna, bez reszty miar."""
    return {k: m[k] for k in KLUCZE_ROZKLADU} | {"instrumentow": instrumentow}


def opis_rozkladu(probki: list[dict]) -> tuple[list[str], float]:
    """Rozklad sredniej ekspozycji na czynniki: linie do wydruku i iloczyn.

    CZYSTA FUNKCJA, bo wpisana wprost w wydruk byla NIESPRAWDZALNA - i zdazyla
    juz raz sklamac. Pierwsza wersja mnozyla udzial swiec w rynku przez
    `srednio_pozycji`, a ta jest JUZ usredniona po swiecach pustych: czynnik
    "ile czasu w rynku" wchodzil do iloczynu dwa razy. Zmierzone 1,29% wychodzilo
    z wydruku jako 0,56%, a linia czytala sie jak wyjasnienie.

    Testy symulacji tego nie widzialy, bo pomylka siedziala w SKLEJENIU miar,
    nie w samych miarach. Wstawienie bledu z powrotem przechodzilo caly zestaw.

    Zwracamy iloczyn osobno, zeby test mogl porownac go ze srednia ekspozycja
    policzona niezaleznie - bez tego "iloczyn zgadza sie sam ze soba" i nie
    dowodzi niczego.

    ILOCZYN NIE JEST DOKLADNIE ROWNY sredniej ekspozycji z wielu okien i tak ma
    byc: srednia iloczynow nie rowna sie iloczynowi srednich, a czynniki
    usredniamy po oknach osobno. Zmierzona roznica to okolo 1,5% wartosci
    (1,27% wobec 1,29%). Obie liczby stoja w raporcie obok siebie - rozbieznosc
    ma byc widoczna, a nie zasypana. Na POJEDYNCZYM oknie tozsamosc jest
    dokladna i pilnuje jej test.
    """
    if not probki:
        return [], 0.0
    w_rynku = statistics.fmean(p["w_rynku"] for p in probki)
    poz = statistics.fmean(p["pozycji_w_rynku"] for p in probki)
    notional = statistics.fmean(p["notional_na_pozycje"] for p in probki)
    budzet = statistics.fmean(p["budzet_na_pozycje"] for p in probki)
    instr = statistics.fmean(p["instrumentow"] for p in probki)
    iloczyn = w_rynku * poz * notional

    linie = [f"  = w rynku {w_rynku:.1%} świec × {poz:.2f} pozycji na świecę "
             f"w rynku × {notional:.2%} kapitału na pozycję = {iloczyn:.2%}"]
    if budzet and instr:
        linie.append(
            f"    do wykorzystania: {instr:.0f} pozycji × {budzet:.2%} "
            f"= {instr * budzet:.1%} kapitału. Zajęte: {poz:.2f} pozycji "
            f"({poz / instr:.0%} miejsc) po {notional / budzet:.0%} budżetu, "
            f"przez {w_rynku:.0%} czasu")
    return linie, iloczyn

# Minimalna liczba instrumentow, ktore musza byc notowane w danym oknie.
# Portfel z garstki rynkow to inna strategia niz portfel z dwudziestu czterech.
MIN_INSTRUMENTS = 8

# Ile okien musi widziec sygnal ZYWY, zanim dostanie wage produkcyjna.
#
# Mediana po oknach, w ktorych sygnal zyl, rozwiazuje problem "brak danych
# liczony jako zero" - ale wprowadza drugi: sygnal zywy w JEDNYM oknie
# dostalby wage z tego jednego okna, czyli z jednego losowania.
#
# Piec okien to przy kroku 30 dni okolo pieciu miesiecy niezaleznych okresow
# testowych - miesci sie w szesciomiesiecznej fazie demonstracyjnej i jest
# wystarczajaco duzo, zeby mediana nie byla pojedynczym pomiarem.
MIN_OKIEN_ZYWEGO = 5
# USUNIETE 2026-09-24: `FEE = 0.00055` i `SLIP = 0.0004`.
#
# Stale lezaly tu nieuzywane od wydzielenia `koszty.py` - ani symulacja, ani
# petla nie czytaly ich ani razu (`grep -rn 'FEE\|SLIP' services/` dawal
# wylacznie te dwie linie i wzmianki w komentarzach). Wartosc `0,00055` to
# w dodatku stawka taker kontraktu wieczystego, czyli dokladnie ta liczba,
# ktorej naglowek `koszty.py` uzywa jako przykladu bledu.
#
# Druga kopia stawki, ktorej nikt nie czyta, jest gorsza niz zadna: przy
# nastepnej zmianie cennika ktos poprawi te, ktora ma przed oczami, i bedzie
# przekonany, ze zmienil koszt. Cennik jest jeden i jest w `koszty.py`.

# Bezwzgledna podloga pokrycia okna treningowego - patrz `prog_pokrycia`.
MIN_SWIEC_OKNA = 100


def krotnosc_zabojcza(koszt_progowy: float | None,
                      spread: float | None) -> float | None:
    """Ile razy szerszy musialby byc spread, zeby zjesc cala przewage.

    DLACZEGO AKURAT TA LICZBA

    Spread mamy zmierzony na ksiedze GLOBALNEJ, a zlecenia skladamy na
    bybit.eu - podmiocie o rzedu 0,1% tamtego wolumenu (migracja 0018).
    Nasz spread jest wiec nieznany i niemal na pewno szerszy; pytanie „ile
    wynosi" nie ma dzis odpowiedzi.

    Pytanie „ile razy szerszy musialby byc, zeby przewaga zeszla do zera" ma
    odpowiedz, i to odpowiedz DAJACA SIE SPRAWDZIC: zamienia nieznane w warunek,
    ktory ktos moze pojsc i zweryfikowac jednym odczytem arkusza z wlasciwego
    hosta. To jest jedyna postac, w jakiej nieznana wielkosc moze wejsc do
    raportu, nie udajac przy tym pomiaru.

    None, gdy pytanie nie ma sensu: przewagi juz nie ma (prog niedodatni),
    spreadu nie zmierzono, albo zmierzony spread jest zerem - wtedy nie ma
    czego mnozyc i zadna krotnosc niczego nie zmieni.
    """
    if koszt_progowy is None or spread is None:
        return None
    if koszt_progowy <= 0 or spread <= 0:
        return None
    return koszt_progowy / spread


def _pb(x: float | None) -> str:
    """Punkty bazowe albo myslnik. Zero bylo by tu klamstwem."""
    return "—" if x is None else f"{x * 1e4:+.2f} pb"


def opis_przewagi(zwroty: list[float], sharpe_okien: list[float],
                  karta: Karta,
                  zwroty_okien: list[list[float]] | None = None) -> list[str]:
    """Przewaga wypisana tak, zeby zalozenie nie wygladalo jak pomiar.

    TRZY BLOKI, W TEJ KOLEJNOSCI, I ZADNEGO NIE WOLNO POMINAC:

      1. co symulacja NALICZYLA - liczby po kosztach, ktore w niej sa,
      2. czego NIE NALICZYLA - skladniki karty bez wartosci, wypisane slowami,
      3. KOSZT PROGOWY - o ile te nienaliczone skladniki musialyby byc mniejsze,
         zeby przewaga przezyla. To jedyna z tych liczb, ktora nie wymaga
         zadnego zalozenia, i jedyna, na ktora warto patrzec przed wejsciem
         na zywo.

    Bloku trzeciego nie da sie zastapic punktowym oszacowaniem kosztu, dopoki
    spread i niewypelnienie nie sa zmierzone. Wstawienie tam wymyslonej liczby
    zamienia „nie wiem" na „wiem i wynosi tyle" - czyli robi dokladnie to,
    przed czym cala ta warstwa ma bronic.
    """
    zwroty_okien = zwroty_okien or []
    p = podsumowanie_przewagi(zwroty, sharpe_okien)
    if not zwroty:
        return ["Przewaga: brak transakcji OOS — nie ma czego liczyć."]

    linie = ["", "PRZEWAGA PO KOSZTACH, KTÓRE SYMULACJA NALICZYŁA:"]
    linie.append(
        f"  Sharpe/transakcję ze ZŁĄCZONEJ próby {p['n_transakcji']} transakcji: "
        f"{p['sharpe_zbiorczy']:+.4f}" if p["sharpe_zbiorczy"] is not None
        else "  Sharpe/transakcję ze złączonej próby: — (za mało transakcji)")
    # OBIE LICZBY NARAZ. Rozjazd miedzy nimi jest wynikiem, nie szczegolem:
    # gdy sa rozne, to znaczy, ze srednia po oknach mowi o wadze okien,
    # a nie o transakcjach.
    linie.append(
        f"  ta sama miara jako ŚREDNIA PO {p['n_okien']} OKNACH: "
        f"{p['sharpe_sredni_po_oknach']:+.4f} — inna liczba niż wyżej, bo "
        f"średnia ilorazów nie jest ilorazem; okno o kilku transakcjach waży "
        f"w niej tyle samo, co okno o dwustu"
        if p["sharpe_sredni_po_oknach"] is not None else "  średnia po oknach: —")
    ok = p["okna"]
    if ok["n"]:
        linie.append(
            f"  rozkład okien: mediana {ok['mediana']:+.4f}, kwartyle "
            f"[{ok['q1']:+.4f}, {ok['q3']:+.4f}]" if ok["q1"] is not None
            else f"  rozkład okien: mediana {ok['mediana']:+.4f}")
        linie.append(
            f"  okien ujemnych: {ok['ujemnych']}/{ok['n']} "
            f"({ok['udzial_ujemnych']:.1%})")
    pf = p["profit_factor_zbiorczy"]
    linie.append(
        f"  profit factor ZBIORCZY: {pf:.4f}" if pf is not None
        else "  profit factor zbiorczy: — (brak strat w próbie)")
    linie.append(f"  średni zwrot z transakcji: {p['sredni_zwrot'] * 100:+.4f}%, "
                 f"trafnych {p['hit_rate']:.1%}")

    linie.append("")
    linie.append("RACHUNEK KOSZTU RUNDY — SKŁADNIK PO SKŁADNIKU, Z POCHODZENIEM:")
    linie.extend(opis_karty(karta))

    linie.append("")
    prog = p["koszt_progowy"]
    if prog is None:
        linie.append("KOSZT PROGOWY: — (brak transakcji)")
    elif prog <= 0:
        linie.append(
            f"KOSZT PROGOWY: {_pb(prog)} na rundę — UJEMNY. Przewagi nie ma "
            f"JUŻ przy kosztach, które symulacja nalicza; nienaliczone "
            f"składniki ({', '.join(karta.nieznane) or 'brak'}) mogą ją tylko "
            f"pogłębić.")
    else:
        linie.append(
            f"KOSZT PROGOWY: {_pb(prog)} na rundę. Tyle wynosi cały zapas na "
            f"to, czego symulacja NIE liczy. Jeśli spread, niewypełnienie "
            f"i finansowanie sumują się do więcej, przewagi nie ma — i nikt "
            f"dotąd nie zmierzył, czy sumują się do mniej.")
        # SKALA ODNIESIENIA, ZEBY PROG NIE WISIAL W PROZNI. Sam spread rzedu
        # kilku punktow bazowych na noge zjada go w calosci przy progach
        # rzedu kilkunastu - i to jest wielkosc, ktora w produkcji LEZY
        # ZMIERZONA w `market.book_snapshot`, tylko nikt jej stad nie czyta.
        linie.append(
            f"  dla skali: koszt naliczony to {karta.policzone * 1e4:.2f} pb "
            f"rundy, czyli {karta.policzone / prog:.2f}× próg")

    # KROTNOSC SPREADU, PRZY KTOREJ PRZEWAGA SCHODZI DO ZERA.
    #
    # Wypisywana WYLACZNIE wtedy, gdy spread w karcie jest podloga z obcej
    # ksiegi - bo tylko wtedy pytanie brzmi „ile razy wiecej", a nie „ile".
    spread_skl = next((s for s in karta.skladniki if s.nazwa == "spread"), None)
    if spread_skl is not None and spread_skl.wartosc and not spread_skl.wiarygodny:
        k = krotnosc_zabojcza(prog, spread_skl.wartosc)
        linie.append("")
        if k is None:
            linie.append(
                f"KROTNOŚĆ SPREADU: nie ma czego liczyć — zapas jest "
                f"niedodatni ({_pb(prog)}), więc spread nie musi być szerszy "
                f"o nic, żeby przewagi nie było.")
        else:
            linie.append(
                f"KROTNOŚĆ SPREADU, PRZY KTÓREJ PRZEWAGA SCHODZI DO ZERA: "
                f"{k:.1f}× spreadu zmierzonego na obcej księdze "
                f"({spread_skl.wartosc * 1e4:.3f} pb).")
            linie.append(
                "  Nasz spread jest nieznany — arkusz czytamy z hosta "
                "globalnego, a zlecenia składamy na bybit.eu, który ma rzędu "
                "0,1% tamtego wolumenu (migracja 0018). To jest warunek do "
                "sprawdzenia, nie wynik: wystarczy jeden odczyt arkusza "
                "z właściwego hosta, żeby wiedzieć, po której stronie stoimy.")

    # DRABINA WRAZLIWOSCI ZAMIAST JEDNEJ LICZBY „PO UCZCIWYCH KOSZTACH".
    #
    # Pokusa jest tu oczywista: wstawic w miejsce nieznanego spreadu liczbe
    # „rozsadna" i podac jeden wynik. To zamienia „nie wiem" na „wiem i wynosi
    # tyle", czyli robi dokladnie to, przed czym ta warstwa ma bronic - tylko
    # z ladniejsza liczba niz poprzednio. Drabina pokazuje NACHYLENIE: o ile
    # Sharpe spada na kazdy punkt bazowy nienaliczonego kosztu. Nachylenie jest
    # policzone z danych i nie wymaga zadnego zalozenia; miejsce, w ktorym
    # strategia siada, kazdy widzi sam.
    if p["sharpe_zbiorczy"] is not None:
        linie.append("")
        linie.append("SHARPE PRZY RÓŻNYCH WARTOŚCIACH TEGO, CZEGO NIE ZMIERZONO "
                     "(dopłata na rundę):")
        linie.append("  dopłata |  zbiorczy | średnia po oknach")
        for bps in (0.0, 2.0, 5.0, 10.0, 20.0):
            z = podsumowanie_przewagi(zwroty, sharpe_okien,
                                      doplata=bps / 1e4)["sharpe_zbiorczy"]
            o = _sharpe_po_oknach(zwroty_okien, bps / 1e4)
            o_txt = "—" if o is None else f"{o:+.4f}"
            znacznik = "   <- dzisiejszy rachunek" if bps == 0 else ""
            linie.append(f"  {bps:6.1f} pb | {z:+9.4f} | {o_txt:>17}{znacznik}")
        linie.append(
            "  Obie kolumny liczone tym samym odchyleniem PRÓBKOWYM (n−1). "
            "`portfolio.metrics` liczy populacyjne (n), czyli przy medianie "
            "dwudziestu transakcji na okno zawyża każdy Sharpe o ok. 2,6% — "
            "zawsze w górę.")
    return linie


def _sharpe_po_oknach(zwroty_okien: list[list[float]],
                      doplata: float) -> float | None:
    """Srednia po oknach z Sharpe'a liczonego W OKNIE. Ta miara, ktora podaje
    dzisiejszy raport - przeliczona przy doliczonym koszcie.

    Okna, w ktorych Sharpe nie istnieje (jedna transakcja, zerowy rozrzut),
    WYPADAJA z tej sredniej zamiast wchodzic do niej jako zera. Zero jest tu
    konkretna wartoscia - „brak przewagi" - i podstawione za „nie da sie
    policzyc" ciagnie srednia do zera tym mocniej, im wiecej okien jest pustych.
    """
    s = [x for x in (sharpe_proby([r - doplata for r in okno])
                     for okno in zwroty_okien) if x is not None]
    return statistics.fmean(s) if s else None


def podsumowanie_przewagi(zwroty: list[float], sharpe_okien: list[float],
                          doplata: float = 0.0) -> dict:
    """Przewaga policzona ze ZLACZONEJ proby transakcji. CZYSTA FUNKCJA.

    DLACZEGO OBOK MIARY, KTORA JUZ JEST

    Raport walk-forward podaje `sharpe` jako SREDNIA PO OKNACH z ilorazow
    liczonych osobno w kazdym oknie. Srednia ilorazow nie jest ilorazem: okno
    z pieciu transakcji wazy w niej tyle samo, co okno z dwustu, a okno o malym
    rozrzucie dostaje duzy iloraz z samego mianownika. Obie liczby stoja tu
    obok siebie, bo to ICH ROZJAZD jest wynikiem - pojedyncza z nich niczego
    nie rozstrzyga.

    `doplata` TO KOSZT, KTOREGO SYMULACJA NIE NALICZA

    Ulamek ceny na cala runde - spread, niewypelnienie, finansowanie; patrz
    `koszty.karta_rundy`. Odejmuje sie go od KAZDEJ transakcji z osobna, bo tak
    wlasnie jest placony. Stala odjeta od kazdej obserwacji przesuwa srednia
    i zostawia rozrzut nietknietym; gdyby ruszyla takze rozrzut, bylby to blad
    rachunku, a nie koszt.

    `koszt_progowy` TO JEDYNA LICZBA TU, KTORA NIE WYMAGA ZADNEGO ZALOZENIA

    Sredni zwrot z transakcji przed doplata - czyli doplata, przy ktorej
    przewaga schodzi do zera. Dopoki spread i niewypelnienie nie sa zmierzone,
    uczciwe zdanie o przewadze brzmi „przezyje, jesli to, czego nie
    policzylismy, jest mniejsze niz tyle", a nie „wynosi tyle". Ujemny prog
    znaczy, ze zapasu nie ma juz teraz - i ma byc wypisany jako liczba ujemna,
    nie jako zero.
    """
    po = [r - doplata for r in zwroty]
    return {
        "n_transakcji": len(po),
        "n_okien": len(sharpe_okien),
        "sredni_zwrot": statistics.fmean(po) if po else None,
        "odchylenie": statistics.stdev(po) if len(po) > 1 else None,
        "sharpe_zbiorczy": sharpe_proby(po),
        # Ta sama liczba, ktora raport podaje dzis - zeby rozjazd byl widoczny,
        # a nie zeby ja zastapic po cichu.
        "sharpe_sredni_po_oknach": (statistics.fmean(sharpe_okien)
                                    if sharpe_okien else None),
        "okna": rozklad(sharpe_okien),
        "profit_factor_zbiorczy": profit_factor(po),
        "hit_rate": (sum(1 for r in po if r > 0) / len(po)) if po else None,
        # PRZED doplata, nie po - to jest zapas, ktory strategia ma na koszty,
        # a nie to, co z niego zostalo po doliczeniu czesci z nich.
        "koszt_progowy": statistics.fmean(zwroty) if zwroty else None,
    }


def przewaga_dsr(zwroty: list[float], n_prob: int) -> dict:
    """Deflowany Sharpe dla ZLACZONEJ proby transakcji OOS. CZYSTA FUNKCJA.

    Cienka warstwa nad `statystyka.deflowany_sharpe` - istnieje, zeby w jednym
    miejscu stalo, KTORA probe i KTORY licznik podstawiamy:

      * `zwroty` to te same transakcje, z ktorych liczy sie `sharpe_zbiorczy`
        w `podsumowanie_przewagi` - czyli zlaczona proba, a NIE Sharpe'y okien.
        Sharpe ze sredniej po oknach nie ma sensownego T do wzoru na PSR:
        „liczba obserwacji" byla by wtedy liczba okien, a koszt placi kazda
        transakcja z osobna.
      * `n_prob` to `learn.hypothesis_trial` policzone dla tego modelu.
        CZEGO TA LICZBA NIE JEST, jest opisane w `docs/01-data-architecture.md`
        i warto to miec przed oczami przy czytaniu wyniku: tabela ma
        `UNIQUE (model_id, config_hash)` i INSERT `ON CONFLICT DO NOTHING`,
        wiec liczy ROZNE WEKTORY WAG, a nie proby. Dwa przebiegi tej samej
        konfiguracji dokladaja tyle wierszy, ile wektorow sie zmienilo -
        a przy `--wybor ic` (domyslnym) jest to jeden na okno i zmienia sie
        co noc, bo granice okien sie przesuwaja.
    """
    return deflowany_sharpe(zwroty, n_prob)


def n_konfiguracji(cur, model_id: int) -> int:
    """Ile ROZROZNIALNYCH konfiguracji tego modelu kiedykolwiek policzono.

    `SELECT count(DISTINCT params) FROM learn.model_version WHERE model_id=%s`.
    To jest N deflacji. Zmierzone na produkcji 2026-09-26: tor 4h ma 143 wersje
    i 57 roznych `params`; caly projekt - 180 wersji i 86 roznych.

    DLACZEGO NIE `learn.hypothesis_trial`, CHOC TO ON MIAL BYC TYM LICZNIKIEM

    Bo policzylby te sama kare dwa razy. Kandydaci z `argmax` sa oceniani
    `ocen(..., train_slices, cache_tr)`, czyli WYLACZNIE na oknie treningowym,
    a raportowany Sharpe pochodzi z okna testowego, ktorego selekcja nigdy nie
    widziala. Walk-forward JUZ za te kandydatow zaplacil - tym, ze wynik liczy
    sie poza oknem wyboru. Doliczenie ich jeszcze do poprzeczki deflacji
    odejmowaloby klatwe zwyciezcy, ktora ten protokol wlasnie usunal.

    (Licznik ma 12 613 wierszy dla toru 4h, z czego - przy 143 przebiegach po
    najwyzej 40 okien, czyli sufitcie 5 720 z wariantu `ic` - co najmniej 6 893
    pochodza z realnego losowego przeszukiwania `argmax`. Nie jest wiec prawda,
    ze ten licznik rosnie samym kalendarzem; nieprawdziwy jest jego STATUS
    mianownika, nie jego pochodzenie.)

    DLACZEGO WLASNIE `model_version.params`

    Bo kazdy taki wiersz to jedna konfiguracja, ktorej wynik OUT-OF-SAMPLE ktos
    zobaczyl i na tej podstawie zdecydowal, co dalej. To jest dokladnie ta
    mnogosc, ktora zawyza raportowany wynik: wybor wariantu, `ENTER_PCTL`,
    swieca, sklad uniwersum, cennik.

    TA LICZBA JEST DOLNA GRANICA I TRZEBA TAK JA PODAWAC

    Do 2026-09-26 `params` nie zapisywalo `kind` ani `limit`, wiec przebiegi
    roznce sie samym cennikiem zlewaly sie w jedna konfiguracje. Historycznych
    wierszy to nie naprawia - 57 zostaje dolna granica dla przeszlosci.
    Prawdziwe N jest WIEKSZE, a wieksze N daje MNIEJSZY DSR, wiec blad tej
    liczby dziala na korzysc strategii.
    """
    cur.execute("SELECT count(DISTINCT params) AS n FROM learn.model_version"
                " WHERE model_id = %s", (model_id,))
    return cur.fetchone()["n"]


def opis_zrodla_n(n: int, trials_kumulatywnie: int) -> str:
    """Skad wzielo sie N - w tym samym zdaniu, co N.

    Bez tego 57 czyta sie jak pomiar zamkniety, a jest dolna granica. I bez
    tego nikt nie wie, ze 12 613 z `hypothesis_trial` NIE jest ta liczba -
    a wlasnie ten licznik komentarz przy migracji 0006 wskazywal jako zrodlo.
    """
    return (f"N = {n} rozróżnialnych konfiguracji z learn.model_version "
            f"(count(DISTINCT params)) — DOLNA GRANICA: `kind` i `limit` nie "
            f"były zapisywane do 2026-09-26, więc przebiegi różniące się samym "
            f"cennikiem zlewały się w jedną. Licznik learn.hypothesis_trial "
            f"({trials_kumulatywnie} wektorów wag) NIE jest tu mianownikiem: "
            f"jego kandydaci byli oceniani in-sample, a walk-forward już za nich "
            f"zapłacił, licząc wynik poza oknem wyboru.")


def opis_kosztu_dsr(doplata: float, nieznane: tuple[str, ...]) -> str:
    """Przy jakim koszcie policzono DSR. ZAWSZE, takze gdy doplata to zero.

    „DSR 0,96" bez tego zdania czyta sie jak wynik po kosztach. Jest po tych
    kosztach, ktore ktos ZMIERZYL - a lista `nieznane` mowi, czego nadal nie ma.
    """
    if doplata > 0:
        czesc = (f"DSR policzony PO doliczeniu {doplata * 1e4:.2f} pb na rundę, "
                 f"których symulacja nie nalicza")
    else:
        czesc = ("DSR policzony przy dopłacie ZERO — nie zmierzono ani jednego "
                 "składnika kosztu poza tymi, które nalicza symulacja")
    if nieznane:
        return czesc + (f"; nadal NIE ZMIERZONO: {', '.join(nieznane)}, więc ta "
                        f"liczba jest GÓRNYM oszacowaniem.")
    return czesc + "; karta kosztu jest pełna."


def werdykt_dsr(zwroty: list[float], *, doplata: float, n: int,
                trials_kumulatywnie: int,
                nieznane: tuple[str, ...]) -> tuple[bool, str]:
    """CALA decyzja DSR jednym wywolaniem - to jest to, co wola `train()`.

    Istnieje jako osobna funkcja po to, zeby dalo sie przetestowac SKLAD: ze
    DSR liczy sie po DOPLACIE, ze N bierze sie z konfiguracji, a nie z licznika
    hipotez, i ze obie liczby razem z ich pochodzeniem trafiaja do opisu.
    `bramka_dsr` sam tego nie upilnuje - nie ma jak sprawdzic, czym go
    nakarmiono.

    DOPLATA JEST TU NAJWAZNIEJSZA. Zmierzony spread 5,85 pb zbija Sharpe
    z +0,0952 na +0,0823, a to wystarcza, zeby DSR nie przeszedl progu przy
    ZADNYM N - takze przy najlagodniejszym. Rozstrzyga wiec koszt, nie selekcja,
    i dlatego werdykt liczony na zwrotach PRZED doplata bylby wada w kierunku
    latwiejszej bramki, niezaleznie od tego, jak dobrze policzono N.
    """
    po = [r - doplata for r in zwroty]
    wynik = przewaga_dsr(po, n)
    ok, opis = bramka_dsr(wynik)
    return ok, " ".join([opis, opis_kosztu_dsr(doplata, nieznane),
                         opis_zrodla_n(n, trials_kumulatywnie)])


def bramka_dsr(wynik: dict) -> tuple[bool, str]:
    """Czy deflowany Sharpe pozwala promowac. FAIL-CLOSED. CZYSTA FUNKCJA.

    BRAK WIEDZY WSTRZYMUJE PROMOCJE, NIE PRZEPUSZCZA JEJ. `dsr = None` znaczy
    „nie dalo sie policzyc" - za malo transakcji, zerowy licznik prob,
    nieobliczalna skosnosc. Kazdy z tych stanow jest POWODEM, zeby nie
    promowac: wynik, o ktorym nie wiadomo, czy nie jest artefaktem selekcji,
    nie rozni sie w skutkach od wyniku, ktory nim jest.

    DSR PODAWANY ZAWSZE Z N I LICZBA PROB, z tego samego powodu, dla ktorego
    odsetek dodatnich okien idzie zawsze z przedzialem ufnosci (patrz
    `statystyka.wilson`). Samo „DSR 0,96" jest nieczytelne: ta sama liczba przy
    17 probach i przy 11 460 opisuje dwa zupelnie rozne wyniki, bo poprzeczka,
    ktora DSR odejmuje, rosnie z liczba prob.
    """
    dsr, n, n_prob = wynik["dsr"], wynik["n_obserwacji"], wynik["n_prob"]
    if dsr is None:
        return False, (f"Deflowany Sharpe NIEOBLICZALNY ({wynik['powod']}) — "
                       f"promocja wstrzymana. Brak tej liczby nie jest wynikiem "
                       f"neutralnym: bez niej nie wiadomo, czy wynik nie jest "
                       f"najlepszym z wielu prób.")
    opis = (f"Deflowany Sharpe {dsr:.4f} przy progu {PROG_DSR:.2f} "
            f"(Sharpe {wynik['sharpe']:+.4f} z {n} transakcji, "
            f"{n_prob} przetestowanych konfiguracji — N SELEKCJI tego "
            f"przebiegu, nie licznik kumulatywny; poprzeczka selekcji "
            f"{wynik['prog']:+.4f}, skośność {wynik['skosnosc']:+.3f}, "
            f"kurtoza {wynik['kurtoza']:.3f}).")
    if dsr >= PROG_DSR:
        return True, opis
    return False, opis + (" Po uwzględnieniu liczby prób wynik nie jest "
                          "odróżnialny od najlepszego z tylu losowych — "
                          "promocja wstrzymana.")


def status_walidacji(total: int, positive: int, median: float | None,
                     bije_odniesienie: bool, dsr_ok: bool) -> str:
    """Werdykt bramki jako CZYSTA FUNKCJA - zeby dal sie sprawdzic testem.

    Do 2026-09-26 ten warunek byl wyrazeniem wpisanym w srodek `train()`
    dlugiego na dziewiecset linii, wiec nie istnial test, ktory by go dotykal.
    Warunki sa KONIUNKCJA i kazdy z nich moze tylko ODEBRAC `validated`:

      * `total >= MIN_WINDOWS_FOR_VALIDATION` - dosc okien, zeby cokolwiek orzec,
      * `pct >= 70` - odsetek okien dodatnich,
      * `median > 0,05` - skala wyniku w typowym oknie,
      * `bije_odniesienie` - przewaga nad kup-i-trzymaj przy TEJ SAMEJ ekspozycji,
      * `dsr_ok` - deflowany Sharpe powyzej progu, patrz `bramka_dsr`.
    """
    if total < MIN_WINDOWS_FOR_VALIDATION:
        return "learning"
    pct = positive / total * 100
    if (pct >= 70 and median is not None and median > 0.05
            and bije_odniesienie and dsr_ok):
        return "validated"
    return "degraded" if pct < 40 else "learning"


def prog_pokrycia(pokrycie: dict[str, int]) -> int:
    """Ile swiec musi miec instrument, zeby liczyl sie w tym oknie. CZYSTA FUNKCJA.

    Pytanie brzmi: czy instrument byl notowany przez WIEKSZOSC tego okna -
    a nie: ile godzin ma doba. Odniesieniem jest wiec najlepiej pokryty
    instrument w TYM oknie, a nie liczba swiec wyliczona z kalendarza.

    Wczesniej prog byl `train_days * 1440 / bar_minutes / 4`, czyli cwierc
    liczby swiec, ktora zmiescilaby sie w oknie na rynku notowanym BEZ PRZERWY.
    Dla krypto to ta sama liczba (2 158 swiec 1h w 90 dniach, cwierc to 539
    wobec dawnych 540 - roznica jednej swiecy nie przesuwa zadnego
    instrumentu). Dla gieldy amerykanskiej okno 90 dni to okolo 62 sesji po
    siedem swiec, czyli 521-524 swiece przy progu 540: NIE PRZECHODZIL ZADEN
    instrument, `aktywne` bylo puste, kazde okno odpadalo na MIN_INSTRUMENTS,
    a walk-forward konczyl sie z zerem okien i komunikatem "za malo
    informatywnych okien" - brzmiacym jak brak danych, a nie jak prog policzony
    dla innego rynku.
    """
    return max(MIN_SWIEC_OKNA, max(pokrycie.values(), default=0) // 4)


def cechy_nadazaja(najnowsza_cecha, ostatnia_swieca, max_staleness_min: int) -> bool:
    """Czy potok cech nadaza za DANYMI. CZYSTA FUNKCJA.

    Odniesieniem jest ostatnia swieca zrodlowa, nie zegar. Cecha powstaje po
    zamknieciu swiecy, wiec przy zdrowym potoku zostaje w tyle najwyzej
    o swiece-dwie - niezaleznie od tego, czy rynek akurat handluje.

    Wczesniej liczylo sie `now - najnowsza_cecha`, czyli zalozenie rynku
    ciaglego. Na gieldzie ostatnia swieca zamyka sie o 20:00 UTC, a nastepna
    przychodzi nazajutrz o 13:00: w pelni aktualna cecha ma nad ranem 800
    minut, a po weekendzie ponad 3900, przy progu 180. Zadanie cykliczne
    trenera chodzi o 02:40 UTC, czyli ZAWSZE przy zamknietej gieldzie - kazdy
    nocny trening equity przerywalby sie, meldujac awarie dzialajacego potoku.

    CZEGO TO NIE LAPIE: sytuacji, w ktorej stanely takze swiece - wtedy cechy
    nadazaja za danymi, bo dane tez stoja. To zadanie `dozor.py --co swiezosc`,
    ktory patrzy wprost na `market.candle`.
    """
    spoznienie_min = (ostatnia_swieca - najnowsza_cecha).total_seconds() / 60
    return spoznienie_min <= max_staleness_min

SEED = 20260827


# ------------------------------------------------------------------ dane
def load_panel(cur, instrument_id: int, h: Horizon,
               since: datetime, until: datetime, fp: str | None = None) -> list[dict]:
    """Panel cech w ukladzie czas -> {rola: wartosc}, plus cena swiecy.

    Cechy filtrujemy po `params->>'bar'`, wiec panel horyzontu 4 h nigdy nie
    zobaczy cech policzonych na swiecy 5-minutowej. Klucze slownika to ROLE,
    dzieki czemu sygnaly nie musza znac horyzontu.

    ODCISK UNIWERSUM (`fp`) jest tu KONIECZNY, nie ozdobny. Klucze slownika to
    role, a `jsonb_object_agg` po roli scala wiersze - wiec dwa warianty cechy
    przekrojowej, policzone dla roznych zbiorow, zderzylyby sie w jednym kluczu
    i wygralby przypadkowy. Bez tego filtru pierwszy eksperyment na innym
    uniwersum po cichu zmienialby wynik przebiegu produkcyjnego.

    STRAZNIK BITEMPORALNY - `fv.ingested_at <= fv.event_time + DWIE swiece`:

    Znacznik `event_time` cechy to POCZATEK swiecy, a wartosc liczy sie z jej
    ZAMKNIECIA - czyli jest znana dopiero po jednej swiecy. Druga swieca to
    zapas na policzenie i zapis. Cecha, ktora trafila do bazy pozniej, w tamtym
    momencie NIE byla dostepna i nie wolno na niej trenowac.

    Wczesniejsza wersja tego zapytania nie filtrowala po `ingested_at` w ogole,
    a potok datowal cechy na `poczatek_swiecy + 3 sekundy` - twierdzil wiec, ze
    wartosc byla znana trzy sekundy po OTWARCIU swiecy, choc pochodzila z jej
    zamkniecia cztery godziny pozniej. Straznik przepuszczal to bez mrugniecia,
    bo sam byl dobrany pod te bledna date.
    """
    if h.aggregate:
        price_cte = """
            SELECT time_bucket(%(bar)s::interval, event_time) AS t,
                   (array_agg(close ORDER BY event_time DESC))[1] AS close,
                   max(high) AS high, min(low) AS low
            FROM market.candle
            WHERE instrument_id = %(iid)s AND timeframe = %(tf)s
              AND event_time BETWEEN %(since)s AND %(until)s
            GROUP BY 1
        """
    else:
        price_cte = """
            SELECT event_time AS t, close, high, low
            FROM market.candle
            WHERE instrument_id = %(iid)s AND timeframe = %(tf)s
              AND event_time BETWEEN %(since)s AND %(until)s
        """

    cur.execute(
        f"""
        WITH f AS (
            SELECT fv.event_time, fd.params->>'role' AS role, fv.value
            FROM feat.feature_value fv
            JOIN feat.feature_def fd ON fd.id = fv.feature_def_id
            WHERE fv.instrument_id = %(iid)s
              AND fv.event_time BETWEEN %(since)s AND %(until)s
              AND fd.params->>'bar' = %(bar_name)s
              AND (fd.params->>'uniwersum' IS NULL
                   OR fd.params->>'uniwersum' = %(fp)s)
              AND fv.ingested_at <= fv.event_time + 2 * %(bar)s::interval
        ),
        p AS ({price_cte})
        SELECT f.event_time AS t, jsonb_object_agg(f.role, f.value) AS feats,
               p.close, p.high, p.low
        FROM f JOIN p ON p.t = f.event_time
        GROUP BY f.event_time, p.close, p.high, p.low ORDER BY f.event_time
        """,
        {"iid": instrument_id, "since": since, "until": until, "tf": h.source_timeframe,
         "bar": f"{h.bar_minutes} minutes", "bar_name": h.bar, "fp": fp},
    )
    return [{"t": r["t"], "f": {k: float(v) for k, v in r["feats"].items()},
             "price": float(r["close"]), "high": float(r["high"]),
             "low": float(r["low"])} for r in cur.fetchall()]


def atr_threshold(panel: list[dict]) -> float | None:
    vals = [r["f"]["atr_pct"] for r in panel if "atr_pct" in r["f"]]
    if len(vals) < 50:
        return None
    vals.sort()
    return vals[int(len(vals) * 0.8)]


# ------------------------------------------------------------------ symulacja
def score_threshold(panel: list[dict], weights: dict[str, dict[str, float]],
                    atr_p80: float | None, h: Horizon,
                    cache: list[tuple] | None = None) -> tuple[float, float]:
    """Progi wejscia i wyjscia z rozkladu |wyniku| na TYM panelu.

    Wolno to policzyc wylacznie na oknie treningowym. Policzone na testowym
    byloby zagladaniem w przyszlosc - prog znalby wtedy rozklad, ktorego
    w tamtym momencie nie dalo sie znac.
    """
    scores = []
    for i, row in enumerate(panel):
        if cache is not None:
            regime, sygnaly = cache[i]
            sc, conf, _ = combine(sygnaly, regime, weights.get(regime), with_contributions=False)
        else:
            regime, _ = classify_regime(row["f"], atr_p80)
            sc, conf, _ = evaluate(row["f"], regime, weights.get(regime), h)
        if conf >= MIN_CONF:
            scores.append(abs(sc))
    if len(scores) < 50:
        return 0.25, 0.12
    scores.sort()
    enter = scores[min(len(scores) - 1, int(len(scores) * ENTER_PCTL))]
    enter = max(enter, 1e-4)
    return enter, enter * EXIT_RATIO


def martwe_sygnaly(cache: dict[str, list[tuple]], prog: float = 0.01) -> set[str]:
    """Sygnaly, ktore na tym oknie NIE ZMIENIAJA sie wcale.

    Przyklad z produkcji: `news_shock` bez klucza Anthropic zwraca zero przy
    kazdej swiecy. Losowe przeszukiwanie wag przypisalo mu mimo to 0,169 w rezimie
    konsolidacji - najwiecej ze wszystkich sygnalow w tym rezimie.

    Nie bylo to obojetne. Martwy sygnal:
      * zjada czesc budzetu wag, rozcienczajac te, ktore cos wnosza,
      * ZANIZA PEWNOSC, bo wchodzi do mianownika (conf_den += weight) z wlasna
        pewnoscia 0,1 - a pewnosc to wlasnie ta wielkosc, ktorej kalibracje
        mierzymy,
      * dostaje wage zupelnie przypadkowa, ktora w dniu wpiecia klucza nagle
        zaczyna dzialac, nigdy nie bedac skalibrowana.

    Sygnal uznajemy za martwy, gdy niezerowy wynik pojawia sie rzadziej niz
    w `prog` czesci swiec.
    """
    liczniki = {name: 0 for name in SIGNALS}
    razem = 0
    for wpisy in cache.values():
        for _regime, sygnaly in wpisy:
            razem += 1
            for name, out in sygnaly.items():
                if abs(out.score) > 1e-9:
                    liczniki[name] += 1
    if razem == 0:
        return set()
    return {name for name, n in liczniki.items() if n / razem < prog}


def _bez_martwych(wagi: dict[str, dict[str, float]],
                  martwe: set[str]) -> dict[str, dict[str, float]]:
    """Zeruje wagi martwych sygnalow i renormalizuje reszte.

    Normalizacja po sumie WARTOSCI BEZWZGLEDNYCH, bo wagi moga byc ujemne -
    suma zwykla moglaby wyjsc bliska zeru i rozdmuchac pozostale wagi.
    """
    if not martwe:
        return wagi
    out = {}
    for regime, w in wagi.items():
        total = sum(abs(v) for k, v in w.items() if k not in martwe) or 1.0
        out[regime] = {k: (0.0 if k in martwe else round(w[k] / total, 4)) for k in w}
    return out


def ic_weights(panels: dict[str, list[dict]], cache: dict[str, list[tuple]],
               martwe: set[str], horyzont_swiec: int = 3) -> dict[str, dict[str, float]]:
    """Wagi WYLICZONE ze zdolnosci predykcyjnej sygnalow, nie wylosowane.

    DLACZEGO LOSOWE PRZESZUKIWANIE NIE DZIALALO

    Przestrzen wag ma 40 wymiarow (8 sygnalow x 5 rezimow). Szescdziesiat
    losowych prob to w takiej przestrzeni nic - "uczenie" sprowadzalo sie do
    wybrania najlepszego z 60 losowych punktow.

    Widac to bylo wprost: po dopuszczeniu wag ujemnych tylko 48% znakow wag
    zgadzalo sie ze zmierzonym wspolczynnikiem informacji, czyli dokladnie tyle,
    ile daja znaki losowe. Sygnal volume_breakout ma IC -0,039 - najmocniejsze
    w calym zestawie - a losowanie przypisalo mu +0,395 w rezimie trendu, czyli
    odwrotnie.

    ZAMIAST SZUKAC PO OMACKU - POLICZYC

    Waga sygnalu proporcjonalna do jego korelacji rangowej z przyszlym zwrotem,
    zmierzonej NA OKNIE TRENINGOWYM. To estymator o niskiej wariancji, ktory
    uzywa wszystkich obserwacji z okna, zamiast probkowac 60 punktow
    z czterdziestowymiarowej kostki.

    Znak wychodzi sam: sygnal o ujemnym IC dostaje ujemna wage, czyli zostaje
    odwrocony - bez zgadywania.

    NADAL OUT-OF-SAMPLE: IC liczy sie wylacznie z okna treningowego, a wynik
    ocenia sie na testowym, ktorego ta funkcja nigdy nie oglada.
    """
    # {rezim: {sygnal: ([wyniki], [zwroty])}}
    zebrane: dict[str, dict[str, tuple[list, list]]] = {}
    for sym, rows in panels.items():
        ceny = [r["price"] for r in rows]
        for i, (regime, sygnaly) in enumerate(cache[sym]):
            j = i + 1 + horyzont_swiec
            if j >= len(ceny) or ceny[i + 1] == 0:
                continue
            zwrot = ceny[j] / ceny[i + 1] - 1
            for name, out in sygnaly.items():
                if name in martwe or out.score == 0.0:
                    continue
                w = zebrane.setdefault(regime, {}).setdefault(name, ([], []))
                w[0].append(out.score)
                w[1].append(zwrot)

    # IC liczone na calym oknie - zapas dla rezimow o malej liczbie swiec.
    ogolne: dict[str, tuple[list, list]] = {}
    for per_sig in zebrane.values():
        for name, (xs, ys) in per_sig.items():
            w = ogolne.setdefault(name, ([], []))
            w[0].extend(xs)
            w[1].extend(ys)

    out: dict[str, dict[str, float]] = {}
    for regime in REGIMES:
        surowe = {}
        for name in SIGNALS:
            if name in martwe:
                surowe[name] = 0.0
                continue
            xs, ys = zebrane.get(regime, {}).get(name, ([], []))
            ic = spearman(xs, ys)
            if ic is None:
                xs, ys = ogolne.get(name, ([], []))
                ic = spearman(xs, ys)
            surowe[name] = ic or 0.0
        total = sum(abs(v) for v in surowe.values())
        if total < 1e-9:
            out[regime] = dict(DEFAULT_WEIGHTS[regime])
        else:
            out[regime] = {k: round(v / total, 4) for k, v in surowe.items()}
    return out


def random_weights(rng: random.Random) -> dict[str, dict[str, float]]:
    """Losowy zestaw wag per rezim, znormalizowany do sumy WARTOSCI BEZWZGLEDNYCH.

    WAGI MOGA BYC UJEMNE - i to jest zmiana, nie drobiazg.

    Poprzednia wersja losowala z przedzialu [0,1), wiec kalibracja mogla sygnal
    najwyzej WYCISZYC, nigdy ODWROCIC. Przy nieujemnych wagach zdanie "ten
    sygnal trafnie porzadkuje sytuacje, tylko w druga strone" bylo niewyrazalne.

    Pomiar wspolczynnika informacji (services/engine/diagnose_signals.py) pokazal,
    ze to ograniczenie kosztuje. Na czterech latach i 24 instrumentach:

        mean_reversion_rsi  IC = +0,036     powrot do sredniej
        range_extreme       IC = +0,019     powrot do sredniej
        cross_section       IC = -0,015     momentum
        momentum_multi      IC = -0,019     momentum
        trend_follow        IC = -0,022     momentum
        volume_breakout     IC = -0,039     momentum

    Wzor jest jednoznaczny: KAZDY sygnal powrotu do sredniej ma IC dodatnie,
    KAZDY sygnal momentum - ujemne. Skladniki niosa informacje; cztery z nich
    niosa ja z odwroconym znakiem. Nieujemne wagi pozwalaly tylko zblizyc te
    cztery do zera, przez co dodatnie i ujemne wklady sie znosily - i stad
    wynik dokladnie 50% dodatnich okien.

    CENA: przestrzen hipotez rosnie dwukrotnie, bo doszedl wybor znaku przy
    kazdym sygnale. Walk-forward placi za to uczciwie - znak wybierany jest
    WYLACZNIE na oknie treningowym, a oceniany poza nim. Licznik hipotez rosnie
    odpowiednio i przy kazdym przyszlym wyniku trzeba go czytac razem z nim.

    Normalizacja idzie po sumie wartosci bezwzglednych, wiec laczny "budzet"
    wagi pozostaje taki sam jak przy wagach nieujemnych.
    """
    out = {}
    for regime in REGIMES:
        raw = {name: rng.uniform(-1.0, 1.0) for name in SIGNALS}
        total = sum(abs(v) for v in raw.values()) or 1.0
        out[regime] = {k: round(v / total, 4) for k, v in raw.items()}
    return out


# --------------------------------------------------- bramka promocji modelu
#
# CO TA BRAMKA ZAMYKA
#
# Do tej pory kazdy nocny przebieg, ktory nie wyszedl `degraded`, stawal sie
# `active` - a `active_model` w petli decyzyjnej handluje aktywna wersja od
# razu, bez pytania. Znaczylo to, ze liczby z docs/05-ochrona-kapitalu.md
# (najnizszy punkt 9 997 USD, najgorsze obsuniecie -2,85%) opisywaly wagi,
# ktorymi system moze juz nie handlowac tydzien pozniej. Nic sie przy tym nie
# psuje i nic nie krzyczy: po prostu handluje co innego niz to, co zmierzono.
#
# Na rachunku papierowym 100 000 to byl poprawny wybor - szybka nauka jest tam
# warta wiecej niz ostroznosc, bo pomylka kosztuje zero. Na 10 000 realnych ta
# sama automatyka zmienia profil ryzyka bez niczyjej zgody.


def tryb_handlu(cur, engine: str) -> str:
    """Czy ten silnik handluje NA ZYWO. Czytane z bazy, nie z flagi trenera.

    Tryb jest wlasnoscia dzialajacego silnika, nie przebiegu treningu. Gdyby
    trener dostal wlasna flage `--mode`, powstalaby druga deklaracja tego samego
    faktu - a rozjazd dwoch deklaracji nie objawia sie awaria, tylko otwarta
    bramka przy zamknietych drzwiach (values.yaml ostrzega przed dokladnie tym
    przy `--kind` i `--limit`). Silnik sam wpisuje swoj tryb do pulsu przy
    kazdym cyklu, wiec pytamy jego.

    Nieswiezosc pulsu jest tu CELOWO nieistotna. Silnik live zatrzymany na dobe
    kill-switchem nadal ma otwarte pozycje i wroci do handlu - podmiana wag pod
    nim jest wtedy tak samo niezapowiedziana jak przy silniku dzialajacym.

    Zlecenia live sa drugim, trwalym swiadectwem: puls to jeden wiersz na
    silnik i po powrocie na demo mowi juz tylko o terazniejszosci, a rachunek,
    na ktorym cos kiedys kupiono za prawdziwe pieniadze, pozostaje rachunkiem
    prawdziwym.
    """
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM risk.engine_heartbeat"
        "                WHERE engine::text = %s AND mode = 'live')"
        "    OR EXISTS (SELECT 1 FROM exec.order"
        "                WHERE engine::text = %s AND mode = 'live') AS zywe",
        (engine, engine))
    return "live" if cur.fetchone()["zywe"] else "demo"


def status_nowej_wersji(status: str, tryb: str, zgoda: bool = False) -> tuple[str, str]:
    """Czy nowa wersja wchodzi do produkcji sama: ('active'|'candidate', powod).

    Czysta funkcja, tak jak `dozor.zmiany`: caly werdykt bramki da sie
    sprawdzic bez bazy i bez przebiegu treningu.

    Bramka jest ROZNA W ZALEZNOSCI OD TRYBU, bo cena bledu jest rozna. W demo
    zla wersja kosztuje jeden dzien papierowego wyniku i wiedze, ze byla zla -
    zatrzymywanie jej czlowiekiem opoznialoby nauke bez powodu. W live ta sama
    podmiana zmienia strategie, na ktorej skalibrowano ochrone kapitalu, i robi
    to o 4:40 nad ranem.

    Zgoda jest JAWNA I JEDNORAZOWA (flaga przebiegu), a nie ustawieniem
    w bazie: ustawienie raz wlaczone zostaje wlaczone na zawsze i po pol roku
    nikt nie pamieta, ze bramka jest podniesiona.
    """
    if status == "degraded":
        # Zachowanie sprzed bramki, zostawione bez zmian: wynik bez przewagi
        # nie promuje sie w zadnym trybie.
        return "candidate", "wynik degraded — wersja zostaje kandydatem"
    if tryb == "live" and not zgoda:
        return "candidate", ("tryb live — nowa wersja czeka na zatwierdzenie "
                             "człowieka (`--zatwierdz-promocje`)")
    if tryb == "live":
        return "active", "tryb live, promocja zatwierdzona jawnie"
    return "active", "tryb demo — promocja automatyczna"


def zapisz_promocje(cur, model_id: int, mv_id: int, docelowy: str) -> None:
    """Nadanie statusu nowej wersji. WYCOFANIE POPRZEDNIEJ TYLKO PRZY PROMOCJI.

    Wycofanie bylo w kodzie bezwarunkowe, bo przed bramka nowa wersja zawsze
    wchodzila na jej miejsce. Zostawione bezwarunkowo byloby gorsze niz brak
    bramki: `active_model` nie znalazlby zadnej aktywnej wersji i cofnalby
    produkcje do zalozen poczatkowych - czyli bramka majaca zapobiec cichej
    zmianie strategii sama by ja co noc wywolywala.
    """
    if docelowy == "active":
        # KOLEJNOSC MA ZNACZENIE: najpierw wycofujemy poprzednia wersje, dopiero
        # potem promujemy nowa. Odwrotna kolejnosc lamie unikalny indeks
        # "jedna aktywna wersja na model".
        cur.execute("UPDATE learn.model_version SET status='retired', retired_at=now()"
                    " WHERE model_id=%s AND id<>%s AND status='active'", (model_id, mv_id))
        cur.execute("UPDATE learn.model_version SET status='active', promoted_at=now()"
                    " WHERE id=%s", (mv_id,))
    else:
        # `promoted_at` zostaje puste: kandydat nie zostal promowany, a data
        # promocji wpisana "na wszelki wypadek" klamie w jedynym miejscu, gdzie
        # pozniej szuka sie odpowiedzi, kiedy strategia sie zmienila.
        cur.execute("UPDATE learn.model_version SET status='candidate' WHERE id=%s",
                    (mv_id,))


def wersja_produkcyjna(cur, model_id: int, mv_id: int) -> int | None:
    """Numer wersji, ktora zostaje na produkcji, gdy nowa nie wchodzi."""
    cur.execute("SELECT version FROM learn.model_version"
                " WHERE model_id=%s AND id<>%s AND status='active'", (model_id, mv_id))
    row = cur.fetchone()
    return row["version"] if row else None


# ------------------------------------------------------------------ trening
def train(conn, engine: str, days: int, trials_per_window: int, h: Horizon,
          kind: str = "spot", egzekucja: Egzekucja | None = None,
          sucho: bool = False, wybor: str = "argmax",
          tylko: set[str] | None = None,
          zakladane_pozycje: int | None = None,
          zgoda_na_promocje: bool = False,
          spread_bps: float | None = None,
          spread_nasza_ksiega: bool = True) -> None:
    cur = conn.cursor()
    # KARTA KOSZTU, NIE JEDNA LICZBA. Wymienia z nazwy skladniki, ktorych
    # symulacja nie nalicza, zamiast wliczac je jako zera - patrz `koszty.py`.
    # `spread_bps` zostaje None, dopoki nikt nie poda POMIARU; wymyslona liczba
    # zamienilaby tu „nie wiem" na „wiem i wynosi tyle".
    karta = karta_rundy(kind, (egzekucja or Egzekucja()).limit,
                        spread_bps=spread_bps,
                        spread_nasza_ksiega=spread_nasza_ksiega)
    rng = random.Random(SEED)
    now = datetime.now(timezone.utc)

    # `tradeable` decyduje o uniwersum produkcyjnym - ta sama flaga, ktora czyta
    # petla decyzyjna (0020). Flaga --instrumenty sluzy WYLACZNIE eksperymentom
    # i zaweza to, co juz z bazy wyszlo; nie potrafi dodac niczego z powrotem.
    cur.execute("SELECT id, symbol, quote_ccy FROM ref.instrument WHERE engine=%s AND active"
                " AND tradeable AND kind <> 'perp' ORDER BY symbol", (engine,))
    instruments = cur.fetchall()
    # NADPISANIE uniwersum na potrzeby eksperymentu.
    #
    # Bez tej flagi trener bierze uniwersum decyzyjne z bazy (`tradeable`) - to
    # samo, ktore obstawia petla. Z flaga wybiera sposrod WSZYSTKICH aktywnych,
    # takze niehandlowalnych, bo inaczej nie dalo by sie porownac uniwersum
    # kandydujacego z obowiazujacym PRZED jego wlaczeniem. Porownanie ma
    # poprzedzac przelaczenie, nie odwrotnie.
    #
    # Dlatego flaga wymaga --sucho: przebieg na uniwersum, ktorego produkcja nie
    # obstawia, nie moze promowac wersji modelu.
    if tylko:
        cur.execute("SELECT id, symbol, quote_ccy FROM ref.instrument WHERE engine=%s AND active"
                    " AND kind <> 'perp' ORDER BY symbol", (engine,))
        instruments = cur.fetchall()
        znane = {i["symbol"] for i in instruments}
        brakujace = sorted(set(tylko) - znane)
        if brakujace:
            print(f"  pominięte, brak w bazie: {', '.join(brakujace)}")
        instruments = [i for i in instruments if i["symbol"] in tylko]
    if not instruments:
        # PUSTE UNIWERSUM TO AWARIA PRZEBIEGU, NIE JEGO WYNIK.
        #
        # ZMIERZONE NA PRODUKCJI 2026-09-24: tor equity w `trader-trainer`
        # wypisal "brak instrumentow", `train()` zrobilo `return`, a `main()`
        # i tak konczylo `return 0`. Zadanie swiecilo na ZIELONO, w panelu nie
        # przybywalo wersji modelu, a petla decyzyjna equity odmawiala handlu
        # z powodu "brak zwalidowanego modelu" - trzy objawy, z ktorych zaden
        # nie wskazywal na przyczyne. Widac ja bylo wylacznie w logu zadania,
        # ktore meldowalo sukces.
        #
        # Trening, ktory nie policzyl niczego, nie ma prawa wygladac jak trening,
        # ktory sie udal - i to zostaje w mocy. Do 2026-09-25 szedl tu kod 2, ten
        # sam co przy zbyt krotkim panelu nizej; rozdzielenie ich opisuje akapit
        # ponizej i komentarz przy stalych `KOD_*`.
        #
        # Dla equity przyczyna byla `tradeable = false` na wszystkich 21
        # instrumentach - ustawione swiadomie w migracji 0047, z adnotacja, ze
        # wlaczenie do uniwersum to osobna migracja PO pierwszym walk-forwardzie.
        #
        # KOD ZMIENIONY 2026-09-25 Z 2 NA `KOD_TOR_NIEGOTOWY`. Nie jest to
        # zluzowanie bramki - bramka odmawia dokladnie tak samo i wypisuje to
        # samo. Chodzi o to, ze `tradeable = false` ustawil tu CZLOWIEK migracja
        # i z adnotacja "osobna migracja PO pierwszym walk-forwardzie", wiec ten
        # stan nie jest awaria do naprawienia dzisiaj. Kod 2 mowil wolajacemu
        # "cos jest zepsute", a skrypt zadania na tej podstawie zabijal wynik
        # toru crypto, ktory wlasnie sie policzyl. Uzasadnienie przy stalych.
        print("PRZERWANO: puste uniwersum decyzyjne — nie ma czego trenować")
        print(f"  sprawdź: SELECT count(*) FROM ref.instrument WHERE engine="
              f"'{engine}' AND active AND tradeable AND kind <> 'perp'")
        print("  (flagę `tradeable` ustawia services/ingest/instrumenty.py "
              "oraz migracje; `--instrumenty` z `--sucho` pozwala zmierzyć "
              "uniwersum kandydujące bez wpisywania go do produkcji)")
        print(f"  tor {engine}/{h.bar} NIEURUCHOMIONY — nic nie policzono "
              f"i nic nie zapisano (kod {KOD_TOR_NIEGOTOWY})")
        raise SystemExit(KOD_TOR_NIEGOTOWY)

    # BRAMKA DANYCH: CENY SKORYGOWANE O PODZIALY AKCJI.
    #
    # `services/exchanges/alpaca.py` pobiera swiece z `adjustment: "raw"` - dlug
    # swiadomy i tam opisany. Skutek dla treningu jest jednak nie do przyjecia:
    # podzial 10:1 wyglada w szeregu jak spadek o 90% (zmierzone na produkcji
    # 2026-09-24: NVDA -90,25%, AVGO -90,07%, NFLX -90,12%), `ret_long` siega
    # 168 swiec, a bufor EMA 288, wiec JEDNO takie zdarzenie zatruwa okolo 670
    # swiec po sobie - i wchodzi do kalibracji jako obserwacja kilkunastosigmowa.
    #
    # DLACZEGO BRAMKA, A NIE PRZEJSCIE NA `adjustment=split`. Bo to nie jest
    # zmiana jednego slowa: Alpaca koryguje ceny wzgledem DZIS, wiec kazdy
    # kolejny podzial uniewaznia cala zapisana historie i wymaga POWTORZENIA
    # backfillu. Ta decyzja - i sam backfill - naleza do wlasciciela; bramka
    # pilnuje tylko, zeby trening nie ruszyl PRZED nia i nie wyprodukowal wersji
    # modelu, ktora wyglada na policzona.
    #
    # ZNACZNIK, NIE DOMYSL. Pytamy o jawne potwierdzenie w `ref.instrument.meta`,
    # bo z samych swiec nie da sie odczytac, czy ktos je juz skorygowal:
    # nieskorygowany szereg wyglada jak szereg z gwaltownym ruchem. Nazwa
    # znacznika pochodzi z modulu, ktory o korekcie DECYDUJE (adapter Alpaki) -
    # jedna nazwa w jednym miejscu, zamiast dwoch literalow do rozjechania.
    if engine == "equity":
        from services.exchanges.alpaca import ZNACZNIK_KOREKTY_PODZIALOW
        # PYTAMY O DOKLADNIE TEN ZBIOR, KTORY WEJDZIE DO TRENINGU (takze po
        # zawezeniu flaga `--instrumenty`), a nie o cale uniwersum decyzyjne.
        # Inaczej bramka mowilaby o instrumentach, ktorych ten przebieg nie
        # dotyka - i zapalalaby sie albo milczala nie o tym, co liczy.
        #
        # `meta->klucz <> 'true'::jsonb` z COALESCE, a nie `(meta->>klucz)::boolean`:
        # rzutowanie na boolean wywraca zapytanie na dowolnej innej tresci
        # w tym polu, czyli bramka danych umialaby paść z powodu danych.
        cur.execute(
            "SELECT count(*) AS bez FROM ref.instrument WHERE id = ANY(%s)"
            "   AND COALESCE(meta->%s, 'false'::jsonb) <> 'true'::jsonb",
            ([i["id"] for i in instruments], ZNACZNIK_KOREKTY_PODZIALOW))
        bez_korekty = cur.fetchone()["bez"]
        if bez_korekty:
            print("PRZERWANO: świece equity nie są potwierdzone jako "
                  "skorygowane o podziały akcji")
            print(f"  {bez_korekty} z {len(instruments)} instrumentów uniwersum "
                  f"nie ma znacznika `meta->'{ZNACZNIK_KOREKTY_PODZIALOW}' = true`")
            print("  podział 10:1 to w szeregu −90% zwrotu, a `ret_long` sięga "
                  "168 świec: jedno zdarzenie zatruwa ~670 świec")
            print("  wymagane PRZED treningiem: adjustment=split w adapterze "
                  "Alpaki, pełny backfill `equity 1h`, potem znacznik")
            print(f"  tor {engine}/{h.bar} NIEURUCHOMIONY — nic nie policzono "
                  f"i nic nie zapisano (kod {KOD_TOR_NIEGOTOWY})")
            raise SystemExit(KOD_TOR_NIEGOTOWY)
    if tylko:
        print(f"  uniwersum zawężone do {len(instruments)}: "
              f"{', '.join(i['symbol'] for i in instruments)}")

    # ODCISK UNIWERSUM. Cechy przekrojowe sa nim filtrowane - patrz load_panel.
    #
    # Gdy dla tego zbioru nikt jeszcze nie policzyl sily wzglednej, sygnal
    # bedzie martwy we wszystkich oknach. To jest poprawne zachowanie (lepsze
    # niz czytanie wartosci policzonej dla innego zbioru), ale MUSI byc
    # powiedziane wprost: cichy brak sygnalu wyglada jak sygnal bez tresci.
    waluta_symbolu = {i["symbol"]: i["quote_ccy"] for i in instruments}
    fp = odcisk([i["symbol"] for i in instruments])
    cur.execute("SELECT count(*) AS n FROM feat.feature_def"
                " WHERE active AND params->>'bar' = %s AND params->>'uniwersum' = %s",
                (h.bar, fp))
    if not cur.fetchone()["n"]:
        print(f"  UWAGA: brak cech przekrojowych dla tego uniwersum (odcisk {fp}).")
        print(f"         Siła względna będzie martwa. Policz je najpierw:")
        print(f"         python services/ingest/features.py --bar {h.bar} --backfill "
              f"--days {days} --tylko-przekroj"
              + (f" --instrumenty {','.join(i['symbol'] for i in instruments)}"
                 if tylko else ""))

    cur.execute(
        "INSERT INTO learn.model (name, engine, kind, description) VALUES (%s,%s,%s,%s)"
        " ON CONFLICT (name) DO UPDATE SET kind = EXCLUDED.kind RETURNING id",
        (f"{engine}_signal_weighting_{h.bar}", engine, "signal_weighting",
         f"Kalibracja wag sygnałów per reżim rynkowy metodą walk-forward, świeca {h.bar}"))
    model_id = cur.fetchone()["id"]

    cur.execute("SELECT COALESCE(max(version), 0) + 1 AS v FROM learn.model_version"
                " WHERE model_id = %s", (model_id,))
    version = cur.fetchone()["v"]

    cur.execute("SELECT array_agg(id) AS ids FROM feat.feature_def"
                " WHERE active AND params->>'bar' = %s"
                "   AND (params->>'uniwersum' IS NULL OR params->>'uniwersum' = %s)",
                (h.bar, fp))
    feature_ids = cur.fetchone()["ids"] or []
    cur.execute("SELECT array_agg(id) AS ids FROM sig.signal_def WHERE engine = %s", (engine,))
    signal_ids = cur.fetchone()["ids"] or []

    # SHA KODU I SKLAD SYGNALOW - RAZEM, BO OSOBNO NIE ZNACZA NIC.
    #
    # `git_sha` istnieje od migracji 0006 i do tej pory nikt go nie wypelnial:
    # 131 wierszy, 131 NULL-i. Bez niego przy metrykach modelu nie da sie
    # powiedziec, jaki kod je policzyl - a rozjazd trening-produkcja zdarzyl sie
    # w tym projekcie kilka razy i za kazdym razem pierwsze pytanie brzmialo
    # "od kiedy?".
    #
    # Sam commit jednak nie wystarcza: `SYGNALY_EKSPERYMENTALNE` zmienia sklad
    # sygnalow zmienna srodowiskowa, wiec ten sam commit potrafi wytrenowac dwa
    # rozne modele. Zapisujemy ROZSTRZYGNIETY zbior (to, co faktycznie weszlo do
    # `precompute`), a nie surowa tresc zmiennej - pusta zmienna znaczy
    # "wszystkie eksperymenty", i taki napis nie mowilby, ktore to byly.
    eksperymenty = sorted(wlaczone_eksperymenty())
    cur.execute(
        "INSERT INTO learn.model_version (model_id, version, status, params, feature_def_ids,"
        " signal_def_ids, trained_from, trained_to, git_sha)"
        " VALUES (%s,%s,'training',%s,%s,%s,%s,%s,%s)"
        " RETURNING id",
        (model_id, version,
         # `kind` I `limit` MUSZA TU BYC, BO `params` JEST TERAZ MIANOWNIKIEM.
         #
         # Liczba roznych `params` tego modelu jest N deflacji (patrz
         # `n_konfiguracji`). Do 2026-09-26 brakowalo tu rodzaju instrumentu
         # i trybu egzekucji, wiec dwa przebiegi roznce sie WYLACZNIE cennikiem
         # (`--kind spot` taker 0,100% wobec `spot_eu` 0,250%) mialy IDENTYCZNE
         # `params` i liczyly sie jako jedna konfiguracja. N bylo przez to
         # zanizone, a zanizone N podnosi DSR - czyli bramka byla latwiejsza.
         #
         # `--days` nie trzeba dopisywac: odtwarza sie z `trained_to -
         # trained_from`, ktore i tak sa w tym samym wierszu.
         json.dumps({"bar": h.bar, "train_days": h.train_days, "test_days": h.test_days,
                     "step_days": h.step_days, "min_trades": h.min_trades,
                     "trials_per_window": trials_per_window, "seed": SEED,
                     "wybor": wybor, "kind": kind,
                     "limit": bool((egzekucja or Egzekucja()).limit),
                     "sygnaly_eksperymentalne": eksperymenty}),
         feature_ids, signal_ids, now - timedelta(days=days), now, sha_kodu()))
    mv_id = cur.fetchone()["id"]

    cur.execute(
        "INSERT INTO learn.training_run (model_version_id, train_from, train_to, status)"
        " VALUES (%s,%s,%s,'running') RETURNING id",
        (mv_id, now - timedelta(days=days), now))
    run_id = cur.fetchone()["id"]
    conn.commit()

    # Panel wczytujemy RAZ na instrument - to najdrozsza operacja w calym treningu.
    panels = {}
    inst_ids = {inst["symbol"]: inst["id"] for inst in instruments}
    for inst in instruments:
        p = load_panel(cur, inst["id"], h, now - timedelta(days=days), now, fp=fp)
        if len(p) > 500:
            panels[inst["symbol"]] = p
        print(f"  {inst['symbol']:10s} {len(p):>7,} punktów panelu")
    if not panels:
        # KOD WYJSCIA MUSI TO ODZWIERCIEDLAC.
        #
        # Ten przypadek oznaczal `return`, czyli kod 0 - trener konczyl sie
        # SUKCESEM, nie majac ani jednego punktu danych. Przebieg wygladal
        # z zewnatrz identycznie jak udany, a dozor nie mial sie o czym dowiedziec,
        # bo dziennik stanu nie dostawal nowego wiersza i porownywal dwa stare.
        #
        # `training_run` byl juz oznaczany jako `failed`, wiec intencja byla
        # jasna od poczatku - brakowalo tylko, zeby zobaczyl to ktos poza baza.
        print("PRZERWANO: za mało danych do treningu — żaden instrument nie ma "
              "panelu dłuższego niż 500 punktów")
        print(f"  sprawdź: czy świeca {h.bar} ma policzone cechy "
              f"(proces trader-features) i czy uniwersum nie jest puste")
        cur.execute("UPDATE learn.training_run SET status='failed', finished_at=now(),"
                    " error='za mało punktów panelu' WHERE id=%s", (run_id,))
        conn.commit()
        raise SystemExit(KOD_NIC_NIE_POLICZONO)

    # CECHY, KTORE PRZESTALY PRZYCHODZIC, TO NIE JEST WYNIK - TO AWARIA.
    #
    # Trener nie ma jak zauwazyc, ze potok cech dla JEGO horyzontu stanal:
    # panel po prostu konczy sie wczesniej, okna z konca wypadaja, a odsetek
    # dodatnich okien zmienia sie o kilka punktow - czyli mniej niz zmierzony
    # szum. Wynik degraduje sie po cichu z kazda doba.
    #
    # Zdarzylo sie to 2026-09-04: drugi horyzont doszedl do zadania cyklicznego,
    # a nie doszedl do procesu liczacego cechy. Cechy 1h stanely na 21 godzin
    # i nikt by tego nie zobaczyl, gdyby nie przeglad recznie.
    #
    # Prog to `max_staleness_min` tego horyzontu - ta sama liczba, ktorej petla
    # decyzyjna uzywa przy odczycie point-in-time, a nie osobna stala dobrana tu
    # na nowo. Kod wyjscia idzie do dozoru, wiec awaria konczy sie
    # powiadomieniem, a nie wpisem w logu, ktorego nikt nie czyta.
    #
    # WIEK MIERZY SIE WZGLEDEM OSTATNIEJ SWIECY, NIE WZGLEDEM ZEGARA.
    #
    # Do 2026-09-24 stalo tu `now - najnowszy`, czyli zalozenie, ze rynek jest
    # notowany bez przerwy. Na gieldzie amerykanskiej ostatnia swieca zamyka sie
    # o 20:00 UTC i nastepna przychodzi dopiero nazajutrz o 13:00, wiec cecha
    # w pelni aktualna ma nad ranem 800 minut, a po weekendzie ponad 3900 -
    # przy progu 180. Zadanie cykliczne trenera chodzi o 02:40 UTC, czyli
    # ZAWSZE przy zamknietej gieldzie: trening equity przerywalby sie co noc
    # i za kazdym razem meldowal awarie potoku, ktory dziala poprawnie.
    #
    # Falszywy alarm psuje czujke skuteczniej niz jej brak - uczy odbiorce, ze
    # mozna ja pomijac. A pytanie, ktore ten straznik NAPRAWDE zadaje, brzmi
    # "czy cechy nadazaja za danymi", nie "czy rynek handluje teraz". Odniesienie
    # do ostatniej swiecy zrodlowej odpowiada dokladnie na to i jest poprawne
    # na obu rynkach naraz, bez wiedzy o godzinach sesji i kalendarzu swiat.
    #
    # CZEGO TEN STRAZNIK PRZEZ TO NIE LAPIE: sytuacji, w ktorej stanely TAKZE
    # swiece. Wtedy cechy nadazaja za danymi, bo dane tez stoja. To nalezy do
    # `dozor.py --co swiezosc`, ktory patrzy wprost na `market.candle`.
    cur.execute(
        "SELECT max(event_time) AS ostatnia FROM market.candle"
        " WHERE instrument_id = ANY(%s) AND timeframe = %s",
        ([inst_ids[s] for s in panels], h.source_timeframe))
    ostatnia_swieca = (cur.fetchone() or {}).get("ostatnia")
    # Brak swiec przy niepustym panelu jest niemozliwy, ale gdyby zapytanie
    # zwrocilo NULL, zegar jest jedynym odniesieniem, jakie zostaje.
    odniesienie = ostatnia_swieca or now

    najnowszy = max(p[-1]["t"] for p in panels.values())
    wiek_min = (odniesienie - najnowszy).total_seconds() / 60
    if not cechy_nadazaja(najnowszy, odniesienie, h.max_staleness_min):
        komunikat = (f"najnowsza cecha jest o {wiek_min:.0f} min starsza od "
                     f"ostatniej świecy, dopuszczalne {h.max_staleness_min} min "
                     f"— potok cech dla świecy {h.bar} stanął")
        print(f"\nPRZERWANO: {komunikat}")
        print(f"  ostatni punkt panelu:  {najnowszy:%Y-%m-%d %H:%M} UTC")
        print(f"  ostatnia świeca {h.source_timeframe}: {odniesienie:%Y-%m-%d %H:%M} UTC")
        print(f"  sprawdź: kubectl logs deploy/trader-features --tail=30")
        cur.execute("UPDATE learn.training_run SET status='failed', finished_at=now(),"
                    " error=%s WHERE id=%s", (komunikat, run_id))
        conn.commit()
        raise SystemExit(KOD_NIC_NIE_POLICZONO)

    idx = 0
    positive = 0
    gate_history: list[float] = []
    weight_history: list[dict] = []
    # Rownolegla do weight_history: ktore sygnaly byly martwe w danym oknie.
    # Bez tego zero po martwym sygnale nie da sie odroznic od zera zmierzonego.
    martwe_w_oknie: list[set[str]] = []
    wybrane_ic = 0
    # Porownanie z nierobieniem niczego. Bez tego odsetek dodatnich okien nie
    # mowi, czy strategia jest LEPSZA od kupienia i trzymania - a strategia
    # wylacznie dluga na rosnacym rynku ma duzo dodatnich okien z samej bety.
    pokonane_odniesienie = 0
    pokonane_pelne = 0        # przy odniesieniu 100% zaangazowanym - do raportu
    ekspozycje: list[float] = []
    zwroty_okien: list[float] = []
    rozklad: list[dict] = []          # czynniki ekspozycji, okno po oknie
    # Ile transakcji przypadlo kazdemu instrumentowi. Uniwersum ma tyle miejsc,
    # ile instrumentow, ale kandydaci konkuruja o wspolny kapital i sa sortowani
    # PO SILE SYGNALU - wiec instrument o mocniejszych sygnalach moze wypchnac
    # pozostale. Bez tej liczby "dolozylismy instrumenty" i "handlujemy tymi
    # instrumentami" wygladaja tak samo.
    udzial_instrumentu: dict[str, int] = {}
    # Sharpe policzony OSOBNO dla kazdej grupy instrumentow, w tym samym oknie
    # i tym samym przebiegu. {grupa: [sharpe okno po oknie]}
    #
    # PYTANIE, NA KTORE TO ODPOWIADA. Dwa uniwersa maja osobno mediane Sharpe
    # okolo +0,11, a ich suma +0,011. Cztery naturalne wyjasnienia zostaly
    # obalone pomiarem (docs/03). Zostaje jedno miejsce: `Sharpe/transakcje`
    # w oknie to SREDNIA DZIELONA PRZEZ ODCHYLENIE po wszystkich transakcjach
    # tego okna - a grupy o roznej zmiennosci wrzucone do jednej takiej
    # statystyki daja wynik, ktory nie jest srednia ich wynikow.
    #
    # Jesli grupy liczone osobno W TYM SAMYM PRZEBIEGU maja nadal ~+0,11,
    # to znaczy, ze transakcje sa w porzadku, a zalamuje sie MIARA. Jesli
    # rowniez spadaja - to zmienil sie zestaw transakcji, czyli konstrukcja
    # portfela, i szukac trzeba tam.
    sharpe_grup: dict[str, list[float]] = {}
    porownywalne = 0
    all_sharpes: list[float] = []
    # Zwroty WSZYSTKICH transakcji OOS, nie usrednione po oknach - patrz
    # `podsumowanie_przewagi`.
    wszystkie_zwroty: list[float] = []
    # To samo rozbite na okna: potrzebne, zeby przeliczyc srednia PO OKNACH
    # przy doliczonym koszcie. Sama lista Sharpe'ow tego nie pozwala.
    zwroty_okien_transakcje: list[list[float]] = []
    worst_dd = 0.0
    predictions: list[tuple[float, bool]] = []
    start = now - timedelta(days=days)

    while start + timedelta(days=h.train_days + h.test_days) <= now:
        train_from = start
        train_to = start + timedelta(days=h.train_days)
        test_from = train_to
        test_to = test_from + timedelta(days=h.test_days)

        # Instrument, ktory w tym oknie jeszcze nie byl notowany, ZOSTAJE POMINIETY -
        # nie kasuje calego okna.
        #
        # Poprzednia wersja sprawdzala min() po WSZYSTKICH instrumentach, wiec
        # jeden mlody szereg (SUI, TIA i SEI weszly na gielde w 2023) zerowal
        # warunek i wyrzucal komplet wczesniejszych okien. Przy 24 instrumentach
        # kosztowalo to dziewiec okien z czterdziestu - czyli prawie czwarta
        # czesc dowodow, i to bez zadnego komunikatu.
        # PROG POKRYCIA LICZY SIE Z TEGO OKNA, NIE Z KALENDARZA.
        #
        # Wczesniej bylo tu `h.train_days * 1440 // h.bar_minutes // 4`, czyli
        # cwierc liczby swiec, ktora zmiescilaby sie w oknie, GDYBY rynek byl
        # notowany bez przerwy. Na krypto to prawda; na gieldzie nie.
        #
        # Okno 90 dni to na gieldzie amerykanskiej okolo 62 sesji po 7 swiec,
        # czyli 521-524 swiece - przy progu 540. Zaden instrument nie
        # przechodzil, `aktywne` bylo puste, kazde okno odpadalo na warunku
        # MIN_INSTRUMENTS i walk-forward konczyl sie z ZEREM okien. Trener
        # meldowal wtedy "za malo informatywnych okien", co brzmi jak brak
        # danych, a bylo progiem policzonym dla innego rynku - i przegrywal
        # o niecale cztery procent, wiec wystarczyloby troche gestszych danych,
        # zeby zaczal "dzialac" bez naprawienia czegokolwiek.
        #
        # Pytanie, ktore ten prog zadaje, brzmi: czy ten instrument byl notowany
        # przez WIEKSZOSC tego okna - a nie: ile godzin ma doba. Odniesieniem
        # jest wiec najlepiej pokryty instrument w TYM oknie. Dla rynku ciaglego
        # wychodzi dokladnie tyle samo co dawniej (2 160 swiec w 90 dniach,
        # cwierc to 540), a dla gieldy - tyle, ile sesja naprawde daje.
        pokrycie = {s: sum(1 for r in p if train_from <= r["t"] < train_to)
                    for s, p in panels.items()}
        min_train_bars = prog_pokrycia(pokrycie)
        aktywne = [s for s, n in pokrycie.items() if n >= min_train_bars]
        # Portfel z garstki instrumentow to juz inna strategia, nie ta sama
        # w chudszym okresie - ponizej tego progu okno naprawde nie niesie
        # informacji o tym, co testujemy.
        if len(aktywne) < MIN_INSTRUMENTS:
            start += timedelta(days=h.step_days)
            continue

        train_slices = {s: [r for r in panels[s] if train_from <= r["t"] < train_to]
                        for s in aktywne}
        test_slices = {s: [r for r in panels[s] if test_from <= r["t"] < test_to]
                       for s in aktywne}

        thresholds = {s: atr_threshold(train_slices[s]) for s in aktywne}
        # Rezimy i sygnaly liczymy RAZ na okno. Bez tego kazda z prob liczyla
        # komplet sygnalow od nowa, choc nie zaleza one od wag.
        cache_tr = precompute_panels(train_slices, thresholds, h)
        # Sygnaly stale na tym oknie wykluczamy z kalibracji - patrz
        # komentarz przy martwe_sygnaly().
        martwe = martwe_sygnaly(cache_tr)
        if martwe and idx == 0:
            print(f"  sygnały bez zmienności na danych, wyłączone z kalibracji: "
                  f"{', '.join(sorted(martwe))}")
        cache_te = precompute_panels({s: v for s, v in test_slices.items() if v},
                                     thresholds, h)
        # Progi wyniku ustalane raz na okno, z zalozen poczatkowych i wylacznie
        # na danych treningowych. Te same progi obowiazuja wszystkie hipotezy
        # w tym oknie - inaczej kazda konfiguracja dostawalaby wlasny prog
        # dobrany pod siebie, co jest kolejnym stopniem swobody do przeuczenia.
        #
        # Wagi bazowe MUSZA byc te same, ktorymi potem sie handluje, czyli juz
        # po wyzerowaniu martwych sygnalow. Liczenie progu z pelnych zalozen,
        # a handlowanie wagami po renormalizacji dawalo wyniki systematycznie
        # wieksze od progu - liczba transakcji na okno skoczyla wtedy z ~40 do
        # ~120, co nie bylo zmiana strategii, tylko rozjazdem miedzy progiem
        # a tym, co go przekracza.
        bazowe = _bez_martwych(DEFAULT_WEIGHTS, martwe)
        gates = {s: score_threshold(train_slices[s], bazowe, thresholds[s], h,
                                    cache_tr[s])
                 for s in aktywne}

        # --- dobor wag WYLACZNIE na oknie treningowym ------------------------
        # Celem jest Sharpe CALEGO PORTFELA, nie srednia z Sharpe'ow liczonych
        # osobno per instrument. Roznica nie jest kosmetyczna: portfel podlega
        # limitowi lacznej ekspozycji i kill-switchowi, wiec uklad wag, ktory
        # wygląda dobrze na kazdym instrumencie z osobna, moze byc niewykonalny,
        # gdy wszystkie chca wejsc jednoczesnie.
        def ocen(w, sl, c):
            res = simulate_portfolio(sl, w, thresholds, gates, h, MIN_CONF,
                                     instrument_ids=inst_ids, cache=c,
                                     kind=kind, egzekucja=egzekucja,
                                     zakladane_pozycje=zakladane_pozycje)
            return portfolio_metrics(res, h.min_trades)

        def zapisz_hipoteze(cand):
            if sucho:
                return
            cfg_hash = hashlib.sha256(json.dumps(cand, sort_keys=True).encode()).digest()
            cur.execute(
                "INSERT INTO learn.hypothesis_trial (model_id, config_hash, params,"
                " n_windows) VALUES (%s,%s,%s,1)"
                " ON CONFLICT (model_id, config_hash) DO NOTHING",
                (model_id, cfg_hash, json.dumps(cand)))

        # WARIANT WYBORU. Domyslny `argmax` bierze najlepszego z trials_per_window
        # losowan, zalozen poczatkowych i wag z IC - czyli MAKSIMUM Z PROBKI.
        # Maksimum z probki jest na oknie treningowym zawyzone o klatwe zwyciezcy
        # i tym bardziej, im wiecej losowan; poza oknem to zawyzenie znika, wiec
        # samo "wygral kandydat X" nic nie mowi o jego wartosci.
        #
        # `ic` i `bazowe` nie wybieraja niczego - biora jeden kandydat wyliczony
        # albo ustalony z gory - wiec nie maja czego zawyzac. Roznica OOS miedzy
        # wariantami na TYCH SAMYCH oknach, progach i bramkach jest jedynym
        # uczciwym pomiarem tego, czy losowe przeszukiwanie cokolwiek wnosi.
        if wybor == "bazowe":
            best_w = bazowe
            zapisz_hipoteze(bazowe)
        elif wybor == "ic":
            best_w = ic_weights(train_slices, cache_tr, martwe)
            zapisz_hipoteze(best_w)
            wybrane_ic += 1
        else:
            best_w, best_score = DEFAULT_WEIGHTS, -1e9
            for _ in range(trials_per_window):
                cand = _bez_martwych(random_weights(rng), martwe)
                score = ocen(cand, train_slices, cache_tr)["sharpe"]
                zapisz_hipoteze(cand)
                if score > best_score:
                    best_w, best_score = cand, score

            # Zalozenia poczatkowe tez sa kandydatem - jesli losowanie nie potrafi ich
            # pobic na oknie treningowym, to znaczy, ze nie ma czego kalibrowac.
            base_score = ocen(bazowe, train_slices, cache_tr)["sharpe"]
            # ZALOZENIA POCZATKOWE TEZ SA PROBA I TEZ MUSZA BYC POLICZONE.
            # `ocen` wlasnie je przetestowal i wynik bierze udzial w wyborze,
            # a `zapisz_hipoteze` bylo tu jedynym pominietym wywolaniem - przez
            # co licznik ZANIZAL liczbe prob. Zanizony licznik podnosi deflowany
            # Sharpe (mniejsza poprzeczka selekcji), czyli ulatwia bramke: nie
            # jest to zaniedbanie kosmetyczne, tylko w zla strone.
            zapisz_hipoteze(bazowe)
            if base_score > best_score:
                best_w, best_score = bazowe, base_score

            # Kandydat WYLICZONY, nie wylosowany: wagi proporcjonalne do zdolnosci
            # predykcyjnej sygnalow na tym oknie. Patrz komentarz przy ic_weights().
            ic_w = ic_weights(train_slices, cache_tr, martwe)
            ic_score = ocen(ic_w, train_slices, cache_tr)["sharpe"]
            zapisz_hipoteze(ic_w)
            if ic_score > best_score:
                best_w, best_score = ic_w, ic_score
                wybrane_ic += 1

        # --- ocena OUT-OF-SAMPLE --------------------------------------------
        oos_res = simulate_portfolio(
            {s: v for s, v in test_slices.items() if v}, best_w, thresholds, gates, h,
            MIN_CONF, instrument_ids=inst_ids, cache=cache_te,
            kind=kind, egzekucja=egzekucja, zakladane_pozycje=zakladane_pozycje)
        oos_m = portfolio_metrics(oos_res, h.min_trades)
        if not oos_res.equity_curve:
            start += timedelta(days=h.step_days)
            continue

        # Transakcje pochodza z JEDNEJ symulacji portfela, wiec sa juz wspolne
        # dla wszystkich instrumentow i podlegaly wspolnym limitom.
        #
        # ZASTRZEZENIE, ktore trzeba czytac razem z wynikiem: instrumenty krypto
        # sa silnie skorelowane, wiec te transakcje nie sa niezalezne. Efektywna
        # liczba obserwacji jest MNIEJSZA niz ich liczba, a istotnosc
        # statystyczna odpowiednio nizsza, niz sugerowalby sam licznik.
        n_trades = oos_m["n_trades"]
        informative = not oos_m.get("insufficient")
        agg = {k: oos_m[k] for k in
               ("sharpe", "sortino", "max_dd", "hit_rate", "avg_r", "profit_factor")}
        if informative:
            all_sharpes.append(agg["sharpe"])
            worst_dd = min(worst_dd, agg["max_dd"])
            if agg["sharpe"] > 0:
                positive += 1
            # ZLACZONA PROBA WSZYSTKICH TRANSAKCJI OOS.
            #
            # `all_sharpes` zbiera ILORAZY policzone osobno w kazdym oknie,
            # a ich srednia nie jest ilorazem ze zlaczonej proby - okno
            # o pieciu transakcjach wazy tam tyle samo, co okno o dwustu.
            # Dokladanie kosztu tez wymaga tej listy: koszt placi kazda
            # transakcja z osobna, nie kazde okno.
            zwroty_okna = [t["ret"] for t in oos_res.trades]
            wszystkie_zwroty.extend(zwroty_okna)
            # ROZBITE NA OKNA, a nie tylko zlaczone - zeby te sama miare, ktora
            # raport podaje dzis (srednia po oknach), dalo sie PRZELICZYC po
            # doliczeniu kosztu. Bez tego „przed" i „po" byloby liczone dwiema
            # roznymi miarami i roznica nie znaczylaby nic.
            zwroty_okien_transakcje.append(zwroty_okna)

        # Kalibracja: deklarowana PRZED wejsciem pewnosc kontra faktyczny wynik.
        # Wczesniejsza wersja budowala "prognoze" z wielkosci samego zwrotu
        # (abs(t) * 50 + 0.5), czyli z rzeczy znanej dopiero po fakcie - wykres
        # kalibracji mierzyl wtedy wlasny wynik i zawsze wygladal dobrze.
        for t in oos_res.trades:
            predictions.append((t["conf"], t["ret"] > 0))
            udzial_instrumentu[t["symbol"]] = udzial_instrumentu.get(t["symbol"], 0) + 1

        # Grupujemy po walucie kwotowanej: to samo, co dzieli uniwersum
        # decyzyjne (USDC) od reszty historii (USDT), bez zadnej nowej flagi.
        if informative and len(waluta_symbolu) > 1:
            wg_grupy: dict[str, list[float]] = {}
            for t in oos_res.trades:
                wg_grupy.setdefault(waluta_symbolu.get(t["symbol"], "?"), []).append(t["ret"])
            for grupa, rety in wg_grupy.items():
                # Ten sam wzor co w portfolio.metrics - inaczej porownywalibysmy
                # dwie rozne statystyki i wniosek bylby o nich, a nie o danych.
                if len(rety) < h.min_trades:
                    continue
                sd = max(statistics.pstdev(rety), 1e-9)
                sharpe_grup.setdefault(grupa, []).append(statistics.fmean(rety) / sd)

        step = max(1, len(next(iter(test_slices.values()), [])) // 50)
        regimes = [classify_regime(r["f"], thresholds[s])[0]
                   for s in aktywne for r in test_slices[s][::step]]
        dominant = max(set(regimes), key=regimes.count) if regimes else None

        if not sucho:
            cur.execute(
                "INSERT INTO learn.wf_window (training_run_id, idx, train_from, train_to,"
                " test_from, test_to, n_trades, dominant_regime)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (run_id, idx, train_from, train_to, test_from, test_to, n_trades, dominant))
            w_id = cur.fetchone()["id"]
            cur.executemany(
                "INSERT INTO learn.wf_metric (wf_window_id, metric, value) VALUES (%s,%s,%s)",
                [(w_id, k, round(v, 4)) for k, v in agg.items()])
            conn.commit()

        gate_history.extend(g[0] for g in gates.values())
        weight_history.append(best_w)
        martwe_w_oknie.append(martwe)

        odniesienie = kup_i_trzymaj({s: v for s, v in test_slices.items() if v}, kind)
        # ODNIESIENIE DOPASOWANE EKSPOZYCJA - uzasadnienie przy `bije_odniesienie`.
        # Kup-i-trzymaj angazuje caly kapital przez cale okno; strategia
        # srednio `ekspozycja` jego czesci. Zwrot z kupienia i trzymania
        # jest w wielkosci pozycji liniowy, wiec czesc `f` kapitalu
        # w tym samym koszyku (reszta w gotowce) zarobilaby dokladnie
        # `f * odniesienie`. To jest kontrfaktyczny wynik przy TYM SAMYM
        # zaangazowaniu i dopiero z nim porownanie mowi cokolwiek
        # o umiejetnosci.
        #
        # POROWNANIE JEST WOLNE OD SKALI, a nie zlagodzone. Zwrot z konta jest
        # w wielkosci pozycji liniowy po obu stronach: przy ekspozycji `f`
        # strategia zarabia `f` razy tyle, co przy pelnej, i odniesienie tak
        # samo. Warunek `r > f * bh` to dokladnie `r/f > bh` - "czy zloty
        # zaangazowany przez strategie zarobil wiecej niz zloty wlozony
        # w koszyk". Malejaca ekspozycja skraca obie strony jednakowo, wiec
        # nie da sie tego warunku przejsc samym zmniejszeniem pozycji.
        ekspozycja = oos_m["avg_exposure"]
        odniesienie_dop = odniesienie * ekspozycja
        # Okno bez ani jednej swiecy z pozycja nie ma czego porownywac:
        # odniesienie dopasowane bylo by zerem, ktore "pokonuje" kazdy dodatni
        # zwrot. Zwykle idzie to w parze z brakiem transakcji, ale nie musi -
        # pozycja otwarta i zamknieta miedzy dwiema swiecami nie zdazy trafic
        # do pomiaru ekspozycji, a transakcje do dziennika juz tak.
        #
        # Wyklucza to okno WYLACZNIE z porownania z odniesieniem. Do odsetka
        # dodatnich okien liczy sie dalej, bo tam zerowa ekspozycja niczego
        # nie psuje - a ciche wypadniecie okna z dwoch miar naraz bylo by
        # trudniejsze do zauwazenia niz z jednej.
        porownywalne_okno = informative and ekspozycja > 0.0
        if porownywalne_okno:
            porownywalne += 1
            ekspozycje.append(ekspozycja)
            zwroty_okien.append(oos_m["total_return"])
            rozklad.append(probka_rozkladu(oos_m, len(aktywne)))
            if oos_m["total_return"] > odniesienie_dop:
                pokonane_odniesienie += 1
            if oos_m["total_return"] > odniesienie:
                pokonane_pelne += 1
        mark = "" if informative else (f"  (poniżej {h.min_trades} transakcji — "
                                       "pominięte w ocenie)")
        if informative:
            mark += (f"  [strategia {oos_m['total_return']:+.2f}% przy ekspozycji "
                     f"{ekspozycja:.1%} vs kup-i-trzymaj {odniesienie:+.2f}% → "
                     f"przy tej samej ekspozycji {odniesienie_dop:+.2f}%]"
                     if porownywalne_okno else
                     "  [zerowa ekspozycja — poza porównaniem z odniesieniem]")
        if oos_res.killswitch_at:
            mark += f"  [KILL-SWITCH {oos_res.killswitch_at:%m-%d %H:%M}]"
        if oos_m["blocked"]:
            mark += f"  odrzuconych wejść: {oos_m['blocked']}"
        print(f"  okno {idx:>2d}  {test_from:%m-%d}–{test_to:%m-%d}  "
              f"Sharpe/transakcję={agg['sharpe']:+.4f}  transakcje={n_trades}  "
              f"reżim={dominant}{mark}")
        idx += 1
        start += timedelta(days=h.step_days)

    # --- kalibracja -------------------------------------------------------
    ece = None
    if predictions:
        ece = 0.0
        for lo in [i / 10 for i in range(0, 10)]:
            bucket = [(p, hit) for p, hit in predictions if lo <= p < lo + 0.1]
            if len(bucket) < 5:
                continue
            pred = statistics.fmean(p for p, _ in bucket)
            obs = statistics.fmean(1.0 if h else 0.0 for _, h in bucket)
            ece += abs(pred - obs) * len(bucket) / len(predictions)
            cur.execute(
                "INSERT INTO learn.calibration_bin (model_version_id, evaluated_at, bin_lo,"
                " bin_hi, predicted_mean, observed_rate, n_samples)"
                " VALUES (%s, now(), %s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (mv_id, lo, lo + 0.1, round(pred, 4), round(obs, 4), len(bucket)))

    # --- dryf cech --------------------------------------------------------
    #
    # DWIE WADY NAPRAWIONE TU 2026-09-23 (znaleziska 3 i 6 audytu). Obie robily
    # to samo: kazaly czujce w `dozor.py` wierzyc, ze mierzy cos, czego nie
    # mierzy.
    #
    # 1. OKNO BYLO POCHODNA `--days`, CZYLI DLUGOSCI PANELU TRENINGOWEGO.
    #    Produkcja chodzi z `--days 1400` (values.yaml), wiec „nowe" znaczylo
    #    ostatnie 466 dni, a „stare" poprzedzajace 934. Cala maszyneria
    #    trwalosci w dozorze (`DRYF_TRWALY_PRZEBIEGOW = 3`,
    #    `DRYF_TRWALY_GODZIN = 48`) zaklada, ze kolejne nocne odczyty sa
    #    NIEZALEZNYMI pomiarami - a okno przesuwajace sie o jeden dzien na 466
    #    daje dwa sasiednie odczyty dzielace ~99,8% danych. „Trzy kolejne
    #    przebiegi powyzej progu" nie bylo wiec trzema dowodami, tylko jednym
    #    policzonym trzy razy - dokladnie tym bledem, przed ktorym ma bronic
    #    `DRYF_TRWALY_GODZIN`.
    #
    #    Skutek byl obustronny. W jedna strone: zmiana rezimu, ktora przesuwa
    #    srednia ostatnich 30 dni o cale historyczne odchylenie, rozcienczona
    #    w oknie 466 dni daje ruch rzedu setnych i nie przekracza progu przez
    #    miesiace. W druga: raz przekroczony prog zostaje przekroczony przez
    #    rok, bo nic ze sredniej nie wypada - a `DRYF_PRZYPOMNIENIE_DNI = 7`
    #    zamienia to w cotygodniowe przypomnienie o stanie, ktorego juz nie ma.
    #
    #    Dlatego okno jest dzis WLASNYM parametrem, dobranym do rytmu reakcji
    #    (miesiac wobec kwartalu), a nie pochodna dlugosci panelu. Te dwie
    #    rzeczy odpowiadaja na rozne pytania i nie maja powodu byc zwiazane:
    #    panel ma byc dlugi, zeby walk-forward mial okna; okno dryfu ma byc
    #    krotkie, zeby kolejny odczyt niosl NOWE dane.
    #
    # 2. `max_psi` LICZYL SIE PO WSZYSTKICH CECHACH. `SELECT ... WHERE active`
    #    bralo takze cechy policzone na swiecy 1h i 5m oraz warianty
    #    z eksperymentalnych uniwersow - a `load_panel` filtruje je scisle
    #    (`params->>'bar'` plus odcisk uniwersum) i ma na to spisane
    #    uzasadnienie. Przebieg dla `--bar 4h` i przebieg dla `--bar 1h`
    #    zapisywaly wiec do `learn.learning_state_log` TE SAMA liczbe, podczas
    #    gdy `ocen_dryf_trwaly` filtruje historie po `bar` w przekonaniu, ze
    #    rozdziela dwa modele (dozor.py:303-306). Filtr rozdzielal dwa wiersze
    #    o identycznej wartosci.
    #
    #    Poniewaz `max` po nadzbiorze jest zawsze >= `max` po podzbiorze, blad
    #    dzialal w strone FALSZYWEGO ALARMU: przy `DRYF_TRWALY_PROG = 0.5`
    #    wystarczyl jeden halasliwy szereg 5-minutowy, zeby czujka migala -
    #    a czujka, ktora miga, uczy odklikiwac (uzasadnienie w dozor.py).
    #
    #    Ten sam filtr co w `load_panel`, slowo w slowo, i z tego samego
    #    powodu. Dolozony jest jeszcze filtr po INSTRUMENTACH tego przebiegu:
    #    bez niego dryf liczyl sie po cechach par, ktorych to uniwersum
    #    w ogole nie handluje.
    max_psi = 0.0
    id_instrumentow = [i["id"] for i in instruments]
    granica_nowe = now - timedelta(days=DRYF_OKNO_NOWE_DNI)
    granica_stare = granica_nowe - timedelta(days=DRYF_OKNO_STARE_DNI)
    print(f"dryf cech: okno nowe {DRYF_OKNO_NOWE_DNI} dni wobec "
          f"poprzedzających {DRYF_OKNO_STARE_DNI}, świeca {h.bar}, "
          f"odcisk uniwersum {fp}")
    cur.execute(
        "SELECT id, name FROM feat.feature_def"
        " WHERE active AND params->>'bar' = %s"
        "   AND (params->>'uniwersum' IS NULL OR params->>'uniwersum' = %s)",
        (h.bar, fp))
    for fd in cur.fetchall():
        cur.execute(
            """
            SELECT stddev_pop(value) AS sd_old, avg(value) AS m_old
            FROM feat.feature_value WHERE feature_def_id=%s
              AND instrument_id = ANY(%s)
              AND event_time >= %s AND event_time < %s
            """, (fd["id"], id_instrumentow, granica_stare, granica_nowe))
        old = cur.fetchone()
        cur.execute(
            """
            SELECT stddev_pop(value) AS sd_new, avg(value) AS m_new
            FROM feat.feature_value WHERE feature_def_id=%s
              AND instrument_id = ANY(%s)
              AND event_time >= %s
            """, (fd["id"], id_instrumentow, granica_nowe))
        new = cur.fetchone()
        if not (old and new and old["sd_old"] and new["sd_new"]):
            continue
        # UPROSZCZONY WSKAZNIK DRYFU - I NIE JEST TO PSI, mimo nazwy kolumny,
        # do ktorej trafia (`learn.drift_metric.psi`, migracja 0057).
        #
        # Przesuniecie sredniej w jednostkach historycznego odchylenia plus
        # wzgledna zmiana samego odchylenia. Population Stability Index liczy
        # co innego - dywergencje rozkladow po koszykach - i ma INNA SKALE.
        #
        # Dlaczego to ma znaczenie: PSI ma powszechnie znane progi (0,1 / 0,25 /
        # 0,5), wiec liczba nazwana "PSI" jest czytana wedlug nich. Progi czujki
        # dla TEJ wielkosci to 0,5 i 1,0 i sa normalnym zakresem pracy.
        # Zmierzone 2026-09-24: `ret_long__4h` = 2,43 przy mediannie OOS Sharpe
        # toru 4h nadal DODATNIEJ (+0,1145) - czytane jako PSI znaczyloby model
        # do wyrzucenia, czytane poprawnie znaczy "srednia odjechala o dwa
        # historyczne odchylenia", co bylo prawda o rynku, nie o potoku.
        #
        # Nazwa zmiennej zostaje `psi`, bo taka jest kolumna; przemianowanie
        # obu naraz to osobna zmiana, nie skutek uboczny komentarza.
        psi = abs(float(new["m_new"]) - float(old["m_old"])) / float(old["sd_old"]) \
            + abs(float(new["sd_new"]) / float(old["sd_old"]) - 1)
        max_psi = max(max_psi, psi)
        cur.execute(
            "INSERT INTO learn.drift_metric (model_version_id, feature_def_id, event_time, psi)"
            " VALUES (%s,%s, now(), %s) ON CONFLICT DO NOTHING",
            (mv_id, fd["id"], round(psi, 4)))

    # --- podsumowanie -----------------------------------------------------
    total = len(all_sharpes)
    pct = (positive / total * 100) if total else 0.0
    median = statistics.median(all_sharpes) if all_sharpes else None
    cur.execute("SELECT count(*) AS n FROM learn.hypothesis_trial WHERE model_id=%s", (model_id,))
    trials = cur.fetchone()["n"]

    # DEFLOWANY SHARPE - LICZONY TU, BO BEZ NIEGO BRAMKA NIE MIERZY SELEKCJI.
    #
    # Licznik `trials` istnieje od migracji 0006 WYLACZNIE po to, zeby te liczbe
    # dalo sie policzyc (komentarz przy tabeli mowi wprost: "bez tego licznika
    # deflated Sharpe jest nieobliczalny i sami siebie oszukamy selekcja"),
    # i do 2026-09-26 nikt jej nie policzyl. Do bramki wchodzila mediana
    # Sharpe'ow okien, ktora nie wie ANI ile konfiguracji sprawdzono, ANI jaki
    # jest rozklad zwrotow - a oba te braki dzialaja na korzysc strategii.
    #
    # PROBA TO ZLACZONE TRANSAKCJE, nie Sharpe'y okien - patrz `przewaga_dsr`.
    #
    # DWIE RZECZY, KTORE MUSZA BYC TU DOKLADNIE TAKIE:
    #
    # 1. DOPLATA. DSR liczy sie na zwrotach PO doliczeniu tej czesci karty
    #    kosztu, ktorej symulacja NIE nalicza (`doplata_poza_symulacja` -
    #    dzis spread, gdy podano pomiar, i niewypelnienie przy limicie).
    #    Liczony przed nia byl by miara innego, tanszego swiata, a wada
    #    szlaby w strone LATWIEJSZEJ bramki. To jest wielkosc rozstrzygajaca:
    #    zmierzone 5,85 pb zbija DSR pod prog przy KAZDYM N.
    #
    # 2. N. Pierwsza wersja tej zmiany podstawiala tu `trials`, czyli `count(*)`
    #    po `hypothesis_trial`. Jest to zle, bo liczyloby te sama kare dwa razy
    #    - uzasadnienie w `n_konfiguracji`. `trials` zostaje w opisie jako
    #    kontekst i nie dotyka werdyktu.
    dsr_ok, opis_dsr = werdykt_dsr(
        wszystkie_zwroty, doplata=karta.doplata_poza_symulacja,
        n=n_konfiguracji(cur, model_id), trials_kumulatywnie=trials,
        nieznane=karta.nieznane)

    # Prog dobrany do miary NA TRANSAKCJE, nie anualizowanej. 0,05 oznacza,
    # ze sredni zwrot z transakcji to 5% jej wlasnego odchylenia - skromnie,
    # ale mierzalnie powyzej zera po kosztach.
    #
    # MIN_WINDOWS_FOR_VALIDATION jest warunkiem koniecznym, nie ozdoba. Bez niego
    # przebieg, w ktorym 39 okien pominieto z braku transakcji, a jedno wyszlo
    # dodatnio, dawal 100% skutecznosci i status "kryteria spelnione" - czyli
    # zielone swiatlo do handlu prawdziwymi pieniedzmi na podstawie jednego okna.
    ci_lo, ci_hi = wilson(positive, total)

    # PRZEWAGA NAD NIEROBIENIEM NICZEGO jest osobnym warunkiem, nie ozdoba.
    #
    # Odsetek dodatnich okien mierzy, jak czesto okno wychodzi na plus - a nie
    # czy warto bylo cokolwiek robic. Strategia o malej ekspozycji na spadajacym
    # rynku ma duzo dodatnich okien z samego nietrzymania pozycji. Pomiar
    # z 2026-09-04: wariant `ic` mial 75% dodatnich okien i 52,5% okien lepszych
    # od kupienia i trzymania, czyli rzut moneta wobec odniesienia.
    #
    # Prog stawiamy na DOLNEJ granicy przedzialu ufnosci, nie na samym odsetku,
    # z tego samego powodu, dla ktorego odsetek podajemy zawsze z przedzialem:
    # przy 26-40 oknach roznica kilku punktow nie jest sygnalem.
    #
    # POROWNANIE MUSI BYC PRZY TEJ SAMEJ EKSPOZYCJI (2026-09-07).
    #
    # Do tego dnia porownywalismy zwrot z konta strategii ze zwrotem z konta
    # zaangazowanego w calosci. Strategia nie moze zaangazowac wiecej niz
    # `max_total_exposure`, czyli 15% kapitalu, i srednio angazuje ulamek tego.
    # Porownanie bylo wiec miedzy rachunkiem w kilku procentach zainwestowanym
    # a rachunkiem w stu.
    #
    # Skutek dalo sie zmierzyc i jest jednoznaczny. W przebiegu nocnym
    # z 2026-09-07 strategia byla lepsza od kup-i-trzymaj:
    #
    #     horyzont 4h:  17/39 okien - i byly to DOKLADNIE te 17 okien,
    #                   w ktorych kup-i-trzymaj wyszedl na minus (17/17),
    #                   oraz zadne z 22, w ktorych wyszedl na plus (0/22)
    #     horyzont 1h:  48/88 - znowu dokladnie te, w ktorych rynek spadl
    #                   (48/48 i 0/40)
    #
    # 127 okien, zero wyjatkow: wynik porownania rownal sie znakowi rynku.
    # Warunek nie mierzyl strategii - mierzyl, czy krypto w danym miesiacu
    # rosly. Przy 15% ekspozycji nie da sie pobic rosnacego rynku i nie da sie
    # przegrac ze spadajacym, niezaleznie od tego, ile umiejetnosci jest
    # w srodku. Warunek byl wiec nieosiagalny przez konstrukcje na rynku, ktory
    # w badanym okresie rosl w 22 oknach na 39 - i przeszedlby sam z siebie,
    # gdyby rynek spadal, przy strategii bez jakiejkolwiek przewagi.
    #
    # Odniesienie skalujemy wiec sredni ekspozycja okna. Nie jest to zlagodzenie
    # progu: jest to zadanie tego samego pytania w sposob, na ktory odpowiedz
    # nie jest z gory znana.
    odn_lo, odn_hi = wilson(pokonane_odniesienie, porownywalne) if porownywalne else (0.0, 1.0)
    bije_odniesienie = porownywalne >= MIN_WINDOWS_FOR_VALIDATION and odn_lo > 0.5

    if total < MIN_WINDOWS_FOR_VALIDATION:
        status = "learning"
        reason = (f"Za mało informatywnych okien: {total} z wymaganych "
                  f"{MIN_WINDOWS_FOR_VALIDATION}. Strategia zawiera zbyt mało transakcji, "
                  f"żeby cokolwiek orzec — to nie jest wynik ujemny, to brak wyniku. "
                  + opis_dsr)
    else:
        status = status_walidacji(total, positive, median, bije_odniesienie, dsr_ok)
        # Przedzial podajemy ZAWSZE razem z odsetkiem. Sam odsetek zacheca do
        # czytania roznicy dwoch punktow jako sygnalu, a przy 40 oknach
        # przedzial ma szerokosc okolo 30 punktow procentowych.
        ci = f" Przedział ufności 95%: {ci_lo:.0%}–{ci_hi:.0%}."
        # Gdy jedyna niespelniona rzecza jest przewaga nad odniesieniem, trzeba
        # to powiedziec wprost. Sam odsetek powyzej progu wyglada jak sukces.
        brak_przewagi = ""
        if pct >= 70 and median and median > 0.05 and not bije_odniesienie:
            brak_przewagi = (f" Kup-i-trzymaj przy tej samej ekspozycji pokonane w "
                             f"{pokonane_odniesienie}/{porownywalne} okien "
                             f"({odn_lo:.0%}–{odn_hi:.0%}) — to nie jest przewaga, "
                             f"więc kryteria NIE są spełnione mimo odsetka powyżej progu.")
        # SKALA WYNIKU IDZIE RAZEM ZE STATUSEM, z tego samego powodu, dla ktorego
        # przedzial idzie razem z odsetkiem.
        #
        # Kryteria mierza UMIEJETNOSC: czy zloty zaangazowany przez strategie
        # zarabia wiecej niz zloty w koszyku. Nie mierza, czy zaangazowanych
        # zlotowek jest na tyle duzo, zeby wynik cokolwiek znaczyl. Pomiar
        # z 2026-09-07: srednia ekspozycja 1,28% kapitalu przy limicie 15%,
        # suma zwrotow z 39 okien +10,07% przez 3,2 roku - okolo 3% rocznie,
        # ponizej stopy wolnej od ryzyka.
        #
        # "Kryteria spelnione" bez tych liczb obok czyta sie jak zielone swiatlo
        # do handlu prawdziwymi pieniedzmi. Z nimi widac, ze to zielone swiatlo
        # dla SYGNALU, a nie dla wielkosci pozycji.
        srednia_eksp = statistics.fmean(ekspozycje) if ekspozycje else 0.0
        suma_zwrotow = sum(zwroty_okien)
        skala = (f" Średnia ekspozycja {srednia_eksp:.2%} kapitału przy limicie "
                 f"{_limit_ekspozycji_pct():.0%}; "
                 f"suma zwrotów z {porownywalne} okien {suma_zwrotow:+.1f}%.")
        # DSR IDZIE DO `reason` PRZY KAZDYM STATUSIE, a nie tylko gdy blokuje.
        # `reason` jest tym, co widzi czlowiek w panelu i w powiadomieniu
        # `dozor.py`; mediana Sharpe'a bez DSR obok czyta sie jak wynik, ktory
        # uwzglednia liczbe prob, a nie uwzglednia jej w niczym.
        skala += " " + opis_dsr
        reason = {
            "validated": f"Dodatnie {positive}/{total} okien out-of-sample ({pct:.0f}%).{ci}"
                         f" Kryteria spełnione — strategia ma przewagę NA JEDNOSTKĘ "
                         f"zaangażowanego kapitału.{skala} O tym, czy ta przewaga jest "
                         f"warta handlowania, kryteria nie mówią nic.",
            "learning": f"Dodatnie {positive}/{total} okien out-of-sample ({pct:.0f}%).{ci}"
                        + (brak_przewagi or " Próg przejścia na live: 70%.") + skala,
            "degraded": f"Dodatnie tylko {positive}/{total} okien ({pct:.0f}%).{ci} "
                        f"Strategia nie wykazuje przewagi.{skala}",
        }[status]

    # Progi obowiazujace produkcje - mediana z okien. Petla decyzyjna czyta je
    # z rejestru, zeby nie ustalac ich wlasna, rownolegla logika.
    prod_enter = round(statistics.median(gate_history), 6) if gate_history else 0.25

    # WAGI DLA PRODUKCJI.
    #
    # Bierzemy MEDIANE wag po oknach, nie wagi z ostatniego okna. Pojedyncze
    # okno to jedno losowanie; mediana po kilkudziesieciu jest odporna na to,
    # ze akurat ostatnie wypadlo nietypowo. Suma po wartosciach bezwzglednych,
    # bo wagi moga byc ujemne.
    #
    # MEDIANA LICZONA WYLACZNIE PO OKNACH, W KTORYCH SYGNAL ZYL.
    #
    # Sygnal martwy na oknie dostaje w nim wage zero - ale to zero nie jest
    # pomiarem "waga powinna byc zerowa", tylko BRAKIEM POMIARU. Wliczanie go
    # do mediany jest tym samym, co usrednianie brakow danych jako zer.
    #
    # Skutek byl konkretny i policzalny. `news_shock` bez klucza Anthropic byl
    # martwy we wszystkich oknach; po wpieciu klucza ozywa najpierw w jednym,
    # potem w kolejnych. Zeby MEDIANA z 39 okien przestala byc zerem, sygnal
    # musialby zyc w 20 z nich - czyli potrzebowalby 600 dni historii newsow.
    # Dwadziescia miesiecy, przy szesciomiesiecznej fazie demonstracyjnej.
    #
    # Sygnal, ktorego dane zaczely istniec dzis, nie mogl wiec wejsc do modelu
    # NIGDY w horyzoncie tego projektu - niezaleznie od tego, ile informacji
    # niesie.
    prod_weights = {}
    if weight_history:
        for regime in REGIMES:
            per_signal = {}
            for name in SIGNALS:
                # Rownolegle listy, nie wyszukiwanie po wartosci: dwa okna moga
                # dac identyczny zestaw wag, a wtedy `index()` wskazalby zle okno.
                zywe = [w[regime][name]
                        for w, martwe_okna in zip(weight_history, martwe_w_oknie)
                        if name in w[regime] and name not in martwe_okna]
                per_signal[name] = statistics.median(zywe) if len(zywe) >= MIN_OKIEN_ZYWEGO else 0.0
            total_w = sum(abs(v) for v in per_signal.values()) or 1.0
            prod_weights[regime] = {k: round(v / total_w, 4) for k, v in per_signal.items()}

    # W trybie SUCHYM nie dotykamy bazy w ogole: porownanie wariantow nie moze
    # promowac wersji modelu ani nadpisywac stanu nauki. Podsumowanie i tak
    # sie wypisuje - po to ten tryb istnieje.
    if not sucho:
        cur.execute(
            "UPDATE learn.model_version SET params = params || %s::jsonb WHERE id = %s",
            (json.dumps({"enter_threshold": prod_enter,
                         "exit_threshold": round(prod_enter * EXIT_RATIO, 6),
                         "min_confidence": MIN_CONF,
                         "weights": prod_weights}), mv_id))

        # BRAMKA PROMOCJI. Do 2026-09-08 w tym miejscu stalo bezwarunkowe
        # wycofanie poprzedniej wersji i promocja nowej dla kazdego wyniku poza
        # `degraded` - patrz komentarz przy `status_nowej_wersji`.
        tryb = tryb_handlu(cur, engine)
        docelowy, powod_bramki = status_nowej_wersji(status, tryb, zgoda_na_promocje)
        zostaje = wersja_produkcyjna(cur, model_id, mv_id) if docelowy != "active" else None
        zapisz_promocje(cur, model_id, mv_id, docelowy)
        # POWOD IDZIE DO `reason`, czyli do jedynego pola stanu nauki, ktore
        # niesie ZDANIE dla czlowieka: /learning/state oddaje je wprost na
        # zakladke. Bramka, o ktorej wie tylko log zadania cyklicznego, jest
        # niewidzialna - a wtedy kandydat czeka na zatwierdzenie miesiacami
        # i system handluje coraz starszym modelem, znowu po cichu.
        #
        # NUMER WERSJI PODAJEMY WPROST z tego samego powodu. `learning_state`
        # wskazuje wersje z OSTATNIEGO przebiegu, wiec po wstrzymaniu promocji
        # zakladka nazywa model, ktorym produkcja wlasnie nie handluje.
        if docelowy != "active":
            reason += (f" PROMOCJA WSTRZYMANA ({powod_bramki}); produkcja handluje "
                       + (f"wersją {zostaje}." if zostaje
                          else "założeniami początkowymi — NIE MA aktywnej wersji."))
        cur.execute("UPDATE learn.training_run SET status='done', finished_at=now(),"
                    " n_windows=%s WHERE id=%s", (total, run_id))
        cur.execute(
            """
            INSERT INTO learn.learning_state (engine, bar, status, model_version_id, windows_total,
                windows_positive, median_oos_sharpe, max_drawdown, calibration_error, max_psi,
                trials_run, reason, positive_ci_low, positive_ci_high, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (engine, bar) DO UPDATE SET status=EXCLUDED.status,
                model_version_id=EXCLUDED.model_version_id, windows_total=EXCLUDED.windows_total,
                windows_positive=EXCLUDED.windows_positive,
                median_oos_sharpe=EXCLUDED.median_oos_sharpe, max_drawdown=EXCLUDED.max_drawdown,
                calibration_error=EXCLUDED.calibration_error, max_psi=EXCLUDED.max_psi,
                trials_run=EXCLUDED.trials_run, reason=EXCLUDED.reason,
                positive_ci_low=EXCLUDED.positive_ci_low,
                positive_ci_high=EXCLUDED.positive_ci_high, updated_at=now()
            """,
            (engine, h.bar, status, mv_id, total, positive,
             round(median, 4) if median is not None else None,
             round(worst_dd, 4), round(ece, 4) if ece is not None else None,
             round(max_psi, 4), trials, reason, round(ci_lo, 4), round(ci_hi, 4)))

        # DZIENNIK, nie kopia stanu. learning_state to jeden wiersz nadpisywany
        # przy kazdym przebiegu, wiec po zapisie nie da sie juz powiedziec, czy
        # cokolwiek sie zmienilo - a to jedyne pytanie, ktore warto zadawac
        # codziennie. Bez tych wierszy dozor musialby albo milczec, albo odzywac
        # sie zawsze (patrz services/engine/dozor.py, migracja 0024).
        cur.execute(
            "INSERT INTO learn.learning_state_log (engine, bar, status, windows_total,"
            " windows_positive, positive_ci_low, positive_ci_high, median_oos_sharpe,"
            " max_drawdown, max_psi, trials_run, model_version_id)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (engine, h.bar, status, total, positive, round(ci_lo, 4), round(ci_hi, 4),
             round(median, 4) if median is not None else None,
             round(worst_dd, 4), round(max_psi, 4), trials, mv_id))
    else:
        cur.execute("UPDATE learn.training_run SET status='done', finished_at=now(),"
                    " error='tryb suchy - porownanie wariantow' WHERE id=%s", (run_id,))
    conn.commit()

    if wybor == "argmax":
        print(f"\nWagi z IC wybrane w {wybrane_ic}/{idx} okien "
              f"(reszta: losowanie albo założenia początkowe)")
    else:
        print(f"\nWariant wyboru: {wybor} — bez losowania, "
              f"jeden kandydat na każde z {idx} okien")
    if porownywalne:
        pct_odn = pokonane_odniesienie / porownywalne * 100
        srednia_eksp = statistics.fmean(ekspozycje) if ekspozycje else 0.0
        print(f"Średnia ekspozycja: {srednia_eksp:.2%} kapitału "
              f"(limit łączny: {_limit_ekspozycji_pct():.0%} kapitału)")
        for linia in opis_rozkladu(rozklad)[0]:
            print(linia)
        print(f"Pokonano kup-i-trzymaj PRZY TEJ SAMEJ EKSPOZYCJI w "
              f"{pokonane_odniesienie}/{porownywalne} oknach "
              f"({pct_odn:.1f}%)   przedział 95%: {odn_lo:.1%}–{odn_hi:.1%}")
        # Liczba sprzed poprawki, podawana dalej, bo opisuje realny zwrot
        # z konta - tyle, ze o strategii nie mowi nic (patrz komentarz wyzej).
        print(f"  dla porównania, wobec odniesienia zaangażowanego w 100%: "
              f"{pokonane_pelne}/{porownywalne} "
              f"({pokonane_pelne / porownywalne * 100:.1f}%) — ta liczba jest funkcją "
              f"znaku rynku, nie strategii")
    print(f"Okna: {total}, dodatnie: {positive} ({pct:.1f}%)"
          f"   przedział ufności 95%: {ci_lo:.1%}–{ci_hi:.1%}")
    print(f"Mediana Sharpe (OOS): {median:+.3f}" if median is not None else "Mediana Sharpe: —")
    # DSR ZAWSZE RAZEM Z N I LICZBA PROB - `bramka_dsr` sklada to w jedno zdanie
    # i to samo zdanie idzie do `reason`, zeby wydruk i baza nie mowily dwoch
    # roznych rzeczy o tym samym pomiarze.
    print(f"Deflowany Sharpe: {opis_dsr}")
    print(f"Najgorsze obsunięcie: {worst_dd:.2f}%")
    for linia in opis_przewagi(wszystkie_zwroty, all_sharpes, karta,
                               zwroty_okien_transakcje):
        print(linia)
    if len(sharpe_grup) > 1:
        print("Sharpe/transakcję policzony OSOBNO dla grup, w tym samym przebiegu:")
        for grupa, wartosci in sorted(sharpe_grup.items()):
            print(f"  {grupa:>6}: mediana {statistics.median(wartosci):+.3f}"
                  f"   okien {len(wartosci)}")
        print("  (powyżej mediana łączna — jeśli grupy osobno są wyraźnie lepsze,"
              " załamuje się MIARA, a nie strategia)")
    if udzial_instrumentu:
        razem = sum(udzial_instrumentu.values())
        rank = sorted(udzial_instrumentu.items(), key=lambda x: -x[1])
        opis = "  ".join(f"{s} {n/razem:.0%}" for s, n in rank[:6])
        print(f"Udział w transakcjach (6 największych z {len(rank)}): {opis}")
        # Rowny podzial to 1/n. Instrument powyzej dwukrotnosci tego udzialu
        # zajmuje miejsce, ktore mialo isc gdzie indziej.
        rowno = 1 / len(rank)
        dominujace = [s for s, n in rank if n / razem > 2 * rowno]
        if dominujace:
            print(f"  wypychają resztę (ponad 2× równy udział {rowno:.0%}): "
                  f"{', '.join(dominujace)}")
    print(f"Przetestowanych konfiguracji: {trials}")
    print(f"Stan: {status} — {reason}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1400)
    ap.add_argument("--trials", type=int, default=40)
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--bar", default=DEFAULT_BAR, choices=sorted(HORIZONS))
    # "stock" dolaczylo 2026-09-24 razem z torem equity. Cennik akcji na Alpace
    # to zero prowizji - patrz `koszty.CENNIK`. Trener MUSI kosztowac tyle, ile
    # kosztuje strategia, ktora kalibruje: tor equity liczony cennikiem Bybita
    # (tak powstaly liczby w docs/08) mierzyl oplaty, ktorych to konto nie placi.
    ap.add_argument("--kind", default="spot",
                    choices=("spot", "spot_eu", "perp", "stock"),
                    help="rodzaj instrumentu - wyznacza stawki prowizji; "
                         "spot_eu to cennik podmiotu unijnego (taker 0,250%%)")
    ap.add_argument("--limit", action="store_true",
                    help="zlecenia z limitem zamiast rynkowych")
    ap.add_argument("--sucho", action="store_true",
                    help="nie zapisuj wyniku do bazy (do porownywania wariantow)")
    ap.add_argument("--spread-obca-ksiega", action="store_true",
                    dest="spread_obca_ksiega",
                    help="podany --spread-bps pochodzi z ksiegi, na ktorej NIE "
                         "skladamy zlecen. Liczba wchodzi wtedy do rachunku "
                         "jako OSZACOWANIE, karta zostaje niepelna, a raport "
                         "podaje krotnosc spreadu, przy ktorej przewaga schodzi "
                         "do zera. OD 2026-09-24 arkusz idzie juz z gieldy, na "
                         "ktorej handlujemy (BybitAdapter.fetch_order_book -> "
                         "self._host), a od 2026-09-25 dotyczy to instrumentow "
                         "`tradeable`; instrumenty niehandlowalne zostaly na "
                         "hoscie globalnym. Flaga jest wiec potrzebna dla okien "
                         "siegajacych przed 2026-09-24 ORAZ dla pomiaru "
                         "liczonego na instrumentach `tradeable = false`")
    ap.add_argument("--spread-bps", type=float, default=None, dest="spread_bps",
                    help="ZMIERZONA polowa spreadu na noge razy dwie nogi, "
                         "w punktach bazowych na runde. Zrodlo: "
                         "market.book_snapshot / cecha book_spread_bps. "
                         "BEZ TEJ WARTOSCI spread zostaje w karcie kosztu jako "
                         "NIE ZMIERZONY - i tak ma byc, dopoki nikt go nie "
                         "policzyl. Nie wpisywac liczby 'na oko'")
    # ZGODA CZLOWIEKA NA PODMIANE STRATEGII W TRYBIE LIVE.
    #
    # Flaga przebiegu, nie ustawienie w bazie i nie wpis w chart: zadanie
    # cykliczne jej NIE MA i miec nie moze, bo caly sens bramki polega na tym,
    # ze nocna automatyka nie potrafi jej podniesc. Podnosi ja czlowiek,
    # uruchamiajac trening recznie, raz.
    ap.add_argument("--zatwierdz-promocje", action="store_true",
                    dest="zgoda_na_promocje",
                    help="pozwol promowac nowa wersje modelu takze wtedy, gdy "
                         "silnik handluje na zywo; bez tego wersja zostaje "
                         "kandydatem, a produkcja handluje dalej poprzednia")
    ap.add_argument("--instrumenty", default="",
                    help="lista symboli po przecinku - NADPISUJE uniwersum decyzyjne "
                         "z bazy, takze o instrumenty jeszcze niehandlowalne. "
                         "Do porownywania uniwersum kandydujacego z obowiazujacym; "
                         "wymaga --sucho. Puste = uniwersum z bazy")
    ap.add_argument("--wybor", default="ic", choices=("argmax", "ic", "bazowe"),
                    help="jak wybierany jest kandydat na oknie treningowym: "
                         "argmax = najlepszy z losowan + zalozenia + IC (do 2026-09-04 produkcja), "
                         "ic = wylacznie wagi wyliczone z IC, "
                         "bazowe = wylacznie zalozenia poczatkowe. "
                         "Warianty inne niz domyslny wymagaja --sucho")
    ap.add_argument("--zakladane-pozycje", type=int, default=0,
                    dest="zakladane_pozycje",
                    help="ile pozycji JEDNOCZESNIE zaklada budzet na pozycje. "
                         "Domyslnie liczba obserwowanych instrumentow - czyli "
                         "zalozenie, ze zapelnia sie wszystkie. Pomiar mowi, ze "
                         "zapelnia sie 2,6 z 10, wiec budzet jest liczony pod "
                         "portfel, ktory nie powstaje. Limit lacznej ekspozycji "
                         "jest sprawdzany osobno w kazdej swiecy, wiec mniejszy "
                         "dzielnik go NIE PODNOSI - zmienia tylko rozklad ryzyka "
                         "na mniej, wiekszych pozycji. Wymaga --sucho")
    args = ap.parse_args()

    # Wariant eksperymentalny NIE MOZE promowac wersji modelu ani nadpisywac
    # stanu nauki - inaczej jeden przebieg porownawczy po cichu zmienialby to,
    # czym handluje produkcja.
    #
    # Porownanie idzie z DOMYSLNA wartoscia, nie z nazwa wpisana na sztywno:
    # gdy pomiar wskaze inny wariant i domyslny sie zmieni, blokada przesuwa
    # sie razem z nim, zamiast zaczac blokowac wlasna produkcje.
    domyslny = ap.get_default("wybor")
    if args.wybor != domyslny and not args.sucho:
        sys.exit(f"--wybor inny niz {domyslny} wymaga --sucho")
    # Ten sam powod: uniwersum produkcyjne zmienia sie migracja, nie flaga.
    if args.instrumenty and not args.sucho:
        sys.exit("--instrumenty nadpisuje uniwersum decyzyjne i wymaga --sucho")
    # I ten sam: wielkosc pozycji jest decyzja o ryzyku, nie parametrem przebiegu.
    if args.zakladane_pozycje and not args.sucho:
        sys.exit("--zakladane-pozycje zmienia wielkosc pozycji i wymaga --sucho")

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    with psycopg.connect(url, row_factory=dict_row) as conn:
        h = get_horizon(args.bar)
        egz = Egzekucja(limit=args.limit)
        print(f"Walk-forward: świeca {h.bar}, trening {h.train_days} dni / "
              f"test {h.test_days} dni, krok {h.step_days} dni")
        # NAGLOWEK PODAWAL DOTAD SAMA LICZBE: „koszt rundy 0.040%". Czytalo sie
        # to jak pelny rachunek, a byla to suma dwoch skladnikow z czterech.
        # Karta wypisuje takze te, ktorych nie ma - slowami, nie zerami.
        karta = karta_rundy(args.kind, args.limit, spread_bps=args.spread_bps,
                            spread_nasza_ksiega=not args.spread_obca_ksiega)
        print(f"Egzekucja: {args.kind}, zlecenia "
              f"{'z limitem' if args.limit else 'rynkowe'}")
        for linia in opis_karty(karta):
            print(linia)
        print(f"Wybór wag na oknie treningowym: {args.wybor}"
              f"{f' ({args.trials} losowań + założenia + IC)' if args.wybor == 'argmax' else ' (bez losowania)'}\n")
        tylko = {s.strip().upper() for s in args.instrumenty.split(",") if s.strip()}
        if args.zakladane_pozycje:
            print(f"Budżet na pozycję liczony pod {args.zakladane_pozycje} "
                  f"pozycji jednocześnie (wariant, nie produkcja)")
        train(conn, args.engine, args.days, args.trials, h, args.kind, egz,
              sucho=args.sucho, wybor=args.wybor, tylko=tylko or None,
              zakladane_pozycje=args.zakladane_pozycje or None,
              zgoda_na_promocje=args.zgoda_na_promocje,
              spread_bps=args.spread_bps,
              spread_nasza_ksiega=not args.spread_obca_ksiega)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
