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
from collections import Counter
from dataclasses import dataclass, field, replace as dc_replace
from decimal import Decimal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg
from psycopg.rows import dict_row

from services.engine.risk_rules import (
    DEFAULT_LIMITS, MIN_CONFIDENCE, START_EQUITY, position_budget, wielkosc_pozycji,
    wartosc_limitu,
)
from services.engine.horizons import DEFAULT_BAR, HORIZONS, Horizon, get as get_horizon
from services.engine.bramki import (
    EXIT_RATIO, MIN_PROB_PROGU, PROG_WEJSCIA_POCZATKOWY, Bramki, bramki_z_prob,
)
from services.engine.portfolio import (
    Egzekucja, indeks_swiec, kalibruj_przebicie, kup_i_trzymaj,
    metrics as portfolio_metrics, okno_wykonania,
    precompute_panels, simulate_portfolio, udzial_powtornych_wejsc,
    wynik_po_doplacie,
)
from services.engine.sesja import sesja_historyczna
from services.engine.koszty import (
    NIEZMIERZONE, Karta, brama_0, karta_rundy, koszt_niewypelnienia, koszty,
    opis_bramy_0, opis_karty, zwroty_po_doplacie,
)
from services.engine.spread import Pomiar, opis_pomiaru, zmierz_spread
from services.engine import baseline as odniesienia
from services.engine import panel_plik
from services.engine.statystyka import (
    MIN_PROB_ROZRZUTU, PROG_DSR, deflowany_sharpe, profit_factor, rozklad,
    liczba_niezaleznych, rozrzut_sharpeow, sharpe as sharpe_proby, sharpe_dla_dsr,
    spearman, wilson,
)
from services.engine.uniwersum import odcisk, opis_wykluczen, wykluczenia_spreadowe
from services.engine.signals import (
    DEFAULT_WEIGHTS, REGIMES, SIGNALS, classify_regime, combine, evaluate,
    wlaczone_eksperymenty,
)
from services.engine.tory import zamkniecie
from services.engine.wersja import sha_kodu
from services.engine.dozor import podsumuj_dryf_cech

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
# EXIT_RATIO i minimum proby stoja w `services/engine/bramki.py` razem
# z polityka malej proby - tutaj byly by druga kopia tej samej liczby.
# Prog pewnosci przychodzi z risk_rules - ta sama liczba, ktora stosuje petla
# decyzyjna. Zdefiniowany tu osobno oznaczalby, ze trener kalibruje wagi przy
# innym progu, niz produkcja potem stosuje.
MIN_CONF = MIN_CONFIDENCE


# UCZENIE TYLKO STRONY, KTORA DA SIE ZAGRAC (2026-10-07, H-6).
#
# Na spocie krotkich nie ma (`koszty(kind).krotka_pozycja is False`): symulacja
# odrzuca kazde wejscie ze score < 0 (`portfolio.simulate_portfolio`,
# `pominiete_shorty`), petla tak samo (`loop.py`, `enter_short`). Do tej daty
# trening uczyl sie jednak OBU stron:
#   * prog wejscia byl percentylem |score|, wiec w rezimie, w ktorym duze
#     wyniki sa ujemne (trend_down), prog stal wysoko z powodu shortow, ktorych
#     nikt nie zagra - a dlugie wejscia, ktore go mijaly, to garstka skrajnych
#     odbic, czyli kupowanie spadkow;
#   * wagi IC nagradzaly sygnal za trafne przewidywanie SPADKOW tak samo jak
#     wzrostow, choc na spocie przewidziany spadek daje co najwyzej "nie
#     wchodz".
# Dla rynku bez shortow obie rzeczy licza sie teraz z obserwacji, na ktorych
# decyzja long jest mozliwa (`proby_wynikow`, `ic_weights`). Rynek z shortami
# (perp) - bez zmian, dwustronnie. Klucz `strona` w `params` tylko poza
# wartoscia domyslna. Od 2026-10-18 (A-5) `stock` jest rynkiem bez shortow
# (`koszty.CENNIK`), wiec tor equity dostaje `strona: dluga`, czyli zmiane
# struktury i nowa wersje `candidate`.
STRONA_DLUGA = "dluga"


def strona_uczenia(kind: str) -> str | None:
    """"dluga", gdy rynek nie pozwala na shorty, inaczej None (obie strony).
    CZYSTA FUNKCJA - ta sama flaga, ktora czyta symulacja i petla."""
    return None if koszty(kind).krotka_pozycja else STRONA_DLUGA

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

# Ile cech o najwyzszym dryfie wypisuje log przebiegu (`dryf_cech`). Mniej niz
# w tresci alarmu (`dozor.DRYF_CECH_W_TRESCI`): log ma wskazac, gdzie patrzec,
# a pelna rozpiska jest w `learn.drift_metric`.
DRYF_CECH_W_LOGU = 3

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


KUBELKI_POKRYCIA = ((0.0, 0.2), (0.2, 0.5), (0.5, 0.9), (0.9, 1.01))


def opis_pokrycia(trans: list[tuple[float, float, str]]) -> list[str]:
    """Wynik wobec POKRYCIA WAG w chwili wejscia. CZYSTA FUNKCJA.

    PYTANIE, KTORE TO ROZSTRZYGA, I DLACZEGO PRODUKCJA GO NIE ROZSTRZYGNIE

    `signals.combine` dzieli wynik przez sume |wag| sygnalow, ktore sie
    ODEZWALY. Renormalizacja mowi "pozostale reprezentuja calosc", wiec wynik
    z jednego sygnalu moze siegnac pelnej sily - i docstring `combine` nazywa to
    CENA TEJ SEMANTYKI, zostawiajac decyzje "od jakiego pokrycia nie handlujemy"
    jako jawna i niepodjeta.

    Zmierzone na produkcji 2026-09-27 (decyzje z 21 dni):

        rezim       decyzji  mediana pokrycia  ponizej 0,5
        range           639             0,051    590 (92%)
        high_vol        269             1,000      0
        trend_up        142             1,000      0

    W `range` odzywaja sie tylko `news_shock` (waga 0,0) i `funding_carry`
    (waga ~0,04); szesc sygnalow cenowo-wolumenowych milczy w 91% decyzji, bo
    w konsolidacji nie maja czego powiedziec. Wynik jest wiec zwykle JEDNYM
    sygnalem wzmocnionym dwudziestokrotnie - a `range` jest jedynym rezimem
    z ujemnym Sharpe'em na transakcje (-0,0478 na 437, tor 4h).

    NA PRODUKCJI REZIMU I POKRYCIA ROZPLESC SIE NIE DA: cale niskie pokrycie
    jest w `range`, caly `range` ma niskie pokrycie. "Niskie pokrycie szkodzi"
    i "`range` jest trudny" daja tam IDENTYCZNE dane. Symulacja ma tysiace
    transakcji w kazdym rezimie i pokrycie zmienne w czasie, wiec te dwie
    hipotezy da sie w niej rozdzielic - i to jest cala tresc tej tabeli.

    CZEGO TA TABELA NIE JEST: nie jest testem. Transakcje nie sa niezalezne,
    a kubelki wybrano przed pomiarem, ale jest ich cztery razy liczba rezimow.
    Sluzy do postawienia progu, nie do jego uzasadnienia.
    """
    if not trans:
        return []
    linie = ["", "WYNIK WOBEC POKRYCIA WAG (złączona próba OOS, po kosztach):"]

    def wiersz(etykieta: str, zwroty: list[float]) -> str:
        n = len(zwroty)
        if n < 2:
            return f"  {etykieta:<30} n={n:<5} — za mało transakcji"
        sr = statistics.fmean(zwroty)
        sd = statistics.stdev(zwroty)
        sh = (sr / sd) if sd > 0 else 0.0
        traf = sum(1 for r in zwroty if r > 0) / n
        return (f"  {etykieta:<30} n={n:<5} Sharpe/transakcję {sh:+.4f} "
                f"±{1.0 / math.sqrt(n):.4f}  trafność {traf:.1%}  "
                f"średnia {sr:+.4%}")

    def kubelek(nazwa: str, wybrane: list[tuple[float, float, str]]) -> list[str]:
        out = []
        for lo, hi in KUBELKI_POKRYCIA:
            zwroty = [r for r, p, _ in wybrane if lo <= p < hi]
            if zwroty:
                out.append(wiersz(f"{nazwa}pokrycie {lo:.1f}–{hi if hi <= 1 else 1.0:.1f}",
                                  zwroty))
        return out

    linie += kubelek("", trans)
    rezimy = sorted({rez for _, _, rez in trans})
    if len(rezimy) > 1:
        linie.append("  po reżimach — TU rozdziela się reżim od pokrycia:")
        for rez in rezimy:
            wybrane = [t for t in trans if t[2] == rez]
            w_kubelkach = kubelek("  ", wybrane)
            if len(w_kubelkach) > 1:
                linie.append(f"    {rez}")
                linie += w_kubelkach
            elif w_kubelkach:
                # JEDEN KUBELEK TO BRAK ROZDZIELENIA, I TRZEBA TO POWIEDZIEC.
                # Rezim, w ktorym wszystkie transakcje maja podobne pokrycie,
                # nie odpowiada na pytanie - a wypisany bez tego zdania wyglada
                # tak samo jak ten, ktory odpowiada.
                linie.append(f"    {rez} — pokrycie NIE ZMIENIA SIĘ w tym "
                             f"reżimie, więc nie rozdziela niczego")
                linie += w_kubelkach
    linie.append("  To NIE JEST test: transakcje nie są niezależne, a kubełków "
                 "jest cztery razy tyle, ile reżimów. Służy do postawienia "
                 "progu, nie do jego uzasadnienia.")
    return linie


def opis_stron(trans: list[tuple[float, int, str]]) -> list[str]:
    """Wynik strony DLUGIEJ osobno od KROTKIEJ, po rezimach. CZYSTA FUNKCJA.

    DLACZEGO TO ISTNIEJE. Naglowek `portfolio.py` mowi wprost, ze audyt
    zmierzyl 135 krotkich sygnalow na 180 ponad progiem i ze "nikt nie wie, ile
    byly warte". To jest ta liczba. Bez niej caly wariant perp - jedyny powod,
    dla ktorego krotkie w ogole istnieja - nie ma zadnej miary.

    DRUGI POWOD JEST MOCNIEJSZY I DOTYCZY PROGU. Prog wejscia jest percentylem
    `abs(score)`, czyli JEDNA liczba dla obu stron - a skala wyniku strony
    dlugiej i krotkiej nie jest ta sama. Zmierzone 2026-09-27 na produkcji,
    rezim `range`: p88 strony dlugiej 0,6205 wobec 0,0568 strony krotkiej,
    czyli JEDENASCIOKROTNIE. Prog `range` = 0,3509 lezy wiec miedzy nimi: po
    stronie dlugiej przepuszcza mniej wiecej zamierzone 12%, a po krotkiej
    niemal nic. `range` jest przez to praktycznie JEDNOKIERUNKOWY i nikt tego
    nie postanowil.

    CZY TO WADA, ROZSTRZYGA DOPIERO WYNIK - i dlatego najpierw ta tabela,
    a nie zmiana bramki. Jesli krotkie w `range` sa bezwartosciowe, dzisiejsze
    zachowanie jest szczescliwym trafem, ktory warto zapisac jako regule;
    jesli sa dobre, tracimy je przez skale, a nie przez decyzje.

    `side` jest +1 dla dlugiej i -1 dla krotkiej (`portfolio.py`). Odtwarzanie
    strony po znaku zwrotu jest NIEMOZLIWE - stratny long i stratny short
    wygladaja identycznie - wiec bez tej kolumny pytania nie da sie zadac.
    """
    if not trans:
        return []
    linie = ["", "STRONA DŁUGA KONTRA KRÓTKA (złączona próba OOS, po kosztach):"]

    def wiersz(etykieta: str, zwroty: list[float]) -> str:
        n = len(zwroty)
        if n < 2:
            return (f"  {etykieta:<26} n={n:<5} — za mało transakcji, żeby "
                    f"policzyć Sharpe'a")
        sr = statistics.fmean(zwroty)
        sd = statistics.stdev(zwroty)
        # Sharpe NA TRANSAKCJE, ta sama miara co wszedzie indziej w tym raporcie.
        sh = (sr / sd) if sd > 0 else 0.0
        traf = sum(1 for r in zwroty if r > 0) / n
        # BLAD STANDARDOWY, BO BEZ NIEGO ROZNICA STRON NIE ZNACZY NIC. Przy
        # Sharpe rzedu 0,1 i n rzedu stu blad jest rzedu 0,1 - czyli tyle, ile
        # cala miara. Brak tej kolumny zamienilby szum w "strona krotka jest
        # gorsza".
        se = 1.0 / math.sqrt(n)
        return (f"  {etykieta:<26} n={n:<5} Sharpe/transakcję {sh:+.4f} "
                f"±{se:.4f}  trafność {traf:.1%}  średnia {sr:+.4%}")

    dlugie = [r for r, side, _ in trans if side > 0]
    krotkie = [r for r, side, _ in trans if side < 0]
    linie.append(wiersz("długa", dlugie))
    linie.append(wiersz("krótka", krotkie))
    udzial = len(krotkie) / len(trans)
    linie.append(f"  udział krótkich w transakcjach: {udzial:.1%}")

    rezimy = sorted({rez for _, _, rez in trans})
    if len(rezimy) > 1:
        linie.append("  po reżimach (reżim DOMINUJĄCY w oknie, nie w chwili wejścia):")
        for rez in rezimy:
            d = [r for r, side, x in trans if x == rez and side > 0]
            k = [r for r, side, x in trans if x == rez and side < 0]
            linie.append(f"    {rez}")
            linie.append("  " + wiersz("długa", d))
            linie.append("  " + wiersz("krótka", k))
    linie.append("  RÓŻNICA STRON NIE JEST TU TESTEM: przedziały ±1/√n liczone "
                 "na transakcjach, które NIE są niezależne (te same świece, te "
                 "same okna). Liczba mówi, czy warto pytać dalej.")
    return linie


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

    # KAZDA LICZBA W TYM BLOKU JEST BRUTTO - i jest tak nazwana.
    #
    # Wolajacy podaje tu zwroty i Sharpe'y okien SPRZED doplaty z karty
    # (`wszystkie_zwroty`, `all_sharpes_brutto`): koszt progowy i drabina
    # wrazliwosci potrzebuja punktu zero, a doplate odejmuja same. Do
    # 2026-10-02 wieczorem `sharpe_okien` przychodzilo juz PO doplacie, wiec
    # w jednym bloku stal Sharpe zlaczony brutto obok sredniej i mediany okien
    # po karcie - dwie miary dwoch swiatow pod jednym naglowkiem.
    linie = ["", "PRZEWAGA BRUTTO — po kosztach, które symulacja naliczyła "
                 "(prowizja, poślizg, finansowanie), PRZED dopłatą z karty:"]
    linie.append(
        f"  brutto: Sharpe/transakcję ze ZŁĄCZONEJ próby {p['n_transakcji']} "
        f"transakcji: {p['sharpe_zbiorczy']:+.4f}" if p["sharpe_zbiorczy"] is not None
        else "  brutto: Sharpe/transakcję ze złączonej próby: — (za mało transakcji)")
    # OBIE LICZBY NARAZ. Rozjazd miedzy nimi jest wynikiem, nie szczegolem:
    # gdy sa rozne, to znaczy, ze srednia po oknach mowi o wadze okien,
    # a nie o transakcjach.
    linie.append(
        f"  brutto: ta sama miara jako ŚREDNIA PO {p['n_okien']} OKNACH: "
        f"{p['sharpe_sredni_po_oknach']:+.4f} — inna liczba niż wyżej, bo "
        f"średnia ilorazów nie jest ilorazem; okno o kilku transakcjach waży "
        f"w niej tyle samo, co okno o dwustu"
        if p["sharpe_sredni_po_oknach"] is not None else "  brutto: średnia po oknach: —")
    ok = p["okna"]
    if ok["n"]:
        linie.append(
            f"  brutto: rozkład okien: mediana {ok['mediana']:+.4f}, kwartyle "
            f"[{ok['q1']:+.4f}, {ok['q3']:+.4f}]" if ok["q1"] is not None
            else f"  brutto: rozkład okien: mediana {ok['mediana']:+.4f}")
        linie.append(
            f"  brutto: okien ujemnych: {ok['ujemnych']}/{ok['n']} "
            f"({ok['udzial_ujemnych']:.1%})")
    pf = p["profit_factor_zbiorczy"]
    linie.append(
        f"  brutto: profit factor ZBIORCZY: {pf:.4f}" if pf is not None
        else "  brutto: profit factor zbiorczy: — (brak strat w próbie)")
    linie.append(f"  brutto: średni zwrot z transakcji: {p['sredni_zwrot'] * 100:+.4f}%, "
                 f"trafnych {p['hit_rate']:.1%}")

    # TE SAME MIARY PO DOPLACIE - to one stoja za bramka i DSR. Liczone ta
    # sama `podsumowanie_przewagi` i `_sharpe_po_oknach` co drabina nizej,
    # czyli przez `koszty.zwroty_po_doplacie`, a nie osobnym rachunkiem.
    doplata = karta.doplata_poza_symulacja
    if doplata:
        po = podsumowanie_przewagi(zwroty, [], doplata=doplata)
        okna_po = [x for x in (sharpe_proby(zwroty_po_doplacie(o, doplata))
                               for o in zwroty_okien) if x is not None]
        linie.append("")
        linie.append(f"PO DOPŁACIE ({opis_doplaty(karta)}) — tak liczy bramka i DSR:")
        linie.append(
            f"  po dopłacie: Sharpe/transakcję ze ZŁĄCZONEJ próby: "
            f"{po['sharpe_zbiorczy']:+.4f}" if po["sharpe_zbiorczy"] is not None
            else "  po dopłacie: Sharpe/transakcję ze złączonej próby: —")
        if okna_po:
            linie.append(
                f"  po dopłacie: średnia po {len(okna_po)} oknach "
                f"{statistics.fmean(okna_po):+.4f}, mediana okien "
                f"{statistics.median(okna_po):+.4f}, okien ujemnych "
                f"{sum(1 for x in okna_po if x < 0)}/{len(okna_po)} "
                f"(odchylenie PRÓBKOWE, jak w drabinie; bramka bierze medianę "
                f"z `portfolio.metrics`, czyli populacyjne)")
        linie.append(f"  po dopłacie: średni zwrot z transakcji: "
                     f"{po['sredni_zwrot'] * 100:+.4f}%, trafnych {po['hit_rate']:.1%}")

    linie.append("")
    linie.append("RACHUNEK KOSZTU RUNDY — SKŁADNIK PO SKŁADNIKU, Z POCHODZENIEM:")
    linie.extend(opis_karty(karta))

    linie.append("")
    prog = p["koszt_progowy"]
    if prog is None:
        linie.append("KOSZT PROGOWY (z brutto): — (brak transakcji)")
    elif prog <= 0:
        linie.append(
            f"KOSZT PROGOWY (z brutto): {_pb(prog)} na rundę — UJEMNY. Przewagi nie ma "
            f"JUŻ przy kosztach, które symulacja nalicza; nienaliczone "
            f"składniki ({', '.join(karta.nieznane) or 'brak'}) mogą ją tylko "
            f"pogłębić.")
    else:
        linie.append(
            f"KOSZT PROGOWY (z brutto): {_pb(prog)} na rundę. Tyle wynosi cały zapas na "
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
        # DOPLATA Z KARTY DOSTAJE WLASNY WIERSZ. Od 2026-10-02 bramka statusu
        # i wszystkie miary okien licza sie wlasnie przy niej, wiec wiersz
        # „0 pb" jest juz BRUTTO, a nie rachunkiem, ktory widzi bramka.
        z_karty = round(karta.doplata_poza_symulacja * 1e4, 4)
        for bps in sorted({0.0, 2.0, 5.0, 10.0, 20.0, z_karty}):
            z = podsumowanie_przewagi(zwroty, sharpe_okien,
                                      doplata=bps / 1e4)["sharpe_zbiorczy"]
            o = _sharpe_po_oknach(zwroty_okien, bps / 1e4)
            o_txt = "—" if o is None else f"{o:+.4f}"
            znacznik = ("   <- dopłata z karty: tak liczy bramka" if bps == z_karty
                        else "   <- brutto (sama symulacja)" if bps == 0 else "")
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
    s = [x for x in (sharpe_proby(zwroty_po_doplacie(okno, doplata))
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
    po = zwroty_po_doplacie(zwroty, doplata)
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


def przewaga_dsr(zwroty: list[float], n_prob: int,
                 odchylenie_prob: float | None = None,
                 wejscia: list | None = None) -> dict:
    """Deflowany Sharpe dla ZLACZONEJ proby transakcji OOS. CZYSTA FUNKCJA.

    Cienka warstwa nad `statystyka.deflowany_sharpe` - istnieje, zeby w jednym
    miejscu stalo, KTORA probe i KTORY licznik podstawiamy:

      * `zwroty` to te same transakcje, z ktorych liczy sie `sharpe_zbiorczy`
        w `podsumowanie_przewagi` - czyli zlaczona proba, a NIE Sharpe'y okien.
        Sharpe ze sredniej po oknach nie ma sensownego T do wzoru na PSR:
        „liczba obserwacji" byla by wtedy liczba okien, a koszt placi kazda
        transakcja z osobna.
      * `n_prob` to N SELEKCJI z `n_konfiguracji` - rozne `params` po wersjach
        modelu, ktore doszly do wyniku. NIE jest to licznik
        `learn.hypothesis_trial`, choc ten opis tak twierdzil do 2026-09-27
        (i tak twierdzil komentarz przy migracji 0006). Tamten licznik ma dla
        toru 4h 12 613 wierszy wobec N = 52, wiec pomylka nie jest kosmetyczna;
        powod, dla ktorego nie jest mianownikiem, stoi w `n_konfiguracji`.
        CZEGO TEN LICZNIK NIE JEST, jest opisane w renacode-infra: `docs/projekty/trader/01-data-architecture.md`
        i warto to miec przed oczami przy czytaniu wyniku: tabela ma
        `UNIQUE (model_id, config_hash)` i INSERT `ON CONFLICT DO NOTHING`,
        wiec liczy ROZNE WEKTORY WAG, a nie proby. Dwa przebiegi tej samej
        konfiguracji dokladaja tyle wierszy, ile wektorow sie zmienilo -
        a przy `--wybor ic` (domyslnym) jest to jeden na okno i zmienia sie
        co noc, bo granice okien sie przesuwaja.
      * `odchylenie_prob` to ZMIERZONY rozrzut Sharpe'a miedzy KONFIGURACJAMI -
        `V[SR_n]` Baileya. Nie miedzy oknami: ta pomylka daje liczbe dziesiec
        razy wieksza i zostala tu raz wdrozona (uzasadnienie liczbowe
        w `odchylenie_prob_z_bazy`). None znaczy "nie zmierzono" i wtedy
        `deflowany_sharpe` podstawia wariancje samego estymatora pod H0, czyli
        idzie droga OPTYMISTYCZNA - zmierzone 2026-09-27 o 2,7%, nie o rzad.
      * `wejscia` to swieca wejscia (`opened_at`) KAZDEJ transakcji z `zwroty`,
        w tej samej kolejnosci. T we wzorze to liczba ROZNYCH swiec wejscia,
        nie liczba transakcji (audyt 2026-10-09, W-2): kilkanascie par
        otwartych na jednej swiecy to jeden szok rynkowy, a liczone osobno
        podnosily DSR samym poszerzeniem handlu - powielenie proby x2 dawalo
        0,8875 -> 0,9949. Uzasadnienie miary przy `deflowany_sharpe`.
        None = stara sciezka (T = liczba transakcji), zostawiona dla
        wolajacych spoza treningu.
    """
    n_efektywne = None
    if wejscia is not None:
        if len(wejscia) != len(zwroty):
            raise ValueError(f"wejscia ({len(wejscia)}) nie odpowiadaja "
                             f"zwrotom ({len(zwroty)}) jeden do jednego")
        # Brak znacznika (nie powinien wystapic) liczy sie jako osobna
        # obserwacja - tak jak przed poprawka, nie jako wspolna grupa `None`.
        n_efektywne = liczba_niezaleznych(
            [w if w is not None else ("bez_wejscia", i) for i, w in enumerate(wejscia)])
    return deflowany_sharpe(zwroty, n_prob, odchylenie_prob, n_efektywne)


# Rozrzut Sharpe'a MIEDZY KONFIGURACJAMI - material na V[SR_n] Baileya.
#
# GRUPOWANIE PO `v.params`, A NIE PO OKNACH ANI PO PRZEBIEGACH. To jest cala
# tresc tego zapytania i powod, dla ktorego stoi ono tutaj z nazwa:
#
#   * `params` to ta sama wielkosc, ktora liczy `n_konfiguracji`
#     (`count(DISTINCT params)`) i ktora panel pokazuje jako N. Poprzeczka
#     deflacji i mianownik, wobec ktorego ja liczymy, MUSZA opisywac ten sam
#     zbior - inaczej odejmujemy klatwe zwyciezcy policzona dla innej mnogosci.
#   * `avg(...)` po przebiegach TEJ SAMEJ konfiguracji, bo dwa kolejne nocne
#     przebiegi dziela 39 z 40 okien (rho ~0,97). Sa jedna proba, nie dwiema;
#     policzone osobno zawyzalyby n i zanizaly rozrzut o czynnik zalezny od
#     tego, ile nocy minelo.
#   * `median_oos_sharpe` jest agregatem po WSZYSTKICH oknach przebiegu, wiec
#     szum estymacji Sharpe'a z jednego okna (~1/n_transakcji) jest w nim
#     usredniony. Pojedyncze okno ma go tyle, ze przeslania wszystko inne -
#     patrz komentarz w `odchylenie_prob_z_bazy`.
_SQL_ROZRZUT_KONFIGURACJI = """
SELECT avg(l.median_oos_sharpe) AS sharpe
FROM learn.learning_state_log l
JOIN learn.model_version v ON v.id = l.model_version_id
WHERE v.model_id = %s AND l.median_oos_sharpe IS NOT NULL
GROUP BY v.params
"""


def odchylenie_prob_z_bazy(cur, model_id: int) -> tuple[float | None, int]:
    """Rozrzut Sharpe'a MIEDZY KONFIGURACJAMI tego modelu. Z BAZY.

    Oddaje PARE: odchylenie (albo None) i liczbe konfiguracji, z ktorych wyszlo.
    Liczba jest potrzebna w opisie, bo to ona rozstrzyga, czy wynik jest
    pomiarem - a nie sama obecnosc liczby.

    CZYM TA WIELKOSC NIE JEST. Dwa bledy sa tu naturalne, oba daja liczbe
    wygladajaca poprawnie i oba zostaly juz raz popelnione:

      1. NIE JEST ROZRZUTEM MIEDZY OKNAMI. Tak bylo do 2026-09-27: funkcja
         czytala `learn.hypothesis_trial.oos_sharpe`, gdzie kazdy wiersz to
         Sharpe z JEDNEGO okna. Zmierzone na produkcji tego dnia, tor 4h:

             miedzy OKNAMI        (wf_metric, n=2353)          sd = 0,2627
             miedzy PRZEBIEGAMI   (learning_state_log, n=29)   sd = 0,0270
             miedzy KONFIGURACJAMI (DISTINCT params, n=28)     sd = 0,0266
             podstawienie z H0    (T=1500)                     sd = 0,0259

         Roznica jest DZIESIECIOKROTNA i przewraca werdykt z "nie przechodzi
         o wlos" na "nie przechodzi o rzad wielkosci". Poprzeczka rosla z
         +0,062 na +0,618, a DSR spadal z 0,71 na 0,00.

         Powod jest mierzalny, nie interpretacyjny. Rozrzut miedzy oknami ma
         DWA skladniki, z ktorych zaden nie jest rozrzutem konfiguracji:
         zmiennosc RYNKU okno po oknie oraz szum estymacji Sharpe'a z proby
         jednego okna. Drugi maleje jak 1/n: przy medianie 35 transakcji na
         okno daje wariancje rzedu 0,029 przy zmierzonej calkowitej 0,069,
         a dopasowanie `0,041 + 0,82/n` ma przy 1/n wspolczynnik 0,82 wobec
         teoretycznego 1,0 - czyli to jest wlasnie szum estymatora.

      2. NIE JEST SZUMEM ESTYMATORA. Ten drugi jest MNIEJSZY: to podstawienie
         z H0 (`sigma/sqrt(T-1)`), ktore `deflowany_sharpe` wstawia, gdy tej
         liczby nie ma. Rozrzut konfiguracji musi byc od niego wieksza albo
         rowny, bo konfiguracje roznia sie CO NAJMNIEJ szumem - dlatego
         `deflowany_sharpe` traktuje podstawienie jako PODLOGE i nie pozwala
         pomiarowi obnizyc poprzeczki pod nia.

    CZEGO TA LICZBA NADAL NIE MIERZY - trzy rzeczy, wszystkie w te sama strone:

      * INNA STATYSTYKA NIZ SR W BRAMCE. `median_oos_sharpe` to mediana po
        oknach, a `SR`, od ktorego odejmujemy poprzeczke, to Sharpe ZLACZONEJ
        proby transakcji. To nie jest ta sama wielkosc (renacode-infra: docs/projekty/trader/03-wynik-walk-forward.md podaje przypadek,
        w ktorym mediana po oknach i wynik zlaczony rozjechaly sie o rzad).
        Zamkniecie wymaga zapisywania Sharpe'a zlaczonego per przebieg - dzis
        nie ma go w zadnej kolumnie.
      * KONFIGURACJE OCENIONE NA TYCH SAMYCH DANYCH. Przebiegi dziela okna,
        wiec rozrzut opisuje "ile zmienia zmiana konfiguracji przy niemal
        ustalonych danych". Bailey chce dokladnie tego, ale to znaczy, ze dla
        innych danych rozrzut byl by WIEKSZY.
      * 28 KONFIGURACJI Z 59, KTORE LICZY N. Pozostale to przebiegi bez wiersza
        w dzienniku stanu (sucho, przerwane, sprzed jego wprowadzenia). Gdyby
        byly systematycznie inne, rozrzut byl by wiekszy.

    Wszystkie trzy zanizaja rozrzut, wiec poprzeczka jest DOLNYM oszacowaniem,
    a DSR GORNYM. Kierunek jest jeden i to jest jedyna dobra wiadomosc o tej
    liscie.
    """
    cur.execute(_SQL_ROZRZUT_KONFIGURACJI, (model_id,))
    sharpes = [float(r["sharpe"]) for r in cur.fetchall() if r["sharpe"] is not None]
    return rozrzut_sharpeow(sharpes), len(sharpes)


def opis_zrodla_odchylenia(wynik: dict, n_konfiguracji_z_oos: int) -> str:
    """Skad wzial sie rozrzut miedzy probami - w tym samym zdaniu, co DSR.

    Bez tego zdania DSR policzony z pomiaru i DSR policzony z podstawienia
    wygladaja identycznie, a znacza co innego: drugi jest GORNYM oszacowaniem
    pierwszego. Do 2026-09-27 istniala tylko druga droga i nikt nie mial jak
    zobaczyc z wyniku, ze nia idzie.

    TRZY DROGI, NIE DWIE. Od poprawki z 2026-09-27 (wieczor) jest jeszcze
    trzecia: pomiar, ktory wyszedl PONIZEJ podlogi H0. Musi byc widoczny osobno,
    bo w liczbach wyglada dokladnie jak podstawienie, a znaczy co innego -
    znaczy "mierzylismy i pomiar jest niepelny".
    """
    sd = wynik.get("odchylenie_prob")
    if sd is None:
        return ""
    zrodlo = wynik.get("zrodlo_odchylenia", "")
    # DWIE POPULACJE, DWIE LICZBY, JEDNO ZDANIE. Rozrzut wychodzi z WEZSZEJ
    # populacji niz N: z konfiguracji, ktore maja ZAPISANY Sharpe OOS
    # (`learning_state_log`), a nie ze wszystkich, ktore doszly do wyniku.
    # Zmierzone 2026-09-27, tor 4h: 28 wobec N = 52. Podstawienie sd z 28 jako
    # sd z 52 jest ZALOZENIEM, nie pomiarem, i musi byc widoczne w tym samym
    # zdaniu - inaczej czytelnik bierze obie liczby za jedna probe.
    n_selekcji = wynik.get("n_prob")
    wspolne = (" Liczone po DISTINCT learn.model_version.params, czyli po tej "
               "samej WIELKOSCI, ktora jest N — nie po oknach: rozrzut miedzy "
               "OKNAMI jest dziesieciokrotnie wieksszy (0,2627 wobec 0,0266 "
               "zmierzonych 2026-09-27) i nie jest rozrzutem konfiguracji.")
    # Zdanie o DWOCH POPULACJACH nalezy WYLACZNIE do galezi `pomiar`. W dwoch
    # pozostalych uzyta sd jest podloga H0, ktora nie pochodzi z zadnej proby
    # konfiguracji - doklejenie tam "rozrzut z N konfiguracji" bylo by falszem
    # o dokladnie tym ksztalcie, ktory ten opis ma tepic.
    dwie_populacje = (
        f" POPULACJE SIE ROZNIA: rozrzut wyszedl z {n_konfiguracji_z_oos} "
        f"konfiguracji z ZAPISANYM Sharpe'em OOS, a N = {n_selekcji} liczy "
        f"wszystkie, ktore doszly do wyniku (2026-09-27, tor 4h: 28 wobec 52). "
        f"Przyjecie rozrzutu tych {n_konfiguracji_z_oos} za rozrzut calej "
        f"populacji N jest ZALOZENIEM, nie pomiarem.")
    if zrodlo.startswith("podloga"):
        return (f"Rozrzut Sharpe'ow miedzy konfiguracjami ZMIERZONY, ale "
                f"ODRZUCONY: wyszedl ponizej samego bledu estymatora, wiec "
                f"uzyto podlogi H0 (sd = {sd:.4f}). Konfiguracje roznia sie CO "
                f"NAJMNIEJ szumem, wiec pomiar mniejszy od niego jest dowodem "
                f"na niepelnosc pomiaru, nie na zgodnosc konfiguracji."
                + wspolne)
    if zrodlo.startswith("pomiar"):
        return (f"Rozrzut Sharpe'ow miedzy konfiguracjami ZMIERZONY: "
                f"sd = {sd:.4f} z {n_konfiguracji_z_oos} konfiguracji "
                f"z policzonym Sharpe'em OOS (podloga H0: "
                f"{wynik.get('odchylenie_podloga_h0', 0.0):.4f}). Jest to DOLNE "
                f"oszacowanie: statystyka proby to mediana po oknach, a nie "
                f"Sharpe zlaczonej proby, konfiguracje oceniano na tych samych "
                f"oknach, a Sharpe ma tylko czesc z nich — wszystkie trzy braki "
                f"ZANIZAJA rozrzut, wiec DSR jest GORNYM oszacowaniem."
                + dwie_populacje + wspolne)
    return (f"Rozrzut Sharpe'ow miedzy konfiguracjami NIE ZMIERZONY: "
            f"podstawiono wariancje samego estymatora pod H0 (sd = {sd:.4f}), "
            f"bo konfiguracji z policzonym Sharpe'em OOS jest "
            f"{n_konfiguracji_z_oos}, a prog to {MIN_PROB_ROZRZUTU}. "
            f"Konfiguracje roznia sie nie tylko szumem, wiec PRAWDZIWY rozrzut "
            f"jest wiekszy, poprzeczka wyzsza, a prawdziwy DSR NIZSZY niz ten — "
            f"zmierzone 2026-09-27 na torze 4h: 0,0266 wobec podlogi 0,0260, "
            f"czyli o 2,7%, a nie o rzad wielkosci." + wspolne)

def _hash_konfiguracji(cand: dict) -> bytes:
    """Odcisk wektora wag - klucz wiersza w `learn.hypothesis_trial`.

    Wydzielony z `zapisz_hipoteze`, bo od 2026-09-27 potrzebuja go DWA miejsca:
    INSERT przy ocenie kandydata i UPDATE, ktory dokleja mu Sharpe OOS po
    symulacji poza oknem. Dwa niezalezne `hashlib.sha256(json.dumps(...))`
    rozjechalyby sie cicho - wystarczyloby, zeby jedno gubilo `sort_keys`,
    i UPDATE trafialby w zero wierszy, a kolumna zostawalaby pusta dokladnie
    tak, jak byla.
    """
    return hashlib.sha256(json.dumps(cand, sort_keys=True).encode()).digest()


def zapisz_oos_sharpe(cur, model_id: int,
                      wg_konfiguracji: dict[bytes, list[float]]) -> int:
    """Doklada Sharpe OOS do wierszy `learn.hypothesis_trial`. Oddaje liczbe wierszy.

    SREDNIA PO OKNACH, a nie wartosc z ostatniego okna. Ta sama konfiguracja
    moze wygrac w kilku oknach; `n_windows` mowi, w ilu, wiec obie kolumny
    opisuja to samo zdarzenie i dzielnik zgadza sie z licznikiem.

    UPDATE, nie UPSERT. Wiersz musi juz istniec - wstawia go `zapisz_hipoteze`
    w chwili oceny kandydata na oknie treningowym. Gdy UPDATE nie trafi
    w zaden wiersz, jest to wada odcisku (`_hash_konfiguracji` policzony
    inaczej w dwoch miejscach), a nie brak danych - i dlatego ta funkcja oddaje
    LICZBE ZMIENIONYCH WIERSZY, ktora da sie porownac z liczba konfiguracji.
    Bez tego rozjazd odciskow zostawialby kolumne pusta dokladnie tak, jak byla,
    i nie bylo by po czym tego poznac.
    """
    zmienione = 0
    for odcisk_cfg, sharpes in wg_konfiguracji.items():
        cur.execute(
            "UPDATE learn.hypothesis_trial SET oos_sharpe = %s, n_windows = %s"
            " WHERE model_id = %s AND config_hash = %s",
            (statistics.fmean(sharpes), len(sharpes), model_id, odcisk_cfg))
        zmienione += cur.rowcount
    return zmienione


def n_konfiguracji(cur, model_id: int, mv_id: int | None = None) -> int:
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
    # PRZEBIEG, KTORY NIGDY NIE DOSZEDL DO WYNIKU, NIE JEST PROBA.
    #
    # Do 2026-09-27 liczylo sie tu KAZDE `params`, takze wersje, ktore zostaly
    # w statusie `training` - czyli wiersze-sieroty po `--sucho` i po zabitych
    # zadaniach (znany skutek `--sucho`, opisany przy tej fladze). Selekcja nie
    # mogla sie na nich oprzec, bo nie oddaly wyniku: nikt ich nie widzial.
    # Liczenie ich w N zawyzalo poprzeczke, czyli mylilo sie NA NIEKORZYSC
    # strategii - ale bylo po prostu nieprawda.
    #
    # Zmierzone na produkcji 2026-09-27, tor 4h, rozbicie 59 roznych `params`:
    #
    #     28  ma zapisany median OOS Sharpe          -> bezsprzeczna proba
    #     24  skonczone (active/retired), bez wiersza w learning_state_log
    #         - to przebiegi `--sucho`: wynik POLICZYLY i wypisaly, wiec proba
    #      7  wylacznie `training` - NIGDY nie doszly do wyniku, NIE proba
    #
    # Skutek na poprzeczke przy sd = 0,0266 i T = 1500:
    #     N=28 -> +0,0544   N=52 -> +0,0609   N=59 -> +0,0622
    # czyli 0,017 DSR miedzy obronnym 52 i dawnym 59. Malo, ale w zla strone
    # i bez powodu.
    #
    # ROZNICA POPULACJI Z ODCHYLENIEM ZOSTAJE i jest nazwana w opisie werdyktu:
    # N liczy sie tutaj (52), a `odchylenie_prob_z_bazy` liczy rozrzut z tych
    # 28, ktore maja zapisany Sharpe. Uzycie rozrzutu 28 jako oszacowania
    # rozrzutu 52 jest ZALOZENIEM, nie pomiarem - i musi byc widoczne.
    #
    # WLASNA WERSJA BIEZACEGO PRZEBIEGU MUSI WEJSC OSOBNO, i to jest jedyny
    # powod, dla ktorego ta funkcja przyjmuje `mv_id`. `train()` zaklada swoj
    # wiersz w `learn.model_version` ZANIM dojdzie do bramki (INSERT ze
    # statusem domyslnym `training`), wiec w chwili liczenia N sam kandydat,
    # ktorego bramka wlasnie ocenia, jeszcze nie wyszedl z `training`. Bez tego
    # czlonu filtr gubilby dokladnie te probe, ktora jest przedmiotem selekcji,
    # a pierwszy przebieg nowego toru dostawalby N=0, czyli DSR NIEOBLICZALNY
    # (bramka fail-closed) - zmierzone: `crypto_signal_weighting_5m`
    # i `equity_signal_weighting_1h` maja dzis po jednej wersji i obie stoja
    # w `training`. `mv_id=None` (panel, ktory nie jest w trakcie przebiegu)
    # liczy sama populacje zakonczona.
    #
    # STATUSY WYPISANE Z NAZWY, nie przez `<> 'training'`: `learn.mv_status` ma
    # jeszcze `failed`, a przebieg, ktory padl, tak samo nie oddal wyniku. Dzis
    # nie ma ani jednego takiego wiersza, wiec na liczbe to nie wplywa - ale
    # lista dodatnia nie przepusci go po cichu, gdy sie pojawi.
    cur.execute(
        "SELECT count(DISTINCT params) AS n FROM learn.model_version"
        " WHERE model_id = %s AND (status IN ('candidate','active','retired')"
        "                          OR id = %s)", (model_id, mv_id))
    return cur.fetchone()["n"]


def opis_zrodla_n(n: int, trials_kumulatywnie: int) -> str:
    """Skad wzielo sie N - w tym samym zdaniu, co N.

    Bez tego 57 czyta sie jak pomiar zamkniety, a jest dolna granica. I bez
    tego nikt nie wie, ze 12 613 z `hypothesis_trial` NIE jest ta liczba -
    a wlasnie ten licznik komentarz przy migracji 0006 wskazywal jako zrodlo.
    """
    return (f"N = {n} rozróżnialnych konfiguracji z learn.model_version "
            f"(count(DISTINCT params) po wersjach candidate/active/retired plus "
            f"wersja tego przebiegu; wersje, które nigdy nie wyszły ze statusu "
            f"`training`, NIE są próbami — nie doszły do wyniku, więc selekcja "
            f"nie mogła się na nich oprzeć: dla toru 4h to 7 z 59, zmierzone "
            f"2026-09-27) — DOLNA GRANICA: `kind` i `limit` nie "
            f"były zapisywane do 2026-09-26, więc przebiegi różniące się samym "
            f"cennikiem zlewały się w jedną. Licznik learn.hypothesis_trial "
            f"({trials_kumulatywnie} wektorów wag) NIE jest tu mianownikiem: "
            f"jego kandydaci byli oceniani in-sample, a walk-forward już za nich "
            f"zapłacił, licząc wynik poza oknem wyboru.")


def znacznik_miary(karta: Karta, egzekucja: Egzekucja | None = None) -> dict[str, float]:
    """Metryki okna mowiace, PRZY JAKIM KOSZCIE policzono jego miary.

    `doplata_pb` - doplata z karty w punktach bazowych na runde, ta sama, ktora
    odejmuje `wynik_po_doplacie` i DSR.

    `spread_niezmierzony` - 1.0, gdy spread stoi w karcie jako NIEZMIERZONE.
    Zapisywany ZAWSZE (0.0 albo 1.0), bo brak wiersza znaczy w `learn.wf_metric`
    „przebieg sprzed znacznika", a nie „zmierzono". Bez niego `doplata_pb = 0`
    znaczylo dwie rozne rzeczy: „karta liczona, spread nieznany" i (dawniej)
    „brutto" - a dozor czytal oba jako brutto i kazdy przeskok miedzy pomiarem
    a jego luka opisywal jako powrot do starej definicji.
    """
    spread = next((s for s in karta.skladniki if s.nazwa == "spread"), None)
    niezmierzony = spread is not None and spread.zrodlo == NIEZMIERZONE
    znacznik = {"doplata_pb": karta.doplata_poza_symulacja * 1e4,
                "spread_niezmierzony": 1.0 if niezmierzony else 0.0}
    # PROWIZJA I MODEL WYPELNIEN (2026-10-07) - `dozor.klasa_miary`.
    #
    # `prowizja_pb` - stawka rundy (obie nogi) z karty, w pb. `params` jej NIE
    # niesie: ma `kind` i `limit`, a 05.10 stawka maker `spot_eu` zmienila sie
    # z 0,10% na 0,25% przy tych samych `kind` i `limit` (cba4833). Bez tej
    # metryki przebieg liczony runda 0,5% porownywal sie z liczonym runda 0,2%.
    # `egzekucja_pesymistyczna` / `opoznienie_min` - model wypelnien (H-5);
    # w `params` tylko poza wartoscia domyslna, tu ZAWSZE (0 = optymistyczny),
    # bo brak wiersza w `learn.wf_metric` znaczy "przebieg sprzed znacznika".
    prowizja = next((s for s in karta.skladniki if s.nazwa == "prowizja"), None)
    if prowizja is not None and prowizja.wartosc is not None:
        znacznik["prowizja_pb"] = prowizja.wartosc * 1e4
    if egzekucja is not None:
        znacznik["egzekucja_pesymistyczna"] = 1.0 if egzekucja.pesymistyczna else 0.0
        znacznik["opoznienie_min"] = float(egzekucja.opoznienie_min or 0)
        # H-10: przebicie kalibrowane do wypelnialnosci demo to inny model
        # wypelnien niz samo dotkniecie. Zapisywana jest wypelnialnosc
        # DOCELOWA, nie samo przebicie: przebicie wychodzi z kalibracji co
        # przebieg inne, a ta sama definicja miary ma dawac ta sama klase.
        znacznik["wypelnialnosc_docelowa"] = float(egzekucja.wypelnialnosc_docelowa or 0)
    return znacznik


_SQL_KROK_I_CENA = """
SELECT i.symbol, i.lot_size, max(c.close) AS cena_max
FROM ref.instrument i
LEFT JOIN market.candle c ON c.instrument_id = i.id AND c.timeframe = %s
                         AND c.event_time BETWEEN %s AND %s
WHERE i.id = ANY(%s)
GROUP BY i.symbol, i.lot_size
"""


_SQL_PROGI_OSTATNIEJ_WERSJI = """
SELECT v.params->'enter_threshold_per_regime' AS progi
FROM learn.model_version v JOIN learn.model m ON m.id = v.model_id
WHERE m.name = %s AND v.status IN ('candidate','active','retired')
  AND v.params ? 'enter_threshold_per_regime'
ORDER BY v.id DESC LIMIT 1
"""


def prog_dla_pylu(cur, nazwa_modelu: str) -> tuple[float, str]:
    """Prog wejscia, z ktorego liczymy notional pozycji dla pylu. (prog, skad).

    MEDIANA PROGOW PER REZIM z ostatniej ZAKONCZONEJ wersji tego modelu - ta
    sama liczba, ktora produkcja przyklada do wyniku. Brak wersji z progami
    (pierwszy przebieg toru, inna baza) -> `PROG_WEJSCIA_POCZATKOWY`, nazwany.

    Zmierzone 2026-10-03, crypto 4h: progi 0,208 / 0,244 / 0,284 / 0,350,
    mediana 0,264 -> udzial 0,528 -> notional 792 przy budzecie 1 500. Wobec
    zrealizowanego `sr_notional` (mediana 1 150 po 240 oknach) to nadal
    ZANIZONY notional, czyli zawyzony pyl: transakcja wchodzi przy wyniku
    POWYZEJ progu, a nie rownym mu.

    FORMALNY PRZECIEK W WALK-FORWARDZIE - NAZWANY, POMIJALNY. Progi pochodza
    z ostatniej wersji modelu, wytrenowanej na danych obejmujacych okna testowe
    BIEZACEGO przebiegu, i ustalaja notional pylu dla wszystkich jego okien.
    Skala: notional 750 (prog poczatkowy) -> 791 (mediana), czyli pyl w karcie
    1,416 -> 1,343 pb, roznica ~0,07 pb przy doplacie rzedu 6 pb (~1%).
    Kierunek: wyzszy prog daje WIEKSZY notional i MNIEJSZY pyl, wiec przeciek
    lagodzi koszt o te 0,07 pb - i nic poza tym; wagi, progi okien i wybor
    konfiguracji go nie widza.

    SAVEPOINT, NIE GOLY `except`. Trener chodzi w JEDNEJ transakcji psycopg
    (`autocommit` wylaczony), a blad zapytania w Postgresie psuje ja calą:
    do 2026-10-03 `except` tu polykal wyjatek, a NASTEPNE zapytanie trenera
    padalo z `InFailedSqlTransaction` - czyli "brak progow" zamienial sie
    w przerwany przebieg. `conn.transaction()` w otwartej transakcji to
    SAVEPOINT: przy bledzie cofa sie wylacznie to jedno zapytanie.
    """
    # `connection` ma kursor psycopg; atrapy kursora w testach - nie zawsze.
    conn = getattr(cur, "connection", None)
    try:
        if conn is not None and hasattr(conn, "transaction"):
            with conn.transaction():
                cur.execute(_SQL_PROGI_OSTATNIEJ_WERSJI, (nazwa_modelu,))
                w = cur.fetchone()
        else:
            cur.execute(_SQL_PROGI_OSTATNIEJ_WERSJI, (nazwa_modelu,))
            w = cur.fetchone()
    except Exception:  # noqa: BLE001 - brak odczytu = prog poczatkowy, nazwany
        w = None
    progi = (w or {}).get("progi") if isinstance(w, dict) else None
    wartosci = sorted(float(v) for v in (progi or {}).values()
                      if isinstance(v, (int, float)) and v > 0)
    if not wartosci:
        return PROG_WEJSCIA_POCZATKOWY, "próg początkowy (brak wersji z progami)"
    return statistics.median(wartosci), (f"mediana {len(wartosci)} progów "
                                         f"ostatniej wersji modelu")


def pyl_toru(cur, kind: str, instruments: list[dict], h: Horizon,
             od: datetime, do: datetime,
             zakladane_pozycje: int | None,
             nazwa_modelu: str | None = None,
             plik: dict | None = None,
             ) -> tuple[float | None, float | None, list[str]]:
    """Skladnik „pyl" karty dla strategii i dla kup-i-trzymaj, plus wydruk.

    Oddaje (pyl strategii, pyl odniesienia, linie opisu per instrument).

    Tylko dla rodzajow z prowizja w walucie bazowej (`koszty.prowizja_w_bazowej`);
    dla pozostalych None i skladnika w karcie po prostu nie ma.

    SKAD LICZBY - te same, z ktorych trener je bral w pomiarze z 2026-10-02:
      * krok: `ref.instrument.lot_size` - ta sama kolumna, z ktorej
        `gielda_exec` buduje `Filtry.krok_ilosci` przy skladaniu zlecen,
      * cena: NAJWYZSZE zamkniecie w oknie przebiegu (konserwatywnie: pyl
        rosnie z cena przy stalym notionalu),
      * notional strategii: budzet na pozycje z TEJ SAMEJ reguly co symulacja
        (`position_budget(DEFAULT_LIMITS, ...)`, kapital START_EQUITY) przez
        `wielkosc_pozycji(baza, prog)`, gdzie prog to mediana zmierzonych progow
        (`prog_dla_pylu`; do 2026-10-03 stale 0,25). Liczba instrumentow to
        cale uniwersum przebiegu (w oknie bywa ich mniej, czyli budzet
        wiekszy), a prog lezy ponizej wyniku, przy ktorym transakcja naprawde
        wchodzi - obie strony daja MNIEJSZY notional, czyli WIEKSZY pyl.
      * notional odniesienia: kup-i-trzymaj angazuje START_EQUITY / n na
        instrument, raz na runde.

    Brak kroku albo ceny dla jakiegokolwiek instrumentu -> None, czyli
    skladnik NIEZMIERZONE (patrz `koszty.pyl_rundy`).
    """
    from services.engine.koszty import koszty as cennik, pyl_per_instrument, pyl_rundy
    if not cennik(kind).prowizja_w_bazowej or not instruments:
        return None, None, []
    if plik is not None:
        # Z pliku panelu: krok z eksportu, cena maksymalna z tych samych swiec
        # zrodlowych i w tym samym przedziale (BETWEEN), co zapytanie nizej.
        wiersze = []
        for i in instruments:
            zam = [c["close"] for c in plik.get("swiece_1h", {}).get(i["symbol"], [])
                   if od <= c["t"] <= do]
            wiersze.append({"symbol": i["symbol"],
                            "lot_size": plik["kroki"].get(i["symbol"]),
                            "cena_max": max(zam) if zam else None})
    else:
        cur.execute(_SQL_KROK_I_CENA, (h.source_timeframe, od, do,
                                       [i["id"] for i in instruments]))
        wiersze = cur.fetchall() or []
    kroki = {r["symbol"]: (float(r["lot_size"]) if r.get("lot_size") is not None
                           else None) for r in wiersze}
    ceny = {r["symbol"]: (float(r["cena_max"]) if r.get("cena_max") is not None
                          else None) for r in wiersze}
    # Instrument bez wiersza tez jest dziura - nie wolno go pominac po cichu.
    for i in instruments:
        kroki.setdefault(i["symbol"], None)
    n = zakladane_pozycje or len(instruments)
    baza = position_budget(DEFAULT_LIMITS, n, Decimal(str(START_EQUITY)))
    if baza is None:
        return None, None, ["  pył: budżetu na pozycję nie da się policzyć — NIEZMIERZONY"]
    if plik is not None:
        prog, skad = plik["prog_pylu"]
    else:
        prog, skad = (prog_dla_pylu(cur, nazwa_modelu) if nazwa_modelu
                      else (PROG_WEJSCIA_POCZATKOWY, "próg początkowy"))
    notional = float(wielkosc_pozycji(baza, prog)[0])
    # PYL PER INSTRUMENT DO LOGU - karta niesie jedna liczbe (srednia), ale
    # rozrzut miedzy instrumentami jest rzedu 10x i to on mowi, gdzie pomiar
    # demo ma szanse model potwierdzic albo obalic.
    per = pyl_per_instrument(kroki, ceny, notional)
    if per is None:
        brak = sorted(s for s, k in kroki.items()
                      if k is None or ceny.get(s) is None)
        linie = [f"  pył: NIEZMIERZONY — brak kroku albo ceny dla: {', '.join(brak)}"]
    else:
        linie = [f"  pył per instrument (pb na rundę; notional {notional:,.0f} z "
                 f"{skad} {prog:.3f}, cena = maksimum okna):",
                 "    " + "  ".join(f"{s} {v * 1e4:.2f}" for s, v in sorted(per.items())),
                 f"    do karty: średnia {sum(per.values()) / len(per) * 1e4:.2f} pb "
                 f"(maksimum {max(per.values()) * 1e4:.2f} pb)"]
    return (pyl_rundy(kroki, ceny, notional),
            pyl_rundy(kroki, ceny, START_EQUITY / len(instruments)), linie)


def opis_doplaty(karta: Karta) -> str:
    """Czym jest dopłata, przy której liczą się miary - BEZ słowa „pełna".

    Do 2026-10-02 (wieczór) wydruk nazywał ją „pełną kartą". Przy `--limit`
    to nieprawda: niewypełnienie stoi w karcie BEZ wartości, więc do dopłaty
    nie wchodzi - dopłata to wtedy sam spread. To samo dotyczy spreadu z obcej
    księgi (wchodzi jako podłoga) i opłat equity. Zdanie ma wymieniać z nazwy,
    czego w dopłacie NIE ma, a nie ogłaszać kompletu.
    """
    braki = [f"{s.nazwa} (NIEZMIERZONE)" for s in karta.skladniki
             if s.wartosc is None and s.zrodlo == NIEZMIERZONE]
    podlogi = [f"{s.nazwa} tylko jako podłoga (obca księga)"
               for s in karta.skladniki if s.wartosc is not None and not s.wiarygodny]
    # SKLAD DOPLATY Z NAZWY. Od 2026-10-03 doplata to nie tylko spread (doszedl
    # pyl), wiec sama suma nie mowi juz, z czego sie sklada.
    sklad = [f"{s.nazwa} {s.wartosc * 1e4:.2f} pb" for s in karta.skladniki
             if s.wartosc is not None and not s.w_symulacji]
    opis = f"dopłata z karty {karta.doplata_poza_symulacja * 1e4:.2f} pb na rundę"
    if sklad:
        opis += f" (w tym: {', '.join(sklad)})"
    if not braki and not podlogi:
        return opis + " — karta pełna"
    return opis + " — karta BEZ: " + "; ".join(braki + podlogi)


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
                nieznane: tuple[str, ...],
                odchylenie_prob: float | None = None,
                konfiguracji_z_oos: int = 0,
                wejscia: list | None = None) -> tuple[bool, str]:
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
    po = zwroty_po_doplacie(zwroty, doplata)
    wynik = przewaga_dsr(po, n, odchylenie_prob, wejscia)
    # PROBA IDZIE DO BRAMKI, bo bez niej odleglosc od progu da sie podac tylko
    # w DSR - a DSR nie ma skali, w ktorej czlowiek umie ocenic "blisko".
    # `po` to te SAME liczby, z ktorych policzono werdykt (juz po doplacie);
    # podanie tu `zwroty` przed doplata liczylo by odpornosc innej proby.
    ok, opis = bramka_dsr(wynik, po)
    czesci = [opis, opis_kosztu_dsr(doplata, nieznane),
              opis_zrodla_n(n, trials_kumulatywnie),
              opis_zrodla_odchylenia(wynik, konfiguracji_z_oos)]
    return ok, " ".join(c for c in czesci if c)


def opis_odpornosci_werdyktu(wynik: dict, zwroty: list[float] | None = None) -> str:
    """Jak blisko progu stoi werdykt - w BLEDACH STANDARDOWYCH, nie w DSR.

    DLACZEGO TO MUSI STAC PRZY WERDYKCIE

    "Deflowany Sharpe 0,9524 przy progu 0,95" czyta sie jak fakt o strategii,
    a jest rozstrzygnieciem o ziarnie drobniejszym od szumu wlasnego pomiaru.
    Zmierzone 2026-09-27 na produkcji, tor 4h, piec przesuniec okna o jeden
    dzien (`--days` 1400..1396, `--sucho`, poza tym IDENTYCZNE nastawy):

        --days  status      DSR    Sharpe   okien dodatnich
          1400  validated  0,9524  +0,1016   27/38 (71%)
          1399  validated  0,9732  +0,1078   28/38 (74%)
          1398  learning   0,9342  +0,0975   31/40 (78%)
          1397  learning   0,9362  +0,0978   28/39 (72%)
          1396  validated  0,9790  +0,1095   29/39 (74%)

    Trzy razy `validated`, dwa razy `learning`. Poprzeczka selekcji jest we
    wszystkich pieciu IDENTYCZNA (+0,0611), wiec nie chodzi o liczbe prob - cala
    zmiana siedzi w Sharpe'ie, ktory chodzi w przedziale 0,0120, czyli sd
    0,0056. To 0,22 bledu standardowego samego Sharpe'a (1/sqrt(1500) = 0,0258).
    Zauwaz odwrotnosc przy 1398: NAJLEPSZY odsetek okien (78%) i mimo to brak
    promocji.

    MIARA JEST Z GORY ZANIZONA i tak trzeba ja czytac: 1/sqrt(T) to blad
    standardowy Sharpe'a przy zwrotach NIEZALEZNYCH, a te nie sa niezalezne
    (nakladajace sie pozycje, te same swiece). Prawdziwy blad jest wiekszy, czyli
    werdykt jest MNIEJ odporny, niz mowi ta liczba - nie bardziej.

    CZEGO TA FUNKCJA NIE ROBI: nie przesuwa progu i nie zmienia werdyktu.
    Bramka zostaje tam, gdzie byla; dokladana jest wylacznie informacja o tym,
    ile ten werdykt znaczy. Przy `zwroty=None` (czyli gdy wolajacy nie ma proby
    pod reka) podaje sama odleglosc w DSR i mowi, ze brakuje przeliczenia na
    blad standardowy - zamiast milczec.
    """
    dsr = wynik.get("dsr")
    if dsr is None:
        return ""
    # T NIEZALEZNYCH obserwacji, jesli policzone (W-2) - ten sam, ktory stoi
    # we wzorze DSR; liczba transakcji zanizalaby blad.
    T = wynik.get("n_efektywne") or wynik.get("n_obserwacji") or 0
    if T < 2:
        return ""
    blad = 1.0 / math.sqrt(T)
    if zwroty:
        cel = sharpe_dla_dsr(zwroty, wynik["n_prob"], PROG_DSR,
                             wynik.get("odchylenie_prob"), wynik.get("n_efektywne"))
        if cel is not None and wynik.get("sharpe") is not None:
            luka = wynik["sharpe"] - cel
            w_bledach = luka / blad
            zdanie = (f" ODPORNOŚĆ WERDYKTU: do progu {PROG_DSR:.2f} "
                      f"{'zostaje' if luka >= 0 else 'brakuje'} "
                      f"{abs(luka):.4f} Sharpe'a na transakcję, czyli "
                      f"{abs(w_bledach):.2f} błędu standardowego "
                      f"(1/√{T} = {blad:.4f}).")
            if abs(w_bledach) < 1.0:
                zdanie += (" WERDYKT NIE JEST ODPORNY: to mniej niż jeden błąd "
                           "standardowy, a zmierzone 2026-09-27 przesunięcie "
                           "okna o jeden dzień przewracało go w obie strony "
                           "(pięć przesunięć: DSR 0,9342–0,9790, trzy razy "
                           "`validated`, dwa razy `learning`). Błąd 1/√T jest "
                           "przy tym ZANIŻONY, bo pozycje nakładają się w czasie.")
            return zdanie
    return (f" ODPORNOŚĆ WERDYKTU: {abs(dsr - PROG_DSR):.4f} DSR od progu; "
            f"przeliczenia na błąd standardowy NIE MA, bo próba nie została tu "
            f"podana — patrz `opis_odpornosci_werdyktu`.")


def bramka_dsr(wynik: dict, zwroty: list[float] | None = None) -> tuple[bool, str]:
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
            f"(Sharpe {wynik['sharpe']:+.4f} z {n} transakcji"
            + (f" na {wynik['n_efektywne']} różnych świecach wejścia — T we wzorze"
               if wynik.get("n_efektywne") not in (None, n) else "")
            + ", "
            f"{n_prob} przetestowanych konfiguracji — N SELEKCJI tego "
            f"przebiegu, nie licznik kumulatywny; poprzeczka selekcji "
            f"{wynik['prog']:+.4f}, skośność {wynik['skosnosc']:+.3f}, "
            f"kurtoza {wynik['kurtoza']:.3f}).")
    odpornosc = opis_odpornosci_werdyktu(wynik, zwroty)
    if dsr >= PROG_DSR:
        return True, opis + odpornosc
    return False, opis + odpornosc + (
        " Po uwzględnieniu liczby prób wynik nie jest odróżnialny od "
        "najlepszego z tylu losowych — promocja wstrzymana.")


def rezim_dominujacy(rezimy: list[str]) -> str | None:
    """Najczestszy rezim w probce okna. REMIS ROZSTRZYGA KOLEJNOSC `REGIMES`.

    Do 2026-10-02 stalo tu `max(set(rezimy), key=rezimy.count)`. Przy remisie
    `max` oddaje pierwszy element w kolejnosci iteracji ZBIORU, a ta dla
    napisow zalezy od `PYTHONHASHSEED` - losowanego przy kazdym starcie
    interpretera, bo CronJob trenera go nie ustawia. Ta sama baza i ten sam
    kod dawaly wiec okno opisane raz jako `range`, raz jako `trend_up`.

    Etykieta NIE wplywa na wagi, progi ani bramke statusu - trafia do wydruku
    okna (`reżim=`, czytanego przez `scripts/porownaj_warianty.py` tylko do
    opisu), do `learn.wf_window.dominant_regime` (panel `/learning`)
    i do rozbic `opis_stron` / `opis_pokrycia`. Nieodtwarzalny byl wiec
    raport, nie model - ale raport, ktorego nie da sie powtorzyc, nie nadaje
    sie do porownania dwoch przebiegow.

    Porzadek jawny, a nie "pierwsze wystapienie": kolejnosc probki zalezy od
    kolejnosci instrumentow, czyli od uniwersum, a `REGIMES` jest stala kodu.
    Rezim spoza `REGIMES` (nie powinien sie zdarzyc) idzie na koniec,
    alfabetycznie - tez deterministycznie.
    """
    if not rezimy:
        return None
    licznik = Counter(rezimy)
    kolejnosc = {r: i for i, r in enumerate(REGIMES)}
    return min(licznik, key=lambda r: (-licznik[r], kolejnosc.get(r, len(REGIMES)), r))


# PRZEWAGA NAD B2 (audyt Fable F-3.4 / T8, 2026-10-03). Momentum przekrojowe
# z filtrem trendu (`baseline.b2_momentum`, parametry w renacode-infra: docs/projekty/trader/hipotezy.md H-B2)
# to odniesienie o porownywalnej prostocie: strategia, ktora go nie bije przy
# tej samej ekspozycji, nie uzasadnia swojej zlozonosci. Prog 60% okien to
# kryterium STOP/GO 1 z planu audytu, zapisane przed pomiarem (H-1).
PROG_BIJE_B2 = 0.60


def kryteria_blokujace(total: int, positive: int, median: float | None,
                       bije_odniesienie: bool, dsr_ok: bool,
                       udzial_b2: float | None) -> list[str]:
    """Ktore warunki `validated` NIE sa spelnione - nazwane, do `reason`.
    CZYSTA FUNKCJA; pusta lista = wszystkie spelnione."""
    if total < MIN_WINDOWS_FOR_VALIDATION:
        return [f"za mało okien ({total} < {MIN_WINDOWS_FOR_VALIDATION})"]
    pct = positive / total * 100
    blokuje = []
    if pct < 70:
        blokuje.append(f"okna dodatnie {pct:.0f}% < 70%")
    if median is None or median <= 0.05:
        blokuje.append("mediana Sharpe'a ≤ 0,05" if median is not None
                       else "brak mediany Sharpe'a")
    if not bije_odniesienie:
        blokuje.append("nie bije kup-i-trzymaj przy tej samej ekspozycji")
    if udzial_b2 is None:
        blokuje.append("brak porównania z B2")
    elif udzial_b2 < PROG_BIJE_B2:
        blokuje.append(f"bije B2 w {udzial_b2:.0%} okien < {PROG_BIJE_B2:.0%}")
    if not dsr_ok:
        blokuje.append("DSR poniżej progu")
    return blokuje


def status_walidacji(total: int, positive: int, median: float | None,
                     bije_odniesienie: bool, dsr_ok: bool,
                     udzial_b2: float | None = None) -> str:
    """Werdykt bramki jako CZYSTA FUNKCJA - zeby dal sie sprawdzic testem.

    Do 2026-09-26 ten warunek byl wyrazeniem wpisanym w srodek `train()`
    dlugiego na dziewiecset linii, wiec nie istnial test, ktory by go dotykal.
    Warunki sa KONIUNKCJA i kazdy z nich moze tylko ODEBRAC `validated`:

      * `total >= MIN_WINDOWS_FOR_VALIDATION` - dosc okien, zeby cokolwiek orzec,
      * `pct >= 70` - odsetek okien dodatnich,
      * `median > 0,05` - skala wyniku w typowym oknie,
      * `bije_odniesienie` - przewaga nad kup-i-trzymaj przy TEJ SAMEJ ekspozycji,
      * `dsr_ok` - deflowany Sharpe powyzej progu, patrz `bramka_dsr`,
      * `udzial_b2 >= PROG_BIJE_B2` - bije B2 (momentum z filtrem) przy tej
        samej ekspozycji w co najmniej 60% okien; brak porownania (None)
        blokuje, bo "nie zmierzono" nie jest przewaga (od 2026-10-03, T8).
    Ktory warunek blokuje - `kryteria_blokujace`.
    """
    if total < MIN_WINDOWS_FOR_VALIDATION:
        return "learning"
    pct = positive / total * 100
    if not kryteria_blokujace(total, positive, median, bije_odniesienie, dsr_ok,
                              udzial_b2):
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

    REWIZJE (`revised_at`, migracja 0070) - wartosc POPRAWIONA po chwili decyzji.

    Potok cech nadpisuje wartosc, gdy zmienila sie swieca, z ktorej ja policzono
    (`features.insert_values`), a `ingested_at` zostaje przy tym `znane_od` -
    wiec sam straznik wyzej przepuszcza kazda rewizje z definicji. Dlatego osobno:
    `revised_at > event_time + 2 swiece` znaczy, ze decyzja podjeta na tej
    swiecy widziala INNA liczbe niz ta, na ktorej trener sie teraz uczy.

    Takiej wartosci NIE odrzucamy. Poprawiona jest funkcja danych, ktore wtedy
    istnialy na gieldzie - zla byla nasza kopia. Odrzucenie wycinaloby z proby
    dokladnie te swiece, na ktorych dane byly bledne (selekcja po bledzie),
    a starej wartosci i tak nie mamy, bo nadpisanie jest w miejscu. Panel
    niesie wiec tylko LICZNIK (`rewizje` w wierszu): trener raportuje odsetek,
    czyli to, jak bardzo to, czego sie uczy, rozni sie od tego, co silnik widzial.

    LICZNIK W ROLACH, NIE W WIERSZACH (2026-10-03). Mianownik odsetka to
    `len(r["f"])`, czyli KLUCZE po `jsonb_object_agg` - jedna wartosc na role,
    bo tyle trafia do sygnalow. Do tej zmiany licznik byl `count(*)`, czyli
    WIERSZE: dwie aktywne definicje tej samej roli (np. cecha bez odcisku
    i wariant z odciskiem uniwersum) dawaly 2 rewizje przy 1 kluczu, a odsetek
    mogl przekroczyc 100%. `count(DISTINCT role)` liczy role, w ktorych
    ktorykolwiek wiersz zostal zrewidowany. Przy zdublowanej roli to jest
    GORNE oszacowanie: `jsonb_object_agg` bez ORDER BY bierze jedna z wartosci
    nieokreslonym porzadkiem, wiec nie wiadomo, czy uczymy sie na tej
    zrewidowanej - liczymy ja, bo kierunek ostrozny to "rozjazd wiekszy".
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
            SELECT fv.event_time, fd.params->>'role' AS role, fv.value,
                   fv.revised_at > fv.event_time + 2 * %(bar)s::interval
                       AS po_decyzji
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
               count(DISTINCT f.role) FILTER (WHERE f.po_decyzji) AS rewizje,
               p.close, p.high, p.low
        FROM f JOIN p ON p.t = f.event_time
        GROUP BY f.event_time, p.close, p.high, p.low ORDER BY f.event_time
        """,
        {"iid": instrument_id, "since": since, "until": until, "tf": h.source_timeframe,
         "bar": f"{h.bar_minutes} minutes", "bar_name": h.bar, "fp": fp},
    )
    return [{"t": r["t"], "f": {k: float(v) for k, v in r["feats"].items()},
             "price": float(r["close"]), "high": float(r["high"]),
             "low": float(r["low"]), "rewizje": int(r["rewizje"])}
            for r in cur.fetchall()]


def rewizje_po_decyzji(panels: dict[str, list[dict]]) -> tuple[int, int]:
    """(wartosci zrewidowane po chwili decyzji, wszystkie wartosci) w panelach.

    OBIE LICZBY W TEJ SAMEJ JEDNOSTCE: role w wierszu panelu, czyli wartosci,
    na ktorych trener sie uczy (patrz "LICZNIK W ROLACH" w `load_panel`).

    Rozjazd "silnik widzial X, trener uczy sie na Y" - patrz `load_panel`.
    `.get`, bo panel spoza `load_panel` (atrapy, narzedzia) moze nie miec pola,
    a brak informacji o rewizjach nie jest rewizja.
    """
    zrewidowane = sum(r.get("rewizje", 0) for p in panels.values() for r in p)
    wszystkie = sum(len(r["f"]) for p in panels.values() for r in p)
    return zrewidowane, wszystkie


def load_swiece_wykonania(cur, instrument_id: int, since: datetime,
                          until: datetime) -> list[dict]:
    """Swiece 1h instrumentu - material modelu wypelnien z opoznieniem.

    Okno o dobe SZERSZE z obu stron niz panel: wysylka po ostatniej swiecy
    decyzyjnej i godzina po niej wypadaja juz za `until`, a cena wysylki
    potrzebuje zamkniecia godziny poprzedniej.
    """
    cur.execute(
        "SELECT event_time AS t, open, high, low, close FROM market.candle"
        " WHERE instrument_id = %s AND timeframe = '1h'"
        "   AND event_time BETWEEN %s AND %s ORDER BY event_time",
        (instrument_id, since - timedelta(days=1), until + timedelta(days=1)))
    return [{"t": r["t"], "open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"])}
            for r in cur.fetchall()]


def n_konfiguracji_z_listy(konfiguracje: list[str], params: dict) -> int:
    """N selekcji w przebiegu z PLIKU: rozne `params` zakonczonych wersji
    (zapisane przy eksporcie) plus ten przebieg - to samo, co `n_konfiguracji`
    liczy w bazie z `mv_id` biezacego przebiegu."""
    return len(set(konfiguracje) | {panel_plik.kanoniczne_params(params)})


def wypelnialnosc_symulacji(wypelnione: int, niewypelnione: int) -> float | None:
    """Ulamek zlecen z limitem, ktore w symulacji weszly - ta sama definicja co
    `poslizg.wypelnialnosc` na produkcji (wypelnione / rozstrzygniete)."""
    razem = wypelnione + niewypelnione
    return wypelnione / razem if razem else None


# WYPELNIALNOSC WEJSC NA DEMO, z ktora H-5 porownuje symulacje: 42 z 64
# rozstrzygnietych wejsc E-1 (2026-10-04..07, `poslizg.py --strategia
# momentum_filtr_trendu`). Stala do wydruku, nie do rachunku - kalibracja
# bierze liczbe z `--wypelnialnosc-docelowa` (chart), zeby jej zmiana byla
# widoczna jako zmiana konfiguracji, a nie edycja kodu.
WYPELNIALNOSC_DEMO_E1 = (42, 64)


def kalibracja_wypelnien(z: "Przebieg", docelowa: float) -> tuple[float, list[str]]:
    """Przebicie (pb) z przebiegu kalibracyjnego i wiersze do logu.

    `z` - liczniki walk-forwardu policzonego BEZ przebicia (samo dotkniecie)
    na tej samej siatce, co przebieg wlasciwy. Patrzy wylacznie na to, czy
    wejscia weszly (`glebokosci_wejsc_oos`), nie na zwroty (H-10).
    """
    prob = z.wypelnione_wejsc_oos + z.niewypelnione_wejsc_oos
    przed = wypelnialnosc_symulacji(z.wypelnione_wejsc_oos, z.niewypelnione_wejsc_oos)
    przebicie = kalibruj_przebicie(z.glebokosci_wejsc_oos, prob, docelowa)
    po = (sum(1 for g in z.glebokosci_wejsc_oos if g >= przebicie) / prob
          if prob else None)
    linie = [
        "  KALIBRACJA WYPEŁNIEŃ (H-10): wypełnialność wejść przy samym dotknięciu "
        + (f"{przed:.1%}" if przed is not None else "—")
        + f" ({prob} wejść), docelowa z demo {docelowa:.1%}",
        f"    przebicie {przebicie:.2f} pb (kupno: `low` co najmniej tyle POD limitem)"
        + (f" → {po:.1%} na tych samych wejściach" if po is not None else "")
        + ("; symulacja już nie wypełnia częściej niż demo, model bez zmian"
           if przebicie == 0 else ""),
    ]
    return przebicie, linie


def opis_wypelnialnosci_wejsc(z: "Przebieg", egzekucja: Egzekucja,
                              kind: str = "spot_eu") -> list[str]:
    """Wypelnialnosc WEJSC w symulacji OOS obok demo - liczba, ktora czyta H-5.

    Do 2026-10-11 log zestawial z demo („ok. 65%") wypelnialnosc wejsc
    i wyjsc RAZEM. Wyjscia ponawiaja sie co decyzje, wiec ta suma nie jest
    wielkoscia, ktora zmierzono na demo (wejscia, 42/64). Pomiar demo jest
    tylko dla crypto - equity dostaje sama liczbe symulacji.
    """
    wyp, niewyp = z.wypelnione_wejsc_oos, z.niewypelnione_wejsc_oos
    w = wypelnialnosc_symulacji(wyp, niewyp)
    if kind not in ("spot", "spot_eu", "perp"):
        return [("Wypełnialność WEJŚĆ w symulacji OOS: "
                 + (f"{w:.1%}" if w is not None else "—")
                 + f" ({wyp} weszło, {niewyp} nie) — pomiaru na koncie dla tego "
                 f"rynku nie ma")]
    demo_wyp, demo_prob = WYPELNIALNOSC_DEMO_E1
    demo = demo_wyp / demo_prob
    linie = [("Wypełnialność WEJŚĆ w symulacji OOS: "
              + (f"{w:.1%}" if w is not None else "—")
              + f" ({wyp} weszło, {niewyp} nie) wobec demo {demo:.1%} "
              f"({demo_wyp}/{demo_prob}, E-1)"
              + (f" — różnica {abs(w - demo) * 100:.1f} pp, kryterium H-5: ≤ 10 pp"
                 if w is not None else ""))]
    if egzekucja.wypelnialnosc_docelowa is not None:
        linie.append(f"  przebicie {egzekucja.przebicie_bps:.2f} pb skalibrowane do "
                     f"{egzekucja.wypelnialnosc_docelowa:.1%} — zgodność z demo jest tu "
                     f"WŁASNOŚCIĄ KALIBRACJI, nie testem; test to porównanie z "
                     f"wejściami demo zebranymi PO kalibracji (H-10)")
    return linie


def atr_threshold(panel: list[dict]) -> float | None:
    vals = [r["f"]["atr_pct"] for r in panel if "atr_pct" in r["f"]]
    if len(vals) < 50:
        return None
    vals.sort()
    return vals[int(len(vals) * 0.8)]


# ------------------------------------------------------------------ symulacja
def proby_wynikow(panels: dict[str, list[dict]],
                  weights: dict[str, dict[str, float]],
                  atr_thresholds: dict[str, float | None], h: Horizon,
                  cache: dict[str, list[tuple]] | None = None,
                  tylko_dluga: bool = False,
                  ) -> dict[str, list[float]]:
    """`abs(wynik)` z okna, POGRUPOWANE PO REZIMIE, ze wszystkich instrumentow.

    `tylko_dluga=True` (rynek bez shortow, `strona_uczenia`) - zamiast
    `abs(wynik)` WYLACZNIE wyniki dodatnie, czyli populacja, z ktorej moze
    powstac wejscie long. Percentyl 0,88 znaczy wtedy "12% najsilniejszych
    sytuacji LONG", a nie "12% najsilniejszych sytuacji, z ktorych polowe
    i tak odrzucimy". Przy rozkladzie symetrycznym czestotliwosc wejsc jest
    zblizona (12% z polowy proby wobec ok. 6% z calej), roznica pojawia sie
    tam, gdzie rozklad jest skosny - czyli tam, gdzie wada byla.

    Liczone wylacznie na oknie TRENINGOWYM. Policzone na testowym byloby
    zagladaniem w przyszlosc - prog znalby rozklad, ktorego w tamtym momencie
    nie dalo sie znac.

    DLACZEGO POPULACJA JEST WSPOLNA DLA INSTRUMENTOW, A OSOBNA DLA REZIMOW.
    Skale wyniku rozsuwaja WAGI, a wagi sa per rezim i identyczne dla wszystkich
    instrumentow (`weights.get(regime)`) - to rezim jest wymiarem, wzdluz
    ktorego percentyl przestaje znaczyc to samo. Roznice miedzy instrumentami
    w obrebie rezimu ida dopiero przez rozklady cech i sa o rzad mniejsze.
    Sa jeszcze dwa powody, oba o pomiarze:
      * probek na prog jest 5-30 razy wiecej, a percentyl 0,88 z 50 obserwacji
        stoi na szostej od gory - czyli sam jest szumem;
      * do 2026-09-27 trener symulowal okno progami PER INSTRUMENT, a produkcja
        dostawala MEDIANE z nich (`prod_enter`). Trening mierzyl wiec strategie,
        ktorej produkcja nie wykonuje. Jedna populacja na rezim znosi ten
        rozjazd: symulacja i produkcja porownuja z ta sama liczba.
    """
    proby: dict[str, list[float]] = {}
    for sym, panel in panels.items():
        wpisy = cache.get(sym) if cache is not None else None
        for i, row in enumerate(panel):
            if wpisy is not None:
                regime, sygnaly = wpisy[i]
                sc, conf, _ = combine(sygnaly, regime, weights.get(regime),
                                      with_contributions=False)
            else:
                regime, _ = classify_regime(row["f"], atr_thresholds.get(sym))
                sc, conf, _ = evaluate(row["f"], regime, weights.get(regime), h)
            # Prog pewnosci odsiewa TAK SAMO jak w produkcji - inaczej percentyl
            # liczylby sie na populacji szerszej niz ta, ktora go potem mija.
            if conf < MIN_CONF:
                continue
            if tylko_dluga:
                # Zero tez odpada: przy score == 0 nie ma wejscia w zadna strone.
                if sc > 0:
                    proby.setdefault(regime, []).append(sc)
            else:
                proby.setdefault(regime, []).append(abs(sc))
    return proby


def bramki_okna(panels: dict[str, list[dict]],
                weights: dict[str, dict[str, float]],
                atr_thresholds: dict[str, float | None], h: Horizon,
                cache: dict[str, list[tuple]] | None = None,
                per_rezim: bool = True, tylko_dluga: bool = False) -> Bramki:
    """Progi wejscia i wyjscia dla CALEGO okna, po jednym na rezim.

    `per_rezim=False` liczy jeden prog ze zlaczonej proby, czyli arytmetyke
    sprzed 2026-09-27 - do pomiaru roznicy na tych samych oknach (`--progi`).
    Uzasadnienie zmiany i polityka malej proby: `services/engine/bramki.py`.
    `tylko_dluga` - prog z rozkladu score > 0 (`proby_wynikow`, H-6).
    """
    return bramki_z_prob(proby_wynikow(panels, weights, atr_thresholds, h, cache,
                                       tylko_dluga=tylko_dluga),
                         ENTER_PCTL, per_rezim=per_rezim)


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
               martwe: set[str], horyzont_swiec: int = 3,
               tylko_dluga: bool = False) -> dict[str, dict[str, float]]:
    """Wagi WYLICZONE ze zdolnosci predykcyjnej sygnalow, nie wylosowane.

    `tylko_dluga` (rynek bez shortow, `strona_uczenia`, H-6) - IC liczone
    wylacznie na obserwacjach, na ktorych decyzja long jest mozliwa. Wariant
    i uzasadnienie wyboru - komentarz w ciele funkcji.

    `horyzont_swiec` - zwrot, z ktorym korelujemy wynik sygnalu. Trener podaje
    `h.ic_horizon_bars` (12 godzin na kazdym horyzoncie, audyt Fable F-2.6);
    domyslne 3 to wartosc toru 4h, zostawiona dla wywolan diagnostycznych.

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
    wagi = _wagi_z_ic(panels, cache, martwe, horyzont_swiec)
    if not tylko_dluga:
        return wagi

    # KTORE OBSERWACJE "DOPUSZCZAJA DECYZJE LONG" - WARIANT NAJPROSTSZY, KTORY
    # NIE JEST BLEDNY (H-6).
    #
    # Na spocie long mozna otworzyc na kazdej swiecy, wiec "wszystkie
    # obserwacje" niczego nie zmienia - roznica jest w tym, KTORA czesc
    # rozkladu wyniku decyduje. Long powstaje tylko z gornego ogona wyniku
    # (score > prog > 0), a korelacja rangowa po calej probie mierzy w polowie
    # dolny ogon: jak dobrze sygnal przewiduje spadki, ktorych nie zagramy.
    # Odrzucone warianty:
    #   * per sygnal "wynik sygnalu > 0" - zly dla sygnalow o ujemnym IC: po
    #     odwroceniu wagi to ich UJEMNE wyniki pchaja w strone long, wiec
    #     zmierzylibysmy niewlasciwa polowe;
    #   * punkt staly (wagi -> podzbior -> wagi -> ...) - nie musi zbiegac,
    #     a liczba iteracji bylaby kolejnym stopniem swobody.
    # Wybrany: JEDEN krok. Wagi dwustronne (dotychczasowe) wyznaczaja
    # obserwacje, na ktorych wynik laczny jest dodatni i pewnosc przechodzi
    # MIN_CONF - czyli te, ktore maja szanse dac wejscie long - i na nich IC
    # liczy sie jeszcze raz. Deterministyczne, z okna TRENINGOWEGO, bez
    # nowego parametru. Znane przyblizenie: po drugim kroku podzbior "score > 0"
    # wyznaczony nowymi wagami nie jest identyczny z tym, na ktorym je
    # policzono; to mierzy ocena OOS, a nie zalozenie.
    def long_mozliwy(regime: str, sygnaly: dict) -> bool:
        sc, conf, _ = combine(sygnaly, regime, wagi.get(regime),
                              with_contributions=False)
        return sc > 0 and conf >= MIN_CONF

    return _wagi_z_ic(panels, cache, martwe, horyzont_swiec, filtr=long_mozliwy)


def _wagi_z_ic(panels: dict[str, list[dict]], cache: dict[str, list[tuple]],
               martwe: set[str], horyzont_swiec: int,
               filtr=None) -> dict[str, dict[str, float]]:
    """Rdzen `ic_weights`: IC per rezim i sygnal na obserwacjach, ktore
    przechodza `filtr(rezim, sygnaly)` (None = wszystkie)."""
    zebrane, ogolne = _pary_ic(panels, cache, martwe, horyzont_swiec, filtr)

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


def _pary_ic(panels: dict[str, list[dict]], cache: dict[str, list[tuple]],
             martwe: set[str], horyzont_swiec: int, filtr=None, filtr_swiecy=None
             ) -> tuple[dict[str, dict[str, tuple[list, list]]],
                        dict[str, tuple[list, list]]]:
    """(wyniki i zwroty per rezim i sygnal, to samo zlaczone po rezimach) -
    jedna definicja obserwacji IC dla wag (`_wagi_z_ic`) i dla diagnostyki
    (`ic_sygnalow`). Etykieta: zwrot od ceny swiecy i+1 do i+1+H.
    `filtr_swiecy(wiersz)` - tylko swiece, na ktorych symulacja handluje
    (diagnostyka na rynku z sesja); wagi go nie podaja."""
    # {rezim: {sygnal: ([wyniki], [zwroty])}}
    zebrane: dict[str, dict[str, tuple[list, list]]] = {}
    for sym, rows in panels.items():
        ceny = [r["price"] for r in rows]
        for i, (regime, sygnaly) in enumerate(cache[sym]):
            j = i + 1 + horyzont_swiec
            if j >= len(ceny) or ceny[i + 1] == 0:
                continue
            if filtr is not None and not filtr(regime, sygnaly):
                continue
            if filtr_swiecy is not None and not filtr_swiecy(rows[i]):
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
    return zebrane, ogolne


# IC PER SYGNAL W LOGU (2026-10-18, A-3). Trener liczyl IC sygnalow na kazdym
# oknie treningowym (`_wagi_z_ic`) i zamienial je od razu w wagi - w logu nie
# zostawala ani jedna liczba, wiec "ktory sygnal cokolwiek przewiduje" nie
# mialo odpowiedzi bez osobnego skryptu. Tabela nizej jest CZYSTA
# DIAGNOSTYKA: liczona z tych samych `cache_te`, ktore ocenia symulacja, po
# wyborze wag, i nie trafia do zadnej decyzji ani zapisu.
#
# NA OKNIE TESTOWYM, OSOBNO W KAZDYM OKNIE. Wynik sygnalu nie zalezy od wag,
# wiec IC na oknie testowym jest pomiarem poza proba w scislym sensie - nic
# go nie dopasowywalo. Srednia po oknach i odsetek okien z IC > 0 zamiast
# jednej korelacji po calym panelu: etykiety sasiednich swiec nachodza na
# siebie o H-1 swiec, wiec "n obserwacji" zawyzalby istotnosc, a liczba
# okien jest uczciwym mianownikiem stabilnosci znaku.
RAZEM_IC = "razem"


def ic_sygnalow(panels: dict[str, list[dict]], cache: dict[str, list[tuple]],
                horyzont_swiec: int, filtr_swiecy=None) -> dict[str, dict[str, float]]:
    """{grupa: {sygnal: IC}} jednego okna; grupa to rezim albo `RAZEM_IC`.
    Sygnal bez IC (za mala proba, stala kolumna) nie ma klucza.

    `filtr_swiecy` - na rynku z sesja tylko swiece, z ktorych symulacja moze
    WEJSC (`swieca_wejscia`): IC sygnalu na swiecy posesyjnej opisuje sytuacje,
    w ktorej nikt nie handluje."""
    zebrane, ogolne = _pary_ic(panels, cache, set(), horyzont_swiec,
                               filtr_swiecy=filtr_swiecy)
    out: dict[str, dict[str, float]] = {}
    for grupa, per_sig in [(RAZEM_IC, ogolne), *zebrane.items()]:
        for name, (xs, ys) in per_sig.items():
            ic = spearman(xs, ys)
            if ic is not None:
                out.setdefault(grupa, {})[name] = ic
    return out


def swieca_wejscia(kt: "Kontekst"):
    """Filtr swiec dla `ic_sygnalow`: None na rynku ciaglym, inaczej te, na
    ktorych `portfolio.okno_wykonania` dopuszcza wejscie."""
    kalendarz = sesja_historyczna(kt.kind)
    if kalendarz is None:
        return None
    return lambda r: okno_wykonania(r["t"], kt.h, kt.egzekucja, kalendarz,
                                    wejscie=True) is not None


def opis_ic_sygnalow(ic_okien: list[dict[str, dict[str, float]]],
                     h: Horizon) -> list[str]:
    """Tabela IC per sygnal: kolumna `razem` i kolumna na kazdy rezim."""
    if not ic_okien:
        return []
    grupy = [RAZEM_IC] + [r for r in REGIMES
                          if any(r in okno for okno in ic_okien)]

    def komorka(wartosci: list[float]) -> str:
        if not wartosci:
            return "—"
        dodatnie = sum(1 for v in wartosci if v > 0)
        return f"{statistics.fmean(wartosci):+.3f} {dodatnie}/{len(wartosci)}"

    linie = [
        f"IC SYGNAŁÓW NA OKNACH TESTOWYCH (diagnostyka — nie wpływa na wagi, "
        f"progi ani wybór modelu): korelacja rangowa wyniku sygnału ze zwrotem "
        f"{h.ic_horizon_bars} świec ({h.ic_horizon_bars * h.bar_minutes / 60:g} h), "
        f"osobno w każdym oknie; komórka = średnia IC, okna z IC > 0 / okna z IC",
        "  " + f"{'sygnał':<20}" + "".join(f"{g:>17}" for g in grupy),
    ]
    for name in SIGNALS:
        kom = [komorka([okno[g][name] for okno in ic_okien
                        if name in okno.get(g, {})]) for g in grupy]
        linie.append("  " + f"{name:<20}" + "".join(f"{k:>17}" for k in kom))
    return linie


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
# razu, bez pytania. Znaczylo to, ze liczby z renacode-infra: docs/projekty/trader/05-ochrona-kapitalu.md
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


# BRAMKA STRUKTURY (audyt Fable F-3.1 / T8, 2026-10-03).
#
# 28.09 format parametrow zmienil sie deployem kodu (progi per rezim: `range`
# 0,28 -> 0,35, `high_vol` 0,28 -> 0,21) i w demo wszedl sam, bo kazda wersja
# nie-`degraded` byla `active`. Od tej chwili produkcja handlowala inna
# strategia (`high_vol` 36,5% -> 10,8% sygnalow ponad progiem), bez zadnej
# bramki. Teraz wersja, ktorej `params` maja inny ZESTAW KLUCZY, inna
# konfiguracje albo progi per rezim rozne o >= 10% od aktywnej, zostaje
# `candidate`; dozor wysyla `learning.zmiana_struktury` (migracja 0090).
#
# Klucze WYNIKOWE (wagi, progi, ich liczby okien) zmieniaja wartosc co
# przebieg i porownuje sie je osobno: wagi wcale (sa stabilne i nie zmieniaja
# struktury), progi - wzglednie, z tolerancja.
KLUCZE_WYNIKOWE = frozenset({"weights", "enter_threshold", "exit_threshold",
                             "enter_threshold_per_regime", "enter_threshold_windows",
                             "min_confidence",
                             # slad pary produkcyjnej (2026-10-07)
                             "enter_threshold_proby",
                             "enter_threshold_per_regime_mediana_okien",
                             "weights_mediana_okien"})
TOLERANCJA_PROGOW = 0.10


def zmiana_struktury(aktywne: dict | None, nowe: dict) -> str | None:
    """Czym nowe `params` roznia sie STRUKTURALNIE od aktywnej wersji; None,
    gdy niczym (albo nie ma aktywnej wersji - pierwsza wersja nie ma z czym
    sie porownac). CZYSTA FUNKCJA."""
    if aktywne is None:
        return None
    czesci = []
    dodane = sorted(set(nowe) - set(aktywne))
    usuniete = sorted(set(aktywne) - set(nowe))
    if dodane or usuniete:
        czesci.append("klucze params: " + ", ".join(
            [f"+{k}" for k in dodane] + [f"−{k}" for k in usuniete]))
    for k in sorted((set(nowe) & set(aktywne)) - KLUCZE_WYNIKOWE):
        if nowe[k] != aktywne[k]:
            czesci.append(f"{k}: {aktywne[k]!r} → {nowe[k]!r}")
    stare_p = aktywne.get("enter_threshold_per_regime") or {}
    nowe_p = nowe.get("enter_threshold_per_regime") or {}
    if set(stare_p) != set(nowe_p):
        czesci.append(f"reżimy z progiem wejścia: {sorted(stare_p)} → {sorted(nowe_p)}")
    for r in sorted(set(stare_p) & set(nowe_p)):
        a, b = float(stare_p[r]), float(nowe_p[r])
        if a and abs(b - a) / abs(a) >= TOLERANCJA_PROGOW:
            czesci.append(f"próg {r} {a:.4f} → {b:.4f} ({(b - a) / a:+.0%})")
    return "; ".join(czesci) or None


def status_nowej_wersji(status: str, tryb: str, zgoda: bool = False,
                        zmiana: str | None = None) -> tuple[str, str]:
    """Czy nowa wersja wchodzi do produkcji sama: ('active'|'candidate', powod).

    Czysta funkcja, tak jak `dozor.zmiany`: caly werdykt bramki da sie
    sprawdzic bez bazy i bez przebiegu treningu.

    BEZ ZGODY NIE WCHODZI NIC, W ZADNYM TRYBIE (2026-10-05, poprawka F).
    Do tej daty demo promowalo samo kazda wersje nie-`degraded`, z argumentem,
    ze zla wersja kosztuje tam jeden dzien papierowego wyniku. Diagnoza z 05.10
    pokazala, ze ten argument nie trzyma: wynik treningu nie ma pomiaru "czy
    nowa wersja jest lepsza od poprzedniej" (H-4 w renacode-infra: docs/projekty/trader/hipotezy.md dopiero go
    zaklada), a status `learning` z walk-forwardu na 3,8 roku przy 0,5% nowych
    danych nie mowi o tym nic. Automatyczna promocja zamieniala wiec co tydzien
    model produkcyjny na niezmierzony - i robila to o 4:40 nad ranem, tak samo
    jak w live.

    Zgoda jest JAWNA I JEDNORAZOWA (flaga przebiegu), a nie ustawieniem
    w bazie: ustawienie raz wlaczone zostaje wlaczone na zawsze i po pol roku
    nikt nie pamieta, ze bramka jest podniesiona.
    """
    if status == "degraded":
        # Zachowanie sprzed bramki, zostawione bez zmian: wynik bez przewagi
        # nie promuje sie w zadnym trybie.
        return "candidate", "wynik degraded — wersja zostaje kandydatem"
    # ZMIANA STRUKTURY - w KAZDYM trybie, takze w demo (`zmiana_struktury`).
    # Zgoda czlowieka (`--zatwierdz-promocje`, po wpisie w renacode-infra: docs/projekty/trader/hipotezy.md)
    # przepuszcza ja tak samo jak w live.
    if zmiana and not zgoda:
        return "candidate", (f"zmiana struktury modelu ({zmiana}) — wymaga wpisu "
                             f"w renacode-infra: docs/projekty/trader/hipotezy.md i `--zatwierdz-promocje`")
    if not zgoda:
        return "candidate", (f"tryb {tryb} — nowa wersja czeka na zatwierdzenie "
                             f"człowieka (`--zatwierdz-promocje`)")
    return "active", f"tryb {tryb}, promocja zatwierdzona jawnie"


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
#
# `train()` PODZIELONE NA CZYSTE FUNKCJE (audyt Fable F-10.1 / T2, 2026-10-03).
#
# Do tego dnia `train()` mialo 1 700 linii: siatka okien, dobor wag, ocena OOS,
# werdykt i zapisy do bazy w jednym ciele, z kilkudziesiecioma licznikami
# wspoldzielonymi przez cala petle. Werdykt dalo sie sprawdzic testem tylko
# tam, gdzie ktos go wczesniej wyjal (`status_walidacji`, `werdykt_dsr`);
# reszta byla testowalna wylacznie przez caly przebieg.
#
# Teraz `train()` jest orkiestratorem, a kazdy etap osobna funkcja:
#   okna_wf      - siatka okien walk-forward (granice + aktywne instrumenty),
#   kandydat_wag - wybor wag na oknie treningowym,
#   ocen_okno    - ocena OOS jednego okna (bez zapisow i bez wydruku),
#   werdykt      - status, powod i DSR z miar wszystkich okien,
#   zapisz_wynik - parametry produkcji, promocja i stan nauki w bazie.
# Rownowaznosc z kodem sprzed podzialu: `tests/test_trener_rownowaznosc.py`
# (caly wydruk i PODSUMOWANIE_JSON na zrzucie referencyjnym).


def _utrwal(conn, sucho: bool) -> None:
    # PRZEBIEG SUCHY NIE ZOSTAWIA W BAZIE NICZEGO (audyt Fable F-2.4).
    #
    # Do 2026-10-03 `--sucho` zakladalo wiersz w `learn.model_version`
    # i `learn.training_run`, zatwierdzalo go od razu i na koncu oznaczalo
    # przebieg jako "tryb suchy" - wersja zostawala w `training` na zawsze,
    # razem z `drift_metric` i `calibration_bin` przypietymi do niej. Zmierzone
    # na produkcji: 106 ze 169 wersji toru 4h w `training`.
    #
    # Teraz przebieg suchy liczy w JEDNEJ transakcji, wycofanej na koncu albo
    # przy przerwaniu. Wlasna wersja jest potrzebna W TRAKCIE (`n_konfiguracji`
    # liczy ja do N) i istnieje - tylko dla tego polaczenia. Cena: przez czas
    # przebiegu suchego rownolegly trening tego samego modelu czeka na wolny
    # numer wersji (UNIQUE (model_id, version)) - przebiegi suche sa reczne.
    if conn is None:
        return
    if sucho:
        conn.rollback()
    else:
        conn.commit()


def _uniwersum(cur, plik: dict | None, engine: str, h: Horizon, days: int,
               tylko: set[str] | None) -> list[dict]:
    """Uniwersum przebiegu: `tradeable` z bazy albo z pliku, zawezone
    `--instrumenty`; puste uniwersum i equity bez korekty podzialow przerywaja."""
    # `tradeable` decyduje o uniwersum produkcyjnym - ta sama flaga, ktora czyta
    # petla decyzyjna (0020). Flaga --instrumenty sluzy WYLACZNIE eksperymentom
    # i zaweza to, co juz z bazy wyszlo; nie potrafi dodac niczego z powrotem.
    if plik is not None:
        if plik["engine"] != engine or plik["bar"] != h.bar:
            raise SystemExit(f"plik panelu jest dla {plik['engine']}/{plik['bar']}, "
                             f"przebieg dla {engine}/{h.bar}")
        if days > plik["dni"]:
            raise SystemExit(f"--days {days} wychodzi poza plik panelu "
                             f"({plik['dni']} dni do {plik['do']:%Y-%m-%d %H:%M})")
        instruments = list(plik["instrumenty"])
    else:
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
        # Z pliku: wylacznie to, co wyeksportowano (uniwersum `tradeable`).
        if plik is None:
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
    if engine == "equity" and plik is not None:
        raise SystemExit("panel z pliku dla equity nieobsługiwany — bramka korekty "
                         "podziałów czyta znacznik z bazy")
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
    return instruments


@dataclass(frozen=True)
class Koszty:
    """Karta kosztu przebiegu i dwie doplaty z niej (strategia, odniesienie)."""
    karta: Karta
    doplata: float
    doplata_odniesienia: float
    pyl: float | None
    spread_bps: float | None
    spread_nasza_ksiega: bool
    egzekucja: Egzekucja


def _koszty(cur, plik: dict | None, engine: str, kind: str, h: Horizon,
            instruments: list[dict], now: datetime, days: int, egzekucja: Egzekucja,
            spread_bps: float | None, spread_nasza_ksiega: bool,
            zakladane_pozycje: int | None) -> Koszty:
    # --- SPREAD: POMIAR Z BAZY, NIE STALA Z CHARTU ---------------------------
    #
    # Liczony TUTAJ, a nie w `main()`, i to jest cala rzecz: dopiero w tym
    # miejscu uniwersum jest ROZSTRZYGNIETE (`active AND tradeable`, po
    # ewentualnym zawezeniu `--instrumenty`), a okno znane z `days`. Agregat
    # policzony na innym zbiorze albo innym okresie opisywalby inny rynek niz
    # ten, ktory symulujemy - i nie bylo by po czym tego poznac.
    #
    # `--spread-bps` podany z linii polecen WYGRYWA i zostaje awaryjnym
    # nadpisaniem (wariant, porownanie, przebieg bez arkusza w bazie). Wtedy
    # o ksiedze mowi flaga `--spread-obca-ksiega`, bo tylko czlowiek wie, skad
    # wzial liczbe. Gdy pomiar idzie z bazy, flage USTAWIA POMIAR: to on wie,
    # czy okno siega przed `spread.DATA_WLASNEJ_KSIEGI` i czy wszystkie
    # instrumenty byly `tradeable`.
    pomiar = None
    if spread_bps is None:
        pomiar = (zmierz_spread(cur, engine, h.bar, h.source_timeframe,
                                h.bar_minutes, [i["id"] for i in instruments],
                                now - timedelta(days=days), now)
                  if plik is None else Pomiar(**plik["spread"]))
        if pomiar.bps is not None:
            spread_bps = pomiar.bps
            spread_nasza_ksiega = pomiar.nasza_ksiega
        for linia in opis_pomiaru(pomiar, {i["id"]: i["symbol"] for i in instruments}):
            print("  " + linia)
    else:
        print(f"  Spread PODANY Z LINII POLECEN: {spread_bps:.3f} pb na rundę "
              f"(nadpisanie awaryjne, nie pomiar z bazy; księga "
              f"{'nasza' if spread_nasza_ksiega else 'OBCA'})")

    # INSTRUMENTY BEZ WEJSC Z POWODU SPREADU (F-1.5) - ta sama regula, ktora
    # stosuje petla (`uniwersum.wykluczenia_spreadowe`: bramka spojnosci, potem
    # `za_szeroki_spread`), na tych samych 7 dniach.
    # Tylko do logu: symulacja nadal liczy cale uniwersum, bo regula patrzy na
    # biezacy arkusz, a okna WF siegaja lat wstecz - wykluczenie dzisiejszym
    # spreadem w 2023 roku byloby selekcja z przyszlosci.
    # Z pliku panelu tej liczby nie ma (arkusz z 7 dni przed eksportem) - log
    # i tak niczego w symulacji nie zmienia.
    wykluczone, bez_wykluczen = (
        wykluczenia_spreadowe(cur, engine, [i["id"] for i in instruments], h, now)
        if plik is None else ({}, None))
    if bez_wykluczen:
        print(f"  {bez_wykluczen} — produkcja też nie wyklucza")
    for linia in opis_wykluczen(wykluczone, {i["id"]: i["symbol"] for i in instruments}):
        print(f"  {linia} — produkcja nie wchodzi, symulacja WF go liczy")

    # SPREAD PER INSTRUMENT DO MODELU WYPELNIEN (`--egzekucja-pesymistyczna`).
    # Ta sama srednia per instrument, z ktorej powstala liczba karty - ale
    # w modelu wypelnien liczy sie spread TEGO instrumentu (NEAR ~21 pb wobec
    # BTC ponizej 1 pb). Instrument bez pomiaru dostaje srednia uniwersum,
    # wypisana z nazwy; spread podany z linii polecen idzie rowno do wszystkich.
    if egzekucja.pesymistyczna:
        po_id = pomiar.per_id if pomiar is not None else {}
        spready = {}
        bez = []
        for i in instruments:
            if i["id"] in po_id:
                spready[i["symbol"]] = po_id[i["id"]]
            else:
                spready[i["symbol"]] = spread_bps or 0.0
                bez.append(i["symbol"])
        egzekucja = dc_replace(egzekucja, spread_per_instrument=spready)
        print("  Model wypełnień PESYMISTYCZNY: limit z ceny 1h po opóźnieniu "
              f"{egzekucja.opoznienie_min} min ± pół spreadu instrumentu, "
              "wypełnienie tylko w godzinie po wysyłce")
        print("    spread per instrument (pb): " + "  ".join(
            f"{s} {v:.2f}" for s, v in sorted(spready.items())))
        if bez:
            print(f"    bez pomiaru, średnia uniwersum {spread_bps or 0.0:.2f} pb: "
                  f"{', '.join(sorted(bez))}")

    # KARTA KOSZTU, NIE JEDNA LICZBA. Wymienia z nazwy skladniki, ktorych
    # symulacja nie nalicza, zamiast wliczac je jako zera - patrz `koszty.py`.
    # `spread_bps` zostaje None, gdy pomiaru NIE MA - wymyslona liczba
    # zamienilaby tu "nie wiem" na "wiem i wynosi tyle".
    pyl, pyl_odniesienia, linie_pylu = pyl_toru(
        cur, kind, instruments, h, now - timedelta(days=days), now,
        zakladane_pozycje, f"{engine}_signal_weighting_{h.bar}",
        plik=plik)
    karta = karta_rundy(kind, egzekucja.limit,
                        spread_bps=spread_bps,
                        spread_nasza_ksiega=spread_nasza_ksiega, pyl=pyl)
    for linia in opis_karty(karta):
        print(linia)
    for linia in linie_pylu:
        print(linia)

    # WSZYSTKIE MIARY OKIEN PO TEJ SAMEJ DOPLACIE, KTORA ODEJMUJE DSR.
    #
    # Do 2026-10-02 doplate (`doplata_poza_symulacja` - dzis spread, przy
    # limicie takze niewypelnienie, gdy zmierzone) odejmowal WYLACZNIE DSR.
    # Odsetek dodatnich okien, mediana Sharpe'a i porownanie z kup-i-trzymaj
    # - trzy z czterech warunkow `status_walidacji` - liczyly sie na zwrotach
    # sprzed niej, czyli opisywaly tanszy swiat niz ten, w ktorym handlujemy.
    # Wada w strone LATWIEJSZEJ bramki, ta sama, ktora opisuje `werdykt_dsr`.
    #
    # Teraz wynik okna przechodzi przez `portfolio.wynik_po_doplacie` zanim
    # cokolwiek z niego policzymy; liczby sprzed doplaty zostaja w logu jako
    # BRUTTO - do porownania, nie do bramki.
    #
    # ODNIESIENIE PLACI SWOJA RUNDE Z TEJ SAMEJ KARTY. Kup-i-trzymaj to jedno
    # wejscie i jedno wyjscie ZLECENIEM RYNKOWYM, wiec doplate bierzemy z karty
    # rynkowej (`maker=False`) przy tym samym pomiarze spreadu. Przy
    # egzekucji rynkowej to ta sama liczba co dla strategii; przy limicie
    # rozni sie tylko o niewypelnienie, ktorego koszyk kupiony po rynku nie
    # ponosi. Uzasadnienie symetrii - w `portfolio.kup_i_trzymaj`.
    doplata = karta.doplata_poza_symulacja
    doplata_odniesienia = karta_rundy(
        kind, False, spread_bps=spread_bps,
        spread_nasza_ksiega=spread_nasza_ksiega,
        pyl=pyl_odniesienia).doplata_poza_symulacja
    # „TA SAMA KWOTA CO W DSR", NIE „TEN SAM KOSZT WSZEDZIE". Do 2026-10-03
    # wydruk mowil „(ta sama co w DSR)" i to bylo prawda wylacznie o kwocie
    # (DSR dostaje `karta.doplata_poza_symulacja`). Trzech rzeczy zdanie nie
    # mowilo, a kazda zmienia sens „po doplacie":
    #   * SELEKCJA WAG in-sample (`ocen` -> `simulate_portfolio`) liczy sie
    #     nadal BRUTTO - doplata wchodzi dopiero w ocenie okna OOS, wiec wybor
    #     wag faworyzuje wysoki obrot. Decyzja wlasciciela, nie poprawka;
    #   * V[SR_n] (`odchylenie_prob_z_bazy`) miesza `oos_sharpe` brutto sprzed
    #     2026-10-02 z wartosciami po doplacie;
    #   * przy `--limit` niewypelnienie jest NIEZMIERZONE i do doplaty nie
    #     wchodzi (`opis_doplaty` nizej wymienia to z nazwy).
    if doplata or doplata_odniesienia:
        print(f"  Miary okien OOS i bramka statusu PO dopłacie {doplata * 1e4:.2f} pb "
              f"na rundę (ta sama KWOTA co w DSR); selekcja wag in-sample "
              f"liczona BRUTTO; V[SR_n] miesza przebiegi brutto sprzed "
              f"2026-10-02 z przebiegami po dopłacie; kup-i-trzymaj po "
              f"{doplata_odniesienia * 1e4:.2f} pb na swoją jedną rundę. "
              f"Liczby „brutto” = przed dopłatą, tylko do porównania.")
    # CZEGO W DOPLACIE NIE MA - zawsze, takze przy doplacie zero. Przy `--limit`
    # niewypelnienie jest NIEZMIERZONE i do dopłaty nie wchodzi; bez tego
    # zdania „po dopłacie" czytaloby sie jak „po wszystkich kosztach".
    print(f"  {opis_doplaty(karta)}")
    return Koszty(karta, doplata, doplata_odniesienia, pyl, spread_bps,
                  spread_nasza_ksiega, egzekucja)


def _przekroj(cur, plik: dict | None, instruments: list[dict], h: Horizon,
              days: int, tylko: set[str] | None) -> tuple[str, dict[str, str]]:
    """Odcisk uniwersum i waluta kwotowana per symbol; ostrzega, gdy brak
    cech przekrojowych dla tego odcisku."""
    # ODCISK UNIWERSUM. Cechy przekrojowe sa nim filtrowane - patrz load_panel.
    #
    #
    # Gdy dla tego zbioru nikt jeszcze nie policzyl sily wzglednej, sygnal
    # bedzie martwy we wszystkich oknach. To jest poprawne zachowanie (lepsze
    # niz czytanie wartosci policzonej dla innego zbioru), ale MUSI byc
    # powiedziane wprost: cichy brak sygnalu wyglada jak sygnal bez tresci.
    waluta_symbolu = {i["symbol"]: i["quote_ccy"] for i in instruments}
    fp = odcisk([i["symbol"] for i in instruments])
    if plik is not None:
        przekroj = plik["przekroj_policzony"]
    else:
        cur.execute("SELECT count(*) AS n FROM feat.feature_def"
                    " WHERE active AND params->>'bar' = %s AND params->>'uniwersum' = %s",
                    (h.bar, fp))
        przekroj = cur.fetchone()["n"]
    if not przekroj:
        print(f"  UWAGA: brak cech przekrojowych dla tego uniwersum (odcisk {fp}).")
        print("         Siła względna będzie martwa. Policz je najpierw:")
        print(f"         python services/ingest/features.py --bar {h.bar} --backfill "
              f"--days {days} --tylko-przekroj"
              + (f" --instrumenty {','.join(i['symbol'] for i in instruments)}"
                 if tylko else ""))
    return fp, waluta_symbolu


def params_przebiegu(h: Horizon, trials_per_window: int, wybor: str, kind: str,
                     progi_per_rezim: bool, egzekucja: Egzekucja,
                     strona: str | None = None) -> dict:
    """`params` wersji modelu - MIANOWNIK N deflacji (patrz `n_konfiguracji`)."""
    eksperymenty = sorted(wlaczone_eksperymenty())
    # `kind` I `limit` MUSZA TU BYC, BO `params` JEST MIANOWNIKIEM N - patrz
    # komentarz przy INSERT nizej. Model wypelnien takze: przebieg pesymistyczny
    # to inna konfiguracja niz optymistyczny. Klucze dopisywane TYLKO poza
    # wartoscia domyslna, zeby przebiegi sprzed zmiany i po niej z tymi samymi
    # nastawami mialy te same `params` (inaczej N roslo by samo od wdrozenia).
    params_przebiegu = {
        "bar": h.bar, "train_days": h.train_days, "test_days": h.test_days,
        "step_days": h.step_days, "min_trades": h.min_trades,
        "trials_per_window": trials_per_window, "seed": SEED,
        "wybor": wybor, "kind": kind,
        # SPOSOB LICZENIA PROGU JEST CZESCIA KONFIGURACJI, nie
        # szczegolem wykonania: ten sam zestaw wag przy progu
        # wspolnym i przy progach per rezim to dwie rozne
        # strategie i nie wolno ich liczyc jako jednej proby.
        "progi": "per_rezim" if progi_per_rezim else "wspolny",
        "limit": bool(egzekucja.limit),
        "sygnaly_eksperymentalne": eksperymenty}
    # PROGI NA WAGACH, KTORYMI SIE HANDLUJE (audyt Fable F-2.1, 2026-10-03).
    # Zmienia progi produkcji przy tych samych wagach - inna strategia, wiec
    # inne `params` i nowa proba w N. Klucz tylko dla `ic`: przy `argmax`
    # i `bazowe` progi dalej licza sie na zalozeniach poczatkowych.
    if wybor == "ic":
        params_przebiegu["progi_na_wagach"] = "ic"
    # PARA PRODUKCYJNA Z OSTATNIEGO OKNA (2026-10-07, `parametry_produkcji`).
    # Ten sam walk-forward, ale produkcja dostaje inne wagi i inne progi niz
    # dotad (mediany po oknach) - inna strategia na zywo, wiec nowa proba w N
    # i `zmiana_struktury` wobec wersji sprzed zmiany.
    params_przebiegu["para_produkcji"] = "ostatnie_okno"
    # Horyzont zwrotu w IC (F-2.6) - klucz tylko poza wartoscia sprzed zmiany
    # (3 swiece), czyli dzis wylacznie dla 1h (12 swiec) i 5m.
    # Od 2026-10-07 (H-7) ta sama liczba przychodzi tez z `--horyzont-etykiety`:
    # inny horyzont na 4h dopisuje klucz, inny na 1h zmienia jego wartosc -
    # w obu wypadkach `zmiana_struktury` to widzi i wersja zostaje `candidate`.
    if h.ic_horizon_bars != 3:
        params_przebiegu["ic_horizon_bars"] = h.ic_horizon_bars
    # MODEL WYPELNIEN W PARAMS (H-5). Od 2026-10-07 przebieg produkcyjny moze
    # liczyc sie modelem pesymistycznym (`_sprawdz_argumenty`), wiec te dwa
    # klucze rozrozniaja konfiguracje w liczniku N i zglaszaja zmiane
    # struktury wobec wersji liczonej modelem sprzed T1.
    if egzekucja.pesymistyczna:
        params_przebiegu["egzekucja_pesymistyczna"] = True
    if egzekucja.opoznienie_min:
        params_przebiegu["opoznienie_min"] = egzekucja.opoznienie_min
    # PRZEBICIE KALIBROWANE DO DEMO (H-10). Klucz to wypelnialnosc DOCELOWA,
    # nie samo przebicie: przebicie liczy sie z danych co przebieg (jak wagi),
    # a konfiguracje wyznacza to, do czego je kalibrujemy.
    if egzekucja.wypelnialnosc_docelowa is not None:
        params_przebiegu["wypelnialnosc_docelowa"] = egzekucja.wypelnialnosc_docelowa
    # STRONA UCZENIA (H-6) - klucz tylko dla rynku bez shortow; ten sam
    # zestaw wag przy progu z |score| i z score > 0 to dwie rozne strategie.
    if strona:
        params_przebiegu["strona"] = strona
    return params_przebiegu


def _zaloz_wersje(cur, conn, plik: dict | None, sucho: bool, engine: str, h: Horizon,
                  fp: str, params_przebiegu: dict, now: datetime,
                  days: int) -> tuple[int | None, int | None, int | None]:
    """`learn.model`, nowa `learn.model_version` w `training` i `learn.training_run`."""
    model_id = mv_id = run_id = None
    if plik is None:
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
             json.dumps(params_przebiegu),
             feature_ids, signal_ids, now - timedelta(days=days), now, sha_kodu()))
        mv_id = cur.fetchone()["id"]

        cur.execute(
            "INSERT INTO learn.training_run (model_version_id, train_from, train_to, status)"
            " VALUES (%s,%s,%s,'running') RETURNING id",
            (mv_id, now - timedelta(days=days), now))
        run_id = cur.fetchone()["id"]
        if not sucho:
            conn.commit()
    return model_id, mv_id, run_id


def _wczytaj_panele(cur, plik: dict | None, instruments: list[dict], h: Horizon,
                    fp: str, now: datetime, days: int, z_godzin: bool):
    """Panele instrumentow (> 500 punktow), swiece 1h do modelu wypelnien
    i liczba rewizji po czasie decyzji - wypisana."""
    # Panel wczytujemy RAZ na instrument - to najdrozsza operacja w calym treningu.
    panels = {}
    inst_ids = {inst["symbol"]: inst["id"] for inst in instruments}
    od_panelu = now - timedelta(days=days)
    for inst in instruments:
        if plik is None:
            p = load_panel(cur, inst["id"], h, od_panelu, now, fp=fp)
        else:
            # Ten sam przedzial co `load_panel` (BETWEEN, obustronnie domkniety).
            p = [r for r in plik["panele"].get(inst["symbol"], [])
                 if od_panelu <= r["t"] <= now]
        if len(p) > 500:
            panels[inst["symbol"]] = p
        print(f"  {inst['symbol']:10s} {len(p):>7,} punktów panelu")
    # SWIECE 1h DLA MODELU WYPELNIEN - tylko gdy model ich uzywa.
    swiece_wykonania = None
    if z_godzin:
        swiece_wykonania = {}
        for inst in instruments:
            if inst["symbol"] not in panels:
                continue
            if plik is None:
                sw = load_swiece_wykonania(cur, inst["id"], od_panelu, now)
            else:
                sw = plik.get("swiece_1h", {}).get(inst["symbol"], [])
            swiece_wykonania[inst["symbol"]] = indeks_swiec(sw)
        print(f"  świece 1h do modelu wypełnień: "
              f"{sum(len(v) for v in swiece_wykonania.values()):,}")
    # REWIZJE PO CZASIE DECYZJI: trenujemy na wartosciach poprawionych, ale
    # silnik decydowal na starych. Liczba idzie do logu, bo to jedyne miejsce,
    # w ktorym ten rozjazd w ogole widac (uzasadnienie: `load_panel`).
    zrew, wszystkie = rewizje_po_decyzji(panels)
    print(f"  rewizje po czasie decyzji: {zrew:,} z {wszystkie:,} wartości cech"
          f" ({100 * zrew / wszystkie if wszystkie else 0:.3f}%)"
          f" — na tych silnik widział inną liczbę niż ta, na której uczy się trener")
    return panels, swiece_wykonania, inst_ids


def _sprawdz_panele(cur, conn, sucho: bool, plik: dict | None,
                    panels: dict[str, list[dict]], inst_ids: dict[str, int],
                    h: Horizon, now: datetime, run_id: int | None) -> None:
    """Brak paneli albo cechy w tyle za swiecami - przebieg przerwany kodem
    `KOD_NIC_NIE_POLICZONO`, `training_run` oznaczony jako `failed`."""
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
        if cur is not None:
            cur.execute("UPDATE learn.training_run SET status='failed', finished_at=now(),"
                        " error='za mało punktów panelu' WHERE id=%s", (run_id,))
        _utrwal(conn, sucho)
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
    if plik is not None:
        ostatnia_swieca = plik.get("ostatnia_swieca")
    else:
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
        print("  sprawdź: kubectl logs deploy/trader-features --tail=30")
        if cur is not None:
            cur.execute("UPDATE learn.training_run SET status='failed', finished_at=now(),"
                        " error=%s WHERE id=%s", (komunikat, run_id))
        _utrwal(conn, sucho)
        raise SystemExit(KOD_NIC_NIE_POLICZONO)


@dataclass(frozen=True)
class Okno:
    """Granice jednego okna walk-forward i instrumenty, ktore w nim licza sie."""
    train_from: datetime
    train_to: datetime
    test_from: datetime
    test_to: datetime
    aktywne: list[str]


def okna_wf(panels: dict[str, list[dict]], h: Horizon, od: datetime,
            do: datetime) -> list[Okno]:
    """Siatka okien walk-forward od `od` do `do`, krok `h.step_days`. CZYSTA FUNKCJA.

    Okno, w ktorym mniej niz `MIN_INSTRUMENTS` instrumentow ma dosc swiec
    treningowych, odpada (przesuniecie siatki idzie dalej).
    """
    okna: list[Okno] = []
    start = od
    while start + timedelta(days=h.train_days + h.test_days) <= do:
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
        if len(aktywne) >= MIN_INSTRUMENTS:
            okna.append(Okno(train_from, train_to, test_from, test_to, aktywne))
        start += timedelta(days=h.step_days)
    return okna


@dataclass(frozen=True)
class Kontekst:
    """To, co jest wspolne dla wszystkich okien przebiegu."""
    h: Horizon
    kind: str
    egzekucja: Egzekucja
    zakladane_pozycje: int | None
    swiece_wykonania: dict | None
    inst_ids: dict[str, int]
    panels: dict[str, list[dict]]
    waluta_symbolu: dict[str, str]
    doplata: float
    doplata_odniesienia: float
    karta: Karta
    progi_per_rezim: bool
    # Rynek bez shortow: progi i wagi IC z obserwacji long (`strona_uczenia`, H-6).
    tylko_dluga: bool = False


@dataclass(frozen=True)
class DaneOkna:
    """Wycinki okna, progi ATR, sygnaly policzone raz i bramki wejscia."""
    train_slices: dict[str, list[dict]]
    test_slices: dict[str, list[dict]]
    thresholds: dict[str, float | None]
    cache_tr: dict
    cache_te: dict
    martwe: set[str]
    bazowe: dict[str, dict[str, float]]
    gates: Bramki | None


def przygotuj_okno(kt: Kontekst, okno: Okno, z_bramkami: bool = True) -> DaneOkna:
    """Wycinki treningowy i testowy, rezimy i sygnaly okna oraz progi wejscia
    liczone WYLACZNIE na oknie treningowym.

    `z_bramkami=False` - progi policzy wolajacy na wagach, ktorymi sie handluje
    (`bramki_na_wagach`), `gates` zostaje None.
    """
    h, panels, aktywne = kt.h, kt.panels, okno.aktywne
    progi_per_rezim = kt.progi_per_rezim
    train_from, train_to = okno.train_from, okno.train_to
    test_from, test_to = okno.test_from, okno.test_to
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
    gates = (bramki_okna({s: train_slices[s] for s in aktywne}, bazowe,
                         thresholds, h, cache_tr, per_rezim=progi_per_rezim,
                         tylko_dluga=kt.tylko_dluga)
             if z_bramkami else None)
    return DaneOkna(train_slices, test_slices, thresholds, cache_tr, cache_te,
                    martwe, bazowe, gates)


def _ocena_treningowa(kt: Kontekst, dane: DaneOkna):
    # --- dobor wag WYLACZNIE na oknie treningowym ------------------------
    # Celem jest Sharpe CALEGO PORTFELA, nie srednia z Sharpe'ow liczonych
    # osobno per instrument. Roznica nie jest kosmetyczna: portfel podlega
    # limitowi lacznej ekspozycji i kill-switchowi, wiec uklad wag, ktory
    # wygląda dobrze na kazdym instrumencie z osobna, moze byc niewykonalny,
    # gdy wszystkie chca wejsc jednoczesnie.
    def ocen(w, sl, c):
        res = simulate_portfolio(sl, w, dane.thresholds, dane.gates, kt.h, MIN_CONF,
                                 instrument_ids=kt.inst_ids, cache=c,
                                 kind=kt.kind, egzekucja=kt.egzekucja,
                                 zakladane_pozycje=kt.zakladane_pozycje,
                                 swiece_wykonania=kt.swiece_wykonania)
        return portfolio_metrics(res, kt.h.min_trades)
    return ocen


def _zapis_hipotez(cur, sucho: bool, model_id: int | None):
    def zapisz_hipoteze(cand):
        if sucho:
            return
        cur.execute(
            "INSERT INTO learn.hypothesis_trial (model_id, config_hash, params,"
            " n_windows) VALUES (%s,%s,%s,0)"
            " ON CONFLICT (model_id, config_hash) DO NOTHING",
            (model_id, _hash_konfiguracji(cand), json.dumps(cand)))
        # `n_windows = 0`, NIE 1 - I TO NIE JEST KOSMETYKA.
        #
        # Kolumna liczy okna, w ktorych ta konfiguracja MA policzony wynik
        # OOS. W chwili INSERT-u nie ma go jeszcze zadna: kandydat jest
        # dopiero oceniony na oknie TRENINGOWYM. Wpisana jedynka twierdzila,
        # ze jedno okno OOS juz policzono, przy `oos_sharpe` rownym NULL -
        # czyli dwie kolumny tego samego wiersza mowily co innego. Przez to
        # ani nie dalo sie uzyc `n_windows` jako dzielnika do sredniej, ani
        # odroznic "nie policzono" od "policzono i wyszlo nic".
        #
        # Historyczne 16 050 wierszy poprawia migracja 0061.
    return zapisz_hipoteze


def kandydat_wag(train_slices: dict[str, list[dict]], cache_tr: dict, martwe: set[str],
                 wybor: str, *, bazowe: dict[str, dict[str, float]], ocen,
                 zapisz_hipoteze, rng: random.Random, trials_per_window: int,
                 horyzont_swiec: int = 3,
                 tylko_dluga: bool = False) -> tuple[dict[str, dict[str, float]], int]:
    """Wagi na jedno okno, dobrane WYLACZNIE na oknie treningowym.

    Zwraca (wagi, 1 gdy wygraly wagi z IC, inaczej 0). `ocen(w, wycinki, cache)`
    to symulacja portfela na oknie treningowym, `zapisz_hipoteze(w)` liczy
    kazdego ocenionego kandydata do `learn.hypothesis_trial`.
    """
    wybrane_ic = 0
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
        best_w = ic_weights(train_slices, cache_tr, martwe, horyzont_swiec,
                            tylko_dluga=tylko_dluga)
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
        ic_w = ic_weights(train_slices, cache_tr, martwe, horyzont_swiec,
                          tylko_dluga=tylko_dluga)
        ic_score = ocen(ic_w, train_slices, cache_tr)["sharpe"]
        zapisz_hipoteze(ic_w)
        if ic_score > best_score:
            best_w, best_score = ic_w, ic_score
            wybrane_ic += 1
    return best_w, wybrane_ic


@dataclass
class Metryki:
    """Wynik OOS jednego okna. `pusty` = symulacja bez krzywej kapitalu
    (okno pominiete, ale jego wypelnienia licza sie do wypelnialnosci)."""
    oos_brutto: object
    oos_res: object
    oos_m: dict
    oos_m_brutto: dict
    pusty: bool = False
    n_trades: int = 0
    informative: bool = False
    agg: dict = field(default_factory=dict)
    ekspozycja_por: float = 0.0
    odn_okna: dict = field(default_factory=dict)
    bije_okna: dict = field(default_factory=dict)
    dominant: str | None = None
    odniesienie: float = 0.0
    odniesienie_brutto: float = 0.0
    ekspozycja: float = 0.0
    odniesienie_dop: float = 0.0
    porownywalne_okno: bool = False


def ocen_okno(kt: Kontekst, okno: Okno, dane: DaneOkna,
              best_w: dict[str, dict[str, float]]) -> Metryki:
    """Ocena OUT-OF-SAMPLE jednego okna wagami `best_w`. Bez zapisow i wydruku."""
    h, kind, doplata, doplata_odniesienia = kt.h, kt.kind, kt.doplata, kt.doplata_odniesienia
    test_slices, thresholds, gates = dane.test_slices, dane.thresholds, dane.gates
    aktywne, test_from, test_to = okno.aktywne, okno.test_from, okno.test_to
    inst_ids, egzekucja, panels = kt.inst_ids, kt.egzekucja, kt.panels
    zakladane_pozycje, swiece_wykonania = kt.zakladane_pozycje, kt.swiece_wykonania
    cache_te, karta = dane.cache_te, kt.karta
    # --- ocena OUT-OF-SAMPLE --------------------------------------------
    oos_brutto = simulate_portfolio(
        {s: v for s, v in test_slices.items() if v}, best_w, thresholds, gates, h,
        MIN_CONF, instrument_ids=inst_ids, cache=cache_te,
        kind=kind, egzekucja=egzekucja, zakladane_pozycje=zakladane_pozycje,
        swiece_wykonania=swiece_wykonania)
    # OD TEGO MIEJSCA `oos_res` I `oos_m` SA PO DOPLACIE Z KARTY - patrz
    # komentarz przy `doplata`. Wersja brutto zostaje pod wlasna nazwa
    # i idzie WYLACZNIE do wydruku oraz do dwoch odbiorcow, ktorzy doplate
    # odejmuja sami (`werdykt_dsr`, `opis_przewagi`) - patrz nizej.
    oos_res = wynik_po_doplacie(oos_brutto, doplata)
    oos_m = portfolio_metrics(oos_res, h.min_trades)
    oos_m_brutto = portfolio_metrics(oos_brutto, h.min_trades)
    if not oos_res.equity_curve:
        return Metryki(oos_brutto, oos_res, oos_m, oos_m_brutto, pusty=True)
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
    # ZNACZNIK MIARY: dopłata, przy której policzono miary tego okna.
    #
    # Od 2026-10-02 miary okna są PO dopłacie z karty, wcześniej brutto.
    # Wiersz `learn.learning_state_log` nie ma kolumny, która by to mówiła,
    # więc `dozor.miara_przebiegu` czyta ją stąd (przez `model_version_id`
    # -> `training_run` -> `wf_window`). Brak tej metryki = przebieg sprzed
    # zmiany; zero = ta sama liczba co brutto. Dozór porównuje wyłącznie
    # przebiegi liczone tą samą miarą - inaczej pierwszy przebieg po
    # wdrożeniu zgłaszałby „zmianę znaku mediany", która jest zmianą
    # definicji, nie rynku.
    #
    # Plus DRUGI znacznik: czy spread w karcie byl w ogole zmierzony. Bez
    # niego doplata zero z powodu luki w `book_snapshot` (`zmierz_spread`
    # oddaje `bps=None`) byla nieodroznialna od przebiegu brutto - patrz
    # `znacznik_miary`.
    agg.update(znacznik_miary(karta, egzekucja))
    # SIODMA MIARA NA OKNO: odsetek transakcji bedacych POWROTEM w ten sam
    # instrument ponizej trzech swiec po wyjsciu z niego. NIE jest to
    # czestotliwosc handlu - patrz `portfolio.udzial_powtornych_wejsc`.
    #
    # Liczona dla KAZDEGO okna, nie tylko dla `range`: bez wartosci
    # w pozostalych rezimach nie ma z czym porownac i liczba nie rozstrzyga
    # niczego. Przy oknie bez transakcji NIE zapisujemy zera - zero znaczy
    # "zmierzono i wyszlo zero", a tu nie ma czego mierzyc.
    powtorne = udzial_powtornych_wejsc(oos_res.trades, h.bar_minutes)
    if powtorne is not None:
        agg["udzial_powtornych_wejsc"] = powtorne
    # ODNIESIENIA B0-B3 NA TYM SAMYM OKNIE (audyt Fable R3 / T5). Ekspozycja
    # strategii to `ekspozycja_przenoszona` - kazdy przedzial, przez ktory
    # pozycja niesie cene, raz (patrz `PortfolioResult`). Kolumny `bije_b*`
    # ida do `learn.wf_metric` jako 1/0; brak porownania = brak wiersza.
    ekspozycja_por = oos_m["ekspozycja_przenoszona"]
    odn_okna = odniesienia.odniesienia_okna(
        {s: panels[s] for s in aktywne}, test_from, test_to,
        h.bar_minutes, kind, doplata_odniesienia)
    bije_okna: dict[str, bool | None] = {}
    for nazwa in ("B1", "B2", "B3"):
        b = odniesienia.bije(oos_m["total_return"], ekspozycja_por, odn_okna[nazwa])
        bije_okna[nazwa] = b if informative else None
        if bije_okna[nazwa] is not None:
            agg[f"bije_{nazwa.lower()}"] = 1.0 if b else 0.0
    for nazwa in ("B2", "B3"):
        if odn_okna[nazwa] is not None:
            agg[f"{nazwa.lower()}_zwrot"] = odn_okna[nazwa].zwrot
            agg[f"{nazwa.lower()}_ekspozycja"] = odn_okna[nazwa].ekspozycja
    step = max(1, len(next(iter(test_slices.values()), [])) // 50)
    regimes = [classify_regime(r["f"], thresholds[s])[0]
               for s in aktywne for r in test_slices[s][::step]]
    dominant = rezim_dominujacy(regimes)
    odniesienie = kup_i_trzymaj({s: v for s, v in test_slices.items() if v}, kind,
                                doplata=doplata_odniesienia)
    odniesienie_brutto = kup_i_trzymaj({s: v for s, v in test_slices.items() if v},
                                       kind)
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
    # EKSPOZYCJA DO POROWNANIA = PRZENOSZONA, nie `avg_exposure`
    # (2026-10-03, audyt Fable F-3.5): `avg_exposure` gubi po jednym
    # przedziale na transakcje, takze na domknieciu koncem okna, czyli
    # zanizala koszyk „przy tej samej ekspozycji" na korzysc strategii.
    # `ekspozycja` (stara miara) zostaje w rozkladzie na czynniki, ktory
    # sie na niej zamyka.
    ekspozycja = oos_m["avg_exposure"]
    odniesienie_dop = odniesienie * ekspozycja_por
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
    porownywalne_okno = informative and ekspozycja_por > 0.0
    return Metryki(oos_brutto, oos_res, oos_m, oos_m_brutto, False, n_trades,
                   informative, agg, ekspozycja_por, odn_okna, bije_okna, dominant,
                   odniesienie, odniesienie_brutto, ekspozycja, odniesienie_dop,
                   porownywalne_okno)


class Przebieg:
    """Liczniki i listy zbierane okno po oknie przez caly walk-forward."""

    def __init__(self):
        self.positive = 0
        # HISTORIA PROGOW PER REZIM: rezim -> lista progow z kolejnych okien.
        # Produkcja dostaje z tego MEDIANE OSOBNO DLA KAZDEGO REZIMU. Jedna
        # wspolna lista (tak bylo do 2026-09-27) mieszala progi z rozlacznych
        # skal i jej mediana nie byla percentylem zadnego z nich.
        self.historia_bramek: dict[str, list[float]] = {}
        # W ilu oknach rezim NIE dostal progu, bo proba byla za mala. Liczba
        # potrzebna w podsumowaniu: rezim bez progu nie handluje, a to musi byc
        # widoczne jako decyzja, nie jako cisza.
        self.okna_bez_progu: dict[str, int] = {}
        self.weight_history: list[dict] = []
        # Rownolegla do weight_history: progi (`Bramki`) KAZDEGO okna, policzone
        # na wagach tego okna. Ostatni element razem z `weight_history[-1]` to
        # para, ktora dostaje produkcja (`parametry_produkcji`, 2026-10-07).
        self.bramki_okien: list[Bramki] = []
        # Rownolegla do weight_history: ktore sygnaly byly martwe w danym oknie.
        # Bez tego zero po martwym sygnale nie da sie odroznic od zera zmierzonego.
        self.martwe_w_oknie: list[set[str]] = []
        self.wybrane_ic = 0
        # Porownanie z nierobieniem niczego. Bez tego odsetek dodatnich okien nie
        # mowi, czy strategia jest LEPSZA od kupienia i trzymania - a strategia
        # wylacznie dluga na rosnacym rynku ma duzo dodatnich okien z samej bety.
        self.pokonane_odniesienie = 0
        self.pokonane_pelne = 0        # przy odniesieniu 100% zaangazowanym - do raportu
        # BRUTTO, czyli przed doplata - wylacznie do wydruku obok miar po doplacie
        # z karty. Do bramki nie wchodzi zadna z tych liczb.
        self.positive_brutto = 0
        # Zlecenia z limitem, ktore w symulacji OOS NIE weszly (wejscia + wyjscia).
        # Do wydruku obok karty: niewypelnienie w karcie jest NIEZMIERZONE jako
        # koszt, a ta liczba mowi przynajmniej, jak czesto symulacja go ponosi.
        self.niewypelnione_oos = 0
        # Mianownik wypelnialnosci i material na skladnik `niewypelnienie` karty.
        self.wypelnione_oos = 0
        self.utracone_oos: list[float] = []
        self.brak_swiec_oos = 0
        # Zlecenia, ktorych symulacja nie zlozyla, bo gielda byla zamknieta
        # (`portfolio.PortfolioResult.poza_sesja`, A-2). Na krypto zawsze 0.
        self.poza_sesja_oos = 0
        self.poza_sesja_wejsc_oos = 0
        # IC per sygnal, okno po oknie (`ic_sygnalow`) - tylko do wydruku.
        self.ic_okien: list[dict[str, dict[str, float]]] = []
        # SAME WEJSCIA (2026-10-11). Kryterium H-5 porownuje z demo wypelnialnosc
        # WEJSC; suma wyzej miesza je z wyjsciami. Glebokosci - material na
        # kalibracje przebicia (`kalibracja_wypelnien`).
        self.wypelnione_wejsc_oos = 0
        self.niewypelnione_wejsc_oos = 0
        self.glebokosci_wejsc_oos: list[float] = []
        # ODNIESIENIA B0-B3 (`baseline.py`): {nazwa: [pobite, porownywalne]}.
        self.bije_odn: dict[str, list[int]] = {n: [0, 0] for n in ("B1", "B2", "B3")}
        self.b0_dodatnie = [0, 0]      # [zwrot > 0, okna]
        # Zwrot (% kapitalu) i ekspozycja strategii oraz odniesien w oknach
        # informatywnych - do podsumowania: same „bije/nie bije" nie mowia, czy
        # odniesienie zarabia, czy tylko strategia traci mniej.
        self.przebieg_odn: dict[str, list[tuple[float, float]]] = {
            n: [] for n in ("strategia", "B1", "B2", "B3")}
        self.b0_4_pobite = 0
        # Zwrot na runde PO DOPLACIE per okno - do tabeli STOP/GO.
        self.all_sharpes_brutto: list[float] = []
        self.pokonane_odniesienie_brutto = 0
        self.ekspozycje: list[float] = []
        self.zwroty_okien: list[float] = []
        self.rozklad: list[dict] = []          # czynniki ekspozycji, okno po oknie
        # Ile transakcji przypadlo kazdemu instrumentowi. Uniwersum ma tyle miejsc,
        # ile instrumentow, ale kandydaci konkuruja o wspolny kapital i sa sortowani
        # PO SILE SYGNALU - wiec instrument o mocniejszych sygnalach moze wypchnac
        # pozostale. Bez tej liczby "dolozylismy instrumenty" i "handlujemy tymi
        # instrumentami" wygladaja tak samo.
        self.udzial_instrumentu: dict[str, int] = {}
        # Sharpe policzony OSOBNO dla kazdej grupy instrumentow, w tym samym oknie
        # i tym samym przebiegu. {grupa: [sharpe okno po oknie]}
        #
        # PYTANIE, NA KTORE TO ODPOWIADA. Dwa uniwersa maja osobno mediane Sharpe
        # okolo +0,11, a ich suma +0,011. Cztery naturalne wyjasnienia zostaly
        # obalone pomiarem (renacode-infra: docs/projekty/trader/03-wynik-walk-forward.md). Zostaje jedno miejsce: `Sharpe/transakcje`
        # w oknie to SREDNIA DZIELONA PRZEZ ODCHYLENIE po wszystkich transakcjach
        # tego okna - a grupy o roznej zmiennosci wrzucone do jednej takiej
        # statystyki daja wynik, ktory nie jest srednia ich wynikow.
        #
        # Jesli grupy liczone osobno W TYM SAMYM PRZEBIEGU maja nadal ~+0,11,
        # to znaczy, ze transakcje sa w porzadku, a zalamuje sie MIARA. Jesli
        # rowniez spadaja - to zmienil sie zestaw transakcji, czyli konstrukcja
        # portfela, i szukac trzeba tam.
        self.sharpe_grup: dict[str, list[float]] = {}
        self.porownywalne = 0
        self.all_sharpes: list[float] = []
        # Sharpe OOS per KONFIGURACJA (odcisk wektora wag) - material na V[SR_n]
        # Baileya. Zbierany w pamieci i dopisywany do `learn.hypothesis_trial`
        # po calym walk-forwardzie, a nie okno po oknie: ta sama konfiguracja moze
        # wygrac w kilku oknach i wtedy `oos_sharpe` ma byc ich SREDNIA, a nie
        # wartoscia z ostatniego okna, ktore ja nadpisalo.
        self.oos_wg_konfiguracji: dict[bytes, list[float]] = {}
        # Zwroty WSZYSTKICH transakcji OOS, nie usrednione po oknach - patrz
        # `podsumowanie_przewagi`.
        self.wszystkie_zwroty: list[float] = []
        # Swieca wejscia (`opened_at`) kazdej transakcji z `wszystkie_zwroty`,
        # ta sama kolejnosc - T niezaleznych obserwacji w DSR (W-2).
        self.wejscia_transakcji: list = []
        self.transakcje_stron: list[tuple[float, int, str]] = []
        self.transakcje_pokrycia: list[tuple[float, float, str]] = []
        # To samo rozbite na okna: potrzebne, zeby przeliczyc srednia PO OKNACH
        # przy doliczonym koszcie. Sama lista Sharpe'ow tego nie pozwala.
        self.zwroty_okien_transakcje: list[list[float]] = []
        self.worst_dd = 0.0
        self.predictions: list[tuple[float, bool]] = []
        # CZAS TRZYMANIA (swiece, czy domkniete koncem okna) - ta sama proba co
        # `wszystkie_zwroty`. Material na `opis_trzymania` (H-7).
        self.trzymanie: list[tuple[int, bool]] = []

    def dodaj_wypelnienia(self, m: Metryki) -> None:
        z = self
        z.niewypelnione_oos += m.oos_m["niewypelnione"]
        z.wypelnione_oos += m.oos_m["wypelnione"]
        z.utracone_oos.extend(m.oos_brutto.utracone_ruchy)
        z.brak_swiec_oos += m.oos_brutto.brak_swiec_wykonania
        z.poza_sesja_oos += m.oos_brutto.poza_sesja
        z.poza_sesja_wejsc_oos += m.oos_brutto.poza_sesja_wejscia
        z.wypelnione_wejsc_oos += m.oos_m["wypelnione_wejscia"]
        z.niewypelnione_wejsc_oos += m.oos_m["niewypelnione_wejscia"]
        z.glebokosci_wejsc_oos.extend(m.oos_brutto.glebokosci_wejsc)

    def dodaj(self, m: Metryki, okno: Okno, h: Horizon,
              waluta_symbolu: dict[str, str], best_w: dict) -> None:
        z = self
        oos_m, oos_m_brutto, oos_res, oos_brutto = m.oos_m, m.oos_m_brutto, m.oos_res, m.oos_brutto
        informative, agg, odn_okna, dominant = m.informative, m.agg, m.odn_okna, m.dominant
        ekspozycja_por, porownywalne_okno, ekspozycja = m.ekspozycja_por, m.porownywalne_okno, m.ekspozycja
        odniesienie, odniesienie_brutto = m.odniesienie, m.odniesienie_brutto
        odniesienie_dop, aktywne = m.odniesienie_dop, okno.aktywne
        # SHARPE OOS KANDYDATA - TUTAJ I TYLKO TUTAJ JEST ZNANY.
        #
        # `zapisz_hipoteze` wola sie wyzej, w momencie, w ktorym znany jest
        # wynik na oknie TRENINGOWYM (`ocen(cand, train_slices, cache_tr)`).
        # Zapisanie tamtej liczby do `oos_sharpe` wypelniloby kolumne
        # wartosciami IN-SAMPLE, ktore wygladaja jak pomiar OOS - a to jest
        # gorsze niz NULL, bo NULL nie klamie. Rozrzut policzony z takich liczb
        # zawieralby klatwe zwyciezcy i przestawialby poprzeczke selekcji
        # w strone, ktorej nikt by nie zobaczyl.
        #
        # OOS liczy sie DLA JEDNEGO kandydata na okno - tego, ktory wygral
        # (`best_w`). Pozostali (`trials_per_window` losowan plus zalozenia
        # poczatkowe plus IC) nie maja wyniku OOS i nigdy go nie mieli.
        # Powiedziane wprost, bo z samej wypelnionej kolumny tego nie widac.
        #
        # `sharpe` moze byc None przy oknie o jednej transakcji - wtedy nie ma
        # czego zapisac i nie zapisujemy zera.
        if oos_m.get("sharpe") is not None and not oos_m.get("insufficient"):
            z.oos_wg_konfiguracji.setdefault(
                _hash_konfiguracji(best_w), []).append(oos_m["sharpe"])
        for nazwa in ("B1", "B2", "B3"):
            if m.bije_okna.get(nazwa) is not None:
                z.bije_odn[nazwa][1] += 1
                z.bije_odn[nazwa][0] += int(m.bije_okna[nazwa])
        if informative:
            z.przebieg_odn["strategia"].append((oos_m["total_return"], ekspozycja_por))
            for nazwa in ("B1", "B2", "B3"):
                if odn_okna[nazwa] is not None:
                    z.przebieg_odn[nazwa].append((odn_okna[nazwa].zwrot,
                                                odn_okna[nazwa].ekspozycja))
            z.b0_dodatnie[1] += 1
            z.b0_dodatnie[0] += int(oos_m["total_return"] > 0)
            z.b0_4_pobite += int(oos_m["total_return"] > odn_okna["B0_4"].zwrot)
        if informative:
            z.all_sharpes.append(agg["sharpe"])
            z.worst_dd = min(z.worst_dd, agg["max_dd"])
            if agg["sharpe"] > 0:
                z.positive += 1
            z.all_sharpes_brutto.append(oos_m_brutto["sharpe"])
            if oos_m_brutto["sharpe"] > 0:
                z.positive_brutto += 1
            # ZLACZONA PROBA WSZYSTKICH TRANSAKCJI OOS.
            #
            # `all_sharpes` zbiera ILORAZY policzone osobno w kazdym oknie,
            # a ich srednia nie jest ilorazem ze zlaczonej proby - okno
            # o pieciu transakcjach wazy tam tyle samo, co okno o dwustu.
            # Dokladanie kosztu tez wymaga tej listy: koszt placi kazda
            # transakcja z osobna, nie kazde okno.
            #
            # BRUTTO, A NIE `oos_res`: obaj odbiorcy tej listy - `werdykt_dsr`
            # i `opis_przewagi` - odejmuja doplate SAMI, przez te sama
            # `koszty.zwroty_po_doplacie`, i podaja ja w opisie. Lista juz po
            # doplacie odjelaby ja tam drugi raz.
            zwroty_okna = [t["ret"] for t in oos_brutto.trades]
            z.wszystkie_zwroty.extend(zwroty_okna)
            z.wejscia_transakcji.extend(t.get("opened_at") for t in oos_brutto.trades)
            # ROZBITE NA OKNA, a nie tylko zlaczone - zeby te sama miare, ktora
            # raport podaje dzis (srednia po oknach), dalo sie PRZELICZYC po
            # doliczeniu kosztu. Bez tego „przed" i „po" byloby liczone dwiema
            # roznymi miarami i roznica nie znaczylaby nic.
            z.zwroty_okien_transakcje.append(zwroty_okna)
            z.trzymanie.extend((t["swiec"], bool(t.get("koniec_okna")))
                               for t in oos_brutto.trades if t.get("swiec") is not None)
        # Kalibracja: deklarowana PRZED wejsciem pewnosc kontra faktyczny wynik.
        # Wczesniejsza wersja budowala "prognoze" z wielkosci samego zwrotu
        # (abs(t) * 50 + 0.5), czyli z rzeczy znanej dopiero po fakcie - wykres
        # kalibracji mierzyl wtedy wlasny wynik i zawsze wygladal dobrze.
        for t in oos_res.trades:
            z.predictions.append((t["conf"], t["ret"] > 0))
            z.udzial_instrumentu[t["symbol"]] = z.udzial_instrumentu.get(t["symbol"], 0) + 1

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
                z.sharpe_grup.setdefault(grupa, []).append(statistics.fmean(rety) / sd)
        # STRONA I REZIM PRZY KAZDEJ TRANSAKCJI - material na `opis_stron`.
        #
        # STOI TU, A NIE WYZEJ PRZY `wszystkie_zwroty`, I TO JEST CALY POWOD:
        # `dominant` powstaje dopiero w tej linii. Zebranie go razem ze
        # zwrotami dawalo NameError na pierwszym oknie - a gdyby zmienna byla
        # juz z czegos zdefiniowana, skutek bylby GORSZY i cichy: rezim
        # POPRZEDNIEGO okna dopisany do transakcji tego.
        #
        # Strony nie da sie odtworzyc z `wszystkie_zwroty`: tamta lista to gole
        # liczby, a stratny long i stratny short wygladaja identycznie.
        #
        # `informative` POWTORZONE CELOWO. Zlaczona proba w `wszystkie_zwroty`
        # bierze wylacznie okna informatywne, a ta tabela ma opisywac TE SAMA
        # probe - inaczej Sharpe stron nie skladalby sie do Sharpe'a calosci
        # i dwie liczby w jednym raporcie mowilyby o dwoch roznych probach.
        if informative:
            z.transakcje_stron.extend(
                (t["ret"], int(t.get("side", 1)), dominant or "(reżim nieznany)")
                for t in oos_res.trades)
            # OSOBNA LISTA, A NIE CZWARTY ELEMENT TAMTEJ KROTKI: `opis_stron`
            # i `opis_pokrycia` odpowiadaja na rozne pytania i maja rozne testy;
            # wspolna krotka wiazalaby je tak, ze zmiana jednej psulaby druga.
            z.transakcje_pokrycia.extend(
                (t["ret"], float(t.get("pokrycie", 1.0)),
                 dominant or "(reżim nieznany)") for t in oos_res.trades)
        if porownywalne_okno:
            z.porownywalne += 1
            z.ekspozycje.append(ekspozycja)
            z.zwroty_okien.append(oos_m["total_return"])
            z.rozklad.append(probka_rozkladu(oos_m, len(aktywne)))
            if oos_m["total_return"] > odniesienie_dop:
                z.pokonane_odniesienie += 1
            if oos_m["total_return"] > odniesienie:
                z.pokonane_pelne += 1
            # Ta sama ekspozycja - doplata nie zmienia ani jednej pozycji.
            if oos_m_brutto["total_return"] > odniesienie_brutto * ekspozycja_por:
                z.pokonane_odniesienie_brutto += 1

    def dodaj_progi(self, gates: Bramki, best_w: dict, martwe: set[str]) -> None:
        z = self
        for rezim in REGIMES:
            prog_rezimu = gates.wejscie(rezim)
            if prog_rezimu is None:
                # Rezim, ktory w tym oknie WYSTAPIL, ale nie uzbieral proby.
                # Rezim, ktorego w oknie nie bylo wcale, nie jest brakiem
                # kalibracji i do tego licznika nie wchodzi.
                if gates.prob.get(rezim):
                    z.okna_bez_progu[rezim] = z.okna_bez_progu.get(rezim, 0) + 1
                continue
            z.historia_bramek.setdefault(rezim, []).append(prog_rezimu)
        z.weight_history.append(best_w)
        z.bramki_okien.append(gates)
        z.martwe_w_oknie.append(martwe)


def zapisz_okno(cur, conn, run_id: int, idx: int, okno: Okno, m: Metryki) -> None:
    cur.execute(
        "INSERT INTO learn.wf_window (training_run_id, idx, train_from, train_to,"
        " test_from, test_to, n_trades, dominant_regime)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (run_id, idx, okno.train_from, okno.train_to, okno.test_from, okno.test_to,
         m.n_trades, m.dominant))
    w_id = cur.fetchone()["id"]
    cur.executemany(
        "INSERT INTO learn.wf_metric (wf_window_id, metric, value) VALUES (%s,%s,%s)",
        [(w_id, k, round(v, 4)) for k, v in m.agg.items()])
    conn.commit()


def opis_okna(idx: int, okno: Okno, m: Metryki, h: Horizon, doplata: float) -> str:
    """Linia wydruku okna (czytana wzorcem przez `scripts/porownaj_warianty.py`)."""
    informative, agg, oos_m, oos_m_brutto = m.informative, m.agg, m.oos_m, m.oos_m_brutto
    oos_res, bije_okna, n_trades, dominant = m.oos_res, m.bije_okna, m.n_trades, m.dominant
    ekspozycja_por, odniesienie, odniesienie_dop = m.ekspozycja_por, m.odniesienie, m.odniesienie_dop
    porownywalne_okno, test_from, test_to = m.porownywalne_okno, okno.test_from, okno.test_to
    mark = "" if informative else (f"  (poniżej {h.min_trades} transakcji — "
                                   "pominięte w ocenie)")
    if informative:
        mark += (f"  [strategia {oos_m['total_return']:+.2f}% przy ekspozycji "
                 f"{ekspozycja_por:.1%} vs kup-i-trzymaj {odniesienie:+.2f}% → "
                 f"przy tej samej ekspozycji {odniesienie_dop:+.2f}%]"
                 if porownywalne_okno else
                 "  [zerowa ekspozycja — poza porównaniem z odniesieniem]")
    if informative:
        znak = {True: "tak", False: "nie", None: "—"}
        mark += "  bije " + " ".join(
            f"{n}={znak[bije_okna[n]]}" for n in ("B1", "B2", "B3"))
    if oos_res.killswitch_at:
        mark += f"  [KILL-SWITCH {oos_res.killswitch_at:%m-%d %H:%M}]"
    if oos_m["blocked"]:
        mark += f"  odrzuconych wejść: {oos_m['blocked']}"
    # POWROTY IDA DO WYDRUKU, A NIE TYLKO DO `learn.wf_metric`.
    #
    # Miara istnieje od 2026-09-27 i odpowiada na pytanie, czy EXIT_RATIO
    # = 0,45 nie wypycha nas z pozycji, w ktora zaraz wracamy. Zapisana
    # WYLACZNIE do bazy odpowiadalaby na nie dopiero po nocnym przebiegu
    # i tylko temu, kto zna nazwe metryki - a `--sucho` nie zapisuje nic,
    # wiec porownanie wariantow (po to `--sucho` jest) tej liczby nie
    # widzialo wcale. Warunek rozstrzygajacy postawiono ZAWCZASU: udzial
    # powyzej 40% w oknach `range` o co najmniej 55 transakcjach i ponizej
    # 15% w `range` o mniej niz 55 - a do jego sprawdzenia potrzebne sa
    # wartosci PO OKNACH, z rezimem i liczba transakcji w tej samej linii.
    powroty = (f"  powroty={agg['udzial_powtornych_wejsc']:.0%}"
               if "udzial_powtornych_wejsc" in agg else "")
    # BRUTTO PO `reżim=`, nie przed: `scripts/porownaj_warianty.py` czyta
    # `Sharpe/transakcję=... transakcje=... reżim=...` jednym wzorcem
    # (WZOR_OKNA), wiec liczba w nim jest ta po doplacie z karty, a dopisek
    # trafia do ogona, ktory skrypt tylko przeszukuje.
    brutto = (f"  [brutto: Sharpe/transakcję={oos_m_brutto['sharpe']:+.4f}, "
              f"zwrot {oos_m_brutto['total_return']:+.2f}%]"
              if doplata and informative else "")
    return (f"  okno {idx:>2d}  {test_from:%m-%d}–{test_to:%m-%d}  "
            f"Sharpe/transakcję={agg['sharpe']:+.4f}  transakcje={n_trades}  "
            f"reżim={dominant}{powroty}{brutto}{mark}")


def _zapisz_oos(cur, conn, sucho: bool, model_id: int | None,
                oos_wg_konfiguracji: dict[bytes, list[float]]) -> None:
    # --- Sharpe OOS do learn.hypothesis_trial ------------------------------
    #
    # Kolumna `oos_sharpe` istnieje od migracji 0006 i do 2026-09-27 byla pusta
    # w 16 050 wierszach z 16 050 (zmierzone na produkcji). Bez niej `V[SR_n]`
    # Baileya nie mial skad sie wziac i `statystyka.deflowany_sharpe` podstawial
    # w to miejsce wariancje samego estymatora pod H0 - droga optymistyczna,
    # opisana tam wprost.
    #
    # PO PETLI, NIE W NIEJ. Konfiguracja moze wygrac w kilku oknach; wtedy
    # `oos_sharpe` ma byc SREDNIA z tych okien, a `n_windows` ich liczba.
    # UPDATE wykonywany okno po oknie nadpisywalby wartosc poprzednia i zapisal
    # wynik ostatniego okna pod nazwa wyniku konfiguracji.
    if not sucho and oos_wg_konfiguracji:
        zapisz_oos_sharpe(cur, model_id, oos_wg_konfiguracji)
        conn.commit()
        print(f"\n  Sharpe OOS zapisany dla {len(oos_wg_konfiguracji)} "
              f"konfiguracji (learn.hypothesis_trial.oos_sharpe)")


def kalibracja(predictions: list[tuple[float, bool]], cur, mv_id: int | None) -> float | None:
    """ECE deklarowanej pewnosci; koszyki do `learn.calibration_bin` przy bazie."""
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
            if cur is None:
                continue
            cur.execute(
                "INSERT INTO learn.calibration_bin (model_version_id, evaluated_at, bin_lo,"
                " bin_hi, predicted_mean, observed_rate, n_samples)"
                " VALUES (%s, now(), %s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (mv_id, lo, lo + 0.1, round(pred, 4), round(obs, 4), len(bucket)))
    return ece


def dryf_cech(cur, instruments: list[dict], h: Horizon, fp: str, now: datetime,
              mv_id: int | None) -> float:
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
    dryfy: list[dict] = []
    id_instrumentow = [i["id"] for i in instruments]
    granica_nowe = now - timedelta(days=DRYF_OKNO_NOWE_DNI)
    granica_stare = granica_nowe - timedelta(days=DRYF_OKNO_STARE_DNI)
    print(f"dryf cech: okno nowe {DRYF_OKNO_NOWE_DNI} dni wobec "
          f"poprzedzających {DRYF_OKNO_STARE_DNI}, świeca {h.bar}, "
          f"odcisk uniwersum {fp}")
    if cur is None:
        # Dryf czyta surowe `feat.feature_value` poza panelem - z pliku go nie
        # ma i nie jest potrzebny: przebieg z pliku nic nie zapisuje.
        print("  (dryf cech pominięty — przebieg z pliku panelu)")
        definicje_cech = []
    else:
        cur.execute(
            "SELECT id, name FROM feat.feature_def"
            " WHERE active AND params->>'bar' = %s"
            "   AND (params->>'uniwersum' IS NULL OR params->>'uniwersum' = %s)",
            (h.bar, fp))
        definicje_cech = cur.fetchall()
    for fd in definicje_cech:
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
        dryfy.append({"feature": fd["name"], "psi": psi})
        cur.execute(
            "INSERT INTO learn.drift_metric (model_version_id, feature_def_id, event_time, psi)"
            " VALUES (%s,%s, now(), %s) ON CONFLICT DO NOTHING",
            (mv_id, fd["id"], round(psi, 4)))
    # KTORE CECHY, a nie samo maksimum (poprawka D, 2026-10-05). Produkcja
    # meldowala "dryf 2,49" i nic poza tym - kto chcial wiedziec, skad ta liczba,
    # musial pytac `learn.drift_metric` recznie. Ta sama regula co w tresci
    # alarmu dozoru (`podsumuj_dryf_cech`), zeby log i powiadomienie nie
    # wymienialy roznych cech.
    if dryfy:
        dryfy.sort(key=lambda w: w["psi"], reverse=True)
        print("  najwyższy dryf: "
              + ", ".join(podsumuj_dryf_cech(dryfy, limit=DRYF_CECH_W_LOGU)))
    return max_psi


def _mianowniki_dsr(cur, plik: dict | None, model_id: int | None, mv_id: int | None,
                    params_przebiegu: dict):
    """(trials, sd_prob, konfiguracji_z_oos, n_selekcji) - z bazy albo z pliku."""
    if plik is not None:
        trials = plik["dsr"]["trials"]
    else:
        cur.execute("SELECT count(*) AS n FROM learn.hypothesis_trial WHERE model_id=%s", (model_id,))
        trials = cur.fetchone()["n"]
    if plik is not None:
        sd_prob, konfiguracji_z_oos = plik["dsr"]["odchylenie"]
        n_selekcji = n_konfiguracji_z_listy(plik["dsr"]["konfiguracje"],
                                            params_przebiegu)
    else:
        sd_prob, konfiguracji_z_oos = odchylenie_prob_z_bazy(cur, model_id)
        n_selekcji = n_konfiguracji(cur, model_id, mv_id)
    return trials, sd_prob, konfiguracji_z_oos, n_selekcji


@dataclass(frozen=True)
class Status:
    """Werdykt przebiegu: status nauki, zdanie dla czlowieka i miary pod nim."""
    status: str
    reason: str
    opis_dsr: str
    total: int
    positive: int
    pct: float
    median: float | None
    ci_lo: float
    ci_hi: float
    odn_lo: float
    odn_hi: float
    dsr: float | None = None


def werdykt(z: Przebieg, *, karta: Karta, n: int, trials: int, sd_prob,
            konfiguracji_z_oos: int) -> Status:
    """Status, powod i DSR z miar wszystkich okien. CZYSTA FUNKCJA.

    `n` - liczba konfiguracji (mianownik selekcji DSR), `trials` - licznik
    `learn.hypothesis_trial` (tylko do opisu), `sd_prob` / `konfiguracji_z_oos`
    - rozrzut Sharpe'ow miedzy konfiguracjami (`odchylenie_prob_z_bazy`).
    """
    all_sharpes, positive = z.all_sharpes, z.positive
    wszystkie_zwroty = z.wszystkie_zwroty
    pokonane_odniesienie, porownywalne = z.pokonane_odniesienie, z.porownywalne
    ekspozycje, zwroty_okien = z.ekspozycje, z.zwroty_okien
    total = len(all_sharpes)
    pct = (positive / total * 100) if total else 0.0
    median = statistics.median(all_sharpes) if all_sharpes else None
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
    # 3. ROZRZUT MIEDZY KONFIGURACJAMI - nie miedzy oknami i nie miedzy
    #    przebiegami. Czytany po DISTINCT `learn.model_version.params`, czyli po
    #    TEJ SAMEJ wielkosci, ktora jest N w punkcie 2; inaczej odejmowalibysmy
    #    klatwe zwyciezcy policzona dla innej mnogosci niz ta, ktora ja
    #    wytworzyla. Gdy konfiguracji z policzonym Sharpe'em jest mniej niz
    #    `MIN_PROB_ROZRZUTU`, `odchylenie_prob_z_bazy` oddaje None i zostaje
    #    podstawienie z H0 - OPISANE jako podstawienie, a nie przemilczane.
    #
    #    KIERUNEK: rozrzut konfiguracji jest WIEKSZY (albo rowny) niz rozrzut
    #    estymatora, wiec poprzeczka idzie W GORE, a DSR W DOL. SKALA jest mala
    #    i to tez trzeba podac, bo do 2026-09-27 wieczorem stala tu liczba
    #    dziesieciokrotnie za duza: 0,0266 wobec podlogi 0,0260, czyli 2,7%.
    #    Liczba 0,2627, ktora tu wczesniej byla, to rozrzut miedzy OKNAMI -
    #    zmiennosc rynku plus szum estymacji z jednego okna, nie konfiguracje.
    #    Gdyby pomiar wyszedl PONIZEJ podlogi, `deflowany_sharpe` bierze podloge
    #    i to mowi: pomiar mniejszy od samego szumu jest dowodem niepelnosci.
    dsr_ok, opis_dsr = werdykt_dsr(
        wszystkie_zwroty, doplata=karta.doplata_poza_symulacja,
        n=n, trials_kumulatywnie=trials,
        nieznane=karta.nieznane, odchylenie_prob=sd_prob,
        konfiguracji_z_oos=konfiguracji_z_oos, wejscia=z.wejscia_transakcji)

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
    pob_b2, por_b2 = z.bije_odn["B2"]
    udzial_b2 = pob_b2 / por_b2 if por_b2 else None
    blokuje = kryteria_blokujace(total, positive, median, bije_odniesienie, dsr_ok,
                                 udzial_b2)

    if total < MIN_WINDOWS_FOR_VALIDATION:
        status = "learning"
        reason = (f"Za mało informatywnych okien: {total} z wymaganych "
                  f"{MIN_WINDOWS_FOR_VALIDATION}. Strategia zawiera zbyt mało transakcji, "
                  f"żeby cokolwiek orzec — to nie jest wynik ujemny, to brak wyniku. "
                  + opis_dsr)
    else:
        status = status_walidacji(total, positive, median, bije_odniesienie, dsr_ok,
                                  udzial_b2)
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
        # KTORE KRYTERIUM BLOKUJE - wprost, przy kazdym statusie poza
        # `validated` (audyt Fable F-3.4): sam status nie mowi, czego brakuje.
        if blokuje:
            skala += " Blokuje: " + "; ".join(blokuje) + "."
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
    dsr = przewaga_dsr(zwroty_po_doplacie(wszystkie_zwroty, karta.doplata_poza_symulacja),
                       n, sd_prob, z.wejscia_transakcji).get("dsr")
    return Status(status, reason, opis_dsr, total, positive, pct, median,
                  ci_lo, ci_hi, odn_lo, odn_hi, dsr)


@dataclass(frozen=True)
class Produkcja:
    """Parametry, ktore dostaje produkcja: progi per rezim i wagi.

    Od 2026-10-07 to PARA Z OSTATNIEGO OKNA walk-forward (patrz
    `parametry_produkcji`). Mediany po oknach zostaja obok jako SLAD
    (`*_mediana_okien`) - do logu i do `params`, nie do handlu.
    """
    prod_progi: dict[str, float]
    prod_okien: dict[str, int]
    prod_enter: float
    prod_weights: dict[str, dict[str, float]]
    bez_progu_w_produkcji: list[str]
    linie: list[str]
    # Slad zmiany z 2026-10-07: to, co produkcja dostawala do tej daty.
    progi_mediana_okien: dict[str, float] = field(default_factory=dict)
    wagi_mediana_okien: dict[str, dict[str, float]] = field(default_factory=dict)
    # Ile obserwacji stoi za progiem kazdego rezimu w oknie, z ktorego pochodzi para.
    prod_proby: dict[str, int] = field(default_factory=dict)


def _mediana_wag(weight_history: list[dict],
                 martwe_w_oknie: list[set[str]]) -> dict[str, dict[str, float]]:
    """Mediana wag po oknach, osobno dla sygnalu i rezimu, liczona WYLACZNIE
    po oknach, w ktorych sygnal zyl. Od 2026-10-07 tylko SLAD - patrz
    `parametry_produkcji`.

    MEDIANA LICZONA WYLACZNIE PO OKNACH, W KTORYCH SYGNAL ZYL.

    Sygnal martwy na oknie dostaje w nim wage zero - ale to zero nie jest
    pomiarem "waga powinna byc zerowa", tylko BRAKIEM POMIARU. Wliczanie go
    do mediany jest tym samym, co usrednianie brakow danych jako zer
    (`news_shock` bez klucza Anthropic: mediana z 39 okien przestalaby byc
    zerem dopiero po 20 zywych oknach, czyli po 600 dniach historii newsow).
    """
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
    return prod_weights


def parametry_produkcji(z: Przebieg) -> Produkcja:
    """Progi i wagi dla produkcji z historii okien. CZYSTA FUNKCJA; `linie`
    to wydruk (rezimy bez progu, progi dla produkcji, slad zmiany).

    PRODUKCJA GRA PARA Z OSTATNIEGO OKNA (2026-10-07).

    Do tej daty produkcja dostawala MEDIANE WAG po oknach (osobno
    znormalizowana) i MEDIANE PROGOW po oknach. Kazdy prog okna jest
    percentylem 0,88 wyniku liczonego wagami TEGO okna - a mediana wag to
    wektor, ktorego nie mialo zadne okno. Wagi IC roznych okien stawiaja na
    rozne sygnaly (czesto z przeciwnym znakiem), wiec ich mediana skleja kilka
    sygnalow, ktore sie wzajemnie znosza: wynik produkcji jest wyraznie
    mniejszy niz wynik, na ktorym liczono prog. Tej pary (mediana wag +
    mediana progow) nie symulowal nikt. Zmierzone na produkcji 2026-10-07,
    wersja v171, rezim `range`: prog 0,341 przy srednim |wyniku| ~0,049
    (~7x, przy p88 oczekiwane ~2x) - 0 wejsc na 18 decyzji.

    Teraz produkcja dostaje DOKLADNIE to, co walk-forward mierzyl w ostatnim
    oknie: wagi tego okna i progi policzone na tych wagach, na jego oknie
    treningowym. To protokol walk-forward przedluzony o jeden krok - para jest
    ta sama, ktora symulacja OOS ostatniego okna zagrala, wiec czestotliwosc
    wejsc jest z konstrukcji tym zamiarem (`ENTER_PCTL`, z H-6 na score > 0).
    Cena: jedno okno to jedno losowanie (`argmax`) albo jedna estymacja IC -
    mniej odpornosci niz w medianie. Dla `ic` to estymacja na najswiezszych
    `train_days` dniach, czyli dokladnie ta, ktora walk-forward co tydzien
    ocenia OOS; dla `argmax` klatwa zwyciezcy jest ta sama, ktora juz
    mierzy kazde okno.

    REZIM BEZ PROGU W OSTATNIM OKNIE NIE DOSTAJE PROGU W PRODUKCJI - nawet
    jesli mialy go wczesniejsze okna. Ta sama zasada co dotad
    (`bramki.MIN_PROB_PROGU`): bez kalibracji nie wchodzimy. Lista idzie do
    `reason` przez `bez_progu_w_produkcji`.
    """
    historia_bramek, okna_bez_progu = z.historia_bramek, z.okna_bez_progu
    weight_history, martwe_w_oknie = z.weight_history, z.martwe_w_oknie
    linie: list[str] = []
    # SLAD: mediana z okien, osobno dla kazdego rezimu - to, co produkcja
    # dostawala do 2026-10-07. Wypisywane i zapisywane obok, zeby roznica
    # "stary vs nowy prog" byla widoczna w logu i w `params` kazdej wersji.
    progi_mediana = {rezim: round(statistics.median(lista), 6)
                     for rezim, lista in historia_bramek.items()}
    wagi_mediana = _mediana_wag(weight_history, martwe_w_oknie)
    # Ile okien kalibrowalo dany rezim - kontekst stabilnosci progu (panel
    # i `enter_threshold_windows`), tylko dla rezimow z progiem w produkcji.
    okien_rezimu = {rezim: len(lista) for rezim, lista in historia_bramek.items()}

    ostatnie: Bramki | None = z.bramki_okien[-1] if z.bramki_okien else None
    prod_weights: dict[str, dict[str, float]] = {}
    prod_progi: dict[str, float] = {}
    prod_proby: dict[str, int] = {}
    prod_enter = PROG_WEJSCIA_POCZATKOWY
    if ostatnie is not None and weight_history:
        # Wagi okna sa juz znormalizowane (suma |w| = 1, `ic_weights`,
        # `_bez_martwych`, `random_weights`); zaokraglenie jak dotad.
        # `combine` i tak dzieli przez sume |wag| obecnych sygnalow, wiec
        # skala wektora nie zmienia wyniku - zmienia go wylacznie PROPORCJA,
        # a ta jest dokladnie ta, na ktorej liczono prog.
        prod_weights = {r: {k: round(float(v), 4) for k, v in w.items()}
                        for r, w in weight_history[-1].items()}
        for rezim in REGIMES:
            prog = ostatnie.wejscie(rezim)
            if prog is not None:
                prod_progi[rezim] = round(prog, 6)
                prod_proby[rezim] = int(ostatnie.prob.get(rezim, 0))
        # `enter_threshold` - prog zapasowy WYJSCIA dla rezimow bez kalibracji
        # i wartosc dla starszych czytnikow (`/learning/wagi`, migracja 0040).
        # Wejsc nim nie wolno (`bramki.Bramki.wejscie`). Z tego samego okna:
        # percentyl zlaczonej proby, z ktorego `bramki_z_prob` liczy zapas.
        if sum(ostatnie.prob.values()) >= MIN_PROB_PROGU:
            prod_enter = round(ostatnie.wyjscie_zapasowe / EXIT_RATIO, 6)
    prod_okien = {r: okien_rezimu.get(r, 1) for r in prod_progi}

    if okna_bez_progu:
        opis_brakow = ", ".join(f"{r}: {n}" for r, n in sorted(okna_bez_progu.items()))
        linie.append(f"  reżimy bez progu wejścia (próba poniżej {MIN_PROB_PROGU} "
              f"obserwacji), liczba okien — {opis_brakow}")
    bez_progu_w_produkcji = [r for r in REGIMES
                             if r not in prod_progi
                             and (okna_bez_progu.get(r) or r in progi_mediana)]
    # PROGI OBOWIAZUJACE PRODUKCJE WYPISUJEMY WPROST, z liczba probek i ze
    # starym progiem (mediana okien) obok - slad zmiany z 2026-10-07.
    if prod_progi:
        opis_progow = ", ".join(
            f"{r}={prod_progi[r]:.4f} (n={prod_proby[r]}"
            + (f"; mediana okien {progi_mediana[r]:.4f}" if r in progi_mediana else "")
            + ")"
            for r in REGIMES if r in prod_progi)
        linie.append(f"\nProgi wejścia dla produkcji (ostatnie okno, wagi tego okna, "
                     f"percentyl {ENTER_PCTL:.2f}): {opis_progow}")
    wycofane = [r for r in REGIMES if r in progi_mediana and r not in prod_progi]
    if wycofane:
        linie.append(f"  reżimy z progiem w medianie okien, ale BEZ progu w ostatnim "
                     f"oknie (produkcja w nich nie wchodzi): {', '.join(wycofane)}")
    return Produkcja(prod_progi, prod_okien, prod_enter, prod_weights,
                     bez_progu_w_produkcji, linie,
                     progi_mediana_okien=progi_mediana,
                     wagi_mediana_okien=wagi_mediana, prod_proby=prod_proby)


def zapisz_wynik(cur, conn, sucho: bool, engine: str, h: Horizon,
                 model_id: int | None, mv_id: int | None, run_id: int | None,
                 w: Status, prod: Produkcja, *, worst_dd: float, ece: float | None,
                 max_psi: float, trials: int, zgoda_na_promocje: bool) -> str:
    """Parametry produkcji, bramka promocji, `training_run`, stan nauki
    i jego dziennik. Zwraca `reason` z dopiskami bramki (do wydruku)."""
    status, reason, total, positive = w.status, w.reason, w.total, w.positive
    median, ci_lo, ci_hi = w.median, w.ci_lo, w.ci_hi
    prod_enter, prod_progi, prod_okien = prod.prod_enter, prod.prod_progi, prod.prod_okien
    prod_weights, bez_progu_w_produkcji = prod.prod_weights, prod.bez_progu_w_produkcji
    # W trybie SUCHYM nie dotykamy bazy w ogole: porownanie wariantow nie moze
    # promowac wersji modelu ani nadpisywac stanu nauki. Podsumowanie i tak
    # sie wypisuje - po to ten tryb istnieje.
    if not sucho:
        cur.execute(
            "UPDATE learn.model_version SET params = params || %s::jsonb WHERE id = %s",
            (json.dumps({"enter_threshold": prod_enter,
                         "exit_threshold": round(prod_enter * EXIT_RATIO, 6),
                         # PROGI, KTORYMI PRODUKCJA NAPRAWDE WCHODZI.
                         "enter_threshold_per_regime": prod_progi,
                         # Ile okien stoi za kazda z tych median. Prog z dwoch
                         # okien i prog z czterdziestu wygladaja w JSON-ie
                         # identycznie, a nie sa tym samym pomiarem.
                         "enter_threshold_windows": prod_okien,
                         "min_confidence": MIN_CONF,
                         "weights": prod_weights,
                         # SLAD ZMIANY Z 2026-10-07 (`parametry_produkcji`):
                         # liczba obserwacji za progiem w oknie, z ktorego
                         # pochodzi para, i to, co produkcja dostalaby po
                         # staremu (mediany po oknach). Nie czyta tego petla.
                         "enter_threshold_proby": prod.prod_proby,
                         "enter_threshold_per_regime_mediana_okien":
                             prod.progi_mediana_okien,
                         "weights_mediana_okien": prod.wagi_mediana_okien}),
             mv_id))

        # BRAMKA PROMOCJI. Do 2026-09-08 w tym miejscu stalo bezwarunkowe
        # wycofanie poprzedniej wersji i promocja nowej dla kazdego wyniku poza
        # `degraded` - patrz komentarz przy `status_nowej_wersji`.
        tryb = tryb_handlu(cur, engine)
        cur.execute("SELECT params FROM learn.model_version"
                    " WHERE model_id=%s AND id<>%s AND status='active'", (model_id, mv_id))
        aktywna = cur.fetchone()
        cur.execute("SELECT params FROM learn.model_version WHERE id=%s", (mv_id,))
        zmiana = zmiana_struktury(aktywna["params"] if aktywna else None,
                                  cur.fetchone()["params"])
        if zmiana:
            # Kolumna (0090), nie klucz w `params`: `params` jest mianownikiem N.
            cur.execute("UPDATE learn.model_version SET zmiana_struktury=%s WHERE id=%s",
                        (zmiana, mv_id))
            print(f"  ZMIANA STRUKTURY względem aktywnej wersji: {zmiana}")
        docelowy, powod_bramki = status_nowej_wersji(status, tryb, zgoda_na_promocje,
                                                     zmiana)
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
        # REZIMY BEZ PROGU IDA DO `reason`. Produkcja nie bedzie w nich wchodzic,
        # a silnik bez wejsc wyglada tak samo jak silnik na spokojnym rynku -
        # wiec jesli tego nie napisze zakladka stanu nauki, nie napisze tego nic.
        if bez_progu_w_produkcji:
            reason += (" BEZ WEJŚĆ w reżimach: "
                       + ", ".join(bez_progu_w_produkcji)
                       + f" — próba poniżej {MIN_PROB_PROGU} obserwacji w każdym "
                         f"oknie, w którym reżim wystąpił, więc próg wejścia nie "
                         f"został skalibrowany.")
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
    elif cur is not None:
        cur.execute("UPDATE learn.training_run SET status='done', finished_at=now(),"
                    " error='tryb suchy - porownanie wariantow' WHERE id=%s", (run_id,))
    _utrwal(conn, sucho)
    return reason


def trzymanie_transakcji(trzymanie: list[tuple[int, bool]]) -> dict | None:
    """Rozklad czasu trzymania w swiecach. CZYSTA FUNKCJA (H-7).

    Dwie populacje, bo zadna sama nie jest uczciwa: wyjscia SYGNALEM to
    prawdziwy czas trzymania, ale bez pozycji domknietych koncem okna mediana
    jest zanizona (odpadaja wlasnie najdluzsze); wszystkie razem zawieraja
    trzymania UCIETE na granicy okna, wiec ich mediana to dolne oszacowanie.
    Prawda lezy miedzy nimi - stad obie liczby i liczba ucietych."""
    if not trzymanie:
        return None
    wszystkie = sorted(n for n, _ in trzymanie)
    sygnalem = sorted(n for n, koniec in trzymanie if not koniec)

    def kwartyle(lista: list[int]) -> list[float] | None:
        if not lista:
            return None
        if len(lista) == 1:
            return [float(lista[0])] * 3
        q = statistics.quantiles(lista, n=4, method="inclusive")
        return [q[0], float(statistics.median(lista)), q[2]]

    return {"n": len(wszystkie), "ucietych_koncem_okna": len(wszystkie) - len(sygnalem),
            "sygnalem_q1_med_q3": kwartyle(sygnalem),
            "wszystkie_q1_med_q3": kwartyle(wszystkie)}


def opis_trzymania(t: dict | None, h: Horizon) -> list[str]:
    """Linie logu: czas trzymania wobec horyzontu etykiety IC (H-7)."""
    etykieta = (f"horyzont etykiety IC {h.ic_horizon_bars} świec "
                f"({h.ic_horizon_bars * h.bar_minutes / 60:g} h)")
    if t is None:
        return [f"Czas trzymania (OOS): brak transakcji; {etykieta}"]

    def q(x: list[float] | None) -> str:
        return "—" if x is None else f"{x[1]:g} świec (q1 {x[0]:g}, q3 {x[2]:g})"
    return [f"Czas trzymania (OOS, {t['n']} transakcji): mediana wyjść sygnałem "
            f"{q(t['sygnalem_q1_med_q3'])}; ze {t['ucietych_koncem_okna']} "
            f"domkniętymi końcem okna (dolne oszacowanie) {q(t['wszystkie_q1_med_q3'])}",
            f"  wobec: {etykieta} — wagi uczą się zwrotu na tym horyzoncie, "
            f"a pozycja trzyma tyle, ile wyżej"]


def wypisz_podsumowanie(z: Przebieg, w: Status, k: Koszty, *, kind: str, wybor: str,
                        idx: int, opis_dsr: str, trials: int,
                        reason: str, h: Horizon) -> tuple[float | None, float | None]:
    """Wydruk podsumowania przebiegu. Zwraca (wypelnialnosc, koszt niewypelnienia)."""
    status, total, positive, pct, median = w.status, w.total, w.positive, w.pct, w.median
    ci_lo, ci_hi, odn_lo, odn_hi = w.ci_lo, w.ci_hi, w.odn_lo, w.odn_hi
    wybrane_ic, porownywalne = z.wybrane_ic, z.porownywalne
    pokonane_odniesienie, pokonane_pelne = z.pokonane_odniesienie, z.pokonane_pelne
    ekspozycje, rozklad = z.ekspozycje, z.rozklad
    wypelnione_oos, niewypelnione_oos = z.wypelnione_oos, z.niewypelnione_oos
    utracone_oos, brak_swiec_oos = z.utracone_oos, z.brak_swiec_oos
    bije_odn, b0_dodatnie, b0_4_pobite = z.bije_odn, z.b0_dodatnie, z.b0_4_pobite
    positive_brutto, all_sharpes_brutto = z.positive_brutto, z.all_sharpes_brutto
    pokonane_odniesienie_brutto, worst_dd = z.pokonane_odniesienie_brutto, z.worst_dd
    wszystkie_zwroty, zwroty_okien_transakcje = z.wszystkie_zwroty, z.zwroty_okien_transakcje
    transakcje_stron, transakcje_pokrycia = z.transakcje_stron, z.transakcje_pokrycia
    sharpe_grup, udzial_instrumentu = z.sharpe_grup, z.udzial_instrumentu
    egzekucja, karta, doplata, pyl = k.egzekucja, k.karta, k.doplata, k.pyl
    spread_bps, spread_nasza_ksiega = k.spread_bps, k.spread_nasza_ksiega
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
    wypelnialnosc = wypelnialnosc_symulacji(wypelnione_oos, niewypelnione_oos)
    koszt_niewyp = None
    if egzekucja.limit and egzekucja.pesymistyczna:
        # SKLADNIK `niewypelnienie` STAJE SIE POMIAREM. Liczony z symulacji,
        # ktora niewypelnienia sama odgrywa, wiec do doplaty nie wchodzi
        # (`niewypelnienie_w_symulacji`) - karta wypisana jeszcze raz, zeby
        # bylo widac, ze skladnik ma wartosc i skad.
        koszt_niewyp = koszt_niewypelnienia(wypelnialnosc, utracone_oos)
        karta_po = karta_rundy(kind, True, spread_bps=spread_bps,
                               spread_nasza_ksiega=spread_nasza_ksiega, pyl=pyl,
                               niewypelnienie=koszt_niewyp,
                               niewypelnienie_w_symulacji=True)
        print("Wypełnialność zleceń z limitem w symulacji OOS: "
              + (f"{wypelnialnosc:.1%}" if wypelnialnosc is not None else "—")
              + f" ({wypelnione_oos} weszło, {niewypelnione_oos} nie; w tym "
              f"{brak_swiec_oos} bez świecy 1h) — wejścia i wyjścia razem, "
              f"NIE do porównania z demo")
        for linia in opis_wypelnialnosci_wejsc(z, egzekucja, kind):
            print(linia)
        for linia in opis_karty(karta_po):
            print(linia)
    elif egzekucja.limit:
        print(f"Niewypełnione zlecenia z limitem w symulacji OOS: "
              f"{niewypelnione_oos} (wejścia + wyjścia, wszystkie okna). "
              f"Ich KOSZT nie jest w dopłacie — w karcie stoi jako NIEZMIERZONE.")
    if z.poza_sesja_oos:
        print(f"Zlecenia NIEZŁOŻONE poza sesją giełdy w symulacji OOS: "
              f"{z.poza_sesja_oos} ({z.poza_sesja_wejsc_oos} wejść, "
              f"{z.poza_sesja_oos - z.poza_sesja_wejsc_oos} wyjść; jedno na ciąg "
              f"świec poza sesją 9:30–16:00 Nowego Jorku dla instrumentu albo "
              f"pozycji) — pętla takiej decyzji nie podejmuje; poza wypełnialnością")
    if egzekucja.limit and kind in ("spot", "spot_eu", "perp"):
        # BRAMA 0 (renacode-infra: docs/projekty/trader/08-przed-go-live.md, 2026-10-11) w kazdym przebiegu crypto z limitem: stawka
        # z cennika (odczyt z konta DEMO), spread z tej samej karty. Koszt
        # niewypelnienia z DEMO trener nie ma - liczba z symulacji to inna
        # wielkosc - wiec stoi jako niezmierzony; przy prowizji 50 pb
        # werdykt i tak nie zalezy od niego.
        for linia in opis_bramy_0(brama_0(
                koszty(kind).maker, stawka_konta_docelowego=False,
                spread_pb=spread_bps, spread_nasza_ksiega=spread_nasza_ksiega,
                niewypelnienie_pb=None)):
            print(linia)
    # ODNIESIENIA B0-B3 - zbiorczo. Kryterium STOP/GO 1 (renacode-infra: docs/projekty/trader/hipotezy.md):
    # bije B2 przy tej samej ekspozycji w >= 60% okien.
    for nazwa in ("B1", "B2", "B3"):
        pob, por = bije_odn[nazwa]
        if por:
            lo, hi = wilson(pob, por)
            print(f"Bije {nazwa} przy tej samej ekspozycji (przenoszonej): "
                  f"{pob}/{por} okien ({pob / por:.0%}, przedział 95% {lo:.0%}–{hi:.0%})")
        else:
            print(f"Bije {nazwa}: brak porównania (brak odniesienia w uniwersum "
                  f"albo okien z ekspozycją)")
    if b0_dodatnie[1]:
        print(f"B0 (gotówka): zwrot > 0 w {b0_dodatnie[0]}/{b0_dodatnie[1]} okien; "
              f"ponad 4% w skali roku na CAŁYM kapitale w {b0_4_pobite}/{b0_dodatnie[1]}")
    # BRUTTO OBOK, NIE ZAMIAST. Gdy doplaty nie ma, obie wersje sa identyczne
    # i wiersz bylby szumem.
    if doplata and total:
        print(f"  brutto (przed dopłatą {doplata * 1e4:.2f} pb, NIE do bramki): "
              f"dodatnie {positive_brutto}/{total}, mediana Sharpe "
              f"{statistics.median(all_sharpes_brutto):+.3f}"
              + (f", kup-i-trzymaj przy tej samej ekspozycji pokonane w "
                 f"{pokonane_odniesienie_brutto}/{porownywalne}"
                 if porownywalne else ""))
    # DSR ZAWSZE RAZEM Z N I LICZBA PROB - `bramka_dsr` sklada to w jedno zdanie
    # i to samo zdanie idzie do `reason`, zeby wydruk i baza nie mowily dwoch
    # roznych rzeczy o tym samym pomiarze.
    print(f"Deflowany Sharpe: {opis_dsr}")
    print(f"Najgorsze obsunięcie: {worst_dd:.2f}%")
    # BRUTTO, i to jest kontrakt `opis_przewagi`: koszt progowy i drabina
    # potrzebuja zera, a miary po doplacie funkcja liczy sama z tej samej karty.
    for linia in opis_przewagi(wszystkie_zwroty, all_sharpes_brutto, karta,
                               zwroty_okien_transakcje):
        print(linia)
    for linia in opis_stron(transakcje_stron):
        print(linia)
    for linia in opis_pokrycia(transakcje_pokrycia):
        print(linia)
    for linia in opis_trzymania(trzymanie_transakcji(z.trzymanie), h):
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
    return wypelnialnosc, koszt_niewyp


def podsumowanie_przebiegu(z: Przebieg, w: Status, k: Koszty, *, days: int, now: datetime,
                           n_selekcji: int, sd_prob, wypelnialnosc: float | None,
                           koszt_niewyp: float | None) -> dict:
    """PODSUMOWANIE_JSON przebiegu. CZYSTA FUNKCJA."""
    egzekucja, karta = k.egzekucja, k.karta
    total, positive, median, status = w.total, w.positive, w.median, w.status
    wszystkie_zwroty, brak_swiec_oos = z.wszystkie_zwroty, z.brak_swiec_oos
    bije_odn, b0_dodatnie, przebieg_odn = z.bije_odn, z.b0_dodatnie, z.przebieg_odn
    # PODSUMOWANIE MASZYNOWE - jedna linia JSON dla `scripts/eksperyment.py`
    # (tabela przesuniec) i dla porownan wariantow. Te same liczby co wyzej,
    # bez parsowania zdan. Zwrot na runde PO doplacie z karty, w pb.
    po_doplacie = zwroty_po_doplacie(wszystkie_zwroty, karta.doplata_poza_symulacja)
    wynik_dsr = przewaga_dsr(po_doplacie, n_selekcji, sd_prob, z.wejscia_transakcji)
    podsumowanie = {
        "days": days, "od": f"{now - timedelta(days=days):%Y-%m-%d %H:%M}",
        "do": f"{now:%Y-%m-%d %H:%M}",
        "pesymistyczna": egzekucja.pesymistyczna,
        "opoznienie_min": egzekucja.opoznienie_min, "limit": egzekucja.limit,
        "okna": total, "dodatnie": positive,
        "transakcji": len(wszystkie_zwroty),
        "zwrot_na_runde_pb": (statistics.fmean(po_doplacie) * 1e4
                              if po_doplacie else None),
        "doplata_pb": karta.doplata_poza_symulacja * 1e4,
        "dsr": wynik_dsr.get("dsr"), "n_selekcji": n_selekcji,
        "mediana_sharpe": median,
        "wypelnialnosc": wypelnialnosc,
        # Same wejscia - te liczbe porownuje z demo H-5 (patrz
        # `opis_wypelnialnosci_wejsc`); `wypelnialnosc` wyzej to wejscia + wyjscia.
        "wypelnialnosc_wejsc": wypelnialnosc_symulacji(z.wypelnione_wejsc_oos,
                                                       z.niewypelnione_wejsc_oos),
        "przebicie_pb": egzekucja.przebicie_bps,
        "wypelnialnosc_docelowa": egzekucja.wypelnialnosc_docelowa,
        "niewypelnienie_pb": koszt_niewyp * 1e4 if koszt_niewyp is not None else None,
        "brak_swiec_1h": brak_swiec_oos,
        "poza_sesja": z.poza_sesja_oos,
        "bije_b1": bije_odn["B1"], "bije_b2": bije_odn["B2"], "bije_b3": bije_odn["B3"],
        "b0_dodatnie": b0_dodatnie, "status": status,
        # H-7: czas trzymania w swiecach (q1, mediana, q3) - patrz
        # `trzymanie_transakcji`.
        "trzymanie_swiec": trzymanie_transakcji(z.trzymanie),
        # {nazwa: [sredni zwrot % w oknie, srednia ekspozycja, okna z zwrotem > 0, okna]}
        "odniesienia": {n: ([statistics.fmean(z for z, _ in v),
                             statistics.fmean(e for _, e in v),
                             sum(1 for z, _ in v if z > 0), len(v)] if v else None)
                        for n, v in przebieg_odn.items()},
    }
    return podsumowanie


# STALA SIATKA OKIEN I PIEC PRZESUNIEC (audyt Fable F-2.2 / T4, 2026-10-03).
#
# Do tego dnia siatka zaczynala sie w `now - --days`, czyli przesuwala sie
# o dobe z kazdym nocnym przebiegiem. Werdykt zalezal od tego, gdzie wypadly
# granice okien, nie od danych: DSR 0,9155-0,979 wokol progu 0,95 przy
# przesunieciu o dobe, 13 zmian statusu w 35 przebiegach (`learning_state_log`).
#
# Teraz granice sa zakotwiczone w kalendarzu: KOTWICA_SIATKI + k * step_days,
# pierwszy punkt nie wczesniej niz poczatek panelu. Dwa przebiegi w odstepie
# doby licza te same okna (nowe okno dochodzi raz na krok siatki). Werdykt
# liczy sie dla przesuniec siatki o {-2..+2} dni w jednym przebiegu, a status
# przyjmuje wartosc z MINIMUM: `validated` tylko wtedy, gdy przechodzi kazde
# przesuniecie (czyli minimum kazdej miary przekracza prog), `degraded` tylko
# wtedy, gdy kazde przesuniecie jest `degraded`. Wszystko pomiedzy to `learning`.
#
# Wagi, progi produkcji, okna w `learn.wf_window` i Sharpe OOS do
# `hypothesis_trial` pochodza z przesuniecia 0. Pozostale przesuniecia sa
# WYLACZNIE pomiarem odpornosci werdyktu: nie dokladaja konfiguracji (ta sama
# metoda na przesunietych granicach, nie nowa hipoteza), nie zapisuja niczego.
KOTWICA_SIATKI = datetime(2022, 9, 1, tzinfo=timezone.utc)
PRZESUNIECIA_SIATKI = (-2, -1, 0, 1, 2)


def poczatek_siatki(kotwica: datetime | None, od_panelu: datetime,
                    step_days: int) -> datetime:
    """Pierwszy punkt `kotwica + k * step_days` nie wczesniej niz `od_panelu`.
    CZYSTA FUNKCJA. `kotwica=None` - siatka od `od_panelu` (zachowanie sprzed
    T4, tylko do pomiarow `--sucho`)."""
    if kotwica is None:
        return od_panelu
    krok = timedelta(days=step_days)
    return kotwica + math.ceil((od_panelu - kotwica) / krok) * krok


def bramki_na_wagach(kt: Kontekst, dane: DaneOkna,
                     wagi: dict[str, dict[str, float]]) -> Bramki:
    """Progi wejscia per rezim na wagach, KTORYMI SIE HANDLUJE (audyt Fable F-2.1).

    Do 2026-10-03 progi liczyly sie na `bazowe` (zalozenia poczatkowe po
    wyzerowaniu martwych sygnalow) takze przy `--wybor ic`, a symulacja
    i produkcja handlowaly wagami IC. Uzasadnienie - jeden prog dla wszystkich
    kandydatow w oknie, zeby zaden nie dostal progu dobranego pod siebie -
    dotyczy `argmax`; przy `ic` kandydat jest jeden. Skutek zmierzony po
    28.09: udzial decyzji ponad progiem `high_vol` 10,8%, `trend_up` 8,2%,
    `range` 0,0% (n=72) wobec zamiaru 12%. Wagi IC sa z okna TRENINGOWEGO,
    wiec prog na nich nie zaglada w przyszlosc.
    """
    return bramki_okna(dane.train_slices, wagi, dane.thresholds, kt.h,
                       dane.cache_tr, per_rezim=kt.progi_per_rezim,
                       tylko_dluga=kt.tylko_dluga)


def walk_forward(kt: Kontekst, okna: list[Okno], *, wybor: str, trials_per_window: int,
                 zapisz_hipoteze, glowny: bool, zapis_okna=None) -> tuple[Przebieg, int]:
    """Petla okien jednej siatki. `glowny` - wydruk okien (przesuniecie 0);
    `zapis_okna(idx, okno, m)` - zapis do bazy, None przy `--sucho`
    i przesunieciach pomiarowych. Zwraca (liczniki, liczba ocenionych okien)."""
    rng = random.Random(SEED)
    z = Przebieg()
    idx = 0
    for okno in okna:
        dane = przygotuj_okno(kt, okno, z_bramkami=(wybor != "ic"))
        if glowny and dane.martwe and idx == 0:
            print(f"  sygnały bez zmienności na danych, wyłączone z kalibracji: "
                  f"{', '.join(sorted(dane.martwe))}")
        best_w, z_ic = kandydat_wag(
            dane.train_slices, dane.cache_tr, dane.martwe, wybor, bazowe=dane.bazowe,
            ocen=_ocena_treningowa(kt, dane), zapisz_hipoteze=zapisz_hipoteze,
            rng=rng, trials_per_window=trials_per_window,
            horyzont_swiec=kt.h.ic_horizon_bars, tylko_dluga=kt.tylko_dluga)
        if dane.gates is None:
            dane = dc_replace(dane, gates=bramki_na_wagach(kt, dane, best_w))
        if glowny and idx == 0:
            rodzaj = "per reżim" if kt.progi_per_rezim else "WSPÓLNY dla reżimów"
            print(f"  progi wejścia ({rodzaj}, percentyl {ENTER_PCTL:.2f}, "
                  f"min. {MIN_PROB_PROGU} próbek): {dane.gates.opis()}")
        z.wybrane_ic += z_ic
        if glowny:
            z.ic_okien.append(ic_sygnalow(
                {s: v for s, v in dane.test_slices.items() if v}, dane.cache_te,
                kt.h.ic_horizon_bars, swieca_wejscia(kt)))
        m = ocen_okno(kt, okno, dane, best_w)
        z.dodaj_wypelnienia(m)
        if m.pusty:
            continue
        z.dodaj(m, okno, kt.h, kt.waluta_symbolu, best_w)
        if zapis_okna is not None:
            zapis_okna(idx, okno, m)
        z.dodaj_progi(dane.gates, best_w, dane.martwe)
        if glowny:
            print(opis_okna(idx, okno, m, kt.h, kt.doplata))
        idx += 1
    return z, idx


def _min_sr_max(wartosci: list[float]) -> tuple[float, float, float] | None:
    return (min(wartosci), statistics.fmean(wartosci), max(wartosci)) if wartosci else None


def werdykt_stabilny(statusy: dict[int, Status]) -> tuple[str, str]:
    """Jeden status z przesuniec siatki: (status, zdanie). CZYSTA FUNKCJA.

    `validated` wylacznie, gdy `validated` jest KAZDE przesuniecie - to samo,
    co "minimum kazdej miary przekracza prog", bo `status_walidacji` jest
    koniunkcja progow. `degraded` wylacznie, gdy `degraded` jest kazde.
    Inaczej `learning`. Przy jednym przesunieciu - jego status, bez zdania.
    """
    wszystkie = list(statusy.values())
    if len(wszystkie) == 1:
        return wszystkie[0].status, ""
    rodzaje = [w.status for w in wszystkie]
    if all(r == "validated" for r in rodzaje):
        status = "validated"
    elif all(r == "degraded" for r in rodzaje):
        status = "degraded"
    else:
        status = "learning"
    n = len(wszystkie)
    liczniki = ", ".join(f"{r} {rodzaje.count(r)}/{n}"
                         for r in ("validated", "learning", "degraded") if r in rodzaje)
    pct = _min_sr_max([w.pct for w in wszystkie if w.total])
    dsr = _min_sr_max([w.dsr or 0.0 for w in wszystkie])
    zakres = f"{min(statusy):+d}…{max(statusy):+d}"
    zdanie = (f"Werdykt z {n} przesunięć siatki okien ({zakres} dni): {liczniki}"
              + (f"; okna dodatnie min/śr/max {pct[0]:.0f}/{pct[1]:.0f}/{pct[2]:.0f}%"
                 if pct else "")
              + (f"; DSR min/śr/max {dsr[0]:.4f}/{dsr[1]:.4f}/{dsr[2]:.4f}" if dsr else "")
              + f" → status {status} (zmienia się tylko, gdy MINIMUM z przesunięć "
                f"przekracza próg).")
    return status, zdanie


def opis_przesuniec(statusy: dict[int, Status], liczniki: dict[int, Przebieg]) -> list[str]:
    """Tabela przesuniec siatki do wydruku."""
    linie = ["Przesunięcia siatki okien (dni): okna, dodatnie, bije B2, DSR, status"]
    for s in sorted(statusy):
        w, z = statusy[s], liczniki[s]
        pob, por = z.bije_odn["B2"]
        b2 = f"{pob}/{por}" if por else "—"
        linie.append(f"  {s:+d}: {w.total:>3} okien, dodatnie {w.positive:>3} "
                     f"({w.pct:.0f}%), bije B2 {b2}, DSR "
                     + (f"{w.dsr:.4f}" if w.dsr is not None else "—") + f", {w.status}")
    return linie


def train(conn, engine: str, days: int, trials_per_window: int, h: Horizon,
          kind: str = "spot", egzekucja: Egzekucja | None = None,
          sucho: bool = False, wybor: str = "argmax",
          tylko: set[str] | None = None,
          zakladane_pozycje: int | None = None,
          zgoda_na_promocje: bool = False,
          spread_bps: float | None = None,
          spread_nasza_ksiega: bool = True,
          progi_per_rezim: bool = True,
          panel: dict | None = None,
          od: datetime | None = None,
          przesuniecia: tuple[int, ...] = (0,),
          hipoteza: str | None = None,
          horyzont_etykiety: int | None = None) -> dict:
    """Walk-forward jednego toru: orkiestrator etapow opisanych wyzej.

    `od` - kotwica siatki okien (`--od`, produkcja: KOTWICA_SIATKI); None to
    siatka od `now - days`, jak przed T4. `przesuniecia` - przesuniecia siatki
    w dniach, dla ktorych liczy sie werdykt (produkcja: PRZESUNIECIA_SIATKI);
    musi zawierac 0 - z niego pochodza wagi, progi i zapisy.
    `horyzont_etykiety` - horyzont zwrotu w IC w swiecach (`--horyzont-etykiety`,
    H-7); None = `h.ic_horizon_bars`.
    """
    if 0 not in przesuniecia:
        raise ValueError("przesuniecia siatki musza zawierac 0")
    if horyzont_etykiety is not None:
        if horyzont_etykiety < 1:
            raise ValueError("horyzont etykiety musi miec co najmniej 1 swiece")
        h = dc_replace(h, ic_horizon_bars=horyzont_etykiety)
    strona = strona_uczenia(kind)
    # `panel` - dane z `panel_plik.wczytaj` (harness offline, `--panel`).
    # Wtedy NIC nie idzie do bazy: `conn` moze byc None, a wszystko, co
    # przebieg z bazy czyta, przychodzi z pliku. Zapisy pomijamy, bo przebieg
    # z pliku jest z definicji suchy.
    plik = panel
    if plik is not None and not sucho:
        raise ValueError("przebieg z pliku panelu (--panel) jest zawsze suchy")
    cur = conn.cursor() if plik is None else None
    now = datetime.now(timezone.utc) if plik is None else plik["do"]
    egzekucja = egzekucja or Egzekucja()
    z_godzin = bool(egzekucja.opoznienie_min or egzekucja.pesymistyczna)

    # Zapas panelu przed `now - days`, zeby przesuniecia ujemne mialy dane.
    zapas = max(0, -min(przesuniecia))
    instruments = _uniwersum(cur, plik, engine, h, days + zapas, tylko)
    k = _koszty(cur, plik, engine, kind, h, instruments, now, days, egzekucja,
                spread_bps, spread_nasza_ksiega, zakladane_pozycje)
    egzekucja = k.egzekucja
    fp, waluta_symbolu = _przekroj(cur, plik, instruments, h, days, tylko)
    params = params_przebiegu(h, trials_per_window, wybor, kind, progi_per_rezim,
                              egzekucja, strona)
    model_id, mv_id, run_id = _zaloz_wersje(cur, conn, plik, sucho, engine, h, fp,
                                            params, now, days)
    panels, swiece_wykonania, inst_ids = _wczytaj_panele(
        cur, plik, instruments, h, fp, now, days + zapas, z_godzin)
    _sprawdz_panele(cur, conn, sucho, plik, panels, inst_ids, h, now, run_id)

    kt = Kontekst(h, kind, egzekucja, zakladane_pozycje, swiece_wykonania, inst_ids,
                  panels, waluta_symbolu, k.doplata, k.doplata_odniesienia, k.karta,
                  progi_per_rezim, tylko_dluga=strona == STRONA_DLUGA)
    print(f"  uczenie: {'TYLKO strona długa (rynek bez shortów, H-6)' if strona else 'obie strony'}"
          f"; horyzont etykiety IC {h.ic_horizon_bars} świec "
          f"({h.ic_horizon_bars * h.bar_minutes / 60:g} h)")
    start = poczatek_siatki(od, now - timedelta(days=days), h.step_days)
    if od is not None:
        print(f"  siatka okien zakotwiczona: {od:%Y-%m-%d} + k×{h.step_days} dni, "
              f"pierwsze okno od {start:%Y-%m-%d %H:%M}; przesunięcia "
              f"{', '.join(f'{p:+d}' for p in sorted(przesuniecia))} dni")
    if egzekucja.wypelnialnosc_docelowa is not None:
        # PRZEBIEG KALIBRACYJNY (H-10): ta sama siatka, samo dotkniecie, bez
        # zapisow. Z niego tylko przebicie - wynik tego przebiegu nie wchodzi
        # nigdzie, bo liczono go INNYM modelem wypelnien niz werdykt.
        kal, _ = walk_forward(kt, okna_wf(panels, h, start, now), wybor=wybor,
                              trials_per_window=trials_per_window,
                              zapisz_hipoteze=lambda w: None, glowny=False)
        przebicie, linie = kalibracja_wypelnien(kal, egzekucja.wypelnialnosc_docelowa)
        for linia in linie:
            print(linia)
        egzekucja = dc_replace(egzekucja, przebicie_bps=przebicie)
        kt = dc_replace(kt, egzekucja=egzekucja)
        k = dc_replace(k, egzekucja=egzekucja)
        # Liczniki kalibracji nie sa juz potrzebne, a tor 1h stoi przy limicie
        # pamieci (values.yaml, trainerCron.resources) - nie trzymac ich przez
        # piec kolejnych walk-forwardow.
        del kal
    zapis = (None if sucho else
             lambda i, okno, m: zapisz_okno(cur, conn, run_id, i, okno, m))
    z, idx = walk_forward(kt, okna_wf(panels, h, start, now), wybor=wybor,
                          trials_per_window=trials_per_window,
                          zapisz_hipoteze=_zapis_hipotez(cur, sucho, model_id),
                          glowny=True, zapis_okna=zapis)
    for linia in opis_ic_sygnalow(z.ic_okien, h):
        print(linia)
    liczniki = {0: z}
    for p in sorted(set(przesuniecia) - {0}):
        liczniki[p], _ = walk_forward(
            kt, okna_wf(panels, h, start + timedelta(days=p), now), wybor=wybor,
            trials_per_window=trials_per_window, zapisz_hipoteze=lambda w: None,
            glowny=False)

    _zapisz_oos(cur, conn, sucho, model_id, z.oos_wg_konfiguracji)
    ece = kalibracja(z.predictions, cur, mv_id)
    max_psi = dryf_cech(cur, instruments, h, fp, now, mv_id)
    trials, sd_prob, konfiguracji_z_oos, n_selekcji = _mianowniki_dsr(
        cur, plik, model_id, mv_id, params)
    statusy = {p: werdykt(zp, karta=k.karta, n=n_selekcji, trials=trials, sd_prob=sd_prob,
                          konfiguracji_z_oos=konfiguracji_z_oos)
               for p, zp in liczniki.items()}
    stabilny, zdanie = werdykt_stabilny(statusy)
    w = statusy[0]
    if zdanie:
        w = dc_replace(w, status=stabilny,
                       reason=f"{zdanie} Przesunięcie 0: {w.reason}")
    prod = parametry_produkcji(z)
    for linia in prod.linie:
        print(linia)
    reason = zapisz_wynik(cur, conn, sucho, engine, h, model_id, mv_id, run_id, w, prod,
                          worst_dd=z.worst_dd, ece=ece, max_psi=max_psi, trials=trials,
                          zgoda_na_promocje=zgoda_na_promocje)
    if zdanie:
        for linia in opis_przesuniec(statusy, liczniki):
            print(linia)
    wypelnialnosc, koszt_niewyp = wypisz_podsumowanie(
        z, w, k, kind=kind, wybor=wybor, idx=idx, opis_dsr=w.opis_dsr, trials=trials,
        reason=reason, h=h)
    podsumowanie = podsumowanie_przebiegu(
        z, w, k, days=days, now=now, n_selekcji=n_selekcji, sd_prob=sd_prob,
        wypelnialnosc=wypelnialnosc, koszt_niewyp=koszt_niewyp)
    if zdanie:
        podsumowanie["od_siatki"] = f"{od:%Y-%m-%d}" if od is not None else None
        podsumowanie["przesuniecia"] = {
            f"{p:+d}": podsumowanie_przebiegu(
                liczniki[p], statusy[p], k, days=days, now=now, n_selekcji=n_selekcji,
                sd_prob=sd_prob, koszt_niewyp=None,
                wypelnialnosc=wypelnialnosc_symulacji(liczniki[p].wypelnione_oos,
                                                      liczniki[p].niewypelnione_oos))
            for p in sorted(liczniki)}
        for p in liczniki:
            podsumowanie["przesuniecia"][f"{p:+d}"]["start_siatki"] = (
                f"{start + timedelta(days=p):%Y-%m-%d %H:%M}")
        podsumowanie["stabilny"] = {
            "status": stabilny,
            "dsr": _min_sr_max([statusy[p].dsr or 0.0 for p in statusy]),
            "dodatnie_pct": _min_sr_max([statusy[p].pct for p in statusy if statusy[p].total]),
        }
    podsumowanie["strona"] = strona or "obie"
    podsumowanie["horyzont_etykiety_swiec"] = h.ic_horizon_bars
    if hipoteza:
        podsumowanie["hipoteza"] = hipoteza
    print("PODSUMOWANIE_JSON " + json.dumps(podsumowanie, ensure_ascii=False))
    return podsumowanie



# REJESTR HIPOTEZ (audyt Fable F-3.3 / T8). Kazdy `--sucho` mierzy jakas
# hipoteze; bez numeru przebieg porownawczy nie zostawia sladu, PRZED ktorym
# zapisano kryterium - a wtedy "wariant wygral" da sie powiedziec o dowolnym.
# Rejestr zyje w prywatnym repo (renacode-infra: docs/projekty/trader/hipotezy.md);
# szukany w kopii roboczej obok tego repo. Bez niej (obraz, CI) sprawdzany
# jest tylko format numeru - tak jak dotad w obrazie.
REJESTR_HIPOTEZ = (Path(__file__).resolve().parents[3] / "renacode-infra" / "docs"
                   / "projekty" / "trader" / "hipotezy.md")
WZOR_HIPOTEZY = r"H-[A-Za-z0-9]+(?:\.[0-9]+)?"


def sprawdz_hipoteze(hipoteza: str, rejestr: Path = REJESTR_HIPOTEZ) -> str | None:
    """Blad do wypisania albo None. Numer musi miec postac `H-n` i - gdy
    rejestr jest dostepny - wiersz w tabeli rejestru hipotez
    (renacode-infra: docs/projekty/trader/hipotezy.md)."""
    import re
    if not re.fullmatch(WZOR_HIPOTEZY, hipoteza or ""):
        return f"--hipoteza: oczekiwano numeru w postaci H-n, dostałem {hipoteza!r}"
    if rejestr.exists():
        tresc = rejestr.read_text(encoding="utf-8")
        if not re.search(rf"^\|\s*{re.escape(hipoteza)}\s*\|", tresc, re.M):
            return (f"--hipoteza {hipoteza}: brak wiersza w {rejestr.name} - "
                    f"zapisz hipotezę i kryterium PRZED przebiegiem")
    return None


def _sprawdz_tor(ap: argparse.ArgumentParser, args) -> None:
    """Cennik toru i tor zamkniety (services/engine/tory.py).

    Trener na torze zamknietym liczy dalej (H-4, dryf), ale wersji `active` -
    jedynej, ktora wolno wpuscic na live - nie nadaje. Promocja wpuszcza
    wersje na live, wiec cennik toru musi byc jawny: domysl `spot` omijalby
    zamkniecie toru `spot_eu`. Ustawia `args.kind`, gdy nie podano.
    """
    if args.kind is None:
        if args.zgoda_na_promocje:
            ap.error("--zatwierdz-promocje wymaga jawnego --kind (cennik toru)")
        args.kind = "spot"
    tor_zamkniety = zamkniecie(args.engine, args.bar, args.kind)
    if tor_zamkniety and args.zgoda_na_promocje:
        ap.error(f"--zatwierdz-promocje odmówione: {tor_zamkniety.opis()}")
    if tor_zamkniety:
        print(f"TOR ZAMKNIĘTY: {tor_zamkniety.opis()}. Przebieg liczy się dalej "
              f"(seria H-4, dryf); żadna wersja nie zostanie promowana.")


def _sprawdz_argumenty(ap: argparse.ArgumentParser, args) -> tuple[datetime | None,
                                                               tuple[int, ...]]:
    """Odmowy wariantow bez `--sucho` i rozbior `--od` / `--przesuniecia`."""
    if args.sucho or args.panel:
        blad = sprawdz_hipoteze(args.hipoteza)
        if blad:
            sys.exit(blad)
    try:
        od = (None if args.od == "teraz"
              else datetime.fromisoformat(args.od).replace(tzinfo=timezone.utc))
        przesuniecia = tuple(sorted({int(p) for p in args.przesuniecia.split(",")
                                     if p.strip()}))
    except ValueError as e:
        sys.exit(f"--od / --przesuniecia: {e}")
    if 0 not in przesuniecia:
        sys.exit("--przesuniecia musza zawierac 0 (z niego pochodza wagi i progi)")
    # Kotwica i przesuniecia sa METODA POMIARU werdyktu produkcji - wariant
    # wolno zmierzyc, nie wolno nim promowac (ten sam powod co przy --wybor).
    if ((args.od != ap.get_default("od") or przesuniecia != PRZESUNIECIA_SIATKI)
            and not args.sucho and not args.panel):
        sys.exit("--od / --przesuniecia inne niz domyslne wymagaja --sucho")

    if args.panel and not args.sucho:
        print("--panel: przebieg z pliku jest zawsze suchy (--sucho wymuszone)")
        args.sucho = True
    if (args.pesymistyczna or args.opoznienie_min) and not args.limit:
        sys.exit("--egzekucja-pesymistyczna i --opoznienie dotycza zlecen "
                 "z limitem - podaj --limit")
    # MODEL PESYMISTYCZNY W PRZEBIEGU PRODUKCYJNYM (2026-10-07, H-5).
    #
    # Do tej daty obie flagi wymagaly `--sucho` ("wariant pomiaru, nie
    # produkcja"). Powod blokady: przebieg z innym modelem wypelnien zapisuje
    # wersje modelu, a do 2026-10-05 wersja w demo promowala sie sama - jeden
    # przebieg porownawczy zmienialby po cichu progi, ktorymi handluje
    # produkcja. H-1 zostawila wlaczenie w `trainerCron` decyzji wlasciciela
    # ("w CronJobie on po akceptacji", audyt T1); decyzja zapadla 2026-10-06/07
    # (demo trenuje algorytm przed live).
    #
    # INTENCJA BLOKADY ZOSTAJE, ZMIENIA SIE MECHANIZM. Ciche wejscie do produkcji
    # zamyka dzis bramka struktury: `params_przebiegu` zapisuje
    # `egzekucja_pesymistyczna` i `opoznienie_min`, wiec pierwsza taka wersja ma
    # inny zestaw kluczy niz aktywna (`zmiana_struktury`) i zostaje `candidate`,
    # a licznik N (`n_konfiguracji`) liczy ja jako osobna konfiguracje.
    # Promocja tylko recznie, `--zatwierdz-promocje` (poprawka F).
    #
    # Bez `--sucho` WYLACZNIE PELNY MODEL Z H-1 (obie flagi naraz, opoznienie
    # > 0). Polowki - sama cena z opoznieniem albo sam pol spreadu i godzina -
    # zostaja wariantami pomiaru: zaden pomiar nie opisal ich jako modelu
    # zblizonego do produkcji.
    pelny_pesymistyczny = args.pesymistyczna and args.opoznienie_min > 0
    if ((args.pesymistyczna or args.opoznienie_min) and not pelny_pesymistyczny
            and not args.sucho):
        sys.exit("bez --sucho model pesymistyczny tylko w calosci: "
                 "--egzekucja-pesymistyczna RAZEM z --opoznienie N (N > 0); "
                 "pojedyncza flaga to wariant pomiaru i wymaga --sucho")
    if args.opoznienie_min < 0:
        sys.exit("--opoznienie nie moze byc ujemne")
    # KALIBRACJA PRZEBICIA (H-10) tylko na pelnym modelu pesymistycznym: przebicie
    # dotyczy godziny po wysylce, a bez niej wejscie ma cala swiece 4h i liczba
    # skalibrowana na jednym modelu opisywalaby inny.
    if args.wypelnialnosc_docelowa is not None:
        if not pelny_pesymistyczny:
            sys.exit("--wypelnialnosc-docelowa wymaga --egzekucja-pesymistyczna "
                     "razem z --opoznienie N (N > 0)")
        if not 0.0 < args.wypelnialnosc_docelowa <= 1.0:
            sys.exit("--wypelnialnosc-docelowa to ulamek z (0, 1], np. 0.656")
    if args.horyzont_etykiety is not None and args.horyzont_etykiety < 1:
        sys.exit("--horyzont-etykiety musi byc >= 1 swieca")

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
    # I ten sam: prog wspolny to arytmetyka sprzed 2026-09-27. Wolno nim
    # ZMIERZYC roznice, nie wolno nim wytrenowac modelu do handlu.
    if args.progi != ap.get_default("progi") and not args.sucho:
        sys.exit(f"--progi inne niz {ap.get_default('progi')} wymaga --sucho")
    return od, przesuniecia


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1400)
    ap.add_argument("--trials", type=int, default=40)
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--bar", default=DEFAULT_BAR, choices=sorted(HORIZONS))
    # "stock" dolaczylo 2026-09-24 razem z torem equity. Cennik akcji na Alpace
    # to zero prowizji - patrz `koszty.CENNIK`. Trener MUSI kosztowac tyle, ile
    # kosztuje strategia, ktora kalibruje: tor equity liczony cennikiem Bybita
    # (tak powstaly liczby w renacode-infra: docs/projekty/trader/08-przed-go-live.md) mierzyl oplaty, ktorych to konto nie placi.
    # Domysl `spot` ustawiany po parsowaniu - przy `--zatwierdz-promocje`
    # `--kind` jest obowiazkowy (patrz tor zamkniety nizej).
    ap.add_argument("--kind", default=None,
                    choices=("spot", "spot_eu", "perp", "stock"),
                    help="rodzaj instrumentu - wyznacza stawki prowizji; "
                         "spot_eu to cennik podmiotu unijnego (taker 0,250%%); "
                         "domyślnie spot, przy --zatwierdz-promocje obowiązkowy")
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
                    help="AWARYJNE NADPISANIE pomiaru z bazy. Od 2026-09-27 "
                         "spread liczy sie SAM, z cechy book_spread_bps, na tym "
                         "samym uniwersum i w tym samym oknie, co trening "
                         "(services/engine/spread.py). Ta flaga sluzy do "
                         "policzenia WARIANTU albo do przebiegu na bazie bez "
                         "arkusza; podana liczba wchodzi jako PELNY spread "
                         "w punktach bazowych na runde (polowa na noge razy dwie "
                         "nogi - dokladnie to, co zawiera cecha). Gdy jest "
                         "podana, o pochodzeniu decyduje --spread-obca-ksiega, "
                         "bo pomiar nie ma jak tego stwierdzic za czlowieka. "
                         "Nie wpisywac liczby 'na oko'")
    # ZGODA CZLOWIEKA NA PODMIANE MODELU - od 2026-10-05 w kazdym trybie,
    # takze w demo (`status_nowej_wersji`, poprawka F).
    #
    # Flaga przebiegu, nie ustawienie w bazie i nie wpis w chart: zadanie
    # cykliczne jej NIE MA i miec nie moze, bo caly sens bramki polega na tym,
    # ze nocna automatyka nie potrafi jej podniesc. Podnosi ja czlowiek,
    # uruchamiajac trening recznie, raz.
    ap.add_argument("--zatwierdz-promocje", action="store_true",
                    dest="zgoda_na_promocje",
                    help="pozwol promowac nowa wersje modelu (w demo i na zywo); "
                         "bez tego wersja zostaje kandydatem, a produkcja "
                         "handluje dalej poprzednia")
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
    ap.add_argument("--progi", default="per-rezim",
                    choices=("per-rezim", "wspolny"),
                    help="na jakiej populacji liczy sie percentyl progu wejscia: "
                         "per-rezim = osobny rozklad |wyniku| dla kazdego rezimu "
                         "(od 2026-09-27 produkcja), wspolny = jeden rozklad dla "
                         "wszystkich rezimow (do 2026-09-27). Wariant `wspolny` "
                         "sluzy WYLACZNIE do zmierzenia roznicy na tych samych "
                         "oknach i wymaga --sucho; zmierzono, ze przepuszczal "
                         "7,0%% sygnalow w `range` i 34,0%% w `high_vol` przy "
                         "zamiarze 12%%")
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
    ap.add_argument("--egzekucja-pesymistyczna", action="store_true",
                    dest="pesymistyczna",
                    help="model wypelnien zlecen z limitem zblizony do produkcji "
                         "(audyt Fable R1): limit z ceny 1h po opoznieniu, pol "
                         "spreadu INSTRUMENTU pod/nad cena, wypelnienie tylko "
                         "w godzinie po wysylce. Wymaga --limit; bez --sucho "
                         "tylko razem z --opoznienie N > 0 (H-5)")
    ap.add_argument("--opoznienie", type=int, default=0, dest="opoznienie_min",
                    help="minuty od zamkniecia swiecy decyzyjnej do wysylki "
                         "zlecenia (produkcja: 5-88, mediana po 02.10 do "
                         "zmierzenia; trainerCron: 20, przyjete w H-1, nie zmierzone). Wymaga "
                         "--limit; bez --sucho tylko razem z "
                         "--egzekucja-pesymistyczna")
    # WYPELNIALNOSC WEJSC Z DEMO (H-10). Liczba z pomiaru, nie z wyboru:
    # `poslizg.py` na wejsciach demo. Zmiana to zmiana konfiguracji (`params`),
    # wiec wersja zostaje `candidate`.
    ap.add_argument("--wypelnialnosc-docelowa", type=float, default=None,
                    dest="wypelnialnosc_docelowa",
                    help="wypelnialnosc WEJSC zmierzona na demo (ulamek, np. "
                         "0.656 = 42/64 z E-1). Przebieg kalibracyjny dobiera "
                         "przebicie limitu tak, zeby symulacja wypelniala wejscia "
                         "nie czesciej niz demo (H-10). Wymaga pelnego modelu "
                         "pesymistycznego")
    ap.add_argument("--panel", default="",
                    help="plik z scripts/eksport_paneli.py - przebieg BEZ BAZY "
                         "(harness offline); wymusza --sucho")
    ap.add_argument("--od", default=f"{KOTWICA_SIATKI:%Y-%m-%d}",
                    help="kotwica siatki okien walk-forward (data UTC): granice "
                         "okien = od + k*step_days, niezalezne od godziny "
                         "uruchomienia (audyt Fable F-2.2). `teraz` = siatka od "
                         "now - --days, jak przed 2026-10-03. Inna niz domyslna "
                         "wymaga --sucho")
    ap.add_argument("--przesuniecia",
                    default=",".join(str(p) for p in PRZESUNIECIA_SIATKI),
                    help="przesuniecia siatki w dniach, dla ktorych liczy sie "
                         "werdykt; status z MINIMUM. Musi zawierac 0. Wartosci "
                         "ujemne podawac jako --przesuniecia=-2,-1,0. Inne niz "
                         "domyslne wymagaja --sucho")
    # HORYZONT ETYKIETY (H-7). Domyslnie `Horizon.ic_horizon_bars` (12 h na
    # kazdym horyzoncie) - liczba NIE jest tu zmieniana na podstawie
    # przypuszczenia. Log przebiegu podaje mediane czasu trzymania w tych samych
    # swiecach (`opis_trzymania`); inna wartosc to decyzja po tym pomiarze,
    # z wpisem w renacode-infra: docs/projekty/trader/hipotezy.md. Bez `--sucho` wolno, bo klucz
    # `ic_horizon_bars` w `params` robi z takiej wersji `candidate`.
    ap.add_argument("--horyzont-etykiety", type=int, default=None,
                    dest="horyzont_etykiety",
                    help="horyzont zwrotu, z ktorym koreluja sie sygnaly w wagach "
                         "IC, w swiecach --bar. Domyslnie wartosc horyzontu "
                         "(4h: 3, 1h: 12, czyli 12 h). Zapisywany w params "
                         "(ic_horizon_bars) - inna wartosc niz aktywna zostawia "
                         "wersje jako candidate")
    ap.add_argument("--hipoteza", default="",
                    help="numer hipotezy z renacode-infra: docs/projekty/trader/hipotezy.md (np. H-1), ktora ten "
                         "przebieg mierzy. WYMAGANY przy --sucho i --panel: "
                         "wiersz z kryterium zapisany PRZED przebiegiem")
    args = ap.parse_args()
    od, przesuniecia = _sprawdz_argumenty(ap, args)
    _sprawdz_tor(ap, args)

    plik = panel_plik.wczytaj(args.panel) if args.panel else None
    url = os.environ.get("DATABASE_URL")
    if not url and plik is None:
        sys.exit("DATABASE_URL nie jest ustawiony")

    with (psycopg.connect(url, row_factory=dict_row) if plik is None
          else _BezBazy()) as conn:
        h = get_horizon(args.bar)
        egz = Egzekucja(limit=args.limit, opoznienie_min=args.opoznienie_min,
                        pesymistyczna=args.pesymistyczna,
                        wypelnialnosc_docelowa=args.wypelnialnosc_docelowa)
        if plik is not None:
            print(f"Panel Z PLIKU {args.panel}: {plik['engine']}/{plik['bar']}, "
                  f"{plik['dni']} dni do {plik['do']:%Y-%m-%d %H:%M} UTC — "
                  f"bez połączenia z bazą")
        print(f"Walk-forward: świeca {h.bar}, trening {h.train_days} dni / "
              f"test {h.test_days} dni, krok {h.step_days} dni")
        # KARTA KOSZTU WYPISUJE SIE W `train()`, NIE TUTAJ - I TO JEST WAZNE.
        #
        # Od 2026-09-27 spread jest MIERZONY z bazy na uniwersum tego przebiegu,
        # a uniwersum rozstrzyga sie dopiero w `train()`. Karta wypisana tutaj
        # bylaby karta SPRZED pomiaru: oglaszalaby "spread NIE ZMIERZONO"
        # w przebiegu, ktory chwile pozniej podaje zmierzona liczbe. Dwa rozne
        # rachunki w jednym logu, oba podpisane jako koszt rundy.
        print(f"Egzekucja: {args.kind}, zlecenia "
              f"{'z limitem' if args.limit else 'rynkowe'}")
        print(f"Wybór wag na oknie treningowym: {args.wybor}"
              f"{f' ({args.trials} losowań + założenia + IC)' if args.wybor == 'argmax' else ' (bez losowania)'}\n")
        tylko = {s.strip().upper() for s in args.instrumenty.split(",") if s.strip()}
        if args.zakladane_pozycje:
            print(f"Budżet na pozycję liczony pod {args.zakladane_pozycje} "
                  f"pozycji jednocześnie (wariant, nie produkcja)")
        train(conn, args.engine, args.days, args.trials, h, args.kind, egz,
              sucho=args.sucho, wybor=args.wybor, tylko=tylko or None,
              progi_per_rezim=(args.progi == "per-rezim"),
              zakladane_pozycje=args.zakladane_pozycje or None,
              zgoda_na_promocje=args.zgoda_na_promocje,
              spread_bps=args.spread_bps,
              spread_nasza_ksiega=not args.spread_obca_ksiega,
              panel=plik, od=od, przesuniecia=przesuniecia,
              hipoteza=args.hipoteza or None,
              horyzont_etykiety=args.horyzont_etykiety)
    return 0


class _BezBazy:
    """Zastepca polaczenia w przebiegu z pliku: `train()` nie wola na nim
    niczego (`cur is None`), a kazde przypadkowe uzycie ma wywrocic przebieg,
    a nie cicho pojsc do bazy."""

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    raise SystemExit(main())
