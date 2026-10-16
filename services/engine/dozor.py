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

CZEGO REGULA "ZGLASZAJ ZMIANE" NIE ZLAPIE - I DLACZEGO JEST NA TO OSOBNA CZUJKA

Regula ma jeden skutek uboczny, ktory kosztowal ten projekt tygodnie ciszy:
STAN TRWALE ZLY NIE JEST ZMIANA. Dryf cech, ktory wszedl wysoko i tam ZOSTAL,
przestaje rosnac - a warunek z punktu 5 wymaga wzrostu, wiec od drugiego
przebiegu nie ma o czym mowic. Model handluje na rozkladzie, ktory odjechal od
treningowego, i nikt sie o tym nie dowiaduje.

To najgorsza kategoria ciszy, jaka ten system moze wyprodukowac, bo jest
NIEODROZNIALNA od spokoju. Dlatego dryf ma tutaj drugi, niezalezny tor:
`ocen_dryf_trwaly` + `sprawdz_dryf` (--co dryf). Nie zastepuje punktu 5,
tylko odpowiada na inne pytanie: nie "czy sie zmienilo", a "czy nadal jest zle".
Ma wlasny prog, wlasny warunek utrzymywania sie i wlasny rytm przypominania -
patrz komentarz przy DRYF_TRWALY_PROG.

TEN SAM WZORZEC ZLAPAL POTEM KILL-SWITCHA - I TAM BYL GROZNIEJSZY

Tlumienie w `sprawdz_ryzyko` brzmialo "istnieje juz powiadomienie mlodsze niz
`triggered_at`", a ten warunek raz spelniony jest spelniony na zawsze, bo
`triggered_at` sie nie zmienia. Aktywny bezpiecznik zglaszal sie wiec RAZ,
a potem czujka milczala - przy czym tu stan trwale zly nie znaczyl "model
handluje na odjechanym rozkladzie", tylko "system w ogole nie handluje".

Odpowiedz jest dwutorowa i kazdy tor odpowiada na inne pytanie: rytm
przypominania rosnacy z wiekiem zdarzenia (`ocen_kill_switch`, komentarz przy
KS_PRZYPOMNIENIE_H) mowi, ze cos sie stalo, a log mowi, w jakim STANIE jest
system przy KAZDYM przebiegu, takze w oknach ciszy powiadomien.

KOD WYJSCIA JEST KANALEM O JEDNYM BICIE I MA NIESC JEDNA RZECZ

Pierwsza proba naprawy (2026-09-09) dolozyla trzeci tor: kod wyjscia niezerowy
przez CALY czas blokady, zeby zatrzymany system nie meldowal zdrowia
w `kubectl get jobs`. Rozumowanie bylo dobre, skutek zmierzony na produkcji
2026-09-10 - zly. Zadanie chodzi co 10 minut, wiec kill-switch aktywny od
poltorej doby wyprodukowal 144 padniete Joby na dobe, 144 pody w stanie Error,
tyle samo maili z monitoringu i aplikacje ArgoCD trwale w stanie Degraded.

Wada jest ta sama, ktora tropi caly ten plik, tyle ze w drugim kanale: sygnal,
ktory przychodzi zawsze, przestaje cokolwiek znaczyc. Czerwony Job co dziesiec
minut uczy przewijac czerwone Joby - takze ten jeden, w ktorym cos naprawde
padlo.

Zrodlem bylo obciazenie jednego bitu dwiema roznymi trescami naraz:

    "sprawdzenie NIE MOGLO SIE WYKONAC"   (awaria czujki - wyjatek, brak bazy)
    "sprawdzenie sie wykonalo i widzi zly STAN"

Pierwsza trescia kod wyjscia jest z natury i tego nie da sie zmienic. Druga
byla doklejona - i dla stanu TRWALEGO oraz JUZ ZGLOSZONEGO nie niesie zadnej
nowej informacji, bo powtarza to, co powiedzialo powiadomienie sprzed godzin.

DZISIEJSZA REGULA, JEDNA DLA WSZYSTKICH CZUJEK W TYM PLIKU

    kod wyjscia niezerowy  <=>  czujka W TYM PRZEBIEGU zglosila cos NOWEGO
                                (albo sama sie wywrocila)

czyli kod wyjscia jest SPOJNY z tlumieniem powiadomien, a nie niezalezny od
niego: milczy dokladnie wtedy, gdy milczy kanal powiadomien, i odzywa sie
dokladnie wtedy, gdy tam cos poszlo. Jeden bit, jedna tresc, zero drugiego
mechanizmu do rozjechania - patrz `kod_wyjscia` nizej.

CO ZOSTAJE Z ARGUMENTU "POWIADOMIENIE DA SIE PRZESPAC"

Zostaje w calosci, tylko przeniesione tam, gdzie nie kosztuje 144 alarmow na
dobe. Stan TRWALY jest wypisywany w logu przy KAZDYM przebiegu, wielkimi
literami i z powodem - `kubectl logs` pokazuje go kazdemu, kto spojrzy. Milczy
wylacznie kod wyjscia, i tylko o tym jednym: ze NIC NOWEGO sie nie stalo.

Cisza w logu byla by tu bledem gorszym niz oba poprzednie razem: zadanie
zielone I log bez sladu to stan nieodrozialny od spokoju w obu kanalach naraz.
Dlatego zaden z powrotow zerem ponizej nie jest niemy.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Prog i komplet limitow koniecznych czytamy z risk kernela, a nie przepisujemy
# tutaj: druga kopia rozjechalaby sie z pierwsza przy pierwszej zmianie rytmu
# pomiaru, a czujka pilnowalaby wtedy progu, ktorego nikt juz nie stosuje.
# Uzywa ich `sprawdz_cisze_pomiaru` nizej. Import musi byc PO sys.path.insert,
# bo ten plik bywa uruchamiany jako skrypt.
from services.engine.risk import LIMITY_KONIECZNE, POMIAR_CO_S  # noqa: E402
from services.engine.wznowienie import HISTEREZA, KARENCJA_MIN  # noqa: E402


# ------------------------------------------------------------- kod wyjscia
def kod_wyjscia(zgloszone: int, trwale: int = 0, co: str = "") -> int:
    """Kod wyjscia czujki Z DWOCH LICZNIKOW - i slad w logu, gdy wychodzi zero.

    `zgloszone` - znaleziska, o ktorych czujka odezwala sie W TYM przebiegu
                  (albo odezwalaby sie, gdyby nie `--sucho`);
    `trwale`    - znaleziska realne, ale JUZ ZGLOSZONE, ktorych powiadomienie
                  jest w tlumieniu. Widoczne w logu, nieobecne w kodzie wyjscia.

    DLACZEGO TO JEST FUNKCJA, A NIE `return len(powody)` W KAZDEJ CZUJCE

    Bo caly sens tej reguly siedzi w gwarancji NEGATYWNEJ: zero z tego miejsca
    NIGDY nie jest nieme. Gdyby kazda czujka liczyla kod wyjscia u siebie,
    pierwsza z nich zapomnialaby wypisac powod - i zadanie zakonczone
    powodzeniem przy trwale zatrzymanym systemie wygladaloby dokladnie tak samo
    jak zadanie, ktoremu nic nie dolega. To jest ten sam blad, ktory ten plik
    tropi w kanale powiadomien, wiec nie moze zostac wpuszczony drugimi drzwiami.

    CZEGO TA FUNKCJA NIE WIE

    Nie wie, czy powiadomienie NAPRAWDE wyszlo - `notify()` moze je jeszcze
    odrzucic wlasnym cooldownem z katalogu. Dla czujek uzywajacych tej funkcji
    to nie ma znaczenia: `killswitch.triggered`, `engine.down`, `instrument.
    delisted` i `risk.pomiar_zamilkl` sa `always_on`, wiec katalogowy cooldown
    ich nie dotyczy i tlumienie po tej stronie jest jedynym, jakie istnieje.
    Jedynym wyjatkiem jest `instrument.suspended` (cooldown 3600 s) - patrz
    komentarz w `sprawdz_instrumenty`. (`instrument.status_nieswiezy`, migracja
    0097, jest `always_on` z powtorka liczona w czujce.)
    """
    if trwale:
        # Zero MUSI powiedziec, dlaczego jest zerem. Inaczej zielone zadanie przy
        # trwale zatrzymanym systemie jest nieodrozialne od zielonego zadania
        # przy systemie zdrowym - a to jest dokladnie ta cisza, ktora ten plik
        # ma likwidowac, nie produkowac.
        print(f"  {trwale} × {co}: stan TRWAŁY i JUŻ ZGŁOSZONY, powiadomienie "
              f"w tłumieniu — kod wyjścia z tego przebiegu tego NIE powtarza "
              f"(opis stanu wyżej; to nie jest zgoda na stan)")
    return zgloszone


# --------------------------------------------------------------- dryf cech
#
# CO TA LICZBA NAPRAWDE MIERZY - bo NIE jest tym, czym sie nazywa.
#
# Kolumna nazywa sie `max_psi`, katalog zdarzen (migracja 0011) obiecuje "PSI
# powyzej 0,25", a dashboard maluje na czerwono powyzej 0,25. Wszystkie trzy
# odwoluja sie do progu z literatury o Population Stability Index (Siddiqi:
# <0,10 bez zmiany, 0,10-0,25 umiarkowana, >0,25 istotna).
#
# Tymczasem `services/engine/train.py` PSI nie liczy. Liczy to:
#
#     d = |srednia_nowa - srednia_stara| / sd_stare
#         + |sd_nowe / sd_stare - 1|
#
# czyli standaryzowane przesuniecie sredniej - dokladnie d Cohena - powiekszone
# o wzgledna zmiane rozrzutu. PSI to suma po kubelkach (p-q)*ln(p/q); ma inna
# konstrukcje i inna skale. Prog przeniesiony z jednej miary na druga nie znaczy
# nic, a wyglada na uzasadniony - i to jest pierwszy powod, dla ktorego liczby
# w tym systemie sie nie zgadzaly.
#
# Skoro miara jest w rdzeniu wielkoscia efektu, wlasciwa jest literatura
# o wielkosci efektu, nie o PSI. Cohen: 0,2 maly, 0,5 sredni, 0,8 duzy. Oba
# progi ponizej sa z tego wyprowadzone, a nie dobrane na oko.

# Prog czujki ZMIANY (punkt 5 w docstringu modulu). Srednia cechy przesunieta
# o cale historyczne odchylenie - efekt wyraznie ponad "duzy" u Cohena. Tak
# wysoko, bo ta czujka odzywa sie po JEDNYM odczycie, a pojedynczy odczyt jest
# halasliwy: trener porownuje ostatnia jedna trzecia okresu z poprzednimi dwiema
# trzecimi tego samego okresu, na szeregu finansowym, gdzie zmiana rezimu o pol
# odchylenia jest zdarzeniem cotygodniowym.
PSI_PROG = 1.0

# Prog czujki STANU TRWALEGO. Efekt "sredni" u Cohena: srednia cechy przesunieta
# o pol historycznego odchylenia. Nizszy niz PSI_PROG i to jest celowe - drugim
# warunkiem jest tu UTRZYMYWANIE SIE, ktore samo w sobie jest dowodem. Jeden
# odczyt 0,7 moze byc artefaktem okna; trzy kolejne noce z rzedu nie sa.
DRYF_TRWALY_PROG = 0.5

# Ile KOLEJNYCH przebiegow musi przekroczyc prog, zanim uznamy stan za trwaly.
#
# Trzy, a nie dwa - z tego samego powodu co SWIEC_ZWLOKI, CYKLI_ZWLOKI
# i MARTWY_PO_MIN nizej. Prog dwoch lezy na gornej granicy normalnego wahania,
# wiec czujka migalaby przy kazdym mocniejszym tygodniu na rynku. Czujka, ktora
# miga, jest tak samo bezuzyteczna jak czujka falszywa: obie ucza odbiorce, ze
# mozna je pomijac.
DRYF_TRWALY_PRZEBIEGOW = 3

# Ile godzin musza obejmowac te przebiegi. NIE JEST TO OZDOBA.
#
# "Trzy przebiegi" i "trzy noce" to nie to samo. Trener bywa uruchamiany
# powtornie tego samego dnia - po awarii, przy nadrabianiu zaleglosci, przy
# recznym przebiegu - i wtedy trzy najnowsze wiersze dziennika moga pochodzic
# z jednej godziny. Liczylyby ten sam odczyt trzy razy i "trwaly" znaczyloby
# "chwilowy". Doba miedzy przebiegami razy dwa odstepy to 48 godzin; ciasniejsze
# okno oznacza, ze czekamy jeszcze jedna noc, i tak ma byc.
DRYF_TRWALY_GODZIN = 48

# Rytm przypominania o stanie, ktory sie nie poprawia.
#
# Ani cisza, ani powtorka przy kazdym przebiegu. Trener chodzi raz na dobe, wiec
# przypominanie przy kazdym przebiegu byloby powiadomieniem codziennym - a takie
# przestaje cokolwiek znaczyc dokladnie tak samo jak cisza. Domyslny cooldown
# zdarzenia w katalogu (86400 s) tego NIE zalatwia: doba to wlasnie odstep
# miedzy przebiegami, wiec przepuszcza kazdy z nich.
#
# Tydzien to najkrotszy odstep, ktory nie jest "przy kazdym przebiegu", i jest
# rzedu czasu potrzebnego na REAKCJE: przetrenowanie modelu na nowym rozkladzie
# to praca na dni, nie na godziny. Przypominanie czesciej, niz da sie
# zareagowac, uczy tylko odklikiwac.
DRYF_PRZYPOMNIENIE_DNI = 7

# Ile cech wymieniamy w tresci powiadomienia. "Cos sie zmienilo" bez wskazania
# CZEGO nie prowadzi do zadnego dzialania - a lista wszystkich cech prowadzi do
# przewijania. Piec czolowych mowi, czy dryf jest skupiony w jednej rodzinie
# cech (do naprawy), czy rozlany po wszystkich (zmiana rezimu rynku).
DRYF_CECH_W_TRESCI = 5

# Ubytek okien informatywnych, ktory traktujemy jako utrate dowodow.
UBYTEK_OKIEN = 0.25


# MIARA, KTORA POLICZONO PRZEBIEG - zeby porownywac tylko liczby tego samego rodzaju.
#
# Od 2026-10-02 trener liczy miary okien (odsetek dodatnich, mediane Sharpe'a)
# PO doplacie z karty kosztu, a wczesniej liczyl je BRUTTO. Pierwszy przebieg po
# wdrozeniu porownany z ostatnim sprzed niego zglaszalby „mediana Sharpe
# zmienila znak" i wyjscie odsetka poza przedzial - zmiane DEFINICJI, nie rynku.
# Tor 1h na 1400 dniach przechodzi przy tym z ~50% na 40,4% dodatnich okien,
# o wlos od progu `degraded`: falszywy alarm bylby prawie pewny.
#
# SKAD WIADOMO, KTORA MIARA - BEZ MIGRACJI. `learn.learning_state_log` nie ma
# kolumny na to, ale ma `model_version_id`, a trener zapisuje od tej zmiany
# metryke okna `doplata_pb` (`learn.wf_metric`) - doplate, przy ktorej miary
# policzono. Wolajacy dociaga ja podzapytaniem (`SQL_STAN_NAUKI`). Brak metryki
# = przebieg sprzed zmiany; zero = liczby identyczne z brutto, wiec to TA SAMA
# miara co dawniej, a nie nowa.
#
# TRZECIA ETYKIETA OD 2026-10-03: karta liczona, ale spread NIEZMIERZONY
# (`zmierz_spread` oddal `bps=None` przy luce w `book_snapshot`), wiec doplata
# wyszla zero. Liczbowo to to samo co brutto - i dlatego jest z brutto
# POROWNYWALNA (`klasa_miary`) - ale nazwana osobno: „brutto" w powiadomieniu
# mowilo, ze przebieg wrocil do starej definicji, a prawda brzmi „zgubilismy
# pomiar spreadu". Rozpoznaje ja metryka `spread_niezmierzony`
# (`train.znacznik_miary`).
MIARA_BRUTTO = "brutto (przed dopłatą z karty)"
MIARA_PO_KARCIE = "po dopłacie z karty kosztu"
MIARA_BEZ_SPREADU = "karta bez pomiaru spreadu (dopłata 0 — liczbowo jak brutto)"

def _metryka_okien(nazwa: str) -> str:
    """Podzapytanie: wartosc metryki okna `nazwa` dla wersji wiersza dziennika
    (`learn.learning_state_log.model_version_id` -> `training_run` ->
    `wf_window` -> `wf_metric`). NULL = przebieg sprzed tej metryki."""
    return ("(SELECT max(m.value)\n"
            "          FROM learn.training_run r\n"
            "          JOIN learn.wf_window w ON w.training_run_id = r.id\n"
            "          JOIN learn.wf_metric m ON m.wf_window_id = w.id\n"
            "         WHERE r.model_version_id = l.model_version_id\n"
            f"           AND m.metric = '{nazwa}')")


# MODEL WYPELNIEN Z METRYKI OKNA, A DLA PRZEBIEGOW SPRZED NIEJ - Z `params`.
# Klucze `egzekucja_pesymistyczna`/`opoznienie_min` sa w `params` wersji od
# H-5 (2026-10-07, `train.params_przebiegu`), metryki okna dopiero od zmiany,
# ktora dopisala je do `train.znacznik_miary`. Przebieg pomiedzy jednym
# a drugim nie moze udawac optymistycznego.
SQL_STAN_NAUKI = f"""
SELECT l.status, l.windows_total, l.windows_positive, l.positive_ci_low,
       l.positive_ci_high, l.median_oos_sharpe, l.max_psi, l.trials_run, l.run_at,
       {_metryka_okien('doplata_pb')} AS doplata_pb,
       {_metryka_okien('spread_niezmierzony')} AS spread_niezmierzony,
       {_metryka_okien('prowizja_pb')} AS prowizja_pb,
       COALESCE({_metryka_okien('egzekucja_pesymistyczna')},
                (SELECT CASE WHEN (mv.params->>'egzekucja_pesymistyczna')::boolean
                             THEN 1 ELSE 0 END
                   FROM learn.model_version mv
                  WHERE mv.id = l.model_version_id)) AS egzekucja_pesymistyczna,
       COALESCE({_metryka_okien('opoznienie_min')},
                (SELECT (mv.params->>'opoznienie_min')::numeric
                   FROM learn.model_version mv
                  WHERE mv.id = l.model_version_id)) AS opoznienie_min,
       {_metryka_okien('wypelnialnosc_docelowa')} AS wypelnialnosc_docelowa
FROM learn.learning_state_log l
WHERE l.engine = %s AND l.bar = %s
ORDER BY l.run_at DESC LIMIT %s
"""

# Ile wierszy wstecz szukamy poprzednika liczonego ta sama miara.
#
# TRENER CHODZI RAZ W TYGODNIU (od ee7a403: `trainerCron.schedule`
# "40 4 * * 0", niedziela 04:40 czasu Warszawy), nie raz na dobe - dawne
# 14 wierszy to wiec CZTERNASCIE TYGODNI, ponad trzy miesiace. Poprzednik
# "tej samej miary" sprzed kwartalu opisuje inny rynek i inny kod: wyjscie
# odsetka poza JEGO przedzial nie mowi nic o ostatnim tygodniu.
#
# 6 wierszy: bez przebiegow recznych to szesc tygodni (poltora miesiaca,
# poltora kroku okna 30-dniowego - nowe okno testowe zdazy dojsc), a przy
# jednym recznym przebiegu na tydzien (tak bylo 04-05.10) nadal okolo trzech
# tygodni, czyli co najmniej dwa planowe. Mniej (np. 3) gubiloby poprzednika
# juz przy jednym tygodniu z dwoma recznymi powtorkami po zmianie miary.
STAN_NAUKI_WSTECZ = 6


# Ile musi sie zmienic doplata miedzy dwoma porownywanymi przebiegami, zeby
# dozor o tym wspomnial - patrz `zmiany`.
PROG_ZMIANY_DOPLATY_PB = 0.5


def _opis_kosztu(wiersz: dict) -> str:
    """Stawka prowizji i model wypelnien, przy ktorych liczono miary okien.

    PROWIZJA I MODEL WYPELNIEN SA CZESCIA DEFINICJI MIARY (2026-10-07).
    05.10 stawka maker `spot_eu` zmienila sie z 0,10% na 0,25% (cba4833), czyli
    runda z 0,2% na 0,5%, przy TYM SAMYM `kind` i `limit` w `params`. 07.10
    trener przeszedl na model pesymistyczny z opoznieniem 20 min (H-5). Kazda
    z tych zmian sama przesuwa odsetek dodatnich okien i mediane Sharpe'a - a bez
    tego rozroznienia pierwszy przebieg po nich (11.10) bylby porownany z 05.10
    jak regres rynku.

    `prowizja_pb` NULL (przebieg sprzed metryki) = stawka nieznana: taki wiersz
    nie jest porownywalny z wierszem, ktory ja zna. Pesymistyczna i opoznienie
    0/NULL = model optymistyczny, jak przed H-5.
    """
    czesci = []
    prowizja = wiersz.get("prowizja_pb")
    if prowizja is not None:
        czesci.append(f"prowizja {float(prowizja):.1f} pb/rundę")
    if wiersz.get("egzekucja_pesymistyczna"):
        czesci.append("wypełnienia pesymistyczne")
    opoznienie = wiersz.get("opoznienie_min")
    if opoznienie:
        czesci.append(f"opóźnienie {float(opoznienie):g} min")
    # H-10 (2026-10-11): przebicie limitu kalibrowane do wypelnialnosci wejsc
    # demo to inny model wypelnien niz samo dotkniecie. 0/NULL = bez kalibracji.
    docelowa = wiersz.get("wypelnialnosc_docelowa")
    if docelowa:
        czesci.append(f"wypełnienia kalibrowane do {float(docelowa):.1%} wejść demo")
    return ", ".join(czesci)


def _miara_doplaty(wiersz: dict) -> str:
    doplata = wiersz.get("doplata_pb")
    if doplata:
        return MIARA_PO_KARCIE
    if doplata is not None and wiersz.get("spread_niezmierzony"):
        return MIARA_BEZ_SPREADU
    return MIARA_BRUTTO


def miara_przebiegu(wiersz: dict) -> str:
    """Ktora definicja miar okien stoi za wierszem dziennika stanu nauki:
    rodzaj doplaty, a od 2026-10-07 takze prowizja i model wypelnien."""
    koszt = _opis_kosztu(wiersz)
    m = _miara_doplaty(wiersz)
    return f"{m}; {koszt}" if koszt else m


def klasa_miary(wiersz: dict) -> str:
    """Ktore przebiegi wolno porownywac liczbami: te o tej samej KLASIE.

    Karta bez pomiaru spreadu ma doplate zero, wiec jej odsetek i mediana sa
    tymi samymi liczbami co brutto - porownanie jest uczciwe. Rozni sie tylko
    nazwa w komunikacie (`miara_przebiegu`). Prowizja i model wypelnien
    (`_opis_kosztu`) wchodza do klasy w calosci: inna stawka albo inny model
    to inna miara.
    """
    m = _miara_doplaty(wiersz)
    m = MIARA_BRUTTO if m == MIARA_BEZ_SPREADU else m
    koszt = _opis_kosztu(wiersz)
    return f"{m}; {koszt}" if koszt else m


def wybierz_poprzedni(wiersze: list[dict]) -> tuple[dict | None, int]:
    """Najnowszy WCZESNIEJSZY przebieg liczony TA SAMA miara co biezacy.

    `wiersze` - najnowszy pierwszy. Oddaje pare: poprzednik (albo None) i liczbe
    przebiegow pominietych po drodze, bo liczone inna miara. Liczba idzie do
    `zmiany`, zeby brak poprzednika z POWODU ZMIANY MIARY nie udawal pierwszego
    przebiegu w historii.
    """
    biezacy = wiersze[0]
    miara = klasa_miary(biezacy)
    pominiete = 0
    for w in wiersze[1:]:
        if klasa_miary(w) == miara:
            return w, pominiete
        pominiete += 1
    return None, pominiete


def zmiany(poprzedni: dict | None, biezacy: dict,
           pominiete_inna_miara: int = 0,
           bezposredni: dict | None = None) -> list[str]:
    """Lista powodow, dla ktorych warto sie odezwac. Pusta = cisza.

    Funkcja jest czysta i to jest celowe: caly werdykt da sie przetestowac bez
    bazy, gieldy i sieci.

    DWA POPRZEDNIKI, BO DWA RODZAJE LICZB

    `poprzedni` - liczony TA SAMA miara co `biezacy` (`wybierz_poprzedni`).
    Z nim porownujemy WYLACZNIE to, co od definicji miary zalezy: odsetek
    dodatnich okien, przedzial ufnosci i znak mediany Sharpe'a.

    `bezposredni` - wiersz tuz przed biezacym, BEZ WZGLEDU na miare (domyslnie
    `poprzedni`). Z nim porownujemy status, liczbe okien i dryf: `windows_total`
    i `max_psi` od miary nie zaleza wcale, a status bramkuje handel - jego
    zmiana jest zdarzeniem niezaleznie od tego, czy wywolal ja rynek, czy nowa
    definicja (wtedy mowimy to w tresci).

    Do 2026-10-03 przebieg bez poprzednika tej samej miary konczyl sie TU
    jednym zdaniem i `return`: status validated -> degraded, ubytek polowy okien
    i dryf ponad progiem przechodzily bez slowa. A stan przejsciowy nie jest
    jednorazowy - kazdy przeskok miedzy karta a luka w pomiarze spreadu wchodzi
    w te galaz od nowa.
    """
    if biezacy["windows_total"] == 0:
        return ["przebieg nie dal ani jednego okna informatywnego"]

    if bezposredni is None:
        bezposredni = poprzedni

    powody: list[str] = []
    kontekst_doplaty: str | None = None

    if poprzedni is None and pominiete_inna_miara:
        powody.append(
            f"pierwszy przebieg liczony miarą „{miara_przebiegu(biezacy)}” — "
            f"{pominiete_inna_miara} wcześniejszych liczono inną definicją, "
            f"więc porównanie odsetka, przedziału i znaku mediany POMINIĘTE; "
            f"bieżący status: {biezacy['status']}")

    if bezposredni is None:
        # Bez zadnego wczesniejszego wiersza nie ma z czym porownac niczego.
        return powody or ["pierwszy zapisany przebieg — nie ma z czym porownac"]

    if bezposredni["status"] != biezacy["status"]:
        m_przed, m_teraz = miara_przebiegu(bezposredni), miara_przebiegu(biezacy)
        dopisek = (f" (zmiana definicji miary: {m_przed} → {m_teraz})"
                   if klasa_miary(bezposredni) != klasa_miary(biezacy) else "")
        powody.append(f"status: {bezposredni['status']} → {biezacy['status']}{dopisek}")

    if poprzedni is not None:
        # DOPLATA ZMIENILA SIE W OBREBIE TEJ SAMEJ KLASY MIARY - zdanie
        # INFORMACYJNE, porownanie ponizej idzie dalej. Klasa jest ta sama (obie
        # liczby po karcie), ale spread mierzony co noc chodzi, a od 2026-10-03
        # doszedl skladnik pylu: przesuniecie odsetka albo mediany o kilka
        # punktow bez tego zdania wygladaloby jak zmiana rynku, a moze byc
        # w calosci zmiana kosztu. Prog 0,5 pb: ponizej tego rzad nocnego
        # wahania pomiaru spreadu, powyzej - wiecej niz caly pyl.
        #
        # KONTEKST, NIE POWOD. Zdanie dopisujemy na koncu i TYLKO wtedy, gdy
        # jest juz inny powod: sama zmiana doplaty przy niezmienionych miarach
        # nie jest zdarzeniem, a kazdy nocny przeskok spreadu o 0,6 pb
        # konczylby sie powiadomieniem - czujka, ktora miga, uczy pomijania.
        dp, db = poprzedni.get("doplata_pb"), biezacy.get("doplata_pb")
        if (dp is not None and db is not None
                and abs(db - dp) > PROG_ZMIANY_DOPLATY_PB):
            kontekst_doplaty = (f"dopłata zmieniła się z {dp:.2f} na {db:.2f} pb "
                                f"na rundę — część zmian powyżej może pochodzić "
                                f"z kosztu, nie z rynku")
        # Prog z pomiaru, nie z przeczucia: dobowa roznica odsetka miesci sie
        # w szumie, wyjscie poza przedzial poprzedniego przebiegu - nie.
        lo, hi = poprzedni.get("positive_ci_low"), poprzedni.get("positive_ci_high")
        odsetek = biezacy["windows_positive"] / biezacy["windows_total"]
        if lo is not None and hi is not None and not (lo <= odsetek <= hi):
            powody.append(
                f"odsetek dodatnich okien {odsetek:.1%} poza przedzialem poprzedniego "
                f"przebiegu ({lo:.1%}–{hi:.1%})")

    poprz_okien = bezposredni["windows_total"]
    if poprz_okien and biezacy["windows_total"] < poprz_okien * (1 - UBYTEK_OKIEN):
        powody.append(
            f"ubylo okien informatywnych: {poprz_okien} → {biezacy['windows_total']} "
            f"— to ubytek dowodow, nie wyniku")

    if poprzedni is not None:
        sp, sb = poprzedni.get("median_oos_sharpe"), biezacy.get("median_oos_sharpe")
        if sp is not None and sb is not None and (sp > 0) != (sb > 0):
            powody.append(f"mediana Sharpe zmienila znak: {sp:+.3f} → {sb:+.3f}")

    # WARUNEK WZROSTU ZOSTAJE - ale dopiero teraz jest bezpieczny.
    #
    # `pb > pp` jest tu poprawny dla czujki ZMIANY: dryf, ktory opada, sam sie
    # cofa i nie wymaga uwagi. Dopoki jednak byl to JEDYNY tor zgloszenia dryfu,
    # ten sam warunek uciszal stan trwale zly - bo stan, ktory stoi, nie rosnie.
    # Za "nadal jest zle" odpowiada `ocen_dryf_trwaly`; tutaj zostaje wylacznie
    # "wlasnie sie pogorszylo".
    pp, pb = bezposredni.get("max_psi") or 0.0, biezacy.get("max_psi") or 0.0
    if pb > PSI_PROG and pb > pp:
        powody.append(f"dryf cech {pb:.2f} powyzej progu {PSI_PROG} i rosnie")

    if powody and kontekst_doplaty:
        powody.append(kontekst_doplaty)
    return powody


# ----------------------------------------------------- tor niegotowy (kod 3)
#
# Kod trenera `train.KOD_TOR_NIEGOTOWY`. Kopia, nie import: `train.py` ciagnie
# za soba symulacje i caly potok cech, a dozor bywa wolany sam. Rownosc pilnuje
# `tests/test_dozor_tor_niegotowy.py`.
KOD_TOR_NIEGOTOWY = 3

# Po ilu dniach bez udanego przebiegu tor niegotowy przestaje byc stanem
# przejsciowym. Jedna-dwie noce to normalny skutek nowego podzialu akcji
# (`backfill.sprawdz_ponowna_korekte` zdejmuje znacznik, operator musi
# przeliczyc) albo wdrozenia w trakcie; trzecia noc z rzedu znaczy, ze nikt
# tego nie zrobil. Mniej - alarm po kazdym podziale, zanim ktokolwiek zdazy
# zareagowac; wiecej - tydzien modelu, ktory sie nie uczy, bez slowa.
TOR_NIEGOTOWY_DNI = 3

# Rytm przypominania - ten sam co przy dryfie trwalym i z tego samego powodu:
# stan znany i czekajacy na operatora nie moze dzwonic co noc.
TOR_NIEGOTOWY_PRZYPOMNIENIE_DNI = 7


def ocen_tor_niegotowy(ostatni_przebieg: datetime | None,
                       ostatnie_zgloszenie: datetime | None,
                       teraz: datetime) -> str | None:
    """Czy tor, ktory trener odmowil liczyc (kod 3), stoi juz za dlugo.

    DLACZEGO TO W OGOLE JEST CZUJKA. Kod 3 to NIE awaria - przebieg nie
    wystartowal, bo nie wolno mu bylo (np. equity bez znacznika
    `swiece_skorygowane_o_podzialy`). Do 2026-10-03 trainer-cron zapisywal go
    wylacznie w echu Joba, a dozor dostawal `--kod-trenera 0`: tor equity stal
    tygodniami z 0/20 znacznikow i jedynym sladem byl log, ktorego nikt nie
    czyta.

    `ostatni_przebieg` - `run_at` ostatniego wiersza dziennika stanu nauki dla
    tej pary (engine, bar); None = nigdy. Brak wiersza liczy sie jak stan
    dowolnie dlugi: torem, ktory nie przeszedl ani razu, nikt nie handluje
    swiadomie. Funkcja czysta - werdykt i rytm testowalne bez bazy i zegara.
    """
    if ostatni_przebieg is not None:
        dni = (teraz - ostatni_przebieg).total_seconds() / 86400.0
        if dni < TOR_NIEGOTOWY_DNI:
            return None
        odkad = f"ostatni udany przebieg {dni:.1f} dnia temu"
    else:
        odkad = "tor nie ma ani jednego udanego przebiegu w dzienniku"
    if ostatnie_zgloszenie is not None and (
            teraz - ostatnie_zgloszenie).total_seconds() < (
            TOR_NIEGOTOWY_PRZYPOMNIENIE_DNI * 86400):
        return None
    return (f"trener odmawia liczenia toru (kod {KOD_TOR_NIEGOTOWY}, tor "
            f"niegotowy) — {odkad}, próg {TOR_NIEGOTOWY_DNI} dni")


async def sprawdz_tor_niegotowy(fetch_all, notify, sucho: bool = False, *,
                                engine: str, bar: str,
                                teraz: datetime | None = None) -> int:
    """`--kod-trenera 3`: ostrzezenie z tygodniowym rytmem, nie alarm krytyczny.

    Rytm liczony z historii powiadomien (`data->>'powod'`), per (engine, bar) -
    ten sam chwyt co przy `learning.drift_high`, bez nowej kolumny.
    """
    teraz = teraz or datetime.now(timezone.utc)
    ostatni = await fetch_all(
        "SELECT max(run_at) AS run_at FROM learn.learning_state_log"
        " WHERE engine = %s AND bar = %s", (engine, bar))
    ostatni_przebieg = ostatni[0]["run_at"] if ostatni else None
    zgl = await fetch_all(
        "SELECT created_at FROM notify.notification"
        " WHERE event_code = 'learning.state_changed' AND engine::text = %s"
        " AND data->>'powod' = 'tor_niegotowy' AND data->>'bar' = %s"
        " ORDER BY created_at DESC LIMIT 1", (engine, bar))
    ostatnie = zgl[0]["created_at"] if zgl else None

    powod = ocen_tor_niegotowy(ostatni_przebieg, ostatnie, teraz)
    print(f"Tor {engine}/{bar}: trener zakończył się kodem {KOD_TOR_NIEGOTOWY} "
          f"(tor niegotowy — przebieg nie wystartował, nic nie zapisano)")
    if powod is None:
        # Cisza z powodem - inaczej nieodroznialna od czujki, ktora padla.
        if ostatnie is not None:
            print(f"  zgłoszone {ostatnie:%Y-%m-%d %H:%M} UTC, przypomnienie co "
                  f"{TOR_NIEGOTOWY_PRZYPOMNIENIE_DNI} dni — cisza")
        else:
            print(f"  krócej niż {TOR_NIEGOTOWY_DNI} dni od udanego przebiegu — cisza")
        return 0
    print(f"  • {powod}")
    if sucho:
        print("Tryb suchy — powiadomienie nie wysyłane.")
        return 0
    odbiorcy = await fetch_all("SELECT id FROM app.\"user\" WHERE is_active AND role = 'owner'")
    if not odbiorcy:
        print("Brak odbiorców — powiadomienie nie ma do kogo pójść.")
        return 0
    tresc = (f"{powod}.\n\nTo nie jest awaria trenera, tylko warunek wstępny, "
             f"którego nikt nie spełnił. Dla equity: procedura korekty podziałów "
             f"(renacode-infra: docs/projekty/trader/08-przed-go-live.md, „korekta podziałów”) i `korekta_podzialow.py --znacznik`.")
    for u in odbiorcy:
        await notify(u["id"], "learning.state_changed",
                     f"Tor niegotowy ({engine}/{bar})", tresc, engine=engine,
                     severity="warning",
                     data={"powod": "tor_niegotowy", "bar": bar,
                           "kod_trenera": KOD_TOR_NIEGOTOWY})
    print(f"Powiadomiono odbiorców: {len(odbiorcy)}")
    return 0


SQL_ZMIANA_STRUKTURY = """
SELECT v.id, v.version, v.zmiana_struktury,
       EXISTS (SELECT 1 FROM notify.notification n
                WHERE n.event_code = 'learning.zmiana_struktury'
                  AND n.data->>'model_version_id' = v.id::text) AS zgloszona
  FROM learn.model_version v
  JOIN learn.model m ON m.id = v.model_id
 WHERE m.name = %s AND v.status = 'candidate' AND v.zmiana_struktury IS NOT NULL
 ORDER BY v.id DESC
 LIMIT 1
"""


def ocen_zmiane_struktury(wiersz: dict | None) -> str | None:
    """Zdanie powiadomienia o zmianie struktury modelu albo None. CZYSTA FUNKCJA.

    Zglaszana jest NAJNOWSZA wersja-kandydat ze zmiana struktury (audyt Fable
    F-3.1, `train.zmiana_struktury`), raz na wersje. Przy treningu tygodniowym
    daje to przypomnienie co tydzien, dopoki czlowiek nie zdecyduje - kazdy
    przebieg tworzy nowego kandydata o tej samej zmianie.
    """
    if not wiersz or wiersz.get("zgloszona"):
        return None
    return (f"wersja {wiersz['version']} ma inną strukturę niż aktywna: "
            f"{wiersz['zmiana_struktury']}")


async def sprawdz_zmiane_struktury(fetch_all, notify, sucho: bool = False, *,
                                   engine: str, bar: str) -> int:
    """`learning.zmiana_struktury` po treningu - wolane z galezi stanu nauki."""
    wiersze = await fetch_all(SQL_ZMIANA_STRUKTURY,
                              (f"{engine}_signal_weighting_{bar}",))
    wiersz = wiersze[0] if wiersze else None
    powod = ocen_zmiane_struktury(wiersz)
    if powod is None:
        print("Struktura modelu: bez nowej zmiany do zgłoszenia.")
        return 0
    print(f"Struktura modelu: {powod}")
    if sucho:
        print("Tryb suchy — powiadomienie nie wysyłane.")
        return 1
    odbiorcy = await fetch_all("SELECT id FROM app.\"user\" WHERE is_active AND role = 'owner'")
    tresc = (f"{powod}.\n\nWersja została kandydatem, produkcja handluje dalej "
             f"aktywną. Promocja: wpis w renacode-infra: docs/projekty/trader/hipotezy.md i ręczny przebieg "
             f"`train.py --zatwierdz-promocje`.")
    for u in odbiorcy:
        await notify(u["id"], "learning.zmiana_struktury",
                     f"Zmiana struktury modelu ({engine}/{bar})", tresc,
                     engine=engine, data={"model_version_id": str(wiersz["id"]),
                                          "bar": bar})
    print(f"Powiadomiono odbiorców: {len(odbiorcy)}")
    return 1


def ocen_stan_nauki(wiersze: list[dict]) -> tuple[list[str], int]:
    """Werdykt `--co stan-nauki` z wierszy dziennika (najnowszy pierwszy).

    Wydzielone z `main`, zeby DOBOR POPRZEDNIKOW byl testowalny bez bazy:
    poprzednik tej samej miary dla liczb od miary zaleznych i wiersz
    bezposrednio wczesniejszy dla statusu, okien i dryfu (patrz `zmiany`).
    Oddaje powody i liczbe przebiegow pominietych jako liczone inna miara.
    """
    biezacy = wiersze[0]
    poprzedni, pominiete = wybierz_poprzedni(wiersze)
    bezposredni = wiersze[1] if len(wiersze) > 1 else None
    return zmiany(poprzedni, biezacy, pominiete, bezposredni), pominiete


def ocen_dryf_trwaly(historia: list[dict], *,
                     ostatnie_zgloszenie: datetime | None = None,
                     zgloszony_dryf: float | None = None,
                     teraz: datetime | None = None) -> dict | None:
    """Czy dryf jest wysoki OD DAWNA i czy wypada juz o tym przypomniec.

    Zwraca None, gdy nalezy milczec. Funkcja jest czysta - caly werdykt, razem
    z rytmem przypominania, da sie przetestowac bez bazy i bez zegara.

    `historia` to wiersze `learn.learning_state_log` dla JEDNEJ pary
    (engine, bar), najnowszy pierwszy, z kluczami `max_psi` i `run_at`. Filtr po
    `bar` nalezy do wolajacego i jest konieczny: swieca 4h i 1h maja osobne
    przebiegi tej samej nocy (migracja 0025), wiec bez filtra "trzy kolejne
    przebiegi" mieszalyby dwa rozne modele.

    TRZY WARUNKI, WSZYSTKIE KONIECZNE

      1. dosc dowodow - DRYF_TRWALY_PRZEBIEGOW wierszy w dzienniku,
      2. kazdy z nich powyzej DRYF_TRWALY_PROG - jeden odczyt ponizej progu
         rozbraja czujke natychmiast, bo problem sie cofnal,
      3. rozpietosc w czasie co najmniej DRYF_TRWALY_GODZIN - inaczej "trwaly"
         znaczyloby "trzy razy uruchomiony tej samej nocy".

    Warunki 2 i 3 sa asymetryczne wzgledem uzbrajania i rozbrajania, i to jest
    celowe: zaczac przypominac ma byc trudno, przestac - latwo. Odwrotna
    asymetria daje czujke, ktora nadaje jeszcze dlugo po naprawie.
    """
    if len(historia) < DRYF_TRWALY_PRZEBIEGOW:
        return None

    okno = historia[:DRYF_TRWALY_PRZEBIEGOW]
    wartosci = [(w.get("max_psi") or 0.0) for w in okno]
    if min(wartosci) <= DRYF_TRWALY_PROG:
        return None

    rozpietosc_h = (okno[0]["run_at"] - okno[-1]["run_at"]).total_seconds() / 3600.0
    if rozpietosc_h < DRYF_TRWALY_GODZIN:
        return None

    biezacy = wartosci[0]
    # Pasmo, a nie sama liczba: przejscie ponad PSI_PROG to jedyna zmiana, ktora
    # lamie rytm tygodniowy. Rozroznienie musi wiec byc jawne i porownywalne
    # miedzy przebiegami, a nie ukryte w tresci komunikatu.
    pasmo = "duzy" if biezacy > PSI_PROG else "sredni"

    pierwsze = ostatnie_zgloszenie is None
    eskalacja = False
    if not pierwsze:
        # POGORSZENIE PASMA LAMIE RYTM. Stan, ktory z "sredniego" przeszedl
        # w "duzy", to nowa informacja, a nie to samo przypomnienie - trzymanie
        # jej tydzien w kolejce byloby cisza wtedy, gdy akurat cos sie dzieje.
        eskalacja = (zgloszony_dryf is not None
                     and zgloszony_dryf <= PSI_PROG < biezacy)
        odkad = ((teraz or datetime.now(timezone.utc)) - ostatnie_zgloszenie)
        if odkad.total_seconds() < DRYF_PRZYPOMNIENIE_DNI * 86400 and not eskalacja:
            return None

    if pierwsze:
        powod = (f"dryf cech {biezacy:.2f} utrzymuje sie powyzej "
                 f"{DRYF_TRWALY_PROG} w {DRYF_TRWALY_PRZEBIEGOW} kolejnych "
                 f"przebiegach — to nie jest wahniecie")
    elif eskalacja:
        powod = (f"dryf cech pogorszyl sie z {zgloszony_dryf:.2f} do "
                 f"{biezacy:.2f} — przekroczyl {PSI_PROG}")
    else:
        powod = (f"dryf cech {biezacy:.2f} NADAL powyzej {DRYF_TRWALY_PROG} — "
                 f"przypomnienie po {DRYF_PRZYPOMNIENIE_DNI} dniach bez poprawy")

    return {"powod": powod, "dryf": biezacy, "pasmo": pasmo,
            "przebiegow": DRYF_TRWALY_PRZEBIEGOW, "godzin": round(rozpietosc_h),
            "pierwsze": pierwsze, "eskalacja": eskalacja}


def podsumuj_dryf_cech(wiersze: list[dict],
                       limit: int = DRYF_CECH_W_TRESCI) -> list[str]:
    """KTORE cechy odjechaly - bo "cos sie zmienilo" nie prowadzi do dzialania.

    `wiersze` to `learn.drift_metric` zlaczone z `feat.feature_def`, posortowane
    malejaco, z kluczami `feature` i `psi`.

    Wymieniamy tylko cechy ponad progiem. Cecha spokojna dopisana do listy nie
    dodaje informacji, a rozciaga komunikat - i po kilku takich odbiorca
    przestaje go czytac do konca, czyli do miejsca, gdzie stoi ta wazna.
    """
    istotne = [w for w in wiersze if (w.get("psi") or 0.0) > DRYF_TRWALY_PROG]
    # Gdy zadna cecha nie przekracza progu, a maksimum z dziennika owszem,
    # rozjechal sie sam dziennik z tabela dryfu. Pokazujemy wtedy czolowke bez
    # filtra: czujka nie moze milczec o powodzie, dla ktorego wlasnie krzyknela.
    wybrane = (istotne or list(wiersze))[:limit]
    return [f"{w['feature']}: {(w.get('psi') or 0.0):.2f}" for w in wybrane]


# Ile moze minac od ostatniego uderzenia serca, zanim uznamy silnik za martwy.
#
# Petla decyzyjna budzi sie co 300 s, wiec trzy pominiete cykle to sygnal,
# a nie wahanie. Zapas jest istotny: po wdrozeniu pod startuje z `sleep 90`
# i kontrola petli, wiec pierwsze uderzenie potrafi spoznic sie o kilka minut.
MARTWY_PO_MIN = 15


# Nazwa procesu w `risk.dozorca_heartbeat`. Z MODULU DOZORCY, nie przepisana:
# `dozorca.bij_heartbeat` sklada klucz jako f"{NAZWA}:{engine}:{mode}", wiec
# druga kopia tego napisu tutaj rozjechalaby sie po cichu i to wlasnie ta czujka
# twierdzilaby wtedy, ze spodziewanego dozorcy nie ma (albo ze jest).
from services.engine.dozorca import NAZWA as DOZORCA_PULS  # noqa: E402


def brakujacy_dozorcy(pary: list[dict], pulsy: list[dict]) -> list[str]:
    """Dozorcy, ktorych puls NIE POWSTAL ANI RAZU. CZYSTA FUNKCJA.

    DLACZEGO BRAK WIERSZA MUSI BYC OSOBNYM PYTANIEM

    `sprawdz_zywotnosc` iteruje WIERSZE `risk.dozorca_heartbeat`, wiec odpowiada
    wylacznie na pytanie "czy ten, kto kiedys bil, bije nadal". Dozorca, ktory
    nigdy nie wstal, nie zostawia wiersza - a brak wiersza jest w tej petli
    nieodroznialny od spokoju.

    Nie jest to hipoteza. Tor `equity` stal w tym stanie: siedem wlaczonych
    limitow dla `equity/demo` (zmierzone 2026-09-27), `risk.dozorca_heartbeat`
    z DOKLADNIE JEDNYM wierszem `dozorca-ryzyka:crypto:demo`, a `wznow()` - jedyna
    droga automatycznego zwolnienia kill-switcha - ma w calym repozytorium jedno
    miejsce wywolania, w `dozorca.py`. Kill-switch na torze equity nie mial wiec
    kto zwolnic poza czlowiekiem w API, i NIC tego nie mowilo.

    KOGO SIE SPODZIEWAMY, BIERZEMY Z LIMITOW, NIE Z LISTY W KODZIE. Para
    engine/mode, ktora ma wlaczony limit konta, to para, ktorej limity ktos MUSI
    oceniac niezaleznie od silnika - i odwrotnie: para bez limitow nie ma czego
    nadzorowac. Lista nazw procesow wpisana tutaj byla by druga kopia zawartosci
    `values.yaml` i rozjechala by sie przy pierwszym nowym silniku.
    """
    maja = {p["nazwa"] for p in pulsy}
    return [n for n in
            (f"{DOZORCA_PULS}:{p['engine']}:{p['mode']}" for p in pary)
            if n not in maja]


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

    I TO SAMO TLUMIENIE RZADZI KODEM WYJSCIA (patrz `kod_wyjscia`). Silnik
    martwy od tygodnia to jedno zgloszenie i jeden czerwony Job, a nie 1008
    czerwonych Jobow - stan zostaje wypisany w logu przy kazdym przebiegu.
    NAWROT jest tu rozroznialy za darmo i bez drugiego mechanizmu: silnik, ktory
    wstal i padl ponownie, ma nowsze `last_beat_at`, wiec stare powiadomienie
    przestaje go tlumic i nowa awaria znowu maluje zadanie na czerwono.
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
    #
    # DWIE KOLUMNY, NIE JEDNA: `engine` i `etykieta`. Do 2026-09-12 puls dozorcy
    # wchodzil do tego zapytania jako `nazwa AS engine` i tak samo wychodzil do
    # `notify(engine=...)`. Ale `notify.notification.engine` ma typ `ref.engine`,
    # czyli enum SILNIKOW HANDLOWYCH ('crypto','equity') - a `nazwa` to
    # identyfikator procesu, `dozorca-ryzyka:crypto:demo`. Zapis konczyl sie
    # `InvalidTextRepresentation` i CZUJKA GINELA W CHWILI, W KTOREJ MIALA
    # KRZYKNAC: dozorca lezal od pietnastu godzin, a jedyne, co bylo widac, to
    # czerwony Job z powodem zupelnie niezwiazanym ze stanem silnika.
    #
    # `etykieta` jest tylko do MOWIENIA (log, tytul, tlumienie), `engine` tylko
    # do KOLUMNY. Dla procesu nadzoru `engine` jest NULL-em - dokladnie tak jak
    # przy pulsie workerow nizej, gdzie „market" i „news" tez nie sa silnikami.
    silniki = await fetch_all(
        "SELECT engine::text AS engine, engine::text AS etykieta,"
        " mode::text AS mode, status, last_beat_at,"
        " round(extract(epoch from (now() - last_beat_at))/60) AS wiek_min"
        " FROM risk.engine_heartbeat"
        " UNION ALL"
        " SELECT NULL::text AS engine, nazwa AS etykieta,"
        " 'nadzor' AS mode, 'running' AS status, last_beat_at,"
        " round(extract(epoch from (now() - last_beat_at))/60) AS wiek_min"
        " FROM risk.dozorca_heartbeat"
        " ORDER BY etykieta")
    if not silniki:
        print("brak wpisow o zywotnosci silnikow")
        return 0

    # DOZORCA, KTOREGO NIE MA WCALE - patrz `brakujacy_dozorcy`. Pytanie zadane
    # PRZED petla nizej, bo petla iteruje istniejace wiersze i o nieistniejacym
    # procesie nie ma jak powiedziec ani slowa.
    pary = await fetch_all(
        "SELECT DISTINCT engine::text AS engine, mode::text AS mode"
        "  FROM risk.limit_def WHERE enabled ORDER BY 1, 2")
    brakujacy = brakujacy_dozorcy(
        pary, await fetch_all("SELECT nazwa FROM risk.dozorca_heartbeat"))
    for nazwa in brakujacy:
        print(f"  BRAK      {nazwa}: ani jednego uderzenia — limity tej pary "
              f"nie mają niezależnego oceniającego, a kill-switcha nie ma kto "
              f"zwolnić (wznow() woła tylko services/engine/dozorca.py)")

    martwe = []
    for s in silniki:
        opis = f"{s['etykieta']}/{s['mode']}: {s['status']}, ostatnie uderzenie {s['wiek_min']:.0f} min temu"
        if s["status"] != "running":
            print(f"  pomijam  {opis} — zatrzymany świadomie")
            continue
        if s["wiek_min"] > prog:
            martwe.append(s)
            print(f"  MARTWY   {opis}")
        else:
            print(f"  żyje     {opis}")

    if not martwe and not brakujacy:
        return 0

    if sucho:
        print("Tryb suchy — powiadomienie nie wysyłane.")
        return 1

    odbiorcy = await fetch_all("SELECT id FROM app.\"user\" WHERE is_active AND role = 'owner'")
    zgloszone = trwale = 0
    for s in martwe:
        # TLUMIENIE PYTA O ETYKIETE, NIE O KOLUMNE. Dla silnika etykieta siedzi
        # w `engine`, dla procesu nadzoru w `data->>'proces'` - `coalesce` bierze
        # ta, ktora istnieje. Gdyby pytalo o samo `engine::text`, awaria dozorcy
        # nie tlumilaby sie NIGDY (kolumna jest wtedy NULL-em), czyli czujka
        # krzyczalaby co dziesiec minut az do wyciszenia kanalu - a wyciszony
        # kanal to gorszy stan niz brak czujki.
        juz = await fetch_all(
            "SELECT 1 FROM notify.notification WHERE event_code = 'engine.down'"
            " AND coalesce(engine::text, data->>'proces') = %s"
            " AND created_at > %s LIMIT 1",
            (s["etykieta"], s["last_beat_at"]))
        if juz:
            # Cisza w kanale powiadomien - i cisza w kodzie wyjscia razem z nia.
            # Log zostaje glosny: linia "MARTWY ..." wyzej idzie przy kazdym
            # przebiegu, niezaleznie od tlumienia.
            trwale += 1
            print(f"  (awaria {s['etykieta']} juz zgloszona — cisza)")
            continue
        zgloszone += 1
        # Podpowiedz ma wskazywac TEN pod, ktory zamilkl. Jedna nazwa dla obu
        # przypadkow wysylalaby po log silnika kogos, komu padl nadzorca.
        pod = "trader-engine" if s["engine"] else "trader-dozorca"
        tresc = (f"Ostatnie uderzenie serca {s['wiek_min']:.0f} minut temu "
                 f"({s['last_beat_at']:%Y-%m-%d %H:%M} UTC), próg to {prog} min.\n\n"
                 f"Sprawdź: kubectl logs deploy/{pod} --tail=50")
        # `proces` idzie do `data` WYLACZNIE dla nadzoru i jest tam po to, zeby
        # tlumienie wyzej mialo po czym rozpoznac swoje powiadomienie.
        dane = {"wiek_min": float(s["wiek_min"]), "status": s["status"]}
        czym = "Silnik" if s["engine"] else "Proces nadzoru"
        if not s["engine"]:
            dane["proces"] = s["etykieta"]
        for u in odbiorcy:
            await notify(u["id"], "engine.down",
                         f"{czym} {s['etykieta']} przestał odpowiadać", tresc,
                         engine=s["engine"], severity="critical", data=dane)
        print(f"  powiadomiono o {s['etykieta']}: {len(odbiorcy)} odbiorców")

    for nazwa in brakujacy:
        # TLUMIENIE DOBOWE, NIE PO `last_beat_at` - bo tego uderzenia nie ma.
        # Stan jest trwaly (pod wylaczony w chartcie albo niewstajacy), wiec bez
        # wlasnego okna czujka krzyczalaby co 10 minut: `engine.down` jest
        # `always_on`, czyli katalogowy cooldown go nie dotyczy. Doba to ten sam
        # rytm, ktorym `sprawdz_instrumenty` przypomina o zamrozonym kapitale,
        # i z tego samego powodu: problem jest problemem codziennie, az do
        # naprawy, ale przypomniany raz na dobe, nie 144 razy.
        juz = await fetch_all(
            "SELECT 1 FROM notify.notification WHERE event_code = 'engine.down'"
            " AND data->>'proces' = %s"
            " AND created_at > now() - make_interval(hours => 24) LIMIT 1",
            (nazwa,))
        if juz:
            trwale += 1
            print(f"  (brak dozorcy {nazwa} zgłoszony w ciągu doby — cisza)")
            continue
        zgloszone += 1
        for u in odbiorcy:
            await notify(
                u["id"], "engine.down",
                f"Proces nadzoru {nazwa} nie istnieje",
                "`risk.dozorca_heartbeat` nie ma ANI JEDNEGO uderzenia o tej "
                "nazwie, a `risk.limit_def` ma dla tej pary włączone limity "
                "konta.\n\nZnaczy to, że limity są egzekwowane wyłącznie "
                "z wnętrza pętli decyzyjnej: gdy pętla żyje i nic nie robi "
                "(zawieszona na zapytaniu), obsunięcia nie liczy nikt. "
                "Automatycznego zwolnienia kill-switcha też nie ma — `wznow()` "
                "jest wołane wyłącznie z services/engine/dozorca.py.\n\n"
                "Sprawdź: kubectl get deploy | grep dozorca",
                engine=None, severity="critical",
                data={"proces": nazwa, "powod": "brak_wiersza_pulsu"})
        print(f"  powiadomiono o braku {nazwa}: {len(odbiorcy)} odbiorców")

    # KOD WYJSCIA NIEZEROWY DOKLADNIE WTEDY, GDY POWIADOMIENIE POSZLO.
    #
    # Drugi, niezalezny od kanalu powiadomien slad jest wart tyle samo co
    # kiedys - ale wart jest go ZGLOSZENIE, a nie kazdy z 144 przebiegow doby.
    # Powtarzany co dziesiec minut przestaje byc sladem i staje sie szumem,
    # ktory uczy przewijac takze te awarie, ktore dopiero co wybuchly.
    return kod_wyjscia(zgloszone, trwale, "silnik bez pulsu")


# ------------------------------------------------------- czujka kill-switcha
#
# Rytm przypominania o AKTYWNYM kill-switchu. NAJWAZNIEJSZA LICZBA W TEJ CZUJCE.
#
# Do 2026-09-09 rytmu nie bylo wcale. Tlumienie brzmialo "istnieje juz
# powiadomienie mlodsze niz `triggered_at`", a ten warunek, raz spelniony, jest
# spelniony NA ZAWSZE - bo `triggered_at` sie nie zmienia. Pierwsze zgloszenie
# szlo, a po nim zapadala cisza bez konca.
#
# Zmierzone na produkcji: zdarzenie z 03:20 blokowalo wejscia czwarta godzine,
# czujka je widziala, wypisywala "(juz zgloszone — cisza)" i zadanie konczylo
# sie POWODZENIEM. To ten sam wzorzec, ktory kosztowal ten projekt tygodnie
# ciszy przy dryfie cech: regula "zglaszaj ZMIANE" nie widzi STANU, ktory stoi,
# a stan stojacy jest NIEODROZNIALNY od spokoju.
#
# Zniesienie tlumienia nie jest jednak naprawa. Zadanie chodzi co 10 minut
# (values.yaml, zywotnoscCron.schedule), wiec "zglaszaj zawsze" znaczy szesc
# powiadomien na godzine o rzeczy, ktora sie nie zmienia - a powiadomienie,
# ktore przychodzi zawsze, przestaje cokolwiek znaczyc dokladnie tak samo jak
# cisza. Dlatego odstep ROSNIE z wiekiem zdarzenia, i to z dwoch roznych
# powodow, bo przez pierwsza dobe pytanie jest inne niz pozniej:
#
#   * przez pierwsza dobe pytamy "czy ktos to w ogole zobaczyl". Szesc godzin
#     to najkrotszy odstep, ktory nie powtarza sie dwa razy w ciagu jednej nocy,
#     a jednoczesnie gwarantuje, ze zdarzenie ze srodka nocy dostanie
#     przypomnienie rano - wlasciciel ma nie klikac w nocy;
#   * pozniej pytamy juz tylko "czy ktos o tym pamieta". Stan jest znany,
#     zwolnienie `min_equity` jest RECZNE z zalozenia, a decyzja handlowa moze
#     trwac. Doba to ten sam rytm, ktorym czujka delistingu przypomina
#     o ZAMROZONYM KAPITALE, i z tego samego powodu: problem jest problemem
#     codziennie, az do zamkniecia.
#
# TEN RYTM RZADZI TAKZE KODEM WYJSCIA ZADANIA, i to jest cala poprawka
# z 2026-09-10. Przez dobe kod wyjscia byl niezerowy przy KAZDYM przebiegu przez
# caly czas blokady - 144 padniete Joby na dobe o jednym, znanym od poltorej
# doby zdarzeniu. Dzis niezerowy jest ten przebieg, w ktorym cos poszlo kanalem
# powiadomien: pierwsze zgloszenie i kazde przypomnienie. Wychodzi z tego kilka
# czerwonych Jobow w pierwszej dobie i po jednym na dobe pozniej - tyle, ile
# naprawde jest zdarzen do zauwazenia. Patrz komentarz na koncu `sprawdz_ryzyko`.
KS_PRZYPOMNIENIE_H = 6
KS_PRZYPOMNIENIE_DLUGIE_H = 24

# Od kiedy przypominamy rzadziej. Doba - bo to granica miedzy "moglo umknac"
# a "jest znane": kill-switch nierozwiazany po calym dniu nie rozwiaze sie
# szybciej dlatego, ze zapytamy o niego cztery razy czesciej. Od tej chwili
# wartosc przypomnienia spada, a koszt (nauka odklikiwania) rosnie.
KS_RYTM_ROSNIE_PO_H = 24


def odstep_przypomnienia_ks(wiek_min: float) -> int:
    """Co ile godzin przypominac o kill-switchu w tym wieku. CZYSTA FUNKCJA.

    Wydzielona, bo potrzebuja jej dwie sciezki: werdykt (czy zglaszac) i log
    ciszy (dlaczego nie zglaszamy). Druga kopia tej liczby w komunikacie
    rozjechalaby sie z pierwsza przy pierwszej zmianie progu, a rozjazd byl by
    widoczny wylacznie dla kogos, kto porownalby log z zachowaniem.
    """
    return (KS_PRZYPOMNIENIE_H if wiek_min < KS_RYTM_ROSNIE_PO_H * 60
            else KS_PRZYPOMNIENIE_DLUGIE_H)


def zdanie_o_wznowieniu(regula: str | None, max_wznowien: int | None) -> str:
    """Co stanie sie z zatrzymaniem dalej - wedlug reguly limitu. CZYSTA FUNKCJA.

    PRZEGLAD 2026-10-04 (T-4). Tresc mowila zawsze „kill-switch nie wygasa sam",
    a na produkcji `max_daily_loss` ma regule `po_ustaniu`, `max_drawdown`
    `po_odbudowie` - oba `wznowienie.wznow` zwalnia sam po karencji. Wlasciciel
    czytal na telefonie, ze handel stoi do jego decyzji, i nie reagowal; po
    godzinie automat wznawial handel. Brak reguly (limit bez wiersza, reczny
    kill-switch z panelu) = `reczne`, bo tak traktuje go automat.
    """
    ile = max_wznowien if max_wznowien is not None else "?"
    prog = f"{HISTEREZA:.0%} limitu"
    if regula == "po_ustaniu":
        return (f"Kill-switch wznowi się AUTOMATYCZNIE, gdy miara wróci poniżej "
                f"{prog} (po co najmniej {KARENCJA_MIN} min od zadziałania), najwyżej "
                f"{ile} razy na dobę; każde wznowienie przyjdzie osobnym alarmem. "
                f"Wcześniejsze zwolnienie: zakładka Ryzyko w interfejsie.")
    if regula == "po_odbudowie":
        return (f"Kill-switch wznowi się AUTOMATYCZNIE, gdy obsunięcie spadnie poniżej "
                f"{prog} (po co najmniej {KARENCJA_MIN} min od zadziałania), najwyżej "
                f"{ile} razy na dobę; każde wznowienie przyjdzie osobnym alarmem. "
                f"Wcześniejsze zwolnienie: zakładka Ryzyko w interfejsie.")
    return ("Silnik NIE WEJDZIE w żadną pozycję do czasu ręcznego zwolnienia — "
            "kill-switch nie wygasa sam, bo automatyczne wznowienie po obsunięciu "
            "to najprostsza droga do powtórzenia tej samej straty.\n\nZwolnienie: "
            "zakładka Ryzyko w interfejsie.")


def ocen_kill_switch(wiek_min: float,
                     od_zgloszenia_min: float | None) -> dict | None:
    """Czy o aktywnym kill-switchu wypada sie TERAZ odezwac. CZYSTA FUNKCJA.

    `wiek_min` to czas od `triggered_at`; `od_zgloszenia_min` - czas od
    OSTATNIEGO powiadomienia `killswitch.triggered` wyslanego po tym zdarzeniu,
    albo None, gdy nie poszlo jeszcze zadne. Oba w minutach i oba liczone
    zegarem bazy: drugi zegar, w Pythonie, rozjechalby sie z pierwszym i rytm
    zalezalby od tego, ktory pod akurat wstal.

    Zwraca None, gdy nalezy milczec. TO NIE ZNACZY "wszystko dobrze" - znaczy
    "nic NOWEGO". Stan jest wypisywany w logu przy kazdym przebiegu; milkna
    razem kanal powiadomien i kod wyjscia, bo obu chodzi o to samo pytanie:
    czy wlasnie stalo sie cos, o czym trzeba sie dowiedziec.
    """
    odstep_h = odstep_przypomnienia_ks(wiek_min)

    if od_zgloszenia_min is None:
        # Nowe zdarzenie idzie NATYCHMIAST, bez wzgledu na rytm. Kill-switch
        # zatrzymal handel i to jest jedyny moment, w ktorym powiadomienie
        # niesie informacje, a nie przypomnienie.
        return {"powod": "kill-switch zadziałał", "pierwsze": True,
                "odstep_h": odstep_h}

    if od_zgloszenia_min < odstep_h * 60:
        return None

    return {"powod": (f"kill-switch NADAL aktywny — {wiek_min / 60:.0f} h bez "
                      f"zwolnienia"),
            "pierwsze": False, "odstep_h": odstep_h}


# --------------------------------------------------- cisza samego bezpiecznika
#
# TRZECIA PRZYCZYNA, OSOBNA OD DWOCH WYZEJ I OD NICH GORSZA
#
# Punkty wyzej odpowiadaja na pytanie "czy zauwazylismy, ze bezpiecznik
# ZADZIALAL". Nie odpowiadaja na pytanie wczesniejsze: "czy bezpiecznik w ogole
# MIERZY". A to jest jedyne pytanie, ktore ma znaczenie, dopoki nic sie nie
# stalo - czyli przez wieksza czesc zycia systemu.
#
# Stan zastany, zmierzony na produkcji 2026-09-09: `check_account()` liczylo
# zuzycie kazdego limitu konta w kazdym cyklu i NIE ZAPISYWALO GO. Ostatni
# wiersz w `risk.limit_usage` pochodzil z dnia ostatniego WEJSCIA w pozycje,
# bo tylko sciezka decyzyjna cokolwiek pisala. Przez trzy doby silnik pracowal,
# oceniat limity co cykl i nie zostawial po tym niczego. Zdanie "ochrona
# kapitalu czuwa" bylo zalozeniem: gdyby ocena przestala byc wolana albo zaczela
# sie wywracac po cichu, dane wygladalyby DOKLADNIE TAK SAMO.
#
# Od zmiany w services/engine/risk.py pomiar zapisuje sie w stalym rytmie
# (POMIAR_CO_S), wiec cisza w tej tabeli znaczy juz tylko jedno: NIKT NIE MIERZY.
# Ta czujka zamienia to w widoczny stan.
#
# DLACZEGO TUTAJ, A NIE W NOWYM MECHANIZMIE. Bo pytanie jest tym samym pytaniem
# co wyzej - "czy bezpiecznik dziala i czy ktos o tym wie" - tylko zadanym
# o poziom nizej. Osobna czujka znaczylaby osobne zadanie, osobny harmonogram
# i osobne miejsce do zapomnienia.

# Ile moze minac od ostatniego zapisanego pomiaru, zanim uznamy, ze bezpiecznik
# przestal raportowac.
#
# Trzy rytmy pomiaru - ta sama zasada co CYKLI_ZWLOKI przy pulsie workerow
# i MARTWY_PO_MIN przy silniku: jedno spoznienie to wahanie, trzy to sygnal.
# Prog dwoch lezalby na gornej granicy normy i migalby kilka razy dziennie,
# a czujka, ktora miga, uczy sie odklikiwac.
#
# 15 minut jest tez wieksze niz wlasna rozdzielczosc tej czujki (zadanie
# cykliczne chodzi co 10 minut) - prog krotszy opisywalby stan, ktorego ta
# czujka nie umie zobaczyc.
POMIAR_CISZA_S = 3 * POMIAR_CO_S


def ocen_cisze_pomiaru(wiek_s: float | None, nigdy: list[str] | None,
                       od_zgloszenia_min: float | None,
                       prog_s: int = POMIAR_CISZA_S) -> dict | None:
    """Czy bezpiecznik przestal zostawiac slad po pomiarze. CZYSTA FUNKCJA.

    `wiek_s`  - wiek NAJNOWSZEGO pomiaru sposrod limitow koniecznych, None gdy
                nie ma ani jednego;
    `nigdy`   - nazwy limitow koniecznych, ktore nie maja ANI JEDNEGO pomiaru
                w calej historii;
    `od_zgloszenia_min` - ile minut od ostatniego powiadomienia o tej ciszy.

    Zwraca None, gdy pomiar jest swiezy i komplety. Inaczej dict z `powod`,
    `zglos` (czy TERAZ wypada sie odezwac), `odstep_h` i `nawrot`.

    `zglos` RZADZI TEZ KODEM WYJSCIA CZUJKI (patrz `kod_wyjscia`), wiec od
    2026-09-10 musi umiec odroznic cisze trwajaca od ciszy, ktora WROCILA -
    inaczej nawrot wpadalby w okno rytmu poprzedniego zgloszenia i nie zostawial
    sladu w zadnym z dwoch kanalow. Robi to `nawrot` nizej.

    LIMIT BEZ ANI JEDNEGO POMIARU TO TEZ CISZA, nawet gdy pozostale mierza sie
    co pieciu minut. Sredni wiek po wszystkich limitach wygladalby wtedy
    znakomicie, a jeden z trzech filarow podlogi kapitalu nie bylby mierzony
    przez nikogo - dokladnie w tym stanie byl `min_equity` do 2026-09-09.
    """
    braki = list(nigdy or [])
    stary = wiek_s is not None and wiek_s > prog_s
    if not braki and not stary and wiek_s is not None:
        return None

    if wiek_s is None:
        opis = ("ANI JEDNEGO zapisanego pomiaru limitów konta w całej historii "
                "— nie ma czym udowodnić, że podłoga kapitału jest liczona")
        wiek_min_do_rytmu = KS_RYTM_ROSNIE_PO_H * 60
    elif stary:
        opis = (f"ostatni zapisany pomiar limitów konta {wiek_s / 60:.0f} min temu "
                f"(próg {prog_s / 60:.0f} min = {prog_s // POMIAR_CO_S} rytmy po "
                f"{POMIAR_CO_S} s) — bezpiecznik nie raportuje")
        wiek_min_do_rytmu = wiek_s / 60
    else:
        # Pomiar swiezy, ale niekompletny: czesc limitow mierzy sie, czesc nigdy.
        opis = "pomiar limitów konta jest NIEPEŁNY"
        wiek_min_do_rytmu = KS_RYTM_ROSNIE_PO_H * 60

    if braki:
        opis += f"; bez ani jednego pomiaru: {', '.join(braki)}"

    # NAWROT PO PRZERWIE TO ZDARZENIE NOWE, A NIE CIAG DALSZY POPRZEDNIEGO.
    #
    # Pomiar zamilkl, poszedl alarm, ktos to naprawil, pomiar wrocil - i po
    # godzinie zamilkl znowu. Sam rytm przypominania widzi wtedy "zgloszone
    # 60 minut temu, cisza" i tluminie DRUGA, osobna awarie, o ktorej nikt nie
    # wie. Dopoki kod wyjscia byl niezalezny od tlumienia, wada byla zamaskowana
    # (zadanie i tak swiecilo sie na czerwono); po zwiazaniu obu kanalow jednym
    # warunkiem nawrot przepadlby w obu naraz, wiec musi byc rozpoznany tutaj.
    #
    # Rozpoznanie nie potrzebuje zadnej nowej kolumny ani stanu: cisza zaczela
    # sie `wiek_s` sekund temu, wiec zgloszenie STARSZE od niej opisywalo cisze
    # POPRZEDNIA. To ten sam chwyt, ktorym `engine.down` porownuje sie
    # z `last_beat_at`, a kill-switch z `triggered_at`.
    #
    # WYLACZNIE dla ciszy z przeterminowanego pomiaru. Limit bez ANI JEDNEGO
    # pomiaru w historii (`braki`) i brak calej tabeli (`wiek_s is None`) nie
    # maja "poczatku ciszy", ktory moglby sie przesunac - stan trwa od zawsze
    # i nie ma czego nawracac. Liczenie nawrotu ze swiezego `wiek_s` przy
    # niepelnym pomiarze dawaloby "zgloszenie starsze niz dwie minuty", czyli
    # alarm przy kazdym przebiegu.
    nawrot = (stary and od_zgloszenia_min is not None
              and od_zgloszenia_min > wiek_s / 60)

    odstep_h = odstep_przypomnienia_ks(wiek_min_do_rytmu)
    return {"powod": opis, "odstep_h": odstep_h, "nawrot": nawrot,
            "zglos": (od_zgloszenia_min is None or nawrot
                      or od_zgloszenia_min >= odstep_h * 60)}


async def sprawdz_cisze_pomiaru(fetch_all, notify, sucho: bool = False) -> int:
    """Czy pomiar limitow konta zostawia slad. Zwraca kod wyjscia (`kod_wyjscia`).

    AKTYWNY KILL-SWITCH NIE JEST JUZ USPRAWIEDLIWIENIEM CISZY - i usuniecie tego
    wyjatku (2026-09-12) jest wazniejsze niz cokolwiek innego w tej funkcji.

    Do tego dnia stal tu parametr `zatrzymane`: pary (engine, mode) z aktywnym
    kill-switchem, dla ktorych cisze wypisywano jako SPODZIEWANA i nie zglaszano.
    Uzasadnienie brzmialo sensownie - `evaluate_account` wracalo w punkcie 1
    przed pierwszym pomiarem, wiec wierszy faktycznie nie bylo, a drugi alarm
    o systemie, ktory juz alarmuje, uczy odklikiwac oba.

    Skutek byl dokladnie odwrotny do zamierzonego. Czujka przestawala widziec
    bezpiecznik W JEDYNYM STANIE, W KTORYM ZOSTAJE JEDNO PYTANIE: czy on jeszcze
    liczy. Przez szesc dob produkcji (zmierzone 2026-09-12: ostatni pomiar sprzed
    9629 minut, `min_equity` bez ani jednego w historii) nie istnial zaden dowod,
    ze ocena limitow w ogole sie wykonuje - a wygladalo to na stan wyjasniony.

    Wyjatek byl obejsciem wady, ktora nalezalo naprawic u zrodla, i zostala
    naprawiona: `risk_rules.pomiar_konta` mierzy bezwarunkowo, wiec zatrzymane
    konto produkuje wiersze tak samo jak handlujace. Cisza w tej tabeli znaczy
    od teraz jedno i to samo w kazdym stanie: NIKT NIE MIERZY.

    Co zostaje z argumentu o drugim alarmie: zostaje rytm przypominania
    (`ocen_cisze_pomiaru`) - ten sam, ktory tlumi powtorki przy kill-switchu.
    Tlumi POWTORKE, a nie PIERWSZE zgloszenie, i to jest cala roznica.
    """
    wiersze = await fetch_all(
        "SELECT d.engine::text AS engine, d.mode::text AS mode,"
        "       round(extract(epoch from (now() - max(u.ostatni)))) AS wiek_s,"
        "       array_agg(d.name ORDER BY d.name)"
        "         FILTER (WHERE u.ostatni IS NULL) AS nigdy,"
        "       h.status AS status,"
        "       round(extract(epoch from (now() - h.last_beat_at))/60)"
        "         AS puls_silnika_min,"
        "       round(extract(epoch from (now() - dh.last_beat_at))/60)"
        "         AS puls_dozorcy_min"
        "  FROM risk.limit_def d"
        "  LEFT JOIN LATERAL (SELECT max(event_time) AS ostatni"
        "                       FROM risk.limit_usage WHERE limit_def_id = d.id)"
        "         u ON true"
        "  LEFT JOIN risk.engine_heartbeat h"
        "         ON h.engine = d.engine AND h.mode = d.mode"
        "  LEFT JOIN risk.dozorca_heartbeat dh"
        "         ON dh.nazwa = 'dozorca-ryzyka:' || d.engine::text"
        "                       || ':' || d.mode::text"
        " WHERE d.enabled AND d.name = ANY(%s)"
        " GROUP BY d.engine, d.mode, h.status, h.last_beat_at, dh.last_beat_at"
        " ORDER BY 1, 2",
        (list(LIMITY_KONIECZNE),))

    zgloszone = trwale = 0
    odbiorcy = None
    for w in wiersze:
        wiek_s = None if w["wiek_s"] is None else float(w["wiek_s"])

        # CZY KTOKOLWIEK, KTO MIALBY MIERZYC, W OGOLE ZYJE.
        #
        # `risk.limit_def` ma wlaczone limity takze dla silnikow, ktorych jeszcze
        # nie ma: `equity` stoi ze statusem `halted` od 2026-08-27 i nie ma ani
        # jednego pomiaru w historii. Bez tego warunku ta czujka alarmowalaby
        # o nim przy kazdym przebiegu, w nieskonczonosc - a czujka, ktora
        # krzyczy o rzeczy zatrzymanej swiadomie, uczy przewijac takze te, ktora
        # krzyczy o awarii.
        #
        # To NIE JEST poszerzenie wyjatku na kazdy martwy proces. Sedno tej
        # czujki brzmi "procesy zyja, a limity nie sa mierzone" - stan, ktorego
        # `--co zywotnosc` nie widzi z definicji. Gdy zaden z dwoch pulsow nie
        # bije, opis stanu nalezy do TAMTEJ czujki (`engine.down`, krytyczne,
        # zawsze wlaczone) i powtarzanie go tutaj byloby echem, nie informacja.
        #
        # Wystarczy JEDEN zywy puls, bo do zapisu pomiaru wystarczy jeden proces:
        # zawieszony silnik przy zywym dozorcy nadal produkuje wiersze, wiec ich
        # brak jest wtedy prawdziwym znaleziskiem.
        mierzacy = [w["puls_silnika_min"] if w["status"] == "running" else None,
                    w["puls_dozorcy_min"]]
        zywy = any(p is not None and float(p) <= MARTWY_PO_MIN for p in mierzacy)
        if not zywy:
            print(f"pomijam pomiar limitów {w['engine']}/{w['mode']} — "
                  f"ani silnik (status {w['status'] or 'brak pulsu'}), ani dozorca "
                  f"nie bije; to opisuje `--co żywotność`, nie ta czujka")
            continue

        ostatnie = await fetch_all(
            "SELECT round(extract(epoch from (now() - max(created_at)))/60)"
            "         AS od_zgloszenia_min"
            " FROM notify.notification"
            " WHERE event_code = 'risk.pomiar_zamilkl' AND engine::text = %s",
            (w["engine"],))
        od_min = ostatnie[0]["od_zgloszenia_min"] if ostatnie else None
        od_min = None if od_min is None else float(od_min)

        werdykt = ocen_cisze_pomiaru(wiek_s, w["nigdy"], od_min)
        if werdykt is None:
            print(f"pomiar limitów {w['engine']}/{w['mode']}: świeży "
                  f"({wiek_s / 60:.0f} min temu)")
            continue

        print(f"  BEZPIECZNIK NIE RAPORTUJE {w['engine']}/{w['mode']}: "
              f"{werdykt['powod']}")

        if not werdykt["zglos"]:
            # Znalezisko realne, ale nie nowe: powiadomienie o TEJ ciszy poszlo
            # i rytm jeszcze trwa. Log krzyczy wyzej, kod wyjscia milczy razem
            # z kanalem powiadomien - patrz `kod_wyjscia`.
            trwale += 1
            print(f"    (zgłoszone {od_min:.0f} min temu, przypomnienie co "
                  f"{werdykt['odstep_h']} h — cisza w kanale powiadomień)")
            continue

        zgloszone += 1
        if werdykt["nawrot"]:
            # Nawrot MA SIE PRZEDSTAWIC. Bez tego zdania w logu drugi alarm
            # o tej samej tresci co poprzedni wyglada na powtorke, ktora ktos
            # zaraz zacznie przewijac - a to jest osobna awaria.
            print(f"    NAWRÓT: poprzednie zgłoszenie sprzed {od_min:.0f} min "
                  f"dotyczyło INNEJ ciszy — ta zaczęła się później, więc idzie "
                  f"jak nowe zdarzenie, bez czekania na rytm")
        if sucho:
            print("    tryb suchy, powiadomienie nie wysyłane.")
            continue

        if odbiorcy is None:
            odbiorcy = await fetch_all("SELECT id FROM app.\"user\" WHERE is_active AND role = 'owner'")
        tytul = f"Bezpiecznik przestał raportować ({w['engine']})"
        tresc = (
            f"{werdykt['powod']}.\n\n"
            f"To NIE jest informacja o stracie. To informacja o tym, że nie "
            f"wiadomo, czy strata byłaby zauważona: ocena limitów konta "
            f"(`RiskKernel.check_account`) zapisuje pomiar co {POMIAR_CO_S} s "
            f"i przestała to robić.\n\n"
            f"Handel może iść dalej — a to jest właśnie powód do niepokoju, bo "
            f"kill-switch nie zadziała na podstawie liczby, której nikt nie "
            f"policzył.\n\n"
            f"Sprawdź w tej kolejności:\n"
            f"  kubectl logs deploy/trader-dozorca --tail=50\n"
            f"  kubectl logs deploy/trader-engine --tail=50\n"
            f"  SELECT max(event_time) FROM risk.limit_usage;")
        for u in odbiorcy:
            await notify(u["id"], "risk.pomiar_zamilkl", tytul, tresc,
                         engine=w["engine"], severity="critical",
                         data={"mode": w["mode"], "wiek_s": wiek_s,
                               "nigdy": list(w["nigdy"] or []),
                               "prog_s": POMIAR_CISZA_S})
        print(f"    powiadomiono: {len(odbiorcy)} odbiorców")

    return kod_wyjscia(zgloszone, trwale, "bezpiecznik nie raportuje")


async def sprawdz_porzucona_probe(fetch_all, notify, sucho: bool = False) -> int:
    """Czy proba pozarowa zginela miedzy podniesieniem progu a jego zwrotem.

    CO TO JEST ZA STAN I DLACZEGO MA WLASNA CZUJKE

    `proba_killswitcha.py` podnosi `min_equity` powyzej kapitalu i przywraca go
    w bloku `finally`. Ten sam UPDATE ustawia `proba_do = now() + OKNO_PROBY_S`,
    a `finally` kasuje znacznik razem z wartoscia. Dlatego `proba_do` PRZESZLE
    i wciaz NIE-NULL to podpis jednego konkretnego zdarzenia: proces zginal
    miedzy jednym a drugim.

    A wtedy podniesiony prog ZOSTAJE NA STALE. `min_equity` ma regule `reczne`
    (migracja 0029), wiec `wznowienie.wznow` go nie rusza - silnik stoi
    bezterminowo, a kazde reczne zwolnienie kill-switcha konczy sie ponownym
    wystrzeleniem w nastepnym cyklu, bo przyczyna nadal tam jest. Czyli
    dokladnie ta awaria, przed ktora kill-switch ma chronic.

    DLACZEGO TO NIE JEST ZBEDNE PO DOLOZENIU HANDLERA SIGTERM (2026-09-23)

    Handler zalatwia SIGTERM, a `activeDeadlineSeconds` podniesiony powyzej
    `OKNO_PROBY_S` sprawia, ze Job nie jest zabijany w polowie wlasnej pracy.
    Zostaje SIGKILL po okresie karencji, OOMKill, zniknienie wezla i twarde
    padniecie interpretera - zadnego z nich nie ominie kod w tamtym pliku.
    Ta czujka jest wiec OSTATNIA warstwa i jedyna, ktora dziala, gdy proces
    nie mial szansy niczego wykonac.

    CZEGO TA CZUJKA NIE ROBI: NIE PRZYWRACA WARTOSCI. Nie da sie - nigdzie nie
    ma zapisanego progu SPRZED proby. `risk.limit_def` trzyma jedna wartosc,
    a historii limitow w tej bazie nie ma. Samoczynny powrot wymaga KOLUMNY
    (np. `wartosc_przed_proba` + termin), czyli migracji i nadania dla roli
    `trader_proba` - i to jest zaleznosc spoza tej poprawki, opisana
    w raporcie. Do tego czasu ta czujka zamienia stan niewidoczny w stan
    zgloszony, co jest cala roznica miedzy "silnik stoi i nikt nie wie"
    a "mail o 05:40".

    TLUMIENIE JAK WSZEDZIE W TYM PLIKU: powiadomienie wyslane PO `proba_do`
    znaczy "ta porzucona proba jest juz zgloszona". Nastepna porzucona proba
    ma pozniejsze `proba_do`, wiec odezwie sie na nowo - nawrot za darmo,
    bez drugiego mechanizmu.
    """
    porzucone = await fetch_all(
        "SELECT engine::text AS engine, mode::text AS mode, name, value,"
        "       proba_do,"
        "       round(extract(epoch from (now() - proba_do))/60) AS od_min"
        "  FROM risk.limit_def"
        " WHERE proba_do IS NOT NULL AND proba_do < now()"
        " ORDER BY proba_do")
    if not porzucone:
        return 0

    odbiorcy = await fetch_all("SELECT id FROM app.\"user\" WHERE is_active AND role = 'owner'")
    zgloszone = trwale = 0
    for p in porzucone:
        print(f"  PORZUCONA PRÓBA POŻAROWA {p['engine']}/{p['mode']}: limit "
              f"{p['name']} = {float(p['value']):.2f}, okno próby minęło "
              f"{float(p['od_min']):.0f} min temu, a znacznik nadal stoi — "
              f"próba zginęła między podniesieniem progu a jego przywróceniem")
        if sucho:
            zgloszone += 1
            continue
        juz = await fetch_all(
            "SELECT 1 FROM notify.notification WHERE event_code = 'engine.down'"
            "   AND data->>'porzucona_proba' = %(k)s AND created_at > %(od)s"
            " LIMIT 1",
            {"k": f"{p['engine']}/{p['mode']}/{p['name']}", "od": p["proba_do"]})
        if juz:
            trwale += 1
            print("    (zgłoszone po zamknięciu tego okna próby — cisza)")
            continue
        zgloszone += 1
        tytul = (f"Próba pożarowa nie posprzątała po sobie "
                 f"({p['engine']}/{p['mode']})")
        tresc = (
            f"Limit `{p['name']}` ma wartość {float(p['value']):.2f} i znacznik "
            f"okna próby z {p['proba_do']:%Y-%m-%d %H:%M}, czyli sprzed "
            f"{float(p['od_min']):.0f} minut. Znacznik kasuje się razem "
            f"z przywróceniem progu, więc ten stan znaczy, że proces próby "
            f"zginął pomiędzy jednym a drugim.\n\n"
            f"PODNIESIONY PRÓG ZATRZYMUJE SILNIK NA STAŁE: `min_equity` ma "
            f"regułę wznowienia `reczne`, więc usługa wznowienia go nie ruszy, "
            f"a każde ręczne zwolnienie kill-switcha kończy się ponownym "
            f"wystrzeleniem w następnym cyklu — przyczyna nadal istnieje.\n\n"
            f"CO ZROBIĆ: przywrócić wartość limitu sprzed próby (zakładka "
            f"Ryzyko), a potem zwolnić kill-switcha. W tej kolejności — "
            f"odwrotna nic nie daje.")
        for u in odbiorcy:
            await notify(u["id"], "engine.down", tytul, tresc,
                         engine=p["engine"], severity="critical",
                         data={"porzucona_proba":
                               f"{p['engine']}/{p['mode']}/{p['name']}",
                               "limit": p["name"], "wartosc": float(p["value"])})
    if sucho:
        print("Tryb suchy — powiadomienie nie wysyłane.")
    if trwale:
        print(f"  {trwale} × porzucona próba pożarowa: stan TRWAŁY i JUŻ "
              f"ZGŁOSZONY, powiadomienie w tłumieniu — kod wyjścia z tego "
              f"przebiegu tego NIE powtarza (opis stanu wyżej)")
    return zgloszone


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

    DWA NIEZALEZNE SYGNALY, BO KAZDY Z NICH ODPOWIADA NA INNE PYTANIE

      * powiadomienie mowi, ze COS SIE STALO. Idzie raz przy zadzialaniu,
        a potem w rytmie rosnacym z wiekiem zdarzenia (`ocen_kill_switch`) -
        bo alarm powtarzany co dziesiec minut uczy sie odklikiwac;
      * log mowi, w JAKIM STANIE JEST SYSTEM. Linia "KILL-SWITCH ..." idzie przy
        KAZDYM przebiegu, takze w oknach ciszy powiadomien, wiec `kubectl logs`
        odpowiada na to pytanie zawsze i bez pytania nikogo o pamiec.

    Kod wyjscia nie jest trzecim torem i nie opisuje stanu - powtarza werdykt
    pierwszego toru (patrz `kod_wyjscia`). Powod, dla ktorego jednak opisywal
    stan przez dobe, i pomiar, ktory to obalil, stoja w docstringu modulu.

    Zdarzenie zwolnione (`released_at`) pomijamy w calosci - problem zostal
    obsluzony przez czlowieka, a powtarzanie alarmu o nim nauczyloby ignorowac
    alarm o nastepnym.
    """
    # `proba_w_toku` - czy zdarzenie powstalo w oknie proby pozarowej TEGO
    # limitu (`risk.limit_def.proba_do`, migracja 0045). Liczone zegarem bazy,
    # tym samym co `wiek_min`. LEFT JOIN, bo stare zdarzenia nie maja
    # `limit_name`, a limit o zasiegu instrumentu nie jest tym, ktory proba
    # podnosi - `scope = 'engine'` zawezajac do jednego wiersza na limit.
    #
    # DRUGI WARUNEK, `k.triggered_at >= d.updated_at`, DOLOZONY 2026-09-23.
    # Sam `proba_do > now()` mowi wylacznie "na TYM LIMICIE trwa proba", a nie
    # "TO KONKRETNE zdarzenie jest jej skutkiem" - a proba podnosi prog,
    # znacznik okna i `updated_at` JEDNYM UPDATE-em (proba_killswitcha.py),
    # wiec `d.updated_at` jest dokladnie chwila podniesienia progu. Zdarzenie
    # STARSZE od niej nie ma prawa pochodzic z tej proby, choc miesci sie
    # w jej oknie: to sytuacja po probie, ktora zginela miedzy podniesieniem
    # a `finally` i zostawila termin do wygasniecia.
    #
    # CZEGO TO NADAL NIE ROZSTRZYGA, I TRZEBA O TYM WIEDZIEC. Prawdziwy spadek
    # kapitalu W TRAKCIE okna proby daje zdarzenie nieodrozialne od jej skutku:
    # ten sam limit, ten sam powod, ten sam wiersz wstawiony przez ten sam
    # `RiskKernel` (migracja 0045 mowi to wprost i jest to CELOWE - proba
    # mierzy prawdziwa sciezke, nie atrape). Rozstrzygnelaby dopiero kolumna
    # w `risk.killswitch_event` z identyfikatorem zdarzenia zapisanym przez
    # probe, czyli migracja plus nadanie. Do tego czasu skutek jest ograniczony
    # w czasie: `proba_do` wygasa po `OKNO_PROBY_S`, wiec zdarzenie, ktore
    # przezyje okno, wraca przy nastepnym przebiegu czujki (co 10 min) do
    # galezi zwyklego kill-switcha razem z kodem wyjscia - a tresc
    # powiadomienia nizej nie zapewnia juz, ze to na pewno proba.
    zdarzenia = await fetch_all(
        "SELECT k.id, k.engine::text AS engine, k.mode::text AS mode, k.reason,"
        "       k.triggered_at,"
        "       round(extract(epoch from (now() - k.triggered_at))/60) AS wiek_min,"
        "       coalesce(d.proba_do > now() AND k.triggered_at >= d.updated_at,"
        "                false) AS proba_w_toku,"
        "       d.proba_do, d.wznowienie, d.max_wznowien"
        " FROM risk.killswitch_event k"
        " LEFT JOIN risk.limit_def d"
        "   ON d.engine = k.engine AND d.mode = k.mode AND d.name = k.limit_name"
        "  AND d.scope = 'engine'"
        " WHERE k.released_at IS NULL"
        " ORDER BY k.triggered_at")

    # CISZA SAMEGO POMIARU - patrz `sprawdz_cisze_pomiaru`. Idzie PRZED wyjsciem
    # po pustej liscie zdarzen, bo to wlasnie wtedy jest jedynym pytaniem, jakie
    # zostaje: brak zatrzyman znaczy albo "nic zlego sie nie dzieje", albo
    # "nikt nie patrzy", a te dwa stany wygladaly do tej pory identycznie.
    #
    # LISTA ZATRZYMAN NIE JEST JUZ TEJ CZUJCE PODAWANA (2026-09-12). Sluzyla
    # wylacznie do uciszania jej przy aktywnym kill-switchu, czyli w jedynym
    # stanie, w ktorym pytanie "czy bezpiecznik liczy" nie ma innego zrodla
    # odpowiedzi - uzasadnienie w docstringu `sprawdz_cisze_pomiaru`.
    zgloszone = await sprawdz_cisze_pomiaru(fetch_all, notify, sucho=sucho)
    # PORZUCONA PROBA POZAROWA - tak samo PRZED wyjsciem po pustej liscie
    # zdarzen i z tego samego powodu. Podniesiony prog zostawiony przez probe,
    # ktora zginela, potrafi istniec BEZ czynnego zdarzenia: wystarczy, ze ktos
    # zwolnil kill-switcha recznie. Silnik wystrzeli wtedy znowu w nastepnym
    # cyklu, bo przyczyna nadal tam jest - a czujka, ktora konczy sie na
    # `if not zdarzenia`, akurat tego stanu nigdy by nie zobaczyla.
    zgloszone += await sprawdz_porzucona_probe(fetch_all, notify, sucho=sucho)
    trwale = 0

    if not zdarzenia:
        print("kill-switch: brak aktywnych zdarzeń")
        return zgloszone

    odbiorcy = await fetch_all("SELECT id FROM app.\"user\" WHERE is_active AND role = 'owner'")
    for z in zdarzenia:
        wiek_min = float(z["wiek_min"])
        # SKUTEK PROBY POZAROWEJ - POWIADAMIAMY, ALE NIE ALARMUJEMY.
        #
        # Proba (`proba_killswitcha.py`) podnosi prog, silnik zadziala, a ogniwo 4
        # proby brzmi "czy dozor zglosil" - i tym dozorem JEST TA CZUJKA. Jedyne
        # powiadomienie `killswitch.triggered` w dziennikach 20-22.09 pochodzi
        # z jej przebiegu o 03:30, nie z `trader-dozorca`. Wyciszenie tu
        # kanalu powiadomien (pierwsza wersja tej poprawki, 2026-09-22 rano)
        # zlamaloby probe: ogniwo 4 czekaloby 660 s na nic.
        #
        # Wiec powiadomienie IDZIE - ale przedstawia sie jako proba, bo odbiorca
        # o 05:30 ma wiedziec, ze to sprawdzian kanalu, nie strata. Nie idzie
        # natomiast do kodu wyjscia: czerwone zadanie co rano o stalej porze
        # to alarm, ktorego po tygodniu nikt nie czyta - a wlasnie o tej porze
        # prawdziwy nocny kill-switch bylby najbardziej prawdopodobny.
        proba = bool(z["proba_w_toku"])
        if proba:
            print(f"  PRÓBA POŻAROWA {z['engine']}/{z['mode']}: zdarzenie "
                  f"id={z['id']} ({wiek_min:.0f} min temu) jest jej skutkiem, "
                  f"okno do {z['proba_do']:%H:%M:%S} — powiadamiam jako próbę, "
                  f"kod wyjścia bez zmian")
        else:
            print(f"  KILL-SWITCH {z['engine']}/{z['mode']}: {z['reason']} "
                  f"({wiek_min:.0f} min temu)")

        # WIEK ostatniego zgloszenia, a nie sam fakt jego istnienia. Warunek
        # "istnieje powiadomienie mlodsze niz triggered_at" byl prawdziwy na
        # zawsze od pierwszego wyslania - i to on produkowal cisze bez konca.
        # `max(...)` po zbiorze pustym daje NULL, wiec zapytanie ma jeden ksztalt
        # w obu przypadkach i nie ma tu drugiej sciezki do rozjechania.
        #
        # `created_at > k.triggered_at` ZALATWIA TEZ NAWROT, i to jest warunek,
        # od ktorego zalezy sensownosc kodu wyjscia po zmianie z 2026-09-10.
        # Bezpiecznik zwolniony i wystrzelony ponownie to NOWY wiersz
        # w `risk.killswitch_event` (RiskKernel._trigger_killswitch dopisuje nowy
        # wiersz, nigdy nie odswieza starego), a stary wypada z zapytania
        # przez `released_at IS NULL`. Nowe `triggered_at` jest pozniejsze niz
        # wszystkie powiadomienia o poprzednim epizodzie, wiec `od_min` wychodzi
        # NULL, `ocen_kill_switch` mowi "pierwsze" i drugi kill-switch znowu
        # maluje zadanie na czerwono. Bez tej granicy przypomnienie sprzed
        # godziny tlumiloby CUDZE zdarzenie - a od zwiazania kodu wyjscia
        # z tlumieniem tlumiloby je w obu kanalach naraz.
        #
        # KOD ZALEZY OD TEGO, CZY TRWA PROBA (2026-10-03, audyt Fable F-5.2).
        # Skutek proby idzie kodem `killswitch.proba` z severity `info`, wiec
        # w oknie proby rytm liczy oba kody - inaczej kazdy przebieg w oknie
        # dokladalby kolejne powiadomienie. Poza oknem liczy WYLACZNIE
        # `killswitch.triggered`: zdarzenie, ktore przezylo okno, moglo byc
        # prawdziwym spadkiem kapitalu, a w oknie dostalo tylko `info`. Pierwszy
        # przebieg po oknie ma wtedy wyslac alarm krytyczny od razu, a nie uznac
        # sprawy za zgloszona.
        kody_rytmu = ("event_code IN ('killswitch.triggered', 'killswitch.proba')"
                      if proba else "event_code = 'killswitch.triggered'")
        ostatnie = await fetch_all(
            "SELECT round(extract(epoch from (now() - max(created_at)))/60)"
            "         AS od_zgloszenia_min"
            " FROM notify.notification"
            f" WHERE {kody_rytmu}"
            "   AND engine::text = %s AND created_at > %s",
            (z["engine"], z["triggered_at"]))
        od_min = ostatnie[0]["od_zgloszenia_min"] if ostatnie else None
        od_min = None if od_min is None else float(od_min)

        werdykt = ocen_kill_switch(wiek_min, od_min)
        if werdykt is None:
            # Cisza jest wynikiem, wiec musi byc widoczna w logu razem
            # z powodem ORAZ z jawnym zastrzezeniem, ze nie jest zgoda na stan.
            # Inaczej nie da sie odroznic czujki, ktora milczy z rytmu, od
            # czujki, ktora zamilkla na dobre.
            #
            # Linia "KILL-SWITCH ..." wyzej poszla juz bezwarunkowo - to ona
            # niesie stan przy kazdym z 144 przebiegow doby. Tutaj dopisujemy
            # tylko powod ciszy, a `trwale` przenosi ten sam werdykt do kodu
            # wyjscia: znalezisko realne, ale NIE NOWE.
            if not proba:
                trwale += 1
            print(f"    (zgłoszone {od_min:.0f} min temu, przypomnienie co "
                  f"{odstep_przypomnienia_ks(wiek_min)} h — cisza w kanale "
                  f"powiadomień; handel STOI, stan opisany w linii wyżej)")
            continue

        if not proba:
            zgloszone += 1
        if sucho:
            print(f"    {werdykt['powod']} — tryb suchy, powiadomienie nie wysyłane.")
            continue

        godzin = wiek_min / 60
        if proba:
            tytul = (f"PRÓBA POŻAROWA (prawdopodobnie): kill-switch zadziałał "
                     f"({z['engine']})")
            # TRESC MOWI, CZEGO TA CZUJKA NIE WIE - bo do 2026-09-23 mowila
            # "To PRÓBA POŻAROWA, nie strata" jako pewnik. Znacznik `proba_do`
            # swiadczy o tym, ze na tym limicie TRWA proba, a nie o tym, ze
            # to zdarzenie jest jej skutkiem; zdarzenie proby jest z zalozenia
            # nieodrozialne od prawdziwego (migracja 0045). Prawdziwy spadek
            # kapitalu w tym samym oknie dostawal wiec zapewnienie, ze to tylko
            # sprawdzian kanalu - i to o porze, o ktorej wedlug komentarza
            # chartu prawdziwy nocny kill-switch jest najbardziej prawdopodobny.
            wstep = (f"Na limicie trwa PRÓBA POŻAROWA (okno do "
                     f"{z['proba_do']:%H:%M:%S}), więc to zdarzenie jest "
                     f"NAJPEWNIEJ jej skutkiem: próg został podniesiony celowo, "
                     f"żeby sprawdzić, czy silnik się zatrzyma i czy to "
                     f"powiadomienie dojdzie. Próba sama zwolni bezpiecznik "
                     f"i przywróci próg.\n\n"
                     f"CZEGO TO NIE DOWODZI: zdarzenie próby jest "
                     f"nieodróżnialne od prawdziwego — ten sam limit, ten sam "
                     f"powód, ten sam wiersz. Jeśli kapitał spadł naprawdę "
                     f"w tym samym oknie, wygląda to DOKŁADNIE tak samo. "
                     f"Sprawdź stan rachunku, zanim uznasz sprawę za zamkniętą; "
                     f"jeśli bezpiecznik będzie aktywny po zamknięciu okna, "
                     f"kolejny przebieg czujki zgłosi go już jako zwykły "
                     f"kill-switch.\n\n")
        elif werdykt["pierwsze"]:
            tytul = f"Kill-switch zadziałał ({z['engine']})"
            wstep = ""
        else:
            tytul = (f"Kill-switch NADAL aktywny ({z['engine']}) — "
                     f"{godzin:.0f} h bez zwolnienia")
            # PRZYPOMNIENIE MA SIE PRZEDSTAWIC. Powtorka nieodrozialna od nowego
            # alarmu kaze odbiorcy sprawdzac od zera, czy to nie druga awaria.
            wstep = (f"To PRZYPOMNIENIE, nie nowe zdarzenie: handel stoi od "
                     f"{godzin:.0f} h. Kolejne za {werdykt['odstep_h']} h, "
                     f"dopóki kill-switch nie zostanie zwolniony.\n\n")
        tresc = (f"{wstep}{z['reason']}\n\n"
                 f"{zdanie_o_wznowieniu(z.get('wznowienie'), z.get('max_wznowien'))}")
        # Proba: `info`, osobny kod - 15 nieprzeczytanych "krytycznych" z osmiu
        # dni prob zaslanialo dzwonek, w ktorym prawdziwy kill-switch by utonal
        # (F-5.2). Ogniwo 4 proby szuka powiadomienia pod obydwoma kodami.
        kod_zdarzenia = "killswitch.proba" if proba else "killswitch.triggered"
        for u in odbiorcy:
            await notify(u["id"], kod_zdarzenia, tytul, tresc,
                         engine=z["engine"],
                         severity="info" if proba else "critical",
                         data={"reason": z["reason"], "event_id": z["id"],
                               "wiek_min": wiek_min,
                               "przypomnienie": not werdykt["pierwsze"],
                               "proba": proba,
                               "odstep_h": werdykt["odstep_h"]})
        print(f"    powiadomiono: {len(odbiorcy)} odbiorców")

    # KOD WYJSCIA MOWI O ZDARZENIU, A NIE O STANIE - i to jest poprawka
    # z 2026-09-10, odwracajaca poprzednia. Historia obu jest tu wazniejsza niz
    # sam wynik, bo bez niej ktos trzeci zrobi to samo kolo jeszcze raz.
    #
    # RUNDA 1 (do 2026-09-09): zwracalo 1 wylacznie przy NOWYM zdarzeniu.
    # Uzasadnienie: "stan znany i czekajacy na reakcje nie moze malowac `kubectl
    # get jobs` na czerwono". Skutek: aktywny bezpiecznik, ktory zablokowal
    # handel cztery godziny wczesniej, mial zadanie zakonczone POWODZENIEM -
    # zatrzymany system meldowal zdrowie.
    #
    # RUNDA 2 (2026-09-09): niezerowy przez CALY czas blokady, bo "system JEST
    # ZATRZYMANY, wiec czerwone zadanie to jedyny prawdziwy opis stanu".
    # Skutek zmierzony nastepnego dnia: kill-switch aktywny od 2190 minut,
    # zadanie chodzi co 10 minut, wiec 144 padniete Joby na dobe, tyle samo
    # podow w stanie Error, mail z kazdego i aplikacja ArgoCD trwale Degraded.
    # Sygnal, ktorego nie da sie wygasic inaczej niz decyzja handlowa, przestal
    # byc sygnalem: monitoring wyglada tak samo przy zatrzymanym systemie
    # i przy zatrzymanym systemie, w ktorym dodatkowo padla czujka.
    #
    # BLAD OBU RUND JEST TEN SAM: proba wcisniecia opisu STANU w kanal o jednym
    # bicie, ktory z natury niesie "czy ten przebieg sie udal". Runda 1 gubila
    # stan, bo bit byl wolny; runda 2 zatkala bit stanem tak trwalym, ze nikt
    # go juz nie odczytywal.
    #
    # DZIS: kod wyjscia powtarza werdykt tlumienia powiadomien (`kod_wyjscia`),
    # a opis stanu ma wlasny, nieograniczony kanal - LOG. Linia "KILL-SWITCH
    # crypto/demo: ... (2190 min temu)" idzie przy kazdym przebiegu, bez wzgledu
    # na rytm, wiec `kubectl logs` odpowiada na pytanie o stan zawsze. Argument
    # "powiadomienie da sie przespac" zostaje w mocy i dlatego przypomnienie
    # NADAL podnosi kod wyjscia - raz na dobe, a nie 144 razy.
    return kod_wyjscia(zgloszone, trwale, "kill-switch aktywny")


# Ile moze minac od ostatniej udanej kopii, zanim uznamy to za problem.
#
# Zadanie chodzi raz na dobe o 03:17 CZASU LOKALNEGO, wiec 26 godzin daje dwie
# godziny zapasu na dlugi zrzut i na przesuniecie startu przez zajety wezel.
# Krotszy prog zglaszalby falszywy alarm przy kazdym wolniejszym przebiegu.
#
# Do 2026-09-12 stalo tu "03:17 UTC" i bylo to nieprawda. CronJob bez pola
# `timeZone` liczy harmonogram w strefie procesu kontrolera, a na tym klastrze
# jest to strefa hosta (Europe/Warsaw) - zmierzone `lastScheduleTime` dla
# `trader-backup` to 2026-09-12T01:17:00Z, czyli 03:17 CEST.
#
# SAM PROG JEST NA TO ODPORNY, bo jest DLUGOSCIA, a nie chwila - myli sie
# dopiero czlowiek porownujacy `lastScheduleTime` z godzina z chartu. Roznica
# godziny przy zmianie czasu miesci sie w tym samym zapasie.
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
#
# Mapa mieszka w `horizons`, bo ten sam przelicznik potrzebny jest potokowi
# cech do rozgrzewki. Tu zostaje sama nazwa, zeby czytelnik tego pliku dalej
# widzial, czym jest prog swiezosci.
from services.engine.horizons import MINUT_TIMEFRAME  # noqa: E402


def ocen_swiezosc(wiersze: list[dict], swiec_zwloki: int = SWIEC_ZWLOKI
                  ) -> list[tuple[str, str, str]]:
    """Ktore zrodla danych sa nieswieze. CZYSTA FUNKCJA.

    Zwraca listę (źródło, tytuł, treść) — pusta znaczy: wszystko świeże.

    ŹRÓDŁO JAKO OSOBNE POLE, a nie do wyłuskania z tytułu. Wołający tłumi
    powiadomienia PER ŹRÓDŁO (patrz `sprawdz_swiezosc`), więc potrzebuje
    klucza, po którym da się je rozróżnić. Wyciąganie go z tytułu działałoby
    do pierwszej zmiany sformułowania — ten sam błąd, przed którym ostrzega
    migracja 0029 przy `limit_name`, i ta sama zasada, dla której
    `ocen_egzekucje` zwraca kod zdarzenia osobno.
    """
    powody = []
    for w in wiersze:
        prog_min = w["minut_swiecy"] * swiec_zwloki
        if w["wiek_min"] is None:
            powody.append((
                w["co"],
                f"Brak danych: {w['co']}",
                f"Tabela {w['co']} nie ma ANI JEDNEGO wiersza. Silnik nie ma na "
                f"czym podejmować decyzji."))
            continue
        if w["wiek_min"] > prog_min:
            powody.append((
                w["co"],
                f"Dane nieświeże: {w['co']}",
                f"Najnowszy wpis sprzed {w['wiek_min']:.0f} min, próg to "
                f"{prog_min:.0f} min ({swiec_zwloki} świece po "
                f"{w['minut_swiecy']:.0f} min).\n\n"
                f"Pętla decyzyjna NIE ZGŁASZA tego sama: bije dalej i podejmuje "
                f"decyzje na tym, co ma. Stare cechy nie wyglądają jak awaria — "
                f"wyglądają jak rynek, który przestał dawać sygnały."))
    return powody


async def rynek_z_sesjami(fetch_all, engine: str) -> bool:
    """Czy rynek tego silnika ZAMYKA sie - z `ref.venue.is_24_7`.

    TA SAMA KOLUMNA, KTORA ROZSTRZYGA W `sesja.stan_sesji`, i to jest caly powod,
    dla ktorego ta funkcja istnieje zamiast przelacznika w chartcie. Lista
    "silniki z sesjami" wpisana do `values.yaml` albo do tego pliku byla by
    DRUGA KOPIA wiedzy o tym, ktory rynek zamyka - a rozjazd takiej kopii
    objawilby sie tu cisza albo alarmem co ranek, czyli dwoma stanami, ktorych
    ten plik tropi.

    DOMYSL IDZIE W STRONE ALARMU. Brak wpisu w `ref.venue` (albo gielda, ktorej
    nie obslugujemy) znaczy "nie wiem" - i wtedy zostaje zachowanie sprzed tej
    zmiany, czyli czujka mowi glosno. Domysl "ma sesje" wyciszalby czujke przez
    BRAK DANEJ, a to jest ten sam ksztalt bledu co `prog_min_equity` liczony
    z nieistniejacej liczby.

    `code = ANY(OBSLUGIWANE)` tak samo jak w `sesja.venue_silnika`: placeholder
    `ibkr` z seeda 0008 nie moze wygrac z gielda, na ktorej naprawde stoi konto.
    """
    from services.exchanges.fabryka import OBSLUGIWANE

    w = await fetch_all(
        "SELECT is_24_7 FROM ref.venue"
        " WHERE engine::text = %s AND code = ANY(%s) ORDER BY id LIMIT 1",
        (engine, list(OBSLUGIWANE)))
    return bool(w) and w[0]["is_24_7"] is False


async def poza_sesja(fetch_all, engine: str) -> bool:
    """Czy silnik sam zglosil, ze jego rynek jest teraz zamkniety.

    DLACZEGO PYTAMY SILNIK, A NIE WLASNEGO KALENDARZA

    Czujka swiezosci porownuje wiek danych z `now()`. Dla rynku z sesjami ta
    roznica rosnie KAZDEJ NOCY az do otwarcia, a przez weekend siega 65 godzin -
    czyli czujka zapalalaby sie co wieczor, przy w pelni zdrowym potoku. Alarm
    przychodzacy codziennie o tej samej porze bez powodu konczy sie wyciszeniem
    kanalu, a wyciszony kanal jest gorszym stanem niz brak czujki (ten sam
    wniosek co przy `engine.down` wyzej).

    Drugi kalendarz w tym pliku bylby DRUGA KOPIA wiedzy o godzinach sesji -
    tej samej, ktora `services/engine/sesja.py` bierze wprost z zegara gieldy.
    Kopie sie rozjezdzaja, a rozjazd objawilby sie tu cisza, nie awaria.

    Petla decyzyjna zapisuje ten stan do `risk.engine_heartbeat.detail` przy
    KAZDYM cyklu, czyli co piec minut, i robi to na podstawie odpowiedzi gieldy.
    To jest ta sama informacja, tylko juz w bazie.

    CZEGO TA FUNKCJA NIE UDAJE: gdy petla nie zyje, `detail` jest stary i moze
    mowic "poza sesja" dowolnie dlugo. Nie jest to luka, bo martwa petla nalezy
    do `sprawdz_zywotnosc` - ta czyta `last_beat_at` z tego samego wiersza
    i krzyczy niezaleznie od tresci `detail`.
    """
    w = await fetch_all(
        "SELECT (detail->>'poza_sesja')::boolean AS poza"
        "  FROM risk.engine_heartbeat WHERE engine::text = %s", (engine,))
    return bool(w and w[0]["poza"])


async def sprawdz_swiezosc(fetch_all, notify, sucho: bool = False,
                           bar: str = "4h", engine: str = "crypto") -> int:
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

    # RYNEK ZAMKNIETY NIE JEST NIESWIEZOSCIA. Patrz `poza_sesja` wyzej: cisza
    # jest tu wynikiem, wiec musi byc widoczna w logu, a nie zalatwiona
    # milczacym `return`.
    if await poza_sesja(fetch_all, engine):
        print(f"świeżość {engine}: rynek zamknięty (silnik zgłosił `poza_sesja`) "
              f"— wiek danych nie jest tu miarą awarii, pomijam")
        return 0

    # ZAPYTANIE FILTRUJE PO SILNIKU, I TO JEST NAPRAWA, NIE OZDOBA.
    #
    # Do 2026-09-24 oba czlony szly po CALEJ tabeli, bez `ref.instrument`.
    # `max(event_time)` bralo wiec najnowszy wiersz JAKIEGOKOLWIEK silnika,
    # a krypto dopisuje swiece co minute, 24/7 - czyli potok equity mogl byc
    # martwy tygodniami i ta czujka nie zauwazylaby tego ANI RAZU. Sprawdzone
    # na produkcji 2026-09-24: swiece equity stoja od poprzedniego zamkniecia,
    # a czujka meldowala "bez zastrzezen", bo patrzyla na krypto.
    #
    # NAZWA ZRODLA ZAWIERA SILNIK, bo to po niej tlumia sie powiadomienia
    # (`data->>'zrodlo'` nizej). Wspolna nazwa dla obu silnikow znaczylaby, ze
    # alarm o equity milknie, bo alarm o krypto byl wczesniej - czyli dokladnie
    # ta awaria, ktora ten plik tropi.
    wiersze = await fetch_all(
        "SELECT %(engine)s || ': market.candle (' || %(tf)s || ', źródło świecy '"
        "       || %(bar)s || ')' AS co,"
        "       %(min)s::numeric AS minut_swiecy,"
        "       round(extract(epoch from (now() - max(c.event_time)))/60) AS wiek_min,"
        # ZNACZNIK STANU dla tlumienia nizej - ta sama chwila, z ktorej liczy
        # sie wiek, tyle ze surowa. Liczona w bazie, bo drugi zegar w Pythonie
        # rozjechalby sie z `created_at` powiadomien, ktore zapisuje baza.
        "       max(c.event_time) AS ostatni"
        "  FROM market.candle c JOIN ref.instrument i ON i.id = c.instrument_id"
        " WHERE c.timeframe::text = %(tf)s AND i.engine::text = %(engine)s"
        " UNION ALL"
        " SELECT %(engine)s || ': feat.feature_value', %(min_bar)s::numeric,"
        "        round(extract(epoch from (now() - max(f.event_time)))/60),"
        "        max(f.event_time)"
        "   FROM feat.feature_value f JOIN ref.instrument i ON i.id = f.instrument_id"
        "  WHERE i.engine::text = %(engine)s",
        {"tf": h.source_timeframe, "bar": bar, "min": minut_zrodla,
         "min_bar": h.bar_minutes, "engine": engine})

    for w in wiersze:
        wiek = "brak danych" if w["wiek_min"] is None else f"{w['wiek_min']:.0f} min temu"
        print(f"świeżość {w['co']}: {wiek}")

    powody = ocen_swiezosc(wiersze)
    if not powody:
        print("  bez zastrzeżeń")
        return 0

    # POMIAR BEZ ALARMU - dla rynku z sesjami, dopoki nie umiemy policzyc wieku
    # danych W CZASIE SESJI.
    #
    # ZMIERZONE, nie przewidziane (produkcja 2026-09-27). Swiece 1h toru equity
    # siedza na siatce 12:00..20:00 UTC, a sesja otwiera sie 13:30 UTC. Prog to
    # trzy swiece zrodlowe, czyli 180 min. W chwili otwarcia najnowsza swieca
    # pochodzi z POPRZEDNIEGO zamkniecia (20:00 UTC dnia poprzedniego), czyli ma
    # okolo 1050 minut - a `poza_sesja` jest juz `false`, bo gielda zglasza
    # otwarcie. Czujka meldowalaby wiec "dane nieswieze" KAZDEGO RANKA, od 13:30
    # do chwili dopisania pierwszej swiecy dnia (okolo 14:20, `history-equity`
    # co 1200 s) - przy potoku w pelni zdrowym.
    #
    # Tlumienie per zrodlo tego nie zasypuje: klucz to "powiadomienie mlodsze
    # niz ostatni wpis tego zrodla", a ostatnim wpisem jest wczorajsze
    # zamkniecie, wiec wczorajszy alarm nie tlumi dzisiejszego. Wychodzi z tego
    # dokladnie jedno powiadomienie `critical` na zrodlo na kazda sesje, czyli
    # dwa dziennie - i jeden czerwony Job, ktory uczy odklikiwac `engine.down`
    # takze wtedy, gdy naprawde padnie potok krypto.
    #
    # CZEGO BRAKUJE, ZEBY TO ALARMOWALO UCZCIWIE: chwili OTWARCIA biezacej
    # sesji. `sesja.StanSesji` oddaje `nastepne_otwarcie` i `nastepne_zamkniecie`
    # (czyli, w sesji, jutrzejsze otwarcie i dzisiejsze zamkniecie), a
    # `risk.engine_heartbeat.detail` niesie sam `poza_sesja` - stan dwuwartosciowy
    # bez historii. Z samych swiec tego nie da sie odtworzyc: "rynek wlasnie
    # wstal" i "potok stoi" maja w `market.candle` IDENTYCZNY obraz - brak
    # nowych wierszy. Drugi kalendarz w tym pliku byl by druga kopia wiedzy
    # o godzinach sesji, przed ktora broni `poza_sesja` wyzej.
    #
    # Dopoki tej chwili nie zapisuje zaden proces, ten tor dostaje POMIAR:
    # wiek obu zrodel w logu przy kazdym przebiegu (dzis nie ma go tam wcale),
    # bez powiadomienia i bez czerwonego Joba. Cisza w logu byla by tu gorsza
    # od obu tych rzeczy.
    if await rynek_z_sesjami(fetch_all, engine):
        for _zrodlo, tytul, _tresc in powody:
            print(f"  {tytul}")
        print(f"  POMIAR BEZ ALARMU: dla `{engine}` wiek danych liczony wobec "
              f"`now()` nie jest miarą awarii potoku — po otwarciu sesji "
              f"najnowsza świeca pochodzi z poprzedniego zamknięcia. "
              f"Powiadomienia NIE wysyłam; proces liczący te dane pilnuje "
              f"`--co workery`, a chwili otwarcia sesji nie zapisuje dziś "
              f"żaden proces.")
        return 0

    # TLUMIENIE - DO 2026-09-23 TEJ CZUJKI NIE MIALA W OGOLE.
    #
    # `engine.down` jest `always_on` (migracja 0011), wiec katalogowy cooldown
    # 600 s NIE DZIALA na nia w ogole: `notifications.py` sprawdza cooldown
    # tylko dla zdarzen bez tej flagi. A zadanie chodzi co 10 minut
    # (values.yaml, `zywotnoscCron.schedule`), wiec staly w piatek wieczorem
    # worker `market` dawal do poniedzialku okolo 430 maili o wadze `critical`
    # i tyle samo padnietych Jobow - dokladnie ta awaria, ktora docstring
    # modulu opisuje jako zmierzona na produkcji 2026-09-10 i ktora naprawiono
    # w pieciu innych czujkach. Ta i `sprawdz_egzekucje` zostaly pominiete.
    #
    # ZNACZNIKIEM STANU JEST WIEK DANYCH, tak jak przy silniku jest nim
    # `last_beat_at` (patrz `sprawdz_zywotnosc`). Powiadomienie wyslane PO
    # ostatnim wpisie danego zrodla znaczy "ta awaria jest juz zgloszona".
    # NAWROT rozpoznaje sie za darmo i bez drugiego mechanizmu: gdy potok
    # wroci, `max(event_time)` przesunie sie do przodu, stare powiadomienie
    # przestanie tlumic i kolejna awaria znowu zapali zadanie na czerwono.
    #
    # PER ZRODLO, nie zbiorczo: stojace swiece i stojace cechy to dwie rozne
    # awarie w dwoch roznych podach. Jedno tlumienie na oba znaczyloby, ze
    # druga z nich milknie, bo pierwsza byla wczesniej. Klucz idzie do
    # `data->>'zrodlo'`, bo tytul jest zdaniem po polsku i zmienia sie razem
    # z liczbami.
    #
    # BRAK DANYCH W OGOLE (`wiek_min IS NULL`) nie ma znacznika, bo nie ma
    # zadnego wpisu - tam tlumimy oknem dobowym, tak samo jak
    # `sprawdz_instrumenty` przy wycofaniu instrumentu.
    ostatni_wpis = {w["co"]: w.get("ostatni") for w in wiersze}
    zgloszone = trwale = 0
    odbiorcy = await fetch_all("SELECT id FROM app.\"user\" WHERE is_active AND role = 'owner'")
    for zrodlo, tytul, tresc in powody:
        print(f"  {tytul}")
        if sucho:
            zgloszone += 1
            continue
        juz = await fetch_all(
            "SELECT 1 FROM notify.notification"
            " WHERE event_code = 'engine.down' AND data->>'zrodlo' = %(z)s"
            "   AND created_at > coalesce(%(od)s::timestamptz,"
            "                             now() - make_interval(hours => 24))"
            " LIMIT 1", {"z": zrodlo, "od": ostatni_wpis.get(zrodlo)})
        if juz:
            trwale += 1
            print("    (zgłoszone po ostatnim wpisie tego źródła — cisza)")
            continue
        zgloszone += 1
        for u in odbiorcy:
            await notify(u["id"], "engine.down", tytul, tresc,
                         severity="critical", data={"zrodlo": zrodlo})
    if sucho:
        print("Tryb suchy — powiadomienie nie wysyłane.")
    return kod_wyjscia(zgloszone, trwale, "źródło danych nieświeże")


# Ile nieudanych wyjsc z RZEDU czyni z otwartej pozycji awarie.
#
# Decyzje zapadaja na swiecy 4h, czyli szesc razy na dobe. Jedno nieudane
# wyjscie jest normalne — zlecenie idzie jako PostOnly i potrafi nie zlapac
# swojej swiecy. Trzy to okolo pol doby pozycji trzymanej WBREW DECYZJI,
# a przy tym wciaz trzy razy mniej niz cztery, ktore awaria SOLUSDC zdazyla
# przejsc w ciszy przez 19 godzin (2026-09-23 17:18 .. 2026-09-24 05:15).
PROB_WYJSCIA_BEZ_SKUTKU = 3


# Od ilu ROZSTRZYGNIETYCH zlecen w oknie stosunek odrzucen cokolwiek mowi.
# „Zadne nie przeszlo" alarmuje bez minimum - to inny warunek i inny powod.
MIN_PROBA_ODRZUCEN = 4


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

    DLACZEGO DOSZLY DWA LICZNIKI, KTORYCH TU WCZESNIEJ NIE BYLO

    2026-09-24 ta czujka wydrukowala „egzekucja (24 h): przyjętych 5,
    odrzuconych 0, o nieznanym stanie 0 — bez zastrzeżeń", podczas gdy pozycja
    SOLUSDC stala niezamykalna od 19 godzin, a cztery kolejne wyjscia odbily
    sie od gieldy. Kazdy z trzech licznikow milczal z INNEGO powodu i zadnego
    z nich nie dalo sie naprawic przestawieniem progu:

      * `przyjete` liczylo wszystko poza `rejected`/`unknown`, wiec `cancelled`
        wpadalo do PRZYJETYCH. Cztery zlecenia, ktore nigdy nie dotarly do
        gieldy, podbijaly licznik sukcesu,
      * `odrzucone` liczy `status='rejected'`, a sciezka bledu zapisywala
        wylacznie `unknown` — licznik byl strukturalnie martwy,
      * `nieznane` liczy wiersze NADAL `unknown` i starsze niz 4 h, a sprzatanie
        przestawialo je na `cancelled` po ~2 h 45 min.

    Stad `bez_kontaktu` (zlecenie domkniete bez `venue_order_id`, czyli takie,
    ktore NIGDY nie dotarlo do gieldy — slad, ktorego sprzatanie nie moze
    nadpisac) oraz `wyjscia_bez_skutku` (pozycja otwarta, przy ktorej zadna
    sprzedaz sie nie wypelnila mimo kolejnych prob). Ta druga regula nie patrzy
    na statusy zlecen W OGOLE i dlatego zlapalaby te awarie od pierwszego dnia:
    pyta o SKUTEK — czy pozycja, ktora system chce zamknac, daje sie zamknac.
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
            "System nie handluje i NIE WYGLĄDA to na awarię: silnik bije, dane "
            "są świeże, kill-switch milczy. Odrzucone zlecenie w logu wygląda "
            "tak samo jak brak sygnału."))
    elif (odrzucone and przyjete and odrzucone >= przyjete
          and odrzucone + przyjete >= MIN_PROBA_ODRZUCEN):
        # MINIMUM PROBY I TRESC ZGODNA Z WARUNKIEM (2026-10-03, audyt 3 T10).
        # Do tej daty jedno odrzucenie PostOnly przy jednym przyjeciu (n = 2)
        # zapalalo Job na czerwono tytulem „Więcej… (1) niż (1)". Przy dwoch
        # zleceniach stosunek nic nie mowi, a tytul przeczyl liczbom obok.
        powody.append((
            "order.rejected",
            f"Odrzucona co najmniej połowa zleceń ({odrzucone} z "
            f"{odrzucone + przyjete})",
            "Przechodzi najwyżej połowa zleceń. Powody:\n" + "\n".join(
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

    if stan.get("bez_kontaktu"):
        powody.append((
            "order.rejected",
            f"Zlecenia zamknięte BEZ KONTAKTU Z GIEŁDĄ: {stan['bez_kontaktu']}",
            f"{stan['bez_kontaktu']} zleceń zostało zamkniętych, choć nigdy nie "
            f"dostały `venue_order_id` — czyli giełda ich nie widziała.\n\n"
            f"Do 2026-09-24 takie zlecenie było NIEWIDOCZNE dla nadzoru: "
            f"sprzątanie przestawiało je z `unknown` na `cancelled` po ~2 h 45 min, "
            f"czyli przed progiem 4 h licznika „o nieznanym stanie\", a `cancelled` "
            f"wpadało do licznika PRZYJĘTYCH. Cztery wyjścia SOLUSDC, które nie "
            f"doszły do giełdy, podbijały w ten sposób licznik sukcesu przez "
            f"19 godzin.\n\n"
            f"Zlecenie bez kontaktu z giełdą nie jest ani wypełnione, ani "
            f"odrzucone przez rynek — jest próbą, która nie dotarła."))

    for w in stan.get("wyjscia_bez_skutku", []):
        powody.append((
            "order.rejected",
            f"Pozycja {w['symbol']} nie daje się zamknąć "
            f"({w['prob']} prób wyjścia bez wypełnienia)",
            f"Pozycja {w['symbol']} (id {w['pozycja']}) jest otwarta od "
            f"{w['opened_at']:%Y-%m-%d %H:%M}, a od tej chwili ANI JEDNA sprzedaż "
            f"się nie wypełniła — mimo {w['prob']} prób, ostatnia "
            f"{w['ostatnia']:%Y-%m-%d %H:%M}.\n\n"
            f"Ta reguła nie patrzy na statusy zleceń, tylko na SKUTEK, i dlatego "
            f"jest niezależna od tego, jak ścieżka błędu zapisała swoją porażkę. "
            f"Pozycja, której system chce się pozbyć i nie potrafi, to ekspozycja "
            f"trzymana wbrew decyzji: risk kernel liczy z niej ekspozycję, więc "
            f"blokuje wejścia, a strategia nie ma jak zrealizować wyniku.\n\n"
            f"Najczęstsza przyczyna zmierzona na produkcji: `exec.position.qty` "
            f"zaksięgowane BRUTTO, gdy giełda pobrała prowizję kupna w kupionym "
            f"aktywie — wyjście prosi wtedy o więcej, niż leży na rachunku, "
            f"i giełda odrzuca CAŁOŚĆ za brak salda."))

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
        # PRZYJETE ZNACZY „GIELDA TO WIDZIALA", A NIE „NIE ZAPISALISMY PORAZKI".
        #
        # Warunek brzmial tu `status NOT IN ('rejected','unknown')`, wiec
        # `cancelled` bylo liczone jako PRZYJETE. Sprzatanie zamyka tym statusem
        # takze zlecenia, ktore NIGDY nie dotarly do gieldy — zmierzone
        # 2026-09-24: cztery martwe wyjscia SOLUSDC podbijaly licznik sukcesu
        # do piatki, przy zerze odrzucen i zerze nieznanych.
        #
        # `venue_order_id` jest jedynym dowodem kontaktu: nadaje go GIELDA przy
        # przyjeciu zlecenia, wiec zadne nasze pozniejsze sprzatanie go nie
        # wytworzy ani nie skasuje.
        "SELECT count(*) FILTER (WHERE status IN ('filled','partial','new')"
        "                          AND venue_order_id IS NOT NULL) AS przyjete,"
        "       count(*) FILTER (WHERE status = 'rejected') AS odrzucone,"
        "       count(*) FILTER (WHERE status = 'unknown') AS nieznane,"
        # ZLECENIE ZAMKNIETE, KTOREGO GIELDA NIGDY NIE WIDZIALA. To jest slad po
        # sprzataniu — jedyny, ktorego sprzatanie nie moze nadpisac.
        "       count(*) FILTER (WHERE status = 'cancelled'"
        "                          AND venue_order_id IS NULL) AS bez_kontaktu"
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
        "SELECT count(*) AS n, max(valid_until) AS ostatni"
        "  FROM journal.planned_order"
        " WHERE status = 'pending' AND valid_until < now() - make_interval(hours => 4)")
    # REGULA NIEZALEZNA OD STATUSOW ZLECEN — PYTA O SKUTEK, NIE O ZAPIS.
    #
    # Wszystkie trzy liczniki wyzej czytaja `exec.order.status`, wiec kazdy da
    # sie ominac zapisujac porazke innym slowem. Ta regula nie da sie: pyta,
    # czy pozycja, ktora system PROBUJE zamykac, daje sie zamknac. Awaria
    # z 2026-09-23/24 wygladala tu jednoznacznie od pierwszego dnia — pozycja
    # 32 otwarta 2026-09-22, cztery sprzedaze i zero wypelnien.
    #
    # DWA WARUNKI, BO JEDEN BY NIE WYSTARCZYL. „Sa nieudane sprzedaze" samo
    # w sobie jest normalne: wyjscie idzie jako PostOnly, wiec potrafi nie
    # zlapac swojej swiecy i wygasnac. Dopiero „zadna sprzedaz sie nie
    # wypelnila" (`NOT EXISTS`) zamienia to w zdanie o AWARII.
    #
    # PROG 3 PROBY, bo decyzje zapadaja na swiecy 4h — szesc razy na dobe.
    # Trzy nieudane wyjscia to okolo pol doby pozycji trzymanej wbrew decyzji,
    # czyli wyraznie wiecej niz jedno pechowe ustawienie ceny makera, a wyraznie
    # mniej niz 19 godzin, ktore ta awaria przezyla w ciszy.
    wyjscia = await fetch_all(
        "SELECT i.symbol AS symbol, p.id AS pozycja, p.opened_at AS opened_at,"
        "       count(*) AS prob, max(o.submitted_at) AS ostatnia"
        "  FROM exec.position p"
        "  JOIN ref.instrument i ON i.id = p.instrument_id"
        "  JOIN exec.order o ON o.instrument_id = p.instrument_id"
        "                   AND o.side = 'sell'"
        "                   AND o.submitted_at >= p.opened_at"
        "                   AND o.status NOT IN ('filled','partial','new')"
        # TYLKO POZYCJE DLUGIE. Wyjsciem z krotkiej jest KUPNO, wiec ta regula
        # jej nie opisuje. Na spocie krotkich nie ma (`koszty.krotka_pozycja`),
        # wiec dzis nic to nie kosztuje — ale warunek stoi tu jawnie, zeby
        # pierwsza pozycja krotka nie wypadla z nadzoru po cichu.
        " WHERE p.closed_at IS NULL AND p.side = 'long'"
        "   AND NOT EXISTS (SELECT 1 FROM exec.order o2"
        "                    WHERE o2.instrument_id = p.instrument_id"
        "                      AND o2.side = 'sell'"
        "                      AND o2.submitted_at >= p.opened_at"
        "                      AND o2.status IN ('filled','partial'))"
        " GROUP BY i.symbol, p.id, p.opened_at"
        " HAVING count(*) >= %(prob)s"
        " ORDER BY p.id", {"prob": PROB_WYJSCIA_BEZ_SKUTKU})
    # ZNACZNIKI STANU DLA TLUMIENIA (patrz nizej). Kazdy mowi "kiedy pojawil sie
    # NAJNOWSZY dowod tej awarii" - powiadomienie mlodsze od znacznika znaczy,
    # ze o tym dowodzie juz powiedzielismy. Oba liczy BAZA, bo porownujemy je
    # z `created_at`, ktore tez zapisuje baza.
    znaczniki = await fetch_all(
        # ZNACZNIK MUSI OBEJMOWAC WSZYSTKIE DOWODY TEJ GALEZI, inaczej nowy
        # dowod nie obudzi alarmu. Doszedl trzeci rodzaj: zlecenie zamkniete
        # bez kontaktu z gielda (`cancelled` bez `venue_order_id`) — gdyby go
        # tu nie bylo, seria takich zlecen zglosilaby sie raz i zamilkla,
        # mimo ze kazde kolejne jest nowym dowodem tej samej awarii.
        "SELECT max(submitted_at) FILTER (WHERE status IN ('rejected','unknown')"
        "                              OR (status = 'cancelled'"
        "                                  AND venue_order_id IS NULL))"
        "         AS ostatnie_zlecenie"
        "  FROM exec.order"
        " WHERE submitted_at > now() - make_interval(hours => %(h)s)",
        {"h": okno_h})

    stan = {
        "przyjete": st[0]["przyjete"] if st else 0,
        "odrzucone": st[0]["odrzucone"] if st else 0,
        # NIEZNANE LICZYMY TYLKO TE STARE. Zlecenie zlozone minute temu moze byc
        # nieznane najzupelniej poprawnie - odpowiedz jeszcze nie wrocila.
        "nieznane": len(nieznane_rows),
        "id_nieznanych": [r["client_order_id"] or "?" for r in nieznane_rows],
        "powody": [r["powod"] for r in powody_rows],
        "zawieszone_plany": zawieszone[0]["n"] if zawieszone else 0,
        "bez_kontaktu": (st[0]["bez_kontaktu"] if st else 0) or 0,
        # BEZ OKNA CZASOWEGO. Pozycja, ktorej nie da sie zamknac, nie przestaje
        # nia byc po dobie — przeciwnie, im dluzej stoi, tym gorzej. Okno 24 h
        # wyciszyloby ja dokladnie wtedy, gdy zaczyna kosztowac najwiecej.
        "wyjscia_bez_skutku": [dict(w) for w in wyjscia],
    }
    print(f"egzekucja ({okno_h} h): przyjętych {stan['przyjete']}, "
          f"odrzuconych {stan['odrzucone']}, o nieznanym stanie {stan['nieznane']}, "
          f"zamkniętych bez kontaktu z giełdą {stan['bez_kontaktu']}, "
          f"pozycji nie do zamknięcia {len(stan['wyjscia_bez_skutku'])}, "
          f"planów bez rozstrzygnięcia {stan['zawieszone_plany']}")

    powody = ocen_egzekucje(stan)
    if not powody:
        print("  bez zastrzeżeń")
        return 0

    # TLUMIENIE - DO 2026-09-23 TEJ CZUJKI NIE MIALA W OGOLE.
    #
    # Ta sama wada co w `sprawdz_swiezosc` wyzej i z tym samym skutkiem:
    # `engine.down` (galaz `zawieszone_plany`) jest `always_on`, wiec
    # katalogowy cooldown go NIE dotyczy, a zadanie chodzi co 10 minut. Stan
    # trwajacy weekend dawal ~430 maili `critical` i tyle samo czerwonych
    # Jobow. `order.rejected` ma cooldown katalogowy, wiec maili wychodzi
    # mniej - ale KOD WYJSCIA liczyl proby, nie wyslania, wiec Job byl czerwony
    # przy kazdym przebiegu tak samo.
    #
    # ZNACZNIK STANU PER GALAZ, a nie jedno okno na wszystko:
    #
    #   * `zawieszone_plany` -> `max(valid_until)` planow, ktore utknely.
    #     Kazdy nowy zawieszony plan przesuwa znacznik do przodu, wiec NOWE
    #     dowody odzywaja sie, a ten sam zastany zbior milczy.
    #   * `order.rejected` -> `max(submitted_at)` zlecen odrzuconych albo
    #     o nieznanym stanie. Gdy gielda zacznie przyjmowac, znacznik przestaje
    #     rosnac; kolejna seria odrzucen go przesunie i alarm wroci.
    #
    # Ta sama konstrukcja co `last_beat_at` w `sprawdz_zywotnosc`: nawrot
    # rozpoznaje sie za darmo, bez drugiego mechanizmu i bez nowej kolumny.
    znacznik = {
        "engine.down": (zawieszone[0]["ostatni"] if zawieszone else None),
        "order.rejected": (znaczniki[0]["ostatnie_zlecenie"] if znaczniki else None),
    }
    odbiorcy = await fetch_all("SELECT id FROM app.\"user\" WHERE is_active AND role = 'owner'")
    zgloszone = trwale = 0
    for kod, tytul, tresc in powody:
        print(f"  {tytul}")
        if sucho:
            zgloszone += 1
            continue
        juz = await fetch_all(
            "SELECT 1 FROM notify.notification WHERE event_code = %(kod)s"
            "   AND created_at > coalesce(%(od)s::timestamptz,"
            "                             now() - make_interval(hours => 24))"
            " LIMIT 1", {"kod": kod, "od": znacznik.get(kod)})
        if juz:
            trwale += 1
            print("    (zgłoszone po najnowszym dowodzie tej awarii — cisza)")
            continue
        zgloszone += 1
        for u in odbiorcy:
            await notify(u["id"], kod, tytul, tresc, severity="critical")
    if sucho:
        print("Tryb suchy — powiadomienie nie wysyłane.")
    return kod_wyjscia(zgloszone, trwale, "awaria ścieżki egzekucji")


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
    from services.engine.poslizg import (opis, opis_selekcji, pobierz,
                                         pobierz_zlecenia, pobierz_zwrot_naprzod,
                                         pomiar, udzial_wyjsc_takerem,
                                         wypelnialnosc, zwrot_naprzod)

    wiersze = await pobierz(fetch_all, engine=engine, mode=mode, okno_h=okno_h)
    # WYPELNIALNOSC TEZ TUTAJ (2026-10-03, audyt 3 T2). Do tej daty liczyl ja
    # tylko `poslizg.main`, ktorego nikt nie uruchamia, a ten raport - czytany
    # codziennie w `zywotnosc` - dwa razy odsylal do „wypelnialnosci nizej",
    # ktorej nizej nie bylo. To jedyna miara efektu E1-E5.
    zlecenia = await pobierz_zlecenia(fetch_all, engine=engine, mode=mode,
                                      okno_h=okno_h)
    okno = "od poczatku" if okno_h is None else f"{okno_h} h"
    print(f"poślizg ({engine}/{mode}, {okno}), "
          f"wypełnień w rejestrze: {len(wiersze)}, "
          f"złożonych zleceń: {len(zlecenia)}")
    for linia in opis(pomiar(wiersze) | {
            "wypelnialnosc": wypelnialnosc(zlecenia),
            "wyjscia_takerem": udzial_wyjsc_takerem(zlecenia)}):
        print(f"  {linia}")
    # SELEKCJA NEGATYWNA (F-4.5): czym roznia sie wejscia, ktore przepadly, od
    # tych, ktore weszly - zwrot naprzod 24/48 h z przedzialem z bootstrapu.
    wejscia = await pobierz_zwrot_naprzod(fetch_all, engine=engine, mode=mode,
                                          okno_h=okno_h)
    for linia in opis_selekcji(zwrot_naprzod(wejscia)):
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
    odbiorcy = await fetch_all("SELECT id FROM app.\"user\" WHERE is_active AND role = 'owner'")

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


def brak_odczytu_statusu(wiersze: list[dict],
                         bez_odswiezacza: bool = False,
                         okno_przejscia: bool = False) -> bool:
    """Czy dla TEGO silnika status nie zostal odczytany ANI RAZU. CZYSTA FUNKCJA.

    ROZNICA MIEDZY "ODSWIEZACZ STANAL" I "ODSWIEZACZA NIE MA" - i dlaczego
    nie wolno ich zglaszac tak samo.

    `sprawdz_instrumenty` mierzy wiek kolumny `status_sprawdzony_at`. Dla
    silnika z odswiezaczem pusta data znaczy "zadanie
    `services/ingest/instrumenty.py` przestalo chodzic" - czyli awarie do
    naprawienia. Dla silnika bez odswiezacza znaczy co innego: nikt o to nie
    pyta i nie zapyta. Tak bylo z torem `equity` do 2026-10-15 - zmierzone na
    produkcji 2026-09-27: 20 handlowalnych instrumentow equity, z tego
    `status_sprawdzony_at` ustawione dla ZERA; dla krypto dla wszystkich 34.
    Od 2026-10-15 Alpaka jest w `instrumenty.VENUE_ODSWIEZANE`, wiec ta
    funkcja ucisza dzis tylko silnik, ktorego tam nie ma.

    Alarm o tym stanie przychodzilby przy kazdym przebiegu zadania (co 10 min),
    a `instrument.suspended` (ktorym szedl do 2026-10-16) mial katalogowy
    cooldown 3600 s, czyli 24 maile na dobe o rzeczy, ktora nie zmieni sie sama
    NIGDY. To jest dokladnie wzorzec
    zmierzony w tym pliku 2026-09-12 przy kill-switchu: sygnal, ktory przychodzi
    zawsze, uczy odklikiwac CALA kategorie - w tym te alarmy o wycofaniu, ktore
    dotycza pary naprawde handlowanej.

    Dlatego "ani razu" jest tu BRAKIEM POMIARU: wypisywanym w logu przy kazdym
    przebiegu, bez powiadomienia i bez czerwonego Joba.

    CZEGO TA FUNKCJA NIE UCISZA - i to sa DWIE osobne granice, obie konieczne.

    PIERWSZA: `bez_odswiezacza` musi byc prawda, czyli gieldy tego silnika nie
    pyta o status ZADEN proces (`instrumenty.VENUE_ODSWIEZANE`). Dla krypto pusta
    data znaczy "zadanie `trader-instrumenty` przestalo chodzic" - i to jest
    awaria do obudzenia czlowieka, dokladnie ta, ktora 2026-09-24 przelezala
    10 godzin za `|| true`. Domysl parametru to `False`, czyli "odswiezacz
    istnieje", wiec dla krypto ta funkcja nie ucisza NICZEGO.

    DRUGA: warunek jest "ANI JEDEN wiersz tego silnika nie ma daty", a nie "ten
    wiersz nie ma daty". Odswiezacz, ktory chodzi i POMIJA czesc uniwersum (nowy
    instrument, nowa kategoria, zmieniony symbol), zostawia mieszanke - i wtedy
    alarm idzie jak dotad, bo wtedy jest to naprawde rozjazd, a nie brak
    mechanizmu.

    OKNO PRZEJSCIA (`okno_przejscia`, przeglad PR #34 2026-10-16). Silnik, ktory
    DOPIERO DOSTAL odswiezacz, ma puste daty takze wtedy, gdy wszystko dziala:
    od wdrozenia do pierwszego przebiegu `trader-instrumenty` mija do 6 h. Przez
    `PRZEGLAD_STATUSU_H` od wdrozenia (`instrumenty.ODSWIEZACZ_OD`,
    `okno_przejscia_h`) "ani jednej daty" to wiec tez BRAK POMIARU. Po tym czasie
    - juz nie: trwaly brak dat po wdrozeniu znaczy, ze odswiezacz tego silnika
    nie dziala (brak albo odrzucony klucz Alpaki), i to jest awaria. Druga
    granica (mieszanka dat alarmuje) obowiazuje takze w oknie.
    """
    return ((bez_odswiezacza or okno_przejscia) and bool(wiersze)
            and all(r["sprawdzony_h"] is None for r in wiersze))


KOD_STATUS_NIESWIEZY = "instrument.status_nieswiezy"


def nieswieze(wiersze: list[dict], prog_h: int = PRZEGLAD_STATUSU_H,
              bez_odswiezacza: bool = False, okno_przejscia: bool = False
              ) -> list[dict]:
    """Wiersze, ktorych status nie byl potwierdzony w progu. CZYSTA FUNKCJA."""
    if brak_odczytu_statusu(wiersze, bez_odswiezacza, okno_przejscia):
        return []
    return [r for r in wiersze
            if r["sprawdzony_h"] is None or r["sprawdzony_h"] > prog_h]


def poczatek_nieswiezosci_h(nieswiezy: list[dict]) -> int | None:
    """Ile godzin temu zaczela sie awaria odswiezacza. CZYSTA FUNKCJA.

    Najstarsze potwierdzenie wsrod nieswiezych wierszy: od tej chwili nikt nie
    pytal. Kazde powiadomienie o TEJ awarii powstalo pozniej (najwczesniej po
    progu), a kazde o POPRZEDNIEJ - wczesniej, bo jej koniec to przebieg, ktory
    te daty ustawil. Dlatego powtorki liczone od tej chwili licza sie od
    AWARII, a nie od objawu, i zeruja po naprawie.

    None = ktorys wiersz nie byl sprawdzony NIGDY - poczatku nie znamy, wiec
    liczymy cala historie tego kodu dla silnika. Najgorszy skutek: pierwsze
    zgloszenie nowej awarii czeka na odstep po ostatnim zgloszeniu starej
    (najwyzej tydzien), nigdy na odwrot - powtorka nie zgubi sie w ciszy.
    """
    if not nieswiezy or any(r["sprawdzony_h"] is None for r in nieswiezy):
        return None
    return max(r["sprawdzony_h"] for r in nieswiezy)


def w_oknie_przejscia(wlaczony_h: float | None,
                      prog_h: int = PRZEGLAD_STATUSU_H) -> bool:
    """Czy odswiezacz silnika zostal wdrozony mniej niz `prog_h` temu. CZYSTA
    FUNKCJA. None (silnik bez wpisu w `ODSWIEZACZ_OD` albo brak wiersza
    migracji) = okna nie ma, czyli pusta data jest awaria."""
    return wlaczony_h is not None and wlaczony_h < prog_h


async def okno_przejscia_h(fetch_all, engine: str) -> float | None:
    """Ile godzin temu wdrozono odswiezacz statusu TEGO silnika - z daty
    zastosowania migracji `instrumenty.ODSWIEZACZ_OD[engine]`. None = brak
    wpisu albo brak wiersza (wtedy okna nie ma). Rola czujki ma SELECT na
    `public.schema_migrations` (migracja 0050)."""
    from services.ingest.instrumenty import ODSWIEZACZ_OD

    wersja = ODSWIEZACZ_OD.get(engine)
    if wersja is None:
        return None
    w = await fetch_all(
        "SELECT (extract(epoch from (now() - applied_at)) / 3600)::float8"
        "         AS wlaczony_h"
        "  FROM public.schema_migrations WHERE version = %s", (wersja,))
    return float(w[0]["wlaczony_h"]) if w else None


def ocen_instrumenty(wiersze: list[dict], prog_h: int = PRZEGLAD_STATUSU_H,
                     bez_odswiezacza: bool = False, okno_przejscia: bool = False
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

    nieswiezy = nieswieze(wiersze, prog_h, bez_odswiezacza, okno_przejscia)
    if nieswiezy:
        opis = ", ".join(
            f"{r['symbol']} ({'nigdy' if r['sprawdzony_h'] is None else str(r['sprawdzony_h']) + ' h temu'})"
            for r in nieswiezy[:12])
        # WLASNY KOD, nie `instrument.suspended` (przeglad PR #34). Tamten ma
        # katalogowy cooldown per (odbiorca, kod), wiec ten meldunek o awarii
        # odswiezacza polykal powiadomienia odswiezacza o zawieszeniu pary -
        # takze pary innego silnika - a te ida tylko przy ZMIANIE stanu i nie
        # wracaja. Powtorke tego kodu liczy `sprawdz_instrumenty` od awarii.
        powody.append((
            KOD_STATUS_NIESWIEZY,
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


async def venue_bez_odswiezacza(fetch_all, engine: str) -> bool:
    """Czy gieldy TEGO silnika nie pyta o status zaden proces.

    `services/ingest/instrumenty.py` - jedyny odswiezacz statusu w repozytorium -
    pyta wylacznie gield z `VENUE_ODSWIEZANE`, po jednej na silnik. Silnik bez
    wpisu tam (albo z wpisem, ktorego gieldy nie ma w `ref.venue` dla tego
    silnika) nie ma ani jednego procesu ustawiajacego
    `ref.instrument.status_sprawdzony_at`, a jego pusta kolumna nie jest awaria
    - jest brakiem mechanizmu.

    NAZWY GIELD IDA Z MODULU ODSWIEZACZA, nie sa przepisane. Druga kopia
    rozjechalaby sie w dniu, w ktorym ktos dopisze adapter statusu - i to
    wlasnie ta czujka twierdzilaby wtedy dalej, ze pomiaru nie ma, czyli
    MILCZALABY o pierwszej awarii nowego odswiezacza. Tak bylo do 2026-10-15
    z Alpaka: od dopisania jej do `VENUE_ODSWIEZANE` pusta data sprawdzenia
    akcji jest awaria, nie brakiem pomiaru.
    """
    from services.ingest.instrumenty import VENUE_ODSWIEZANE

    venue = VENUE_ODSWIEZANE.get(engine)
    if venue is None:
        return True
    return not await fetch_all(
        "SELECT 1 FROM ref.venue WHERE engine::text = %s AND code = %s LIMIT 1",
        (engine, venue))


async def sprawdz_instrumenty(fetch_all, notify, sucho: bool = False, *,
                              engine: str = "crypto", mode: str = "demo",
                              prog_h: int | None = None,
                              teraz: datetime | None = None) -> int:
    """Czy któryś instrument uniwersum przestał istnieć u giełdy.

    ZAKRES: instrument w uniwersum decyzyjnym ALBO z otwartą pozycją. Migracja
    0020 wyłączyła czternaście par USDT, których podmiot unijny nie oferuje —
    alarm o nich przy każdym przebiegu nauczyłby czytać tę czujkę nieuważnie
    akurat wtedy, gdy dotyczy pary, którą naprawdę handlujemy.
    """
    prog = PRZEGLAD_STATUSU_H if prog_h is None else prog_h
    teraz = teraz or datetime.now(timezone.utc)
    bez_odswiezacza = await venue_bez_odswiezacza(fetch_all, engine)
    wlaczony_h = None if bez_odswiezacza else await okno_przejscia_h(fetch_all, engine)
    okno = w_oknie_przejscia(wlaczony_h, prog)
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
    if okno and brak_odczytu_statusu(wiersze, False, okno):
        # OKNO PRZEJSCIA - uzasadnienie w `brak_odczytu_statusu`. Linia idzie
        # przy KAZDYM przebiegu i mowi, KIEDY cisza sie skonczy.
        print(f"  BRAK POMIARU (okno przejścia): odświeżacz statusu dla `{engine}` "
              f"wdrożono {wlaczony_h:.1f} h temu, a żaden z {len(wiersze)} "
              f"instrumentów nie ma jeszcze daty sprawdzenia — pierwszy przebieg "
              f"trader-instrumenty jeszcze nie przyszedł albo się nie udał. "
              f"Powiadomienia NIE wysyłam do {prog} h od wdrożenia; potem ten "
              f"stan jest AWARIĄ odświeżacza (np. klucz Alpaki).")
    elif brak_odczytu_statusu(wiersze, bez_odswiezacza):
        # BRAK POMIARU, NIE ZGODNOSC. Uzasadnienie w `brak_odczytu_statusu`.
        # Linia idzie przy KAZDYM przebiegu, zeby stan nie stal sie cisza.
        print(f"  BRAK POMIARU: statusu u giełdy nie odczytano dla ANI JEDNEGO "
              f"z {len(wiersze)} instrumentów tego silnika — dla `{engine}` nie "
              f"istnieje proces, który by o to pytał (services/ingest/"
              f"instrumenty.py nie ma giełdy tego silnika w VENUE_ODSWIEZANE). "
              f"To nie jest potwierdzenie handlu: delisting w tym stanie "
              f"przeszedłby niezauważony. Powiadomienia NIE wysyłam — stan jest "
              f"trwały i nie zmieni się bez nowego odświeżacza.")

    powody = ocen_instrumenty(wiersze, prog, bez_odswiezacza, okno)
    if not powody:
        print("  bez zastrzeżeń")
        return 0

    # TLUMIENIE DOBOWE. `instrument.delisted` jest always_on, wiec omija zwykly
    # cooldown - a wycofanie trwa miesiacami. Bez wlasnego tlumienia czujka
    # krzyczalaby przy kazdym przebiegu i nauczylaby ignorowac wlasnie ten alarm.
    #
    # PER SILNIK (przeglad PR #34). Bez filtra wycofanie akcji zgloszone przez
    # odswiezacz equity uciszalo na dobe przypomnienie o zamrozonej pozycji
    # krypto - i odwrotnie.
    juz = await fetch_all(
        "SELECT 1 FROM notify.notification WHERE event_code = 'instrument.delisted'"
        "   AND engine::text = %s"
        "   AND created_at > now() - make_interval(hours => 24) LIMIT 1", (engine,))

    odbiorcy = await fetch_all("SELECT id FROM app.\"user\" WHERE is_active AND role = 'owner'")
    zgloszone = trwale = 0
    for kod, tytul, tresc in powody:
        print(f"  {tytul}")
        if sucho:
            zgloszone += 1
            continue
        if kod == KOD_STATUS_NIESWIEZY:
            # POWTORKA Z ROSNACYM ODSTEPEM, liczona od AWARII - tak jak dla
            # `job.nieudany` (`odstep_powtorki_zadania`). Kod jest `always_on`
            # (migracja 0097), wiec kazde zgloszenie zostawia wiersz, a ten
            # rachunek jest jedynym tlumieniem. Poczatek awarii: patrz
            # `poczatek_nieswiezosci_h`.
            od_h = poczatek_nieswiezosci_h(
                nieswieze(wiersze, prog, bez_odswiezacza, okno))
            poprzednie = await fetch_all(
                "SELECT count(DISTINCT date_trunc('minute', created_at)) AS n,"
                "       max(created_at) AS ostatnie FROM notify.notification"
                " WHERE event_code = %s AND engine::text = %s"
                "   AND (%s::int IS NULL"
                "        OR created_at > now() - make_interval(hours => %s::int))",
                (KOD_STATUS_NIESWIEZY, engine, od_h, od_h))
            n = (poprzednie[0]["n"] or 0) if poprzednie else 0
            ostatnie = poprzednie[0]["ostatnie"] if poprzednie else None
            if n and ostatnie is not None and teraz - ostatnie < odstep_powtorki_zadania(n):
                nastepne = ostatnie + odstep_powtorki_zadania(n)
                print(f"    (zgłoszone {n}×, ostatnio {ostatnie:%m-%d %H:%M} UTC; "
                      f"następne przypomnienie najwcześniej {nastepne:%m-%d %H:%M} UTC)")
                trwale += 1
                continue
        if kod == "instrument.delisted" and juz:
            # Wycofanie trwa MIESIACAMI, a zadanie chodzi co 10 minut. Do
            # 2026-09-10 kod wyjscia nie znal tego tlumienia i przez caly ten
            # czas malowal zadanie na czerwono 144 razy na dobe przy jednym
            # powiadomieniu - patrz `kod_wyjscia` i docstring modulu.
            trwale += 1
            print("    (zgłoszone w ciągu ostatniej doby — cisza)")
            continue
        zgloszone += 1
        sev = "critical" if kod == "instrument.delisted" else "warning"
        for u in odbiorcy:
            await notify(u["id"], kod, tytul, tresc, engine=engine, severity=sev)
    if sucho:
        print("Tryb suchy — powiadomienie nie wysyłane.")
    # CZEGO TA CZUJKA NADAL NIE UMIE, I DLACZEGO NIE ZALATWIA SIE TEGO TUTAJ.
    #
    # `instrument.suspended` jest jedynym zdarzeniem uzywanym w tym pliku, ktore
    # NIE JEST `always_on`: ma katalogowy cooldown 3600 s, wiec czesc wywolan
    # `notify()` jest po cichu odrzucana w warstwie powiadomien. Od 2026-10-16
    # idzie nim juz tylko instrument POZA HANDLEM (pozycja albo niezamknieta
    # bramka); "status nie byl sprawdzany", czyli trwala awaria odswiezacza,
    # ma wlasny kod `always_on` z powtorka liczona wyzej.
    #
    # Wlasne tlumienie po tej stronie byloby DRUGIM mechanizmem na to samo -
    # dokladnie tym, czego ten plik unika - a do tego globalnym tam, gdzie
    # katalogowy jest per uzytkownik. Wlasciwa naprawa to odczytanie werdyktu
    # z `notify()` (zwraca id albo None), czyli zmiana kontraktu wolanego
    # z pieciu miejsc. Zostawione swiadomie i osobno.
    return kod_wyjscia(zgloszone, trwale, "instrument poza handlem")


# ------------------------------------------------------- puls workerow danych
#
# Ile CYKLI DANEGO WORKERA moze minac bez pulsu, zanim uznamy go za martwego.
#
# Prog wyprowadzony z interwalu, nie jedna liczba dla wszystkich: `market`
# chodzi co 60 s, a `history` co 1200 s, wiec kwadrans ciszy znaczy przy nich
# co innego. Jedna wspolna liczba albo krzyczalaby na historii, albo milczala
# przy rynku - a czujka, ktora robi jedno z tych dwoch, nie jest czujka.
#
# Trzy cykle, a nie dwa - z tego samego powodu co SWIEC_ZWLOKI wyzej. Prog
# dwoch lezalby na gornej granicy normy (cykl trwa niezerowo, a jego dlugosc
# jest zmienna) i migalby kilka razy dziennie. Ta sama liczba stoi zreszta za
# MARTWY_PO_MIN: 15 minut to dokladnie trzy cykle petli decyzyjnej po 300 s.
CYKLI_ZWLOKI = 3

# Podloga progu. Bez niej `market` (60 s) dostalby prog trzech minut, a to jest
# mniej niz:
#
#   * okres samej czujki - zadanie cykliczne chodzi co 10 minut (values.yaml,
#     zywotnoscCron.schedule), wiec prog krotszy niz jej wlasna rozdzielczosc
#     opisuje stan, ktorego ta czujka nie umie zobaczyc,
#   * czas restartu poda - pobranie obrazu, start procesu i pierwszy pelny cykl
#     to minuty. Kazde wdrozenie konczyloby sie alarmem o martwym workerze.
#
# 15 minut to ta sama podloga, ktora MARTWY_PO_MIN daje silnikowi, i z tego
# samego powodu. Czujka, ktora miga, jest tak samo bezuzyteczna jak falszywa:
# obie ucza odbiorce, ze mozna je pomijac.
#
# Progi wychodza z tego takie: market 15 min, news 15 min, features 45 min,
# history 60 min.
MIN_PROG_S = 900


# PRZYCZYNA BRAKU ANALIZ NEWSOW W TYTULE ALARMU (audyt 2026-10-09, S-2).
#
# Worker news zglasza "cisze zamiast pracy" jako awarie cyklu (`cisza_zamiast_
# pracy` w news_worker.py), wiec trafia tu jak kazdy worker, ktory "nie domyka
# cykli" - z diagnoza "szukaj po stronie zrodla danych albo bazy". Przy
# odcietych dostawcach LLM to jest zla wskazowka: zrodla i baza dzialaja,
# a trzy przyczyny wymagaja trzech roznych reakcji. Rozpoznajemy je z napisow,
# ktore sklada `services/ingest/llm.py` (`Wyczerpany`/`Przeciazony`).
PRZYCZYNY_BRAKU_ANALIZ = (
    # (wzorzec, krotko do tytulu, co zrobic)
    (r"(\w+): brak srodkow na koncie",
     "{d}: brak środków na koncie",
     "{d}: konto bez środków — samo się nie odnowi, wymaga doładowania"),
    (r"(\w+): klucz odrzucony",
     "{d}: klucz odrzucony",
     "{d}: klucz odrzucony albo bez uprawnień do modelu — wymaga wymiany klucza"),
    (r"(\w+): wyczerpany limit DOBOWY",
     "{d}: limit dobowy",
     "{d}: wyczerpany limit dobowy klucza — odnowi się następnej doby"),
    (r"(\w+): (?:awaria po stronie dostawcy|limit tempa|brak odpowiedzi)",
     "{d}: awaria przejściowa",
     "{d}: awaria albo przeciążenie po stronie dostawcy — przejściowe"),
)


def przyczyny_braku_analiz(blad: str | None) -> list[tuple[str, str]]:
    """(krotko, co zrobic) dla kazdego rozpoznanego dostawcy. CZYSTA FUNKCJA.
    Pusta lista: to nie jest brak analiz albo przyczyny nie rozpoznano."""
    if not blad or "BEZ ANALIZ" not in blad:
        return []
    wynik, widziane = [], set()
    for wzorzec, krotko, rada in PRZYCZYNY_BRAKU_ANALIZ:
        for d in re.findall(wzorzec, blad):
            # `gemini: wyczerpany limit DOBOWY: wyczerpany limit DOBOWY ...` -
            # ten sam dostawca dwa razy w jednym napisie.
            if d in widziane:
                continue
            widziane.add(d)
            wynik.append((krotko.format(d=d), rada.format(d=d)))
    return wynik


def ocen_puls_workerow(wiersze: list[dict], cykli_zwloki: int = CYKLI_ZWLOKI,
                       min_prog_s: int = MIN_PROG_S,
                       prog_s: int | None = None) -> list[tuple[str, str, str]]:
    """Ktore workery danych zamilkly. CZYSTA FUNKCJA.

    Zwraca liste (worker, tytul, tresc) - pusta znaczy: wszystkie bija.

    JEDEN WARUNEK ALARMU, NIE DWA
    -----------------------------
    Alarmuje wylacznie wiek `last_beat_at`. Kusi, zeby dolozyc drugi warunek na
    `awarii_pod_rzad`, ale to bylby ten sam problem zgloszony dwa razy: worker
    konczacy cykle bledem NIE odswieza `last_beat_at` z definicji (patrz
    services/ingest/puls.py), wiec i tak wpadnie w ten warunek. Dwa powiadomienia
    o jednej awarii ucza odbiorce, ze mozna je pomijac - a to jest ta sama szkoda
    co czujka falszywa.

    Kolumny awarii zmieniaja wiec TRESC, a nie liczbe alarmow. I to jest ich
    cala wartosc, bo rozdzielaja dwa stany nie do odroznienia po samym pulsie:

        proces STOI            ostatnia proba jest rownie stara co puls
        proces NIE DAJE RADY   ostatnia proba jest swieza, jest opis bledu

    Pierwszy to „zajrzyj, czy pod zyje". Drugi to „gielda odmawia od godziny".
    """
    powody: list[tuple[str, str, str]] = []
    for w in wiersze:
        interwal = int(w["interwal_s"])
        prog = prog_s if prog_s is not None else max(cykli_zwloki * interwal,
                                                     min_prog_s)
        if w["wiek_s"] <= prog:
            continue

        # Ostatnia proba rownie stara jak puls => proces nie zaczyna cyklu.
        # Swieza => cykle sie koncza, tylko bledem.
        pracuje = (w["proba_wiek_s"] is not None and w["proba_wiek_s"] <= prog
                   and w["proba_wiek_s"] < w["wiek_s"])
        llm = przyczyny_braku_analiz(w.get("ostatni_blad")) if pracuje else []
        if llm:
            tytul = (f"Analiza newsów stoi — "
                     f"{'; '.join(k for k, _ in llm)}")
            diagnoza = (
                f"Worker {w['worker']} pobiera artykuły, ale żaden dostawca LLM "
                f"ich nie analizuje ({w['awarii_pod_rzad']} cykli pod rząd):\n"
                + "\n".join(f"  • {r}" for _, r in llm)
                + "\n\nTo NIE jest awaria poda, źródeł ani bazy — restart nie "
                f"pomoże. Cecha news_sent ma lukę do czasu powrotu analizy.\n"
                f"Pełny opis: {w['ostatni_blad']}")
        elif pracuje:
            tytul = f"Worker {w['worker']} nie domyka cykli"
            diagnoza = (
                f"Proces ODPOWIADA — ostatnia próba {w['proba_wiek_s'] / 60:.0f} min "
                f"temu — ale kończy cykle błędem: {w['awarii_pod_rzad']} pod rząd.\n"
                f"Ostatni błąd: {w['ostatni_blad'] or 'bez opisu'}\n\n"
                f"To NIE jest zawieszony proces. Szukaj po stronie źródła danych "
                f"albo bazy, nie poda.")
        else:
            proba = ("brak" if w["proba_wiek_s"] is None
                     else f"{w['proba_wiek_s'] / 60:.0f} min temu")
            tytul = f"Worker {w['worker']} zamilkł"
            diagnoza = (
                f"Proces nie domknął ANI JEDNEJ próby w tym oknie "
                f"(ostatnia: {proba}).\n\n"
                f"Tak wygląda proces ZAWIESZONY, a nie martwy: nie kończy cyklu, "
                f"więc nie podnosi licznika awarii, nie kończy się i nie "
                f"restartuje. `kubectl get pods` pokazuje `Running` bez końca.")

        powody.append((
            w["worker"], tytul,
            f"Ostatni UDANY cykl {w['wiek_s'] / 60:.0f} min temu, próg to "
            f"{prog / 60:.0f} min ({cykli_zwloki} cykle po {interwal} s).\n\n"
            f"{diagnoza}\n\n"
            f"Sprawdź: kubectl logs deploy/trader-{w['worker']} --tail=50"))
    return powody


async def sprawdz_puls_workerow(fetch_all, notify, sucho: bool = False,
                                prog_min: int | None = None) -> int:
    """Czy cztery procesy wejscia danych domykaja cykle.

    DLACZEGO TO NIE JEST `--co swiezosc` JESZCZE RAZ

    Swiezosc wykrywa SKUTEK: dane przestaly naplywac. To dobra obrona i ta jej
    nie zastepuje - lapie tez awarie, ktorych puls nie zobaczy (skasowany
    Deployment, zapchany dysk, worker piszacy nie tam, gdzie trzeba). Ale
    odpowiada dopiero po czasie jednej swiecy zrodlowej (przy 1h - trzy
    godziny), nie odroznia „worker stoi" od „gielda nie oddaje danych" i nie
    mowi, KTORY z czterech procesow zamilkl: sygnal z `feat.feature_value` jest
    ten sam niezaleznie od tego, czy stanal `market`, `history` czy `features`.

    Puls odpowiada na pytanie o PROCES, swiezosc na pytanie o DANE. Zdrowy
    proces moze nie miec danych i to nie jest ta sama awaria.

    DLACZEGO TO NIE JEST `--co zywotnosc` JESZCZE RAZ

    Tamta czyta `risk.engine_heartbeat` i `risk.dozorca_heartbeat` - pulsy
    procesow, ktore DECYDUJA. Ten czyta `risk.worker_heartbeat` - pulsy
    procesow, ktore ZBIERAJA. Osobna funkcja, bo prog jest liczony inaczej
    (z interwalu w wierszu, nie ze stalej) i bo tresc alarmu ma prowadzic gdzie
    indziej.

    STAN, NIE ZDARZENIE

    Pytamy o wiek wiersza, a nie o to, czy przyszedl jakis sygnal. Stan jest
    odporny na kazdy sposob, w jaki proces moze zamilknac - takze na taki,
    ktorego nie przewidzielismy.

    CZEGO TA CZUJKA NIE POWIE

    Wiersz zaklada PIERWSZY UDANY CYKL workera (puls.py). Worker, ktory od
    wdrozenia nie domknal ani jednego, nie ma wiersza i tu sie nie pojawi -
    lapie go licznik pieciu awarii (CrashLoopBackOff widac w `kubectl get pods`)
    oraz `--co swiezosc`. Zgadywanie, ktore workery „powinny" istniec, byloby
    czwarta kopia listy z values.yaml.
    """
    # Prog jako parametr - zeby dalo sie wywolac SCIEZKE ALARMOWA na zadanie.
    # Bez tego jedynym sposobem sprawdzenia, czy alarm dziala, byloby zabicie
    # workera na produkcji.
    prog_s = None if prog_min is None else prog_min * 60

    wiersze = await fetch_all(
        "SELECT worker, interwal_s, last_beat_at, awarii_pod_rzad, ostatni_blad,"
        "       extract(epoch from (now() - last_beat_at)) AS wiek_s,"
        "       extract(epoch from (now() - ostatnia_proba_at)) AS proba_wiek_s"
        "  FROM risk.worker_heartbeat ORDER BY worker")
    if not wiersze:
        # Cisza jest wynikiem, wiec musi byc widoczna. Inaczej nie da sie
        # odroznic czujki, ktora nic nie znalazla, od czujki, ktora nie dziala.
        print("brak wpisow o pulsie workerow — żaden nie domknął jeszcze cyklu")
        return 0

    po_workerze = {w["worker"]: w for w in wiersze}
    powody = ocen_puls_workerow(wiersze, prog_s=prog_s)
    ciche = {p[0] for p in powody}
    for w in wiersze:
        znacznik = "MILCZY  " if w["worker"] in ciche else "żyje    "
        print(f"  {znacznik} {w['worker']}: ostatni udany cykl "
              f"{w['wiek_s'] / 60:.0f} min temu, interwał {w['interwal_s']} s, "
              f"awarii pod rząd {w['awarii_pod_rzad']}")

    if not powody:
        print("  bez zastrzeżeń")
        return 0

    if sucho:
        print("Tryb suchy — powiadomienie nie wysyłane.")
        return len(powody)

    odbiorcy = await fetch_all("SELECT id FROM app.\"user\" WHERE is_active AND role = 'owner'")
    zgloszone = trwale = 0
    for worker, tytul, tresc in powody:
        # TLUMIENIE BEZ DODATKOWEJ KOLUMNY, tak samo jak przy zywotnosci
        # silnikow: jesli powiadomienie o TYM workerze poszlo juz PO jego
        # ostatnim udanym cyklu, ta awaria jest zgloszona. Gdy worker wroci,
        # `last_beat_at` przesunie sie i nastepna awaria bedzie nowa.
        #
        # `engine.down` jest zdarzeniem `always_on`, wiec omija zwykly cooldown -
        # bez tlumienia czujka krzyczalaby co dziesiec minut, az ktos wyciszy
        # kanal. Wyciszony kanal to gorszy stan niz brak czujki.
        juz = await fetch_all(
            "SELECT 1 FROM notify.notification WHERE event_code = 'engine.down'"
            " AND data->>'worker' = %s AND created_at > %s LIMIT 1",
            (worker, po_workerze[worker]["last_beat_at"]))
        if juz:
            # Worker martwy od tygodnia to jedno zgloszenie i jeden czerwony
            # Job, a nie 1008 - linia "MILCZY ..." wyzej opisuje stan przy
            # kazdym przebiegu. `last_beat_at` odroznia tu nawrot za darmo:
            # worker, ktory domknal cykl i zamilkl ponownie, ma nowszy puls niz
            # stare powiadomienie, wiec przestaje byc tlumiony.
            trwale += 1
            print(f"  (awaria {worker} juz zgloszona — cisza)")
            continue
        zgloszone += 1
        print(f"  {tytul}")
        for u in odbiorcy:
            # `engine` NIE jest tu podawany: notify.notification.engine ma typ
            # ref.engine, a „market" czy „news" nie sa silnikami handlowymi.
            # Nazwa workera idzie do `data`, skad czyta ja tlumienie wyzej.
            await notify(u["id"], "engine.down", tytul, tresc, severity="critical",
                         data={"worker": worker,
                               "awarii_pod_rzad": po_workerze[worker]["awarii_pod_rzad"]})

    # KOD WYJSCIA IDZIE ZA TLUMIENIEM, tak samo jak w pozostalych czujkach tego
    # pliku - patrz `kod_wyjscia` i docstring modulu. Drugi, niezalezny od
    # kanalu powiadomien slad zostaje przy ZGLOSZENIU; przy kazdym przebiegu
    # bylby tylko szumem, ktory uczy przewijac czerwone Joby.
    return kod_wyjscia(zgloszone, trwale, "worker bez pulsu")


# KANAL ALARMOWY NIE MOZE BYC JEDYNYM MIEJSCEM, W KTORE NIKT NIE PATRZY
#
# `inapp` to powiadomienie W PANELU. Dla zdarzenia `always_on` o wadze
# `critical` - kill-switch, martwy silnik, brak kopii zapasowej - znaczy to
# tyle, ze alarm dotrze do czlowieka dopiero wtedy, gdy ten sam z siebie
# otworzy dashboard. O trzeciej w nocy nie otworzy.
#
# ZMIERZONE NA PRODUKCJI 2026-09-24: `notify.delivery` mialo 89 dostarczen
# i WSZYSTKIE rodzaju `inapp`, przy 39 powiadomieniach `critical` w ostatniej
# dobie. Kanal wychodzacy (whatsapp, webhook) nie byl skonfigurowany ani razu
# w calej historii tabeli.
#
# DLACZEGO TA CZUJKA JEDNAK POWIADAMIA - KOREKTA Z TEGO SAMEGO DNIA
#
# Pierwsza wersja nie wysylala nic i mowila wylacznie kodem wyjscia. Powod:
# czujka sprawdzajaca kanal alarmowy nie powinna uzywac kanalu alarmowego -
# to ta sama wada, ktora opisuje `nadzor ginie razem z nadzorowanym`.
#
# Rozumowanie bylo poprawne, wniosek zly. Brak kanalu jest luka KONFIGURACYJNA
# i nie mija sam, wiec czujka bez tlumienia swiecilaby na czerwono co dziesiec
# minut BEZ KONCA - a `trader-zywotnosc` uruchamia w tym samym zadaniu osiem
# innych sprawdzen. Trwale czerwony Job uczy przewijac czerwone Joby, czyli
# psuje sygnal dla WSZYSTKICH czujek, nie tylko dla tej. To jest dokladnie ta
# szkoda, przed ktora broni sie `kod_wyjscia`.
#
# Obieg zamkniety zostaje i jest nazwany wprost w tresci zdarzenia
# `notify.no_channel` (migracja 0056): powiadomienie o tym, ze alarmy nie
# wychodza poza panel, przychodzi do panelu. Nie da sie inaczej - nie ma czym.
#
# DRUGI SLAD ZOSTAJE NIEZALEZNY OD KANALU: pierwsze zgloszenie daje niezerowy
# kod wyjscia, wiec Job swieci na czerwono RAZ, a linia w logu wypisuje pelny
# stan przy kazdym przebiegu. Tlumione sa dopiero powtorzenia.
RODZAJ_TYLKO_PANEL = "inapp"


def ocen_kanaly(wiersze: list[dict]) -> list[tuple[str, str]]:
    """Ktore krytyczne zdarzenia nie maja drogi poza panel. CZYSTA FUNKCJA.

    Wiersz opisuje PARE odbiorca-zdarzenie, bo preferencje sa per odbiorca:
    jeden uzytkownik z WhatsAppem nie zalatwia drugiego, ktory ma sam panel.

    Zwraca (klucz, opis). Klucz jest stabilny (kod zdarzenia + id odbiorcy),
    zeby wolajacy mogl po nim tlumic - tak samo jak przy pozostalych czujkach.
    """
    braki = []
    for w in wiersze:
        kanaly = [k for k in (w["kanaly"] or []) if k != RODZAJ_TYLKO_PANEL]
        if kanaly:
            continue
        braki.append((
            f"{w['event_code']}:{w['user_id']}",
            f"{w['event_code']} ({w['severity']}) dla {w['login']}: "
            f"wylacznie panel"))
    return braki


async def sprawdz_kanaly(fetch_all, notify, sucho: bool = False) -> int:
    """Czy alarm krytyczny ma jak opuscic dashboard.

    MILCZY, DOPOKI CALY SYSTEM CHODZI W DEMO - decyzja wlasciciela 2026-09-24:
    "to jest demo, nie musze miec alarmow od demo, zostawiaj na stronie".

    Panel jako jedyny kanal jest wtedy WYBOREM, nie luka, a czujka zglaszajaca
    swiadomy wybor raz na dobe uczy odbiorce przewijac powiadomienia - czyli
    psuje dokladnie ten kanal, ktorego pilnuje.

    WARUNEK CZYTAMY ZE STANU, NIE Z FLAGI. Gdyby tryb przychodzil parametrem
    z chartu, przejscie na `live` wymagaloby, zeby ktos pamietal o zdjeciu
    wyciszenia - a wyciszenie, ktore trzeba pamietac zdjac, jest wylaczone na
    stale. `risk.engine_heartbeat.mode` zapisuje KAZDY silnik przy kazdym cyklu,
    wiec pierwszy silnik przelaczony na `live` wlacza te czujke sam, bez niczyjej
    pamieci. To ta sama zasada, ktora w tym pliku odroznia stan od zdarzenia.
    """
    # BRAMKA RENACODE JEST DROGA POZA PANEL (2026-10-04): `notify()` przekazuje
    # do niej kazdy alarm krytyczny, niezaleznie od kanalow uzytkownika
    # (services/api/bramka.py).
    #
    # ZMIENNA W SRODOWISKU TO NIE DROGA (przeglad 2026-10-04). Do tej poprawki
    # "skonfigurowana" znaczylo "BRAMKA_KLUCZ jest ustawiony" - klucz
    # odrzucany przez bramke (literowka, rotacja tylko po jednej stronie)
    # wygladal tu identycznie jak dzialajacy. Teraz `GET /v1/stan` z tym
    # kluczem: 200 = bramka go zna. Przed wyciszeniem demo, zeby linia w logu
    # mowila prawde takze wtedy, gdy czujka nie ma prawa alarmowac.
    from services.api import bramka
    droga_bramka = False
    if bramka.skonfigurowana():
        try:
            stan = await bramka.stan()
            droga_bramka = True
            print(f"kanaly: bramka RenaCode przyjmuje klucz tradera (kanal teraz: "
                  f"{stan.get('kanal_auto', '?')}) — alarmy krytyczne maja droge "
                  f"poza panel; slad w notify.delivery, kind 'bramka'")
        except Exception as e:                                # noqa: BLE001
            print(f"kanaly: BRAMKA NIE PRZYJMUJE klucza tradera albo nie odpowiada "
                  f"({type(e).__name__}: {e}) — alarmy krytyczne zostaja w panelu, "
                  f"jesli nie ma innego kanalu")

    zywe = await fetch_all(
        "SELECT DISTINCT mode::text AS mode FROM risk.engine_heartbeat")
    tryby = {w["mode"] for w in zywe}
    if tryby and "live" not in tryby:
        print(f"kanaly: wszystkie silniki w trybie {', '.join(sorted(tryby))} — "
              f"panel jako jedyny kanal jest wyborem, nie luka; czujka wlaczy "
              f"sie sama, gdy ktorykolwiek silnik przejdzie na `live`")
        return 0

    if droga_bramka:
        return 0

    wiersze = await fetch_all(
        "SELECT et.code AS event_code, et.default_severity AS severity,"
        "       u.id AS user_id, u.login,"
        "       coalesce(p.channels::text[], ARRAY[]::text[]) AS kanaly"
        "  FROM notify.event_type et"
        "  CROSS JOIN app.\"user\" u"
        "  LEFT JOIN notify.preference p"
        "         ON p.event_code = et.code AND p.user_id = u.id AND p.enabled"
        " WHERE u.is_active AND u.role = 'owner'"
        "   AND et.always_on AND et.default_severity = 'critical'"
        " ORDER BY et.code, u.login")
    if not wiersze:
        print("kanaly: brak krytycznych zdarzen always_on albo brak aktywnych "
              "odbiorcow — nie ma czego sprawdzac")
        return 0

    braki = ocen_kanaly(wiersze)
    for w in wiersze:
        kanaly = ", ".join(w["kanaly"]) if w["kanaly"] else "ZADNEGO"
        znacznik = "TYLKO PANEL" if any(k.startswith(f"{w['event_code']}:{w['user_id']}")
                                        for k, _ in braki) else "ok         "
        print(f"  {znacznik} {w['event_code']} -> {w['login']}: {kanaly}")

    if not braki:
        print("  każde krytyczne zdarzenie ma drogę poza panel")
        return 0

    tytul = f"{len(braki)} × alarm krytyczny wyłącznie w panelu"
    tresc = (
        "Zdarzenia krytyczne bez kanału innego niż panel:\n  "
        + "\n  ".join(opis for _, opis in braki)
        + "\n\nKill-switch, martwy silnik i brak kopii dotrą do człowieka "
          "dopiero, gdy ten sam otworzy dashboard. Skonfiguruj kanał "
          "`whatsapp` albo `webhook` w Ustawieniach i włącz go dla tych zdarzeń."
        "\n\nTo powiadomienie z konieczności przyszło tą samą drogą, której "
        "brak opisuje — innej nie ma.")
    print(f"\n  {tytul}")
    if sucho:
        print("  Tryb suchy — powiadomienie nie wysyłane.")
        return len(braki)

    # JEDNO POWIADOMIENIE NA PRZEBIEG, NIE JEDNO NA LUKE - i tlumienie liczone
    # DOKLADNIE TYM SAMYM WARUNKIEM, ktorego uzywa `notify()`.
    #
    # ZMIERZONE NA PRODUKCJI 2026-09-24, zadanie trader-zywotnosc-29837520.
    # Pierwsza wersja wolala `notify()` osobno dla kazdej z pieciu luk i tlumila
    # po kluczu `zdarzenie:odbiorca`. Powstal JEDEN wiersz w `notify.notification`:
    # `notify.no_channel` nie jest `always_on`, wiec `notify()` zastosowal wlasny
    # cooldown katalogowy (86400 s, per odbiorca i kod zdarzenia) i odrzucil
    # cztery kolejne wywolania - po cichu, bo odrzucenie przez cooldown nie jest
    # bledem.
    #
    # Skutek: przy kazdym nastepnym przebiegu moje tlumienie znajdowalo wiersz
    # dla JEDNEJ luki i liczylo pozostale cztery jako nowe. `kod_wyjscia` wracalo
    # 4, Job swiecil na czerwono bez konca - czyli dokladnie ta wada, ktorej ta
    # poprawka mial zapobiec, tyle ze piętro nizej.
    #
    # DWA WARUNKI TLUMIENIA, KTORE MIERZA CO INNEGO, ZAWSZE SIE ROZJADA. Dlatego
    # pytamy teraz o to samo, co `notify()`: czy powiadomienie tego typu poszlo
    # do tego odbiorcy w oknie cooldownu. Nie o konkretna luke - tresc i tak
    # wymienia wszystkie z nazwy, wiec jedno powiadomienie niesie pelny stan.
    #
    # CO TRACIMY: luka, ktora dojdzie w srodku doby, nie wywola osobnego
    # powiadomienia. Zostaje w linii logu przy KAZDYM przebiegu i wejdzie do
    # dobowego przypomnienia. Przy stanie, ktory nie mija sam, to jest wlasciwy
    # rytm - a zbieznosc do zielonego Joba jest warunkiem, zeby czerwony cokolwiek
    # znaczyl.
    odbiorcy = await fetch_all("SELECT id FROM app.\"user\" WHERE is_active AND role = 'owner'")
    juz = await fetch_all(
        "SELECT 1 FROM notify.notification"
        " WHERE event_code = 'notify.no_channel'"
        "   AND created_at > now() - interval '24 hours' LIMIT 1")
    if juz:
        return kod_wyjscia(0, len(braki), "alarm krytyczny tylko w panelu")

    klucze = "+".join(sorted(k for k, _ in braki))
    for u in odbiorcy:
        await notify(u["id"], "notify.no_channel", tytul, tresc,
                     severity="critical", data={"klucze": klucze, "ile": len(braki)})
    return kod_wyjscia(len(braki), 0, "alarm krytyczny tylko w panelu")


# Powody pominiecia, ktore znacza ODLOZENIE, a nie odmowe. Lista jest DODATNIA:
# `journal.pominiecie` trzyma takze powody, ktore znacza swiadoma odmowe wejscia
# (pasmo ceny, sesja, minimum wartosci) i te NIE sa awaria - swieca dostaje wtedy
# decyzje `hold`. Odlozenie jest czym innym: decyzji NIE MA jeszcze wcale.
POWODY_ODLOZENIA = ("niepelne_sygnaly", "za_malo_cech")


def ocen_odlozone_bez_decyzji(wiersze: list[dict]) -> list[tuple[str, str]]:
    """Swiece ODLOZONE, do ktorych petla nigdy nie wrocila. CZYSTA FUNKCJA.

    Wiersz opisuje jedna swiece jednego instrumentu: `symbol`, `bar_start`,
    `powod`. Oddaje (klucz, opis); klucz jest stabilny, zeby wolajacy mogl po nim
    tlumic, tak samo jak w pozostalych czujkach.

    ZERO JEST WARTOSCIA ODNIESIENIA, I TO JEST ZMIERZONE. Odlozenie samo w sobie
    nie jest awaria - to normalna praca maszyny: cechy pochodne sa gotowe od razu
    po zamknieciu swiecy, a cenowe 20-35 min pozniej, wiec pierwszy cykl odklada,
    a ktorys z nastepnych rozstrzyga (uzasadnienie bramki `MIN_POKRYCIE`
    w `loop.py` i w commicie d8c7759). Zmierzone na produkcji 2026-09-27 na CALEJ
    historii `journal.pominiecie`: kazdego dnia 40-120 odlozen i KAZDE skonczylo
    sie decyzja w swoim oknie. Dlatego czujka nie ma progu tolerancji - jeden
    taki wiersz jest odstepstwem od stanu, ktory trwa od poczatku.
    """
    return [(f"{w['symbol']}:{w['bar_start'].isoformat()}",
             f"{w['symbol']} świeca {w['bar_start']:%m-%d %H:%M} "
             f"({w['powod']})") for w in wiersze]


def dlugosc_swiecy_petli(bar_starty: list[datetime]) -> int | None:
    """Ile minut ma swieca, ktora PETLA przetwarza. Z DANYCH. CZYSTA FUNKCJA.

    DLACZEGO NIE Z PARAMETRU `--bar`, I TO JEST WADA ZMIERZONA NA PRODUKCJI
    2026-09-27, DWADZIESCIA MINUT PO WDROZENIU TEJ CZUJKI.

    `journal.pominiecie` NIE MA kolumny z rozmiarem swiecy. Pierwsza wersja brala
    dlugosc okna z `--bar`, wiec uruchomiona z `--bar 1h` czytala te SAME wiersze
    (swiece czterogodzinne, bo tylko takie petla przetwarza) i wymagala decyzji
    w 60 minutach od otwarcia swiecy. Cechy cenowe sa gotowe 20-35 min po
    zamknieciu swiecy, czyli grubo po tej granicy - i tak powstalo 190 „swiec bez
    decyzji" na w pelni zdrowej produkcji. Byloby to KRYTYCZNE powiadomienie
    o stanie, ktorego nie ma, przy pierwszym nocnym przebiegu.

    Dlugosc bierzemy wiec stamtad, gdzie jest naprawde: z ODSTEPU miedzy
    kolejnymi swiecami, ktore petla zapisala. Petla zaklada jeden wiersz na swiece,
    ktora przetworzyla, wiec najczestszy odstep JEST jej swieca. Zmierzone na
    produkcji: odstepy 240 min co do minuty (12:00, 08:00, 04:00, 00:00, 20:00).

    None przy mniej niz trzech roznych swiecach - z dwoch punktow nie da sie
    odczytac „najczestszego" odstepu, a zgadniecie dalo by dokladnie te wade,
    ktora ta funkcja usuwa.
    """
    rozne = sorted(set(bar_starty))
    if len(rozne) < 3:
        return None
    odstepy = [round((b - a).total_seconds() / 60) for a, b in zip(rozne, rozne[1:])]
    odstepy = [o for o in odstepy if o > 0]
    if not odstepy:
        return None
    return max(set(odstepy), key=odstepy.count)


async def sprawdz_odlozone_decyzje(fetch_all, notify, sucho: bool = False, *,
                                   engine: str = "crypto", mode: str = "demo",
                                   okno_h: int = 48) -> int:
    """Czy swieca ODLOZONA przez niepelne sygnaly dostala potem decyzje.

    CZEGO TA CZUJKA PILNUJE, A CZEGO NIE

    Bramka `MIN_POKRYCIE` z 2026-09-22 slusznie zamienila handel na niepelnym
    komplecie sygnalow (jedenascie dni, pokrycie 0,02-0,10) na ODLOZENIE decyzji.
    Zamienila przy tym awarie GLOSNA na CICHA: przy trwalym braku cech kazda
    swieca zostanie odlozona, nikt nie wejdzie w rynek, a silnik bez wejsc
    wyglada dokladnie tak samo jak silnik na spokojnym rynku. Puls bije, swiece
    przychodza, zadania sa zielone.

    Zadna z jedenastu pozostalych czujek tego nie widzi: `journal.pominiecie` nie
    bylo czytane przez ani jedna z nich, a licznik `odlozone_niepelne` zyje
    wylacznie w pamieci petli.

    NIE ZASTEPUJE `engine.down`. Petla, ktora nie chodzi wcale, nie zapisuje tez
    odlozen, wiec ta miara wtedy milczy - i tak ma byc, bo o tamtym stanie mowi
    `risk.engine_heartbeat`. Ta pilnuje stanu ODWROTNEGO: petla zyje, cyklicznie
    odklada i nigdy nie wraca.

    OKNO SWIECY BIERZEMY Z HORYZONTU, nie z parametru: "czy wrocil w oknie tej
    swiecy" znaczy co innego dla swiecy godzinnej i czterogodzinnej, a druga
    definicja dlugosci swiecy w tym pliku rozjechalaby sie z `horizons.py`.
    """
    if await poza_sesja(fetch_all, engine):
        print(f"odlozone ({engine}/{mode}): rynek zamkniety — poza sesja swiece "
              f"nie powstaja, wiec nie ma czego rozstrzygac")
        return 0

    # DLUGOSC SWIECY Z DANYCH, NIE Z PARAMETRU - uzasadnienie liczbowe
    # w `dlugosc_swiecy_petli`. Brak odczytu to LUKA POMIAROWA, nie alarm:
    # bez dlugosci okna pytanie "czy wrocil w oknie tej swiecy" nie ma tresci.
    znaczniki = await fetch_all(
        "SELECT DISTINCT bar_start FROM journal.pominiecie"
        " WHERE engine::text = %s AND mode::text = %s"
        "   AND powod = ANY(%s)"
        "   AND bar_start >= now() - make_interval(hours => %s)"
        " ORDER BY bar_start", (engine, mode, list(POWODY_ODLOZENIA), okno_h))
    minut = dlugosc_swiecy_petli([w["bar_start"] for w in znaczniki])
    if minut is None:
        print(f"odlozone ({engine}/{mode}, {okno_h} h): mniej niz trzy rozne "
              f"swiece z odlozeniem — dlugosci swiecy petli NIE DA SIE odczytac "
              f"z danych, a zgadniecie dalo by falszywe alarmy")
        return 0

    wiersze = await fetch_all(
        "SELECT i.symbol, p.bar_start, p.powod"
        "  FROM journal.pominiecie p"
        "  JOIN ref.instrument i ON i.id = p.instrument_id"
        " WHERE p.engine::text = %s AND p.mode::text = %s"
        "   AND p.powod = ANY(%s)"
        "   AND p.bar_start >= now() - make_interval(hours => %s)"
        # SWIECA MUSI BYC ZAMKNIETA I MIEC SZANSE NA DRUGI CYKL. Swieca trwajaca
        # jest odlozona ZGODNIE Z ZAMYSLEM i alarm o niej zapalalby sie przy
        # kazdym przebiegu na w pelni zdrowym systemie.
        "   AND p.bar_start < now() - make_interval(mins => %s)"
        # OKNO DECYZJI DLA SWIECY B TO [B + swieca, B + 2*swieca) - tak samo
        # jak `loop.okno_decyzji`: decyzja dla B zapada dopiero PO jej
        # zamknieciu. Do 2026-10-02 stalo tu [B, B + swieca), czyli okno, w ktorym
        # petla rozstrzyga swiece POPRZEDNIA. Kazda decyzja dla B-1 "gasila"
        # brak decyzji dla B, wiec czujka milczala, a odzywala sie najwczesniej
        # jedna swieca za pozno - gdy brakowalo juz dwoch z rzedu.
        "   AND NOT EXISTS ("
        "         SELECT 1 FROM journal.decision d"
        "          WHERE d.instrument_id = p.instrument_id"
        "            AND d.engine = p.engine AND d.mode = p.mode"
        "            AND d.decided_at >= p.bar_start"
        "                              + make_interval(mins => %s)"
        "            AND d.decided_at < p.bar_start"
        "                              + make_interval(mins => %s))"
        " ORDER BY p.bar_start DESC, i.symbol",
        (engine, mode, list(POWODY_ODLOZENIA), okno_h, 2 * minut, minut, 2 * minut))

    braki = ocen_odlozone_bez_decyzji(wiersze)
    if not braki:
        print(f"odlozone ({engine}/{mode}, swieca {minut} min z danych, "
              f"{okno_h} h): kazda odlozona swieca dostala decyzje w swoim oknie")
        return 0

    tytul = (f"{len(braki)} × świeca odłożona i NIEROZSTRZYGNIĘTA "
             f"({engine}/{mode}, świeca {minut} min)")
    tresc = (
        "Pętla odłożyła te świece z powodu niepełnych sygnałów i nigdy do nich "
        "nie wróciła — w żadnej z nich nie było ŻADNEJ decyzji, ani wejścia, "
        "ani wyjścia, ani `hold`:\n  "
        + "\n  ".join(opis for _, opis in braki[:20])
        + (f"\n  … i {len(braki) - 20} więcej" if len(braki) > 20 else "")
        + "\n\nWartością odniesienia jest ZERO: na całej historii produkcji "
          "każde odłożenie kończyło się decyzją w swoim oknie (zmierzone "
          "2026-09-27, 40–120 odłożeń na dobę, zero nierozstrzygniętych).\n\n"
          "Handel stoi, ale nie z powodu rynku ani ryzyka — z powodu braku "
          "danych. Silnik bez wejść wygląda identycznie jak silnik na spokojnym "
          "rynku, dlatego ten stan ma własny alarm. Sprawdź pokrycie wag "
          "w `journal.decision.pokrycie_sygnalow` i świeżość cech "
          "w `feat.feature_value`; bramkę opisuje `MIN_POKRYCIE` w loop.py.")
    print(f"\n  {tytul}")
    for _, opis in braki[:20]:
        print(f"    {opis}")
    if sucho:
        print("  Tryb suchy — powiadomienie nie wysylane.")
        return len(braki)

    # TLUMIENIE TYM SAMYM WARUNKIEM, KTOREGO UZYWA `notify()` - uzasadnienie
    # w `sprawdz_kanaly`: dwa warunki tlumienia mierzace co innego zawsze sie
    # rozjada, a rozjazd objawia sie Jobem czerwonym bez konca.
    juz = await fetch_all(
        "SELECT 1 FROM notify.notification"
        " WHERE event_code = 'engine.decyzje_odlozone'"
        "   AND created_at > now() - interval '24 hours' LIMIT 1")
    if juz:
        return kod_wyjscia(0, len(braki), "swieca odlozona bez decyzji")
    odbiorcy = await fetch_all("SELECT id FROM app.\"user\" WHERE is_active AND role = 'owner'")
    for u in odbiorcy:
        await notify(u["id"], "engine.decyzje_odlozone", tytul, tresc,
                     engine=engine, severity="critical",
                     data={"ile": len(braki), "swieca_min": minut,
                           "klucze": "+".join(sorted(k for k, _ in braki[:20]))})
    return kod_wyjscia(len(braki), 0, "swieca odlozona bez decyzji")


# ------------------------------------------------- decyzje na kazda swiece
#
# CZUJKI WYKRYWALY BRAK PULSU, NIE BRAK SENSU (audyt Fable F-8.3, 2026-10-03).
# Puls silnika bije, gdy cykl sie udal - takze gdy nie bylo ani jednej decyzji.
# `swiezosc` pilnuje cech, `odlozone` swiec odlozonych przez niepelny komplet
# sygnalow; zadna nie pytala wprost "czy ta swieca ma decyzje dla kazdego
# instrumentu uniwersum, skoro cechy przyszly".
#
# Ile domknietych swiec z rzedu bez kompletu daje alarm. Jedna to restart poda
# albo pojedynczy wywrocony cykl - nie stan.
SWIEC_BEZ_DECYZJI = 2

# Po ilu minutach od ZAMKNIECIA swiecy jej decyzje musza juz byc. Zmierzone
# opoznienie decyzji przed F-4.1 siegalo 88 min (journal.decision 02.10), po
# skroceniu interwalow history/features do 300 s najgorszy przypadek to 15 min.
# 90 min lezy nad historycznym maksimum, wiec czujka nie miga przy powolnej
# swiecy, a dwie swiece z rzedu i tak rozstrzygaja o alarmie.
ZWLOKA_DECYZJI_MIN = 90


def swiece_do_oceny(teraz: datetime, minut: int, zwloka_min: int,
                    ile: int) -> list[datetime]:
    """Poczatki swiec, ktorych decyzje MUSZA juz byc - od najnowszej. CZYSTA.

    Decyzja dla swiecy B zapada w oknie [B + swieca, B + 2*swieca)
    (`loop.okno_decyzji`); oceniamy B dopiero, gdy od jej zamkniecia minela
    `zwloka_min`.
    """
    krok = minut * 60
    granica = teraz - timedelta(minutes=minut + zwloka_min)
    najnowsza = datetime.fromtimestamp(granica.timestamp() // krok * krok,
                                       tz=timezone.utc)
    return [najnowsza - timedelta(minutes=minut * k) for k in range(ile)]


def ocen_decyzje_na_swiece(wiersze: list[dict], uniwersum: int,
                           swiec: int = SWIEC_BEZ_DECYZJI) -> dict | None:
    """Czy ostatnie `swiec` swiec ma decyzje dla mniej niz calego uniwersum,
    choc cechy tych swiec przyszly. CZYSTA FUNKCJA.

    Wiersz: `bar_start`, `decyzji` (rozne instrumenty z decyzja w oknie tej
    swiecy), `z_cechami` (rozne instrumenty z jakakolwiek cecha tej swiecy).
    Od najnowszej. None = cisza.

    BRAK CECH TO NIE TA CZUJKA: potok cech, ktory stanal, zglasza `swiezosc`.
    Dwa alarmy o jednej awarii ucza przewijac oba.
    """
    ostatnie = wiersze[:swiec]
    if len(ostatnie) < swiec or not uniwersum:
        return None
    if any(w["decyzji"] >= uniwersum or not w["z_cechami"] for w in ostatnie):
        return None
    return {"swiece": [w["bar_start"] for w in ostatnie],
            "opis": "; ".join(
                f"{w['bar_start']:%m-%d %H:%M}: decyzji {w['decyzji']}/{uniwersum}, "
                f"z cechami {w['z_cechami']}/{uniwersum}" for w in ostatnie)}


SQL_DECYZJE_NA_SWIECE = (
    "SELECT s.b AS bar_start,"
    "       (SELECT count(DISTINCT d.instrument_id) FROM journal.decision d"
    "         WHERE d.engine::text = %(engine)s AND d.mode::text = %(mode)s"
    "           AND d.instrument_id = ANY(%(ids)s)"
    "           AND d.decided_at >= s.b + make_interval(mins => %(m)s)"
    "           AND d.decided_at <  s.b + make_interval(mins => 2 * %(m)s)"
    "       ) AS decyzji,"
    "       (SELECT count(DISTINCT fv.instrument_id) FROM feat.feature_value fv"
    "          JOIN feat.feature_def fd ON fd.id = fv.feature_def_id"
    "         WHERE fd.active AND fd.params->>'bar' = %(bar)s"
    "           AND fv.instrument_id = ANY(%(ids)s)"
    "           AND fv.event_time = s.b"
    "       ) AS z_cechami"
    "  FROM unnest(%(swiece)s::timestamptz[]) AS s(b)"
    " ORDER BY s.b DESC")


async def sprawdz_decyzje(fetch_all, notify, sucho: bool = False, *,
                          engine: str = "crypto", mode: str = "demo",
                          bar: str = "4h", teraz: datetime | None = None) -> int:
    """Czy silnik podejmuje decyzje dla kazdego instrumentu na kazdej swiecy.

    Uniwersum z `uniwersum.WARUNEK_UNIWERSUM_DECYZYJNEGO` - tym samym, ktorym
    petla wybiera instrumenty. Cechy - z tego samego horyzontu.
    """
    from services.engine.horizons import get as get_horizon
    from services.engine.uniwersum import sql_uniwersum_decyzyjnego

    h = get_horizon(bar)
    teraz = teraz or datetime.now(timezone.utc)
    instrumenty = await fetch_all(sql_uniwersum_decyzyjnego("id, symbol"), (engine,))
    if not instrumenty:
        print(f"decyzje ({engine}/{mode}/{bar}): uniwersum decyzyjne puste — "
              f"nie ma czego oceniac")
        return 0
    ids = [w["id"] for w in instrumenty]
    swiece = swiece_do_oceny(teraz, h.bar_minutes, ZWLOKA_DECYZJI_MIN,
                             SWIEC_BEZ_DECYZJI)
    wiersze = await fetch_all(SQL_DECYZJE_NA_SWIECE, {
        "engine": engine, "mode": mode, "ids": ids, "m": h.bar_minutes,
        "bar": bar, "swiece": swiece})
    for w in wiersze:
        print(f"decyzje ({engine}/{mode}/{bar}) świeca {w['bar_start']:%m-%d %H:%M}: "
              f"{w['decyzji']}/{len(ids)} instrumentów z decyzją, "
              f"{w['z_cechami']}/{len(ids)} z cechami")

    werdykt = ocen_decyzje_na_swiece(wiersze, len(ids))
    if werdykt is None:
        if any(not w["z_cechami"] for w in wiersze):
            print("  świeca bez cech — to stan dla `--co swiezosc`, nie dla tej "
                  "czujki")
        else:
            print(f"  komplet decyzji w co najmniej jednej z ostatnich "
                  f"{SWIEC_BEZ_DECYZJI} świec — cisza")
        return 0

    brakujace = await fetch_all(
        "SELECT i.symbol FROM ref.instrument i"
        " WHERE i.id = ANY(%(ids)s) AND NOT EXISTS ("
        "   SELECT 1 FROM journal.decision d"
        "    WHERE d.instrument_id = i.id"
        "      AND d.engine::text = %(engine)s AND d.mode::text = %(mode)s"
        "      AND d.decided_at >= %(od)s AND d.decided_at < %(do)s)"
        " ORDER BY i.symbol",
        {"ids": ids, "engine": engine, "mode": mode,
         "od": werdykt["swiece"][0] + timedelta(minutes=h.bar_minutes),
         "do": werdykt["swiece"][0] + timedelta(minutes=2 * h.bar_minutes)})
    symbole = ", ".join(w["symbol"] for w in brakujace) or "?"
    tytul = (f"Brak decyzji dla części uniwersum przez {SWIEC_BEZ_DECYZJI} "
             f"świece ({engine}/{bar})")
    tresc = (f"{werdykt['opis']}.\n\nBez decyzji w ostatniej świecy: {symbole}.\n\n"
             f"Cechy tych świec przyszły, a silnik nie zapisał decyzji (ani "
             f"`hold`) dla każdego instrumentu. Puls silnika tego nie widzi — "
             f"bije, gdy cykl się udał. Sprawdź log `trader-engine` "
             f"i `journal.pominiecie`.")
    print(f"  {tytul}\n  bez decyzji: {symbole}")
    if sucho:
        print("  Tryb suchy — powiadomienie nie wysylane.")
        return 1

    juz = await fetch_all(
        "SELECT 1 FROM notify.notification"
        " WHERE event_code = 'engine.brak_decyzji' AND engine::text = %s"
        "   AND created_at > now() - interval '24 hours' LIMIT 1", (engine,))
    if juz:
        return kod_wyjscia(0, 1, "brak decyzji dla części uniwersum")
    odbiorcy = await fetch_all("SELECT id FROM app.\"user\" WHERE is_active AND role = 'owner'")
    for u in odbiorcy:
        await notify(u["id"], "engine.brak_decyzji", tytul, tresc,
                     engine=engine, severity="warning",
                     data={"swiece": [s.isoformat() for s in werdykt["swiece"]],
                           "bez_decyzji": symbole})
    return kod_wyjscia(1, 0, "brak decyzji dla części uniwersum")


# ------------------------------------------------------ przebiegi CronJobow
#
# NIEUDANE JOBY BEZ ESKALACJI (audyt Fable F-8.4, 2026-10-03, migracja 0074).
# Kazdy CronJob dopisuje po zakonczeniu wiersz do `risk.job_run`
# (`przebieg_zadania.py`; backup - `trap` w powloce). Ta czujka czyta ostatnie
# przebiegi kazdego WLACZONEGO zadania - liste podaje chart, bo tylko on wie,
# ktore zadania sa wlaczone.
#
# ODSTEP Z DANYCH ALBO Z HARMONOGRAMU PODANEGO PRZEZ CHART - nigdy z kopii tutaj.
# Harmonogram mieszka w `values.yaml`; druga kopia w kodzie rozjechalaby sie przy
# pierwszej zmianie (trening nocny -> tygodniowy, F-2.3) i czujka krzyczalaby
# o zadaniu, ktore chodzi dokladnie tak, jak ma.
#
# SAMA MEDIANA Z DANYCH NIE WYSTARCZA (audyt 2026-10-09, S-4). Przy tygodniowym
# trenerze 03-05.10 poszly trzy przebiegi w dwie doby (reczne powtorki po padzie
# z 04.10 i weryfikacje), mediana wyszla 29,9 h i od 08.10 czujka zglaszala
# "trainer: cisza" o zadaniu, ktore do 11.10 nie mialo kiedy pojsc. Dlatego chart
# podaje `schedule` kazdego CronJoba (`--harmonogramy`, ten sam napis co w
# `values.yaml`, wiec bez drugiej kopii), a oczekiwany odstep to WIEKSZA
# z median i najdluzszej przerwy w harmonogramie: skupione powtorki nie skracaja
# oczekiwania ponizej planu, a zadanie chodzace rzadziej niz plan nadal
# podnosi je z danych.
#
# Cisza = brak przebiegu dluzej niz CISZA_KROTNOSC oczekiwanych odstepow + zapas.
CISZA_KROTNOSC = 2
CISZA_ZAPAS_MIN = 30
#
# ...ALE NIE WTEDY, GDY ODSTEP POCHODZI Z HARMONOGRAMU (audyt 2026-10-10, N2-4).
# Krotnosc 2 ma sens dla mediany z danych - to ona jest niepewna. Odstep
# z harmonogramu jest PLANEM: tygodniowy trener, ktory nie wystartowal
# (ImagePull, brak wiersza `job_run`), byl zglaszany dopiero po 2 x 7 d, czyli
# przy DRUGIM opuszczonym terminie. Z harmonogramu prog to plan + zapas na czas
# trwania przebiegu i rozrzut startu: CZESC_ZAPASU planu, nie mniej niz
# CISZA_ZAPAS_MIN i nie wiecej niz ZAPAS_HARMONOGRAMU_MAX_H (trener ma limit
# 3 h, `activeDeadlineSeconds`; 12 h to cztery takie limity). Tydzien -> 7,5 d,
# doba -> 30 h, 6 h -> 7,5 h.
CZESC_ZAPASU = 0.25
ZAPAS_HARMONOGRAMU_MAX_H = 12
# Ile przebiegow wstecz czytamy i ile trzeba, zeby odstep cokolwiek znaczyl.
PRZEBIEGOW_WSTECZ = 10
MIN_PRZEBIEGOW_ODSTEPU = 3
# Zadanie bez ANI JEDNEGO wiersza jest cisza dopiero wtedy, gdy dziennik
# istnieje dluzej niz najrzadsze zadanie (tydzien) - inaczej kazde wdrozenie
# zaczynaloby sie alarmem o zadaniach, ktore jeszcze nie mialy kiedy pojsc.
BRAK_WIERSZY_PO_DNIACH = 8

# PRZEBIEG PLANOWY A RECZNY (poprawka B, 2026-10-05; zawezona tego samego dnia).
#
# 2026-10-04 planowy trener 4h padl na `connection refused`, ktos powtorzyl go
# recznie i ten przebieg przeszedl. Czujka patrzyla wylacznie na OSTATNI wiersz,
# wiec zielone powtorzenie moglo zakryc padniety harmonogram.
#
# PIERWSZA WERSJA ROZROZNIALA PO NAZWIE PODA (`<cronjob>-<minuta>-<5 znakow>`)
# i byla bledna: `kubectl create job --from=cronjob/...` to TEN SAM szablon
# poda (te same polecenie, obraz, rola, kontener startowy z poprawki A), tylko
# z nazwa nadana przez czlowieka. Udane takie powtorzenie dowodzi, ze szablon
# dziala - a czujka uznala je za "reczne" i od wdrozenia (08:30 UTC 05.10)
# trzymala `trader-zywotnosc` na czerwono o jednym, znanym zdarzeniu z 04.10.
#
# Dzis RECZNY jest wylacznie przebieg uruchomiony POZA szablonem CronJoba, a o tym
# wie tylko ten, kto go uruchamia: `przebieg_zadania.py --reczny` zapisuje
# `pod` z przedrostkiem `reczny:` (bez migracji - `pod` to wolny tekst).
# Wszystko inne, takze wiersz bez `pod`, jest planowe.
from services.engine.przebieg_zadania import PRZEDROSTEK_RECZNY  # noqa: E402


def przebieg_planowy(przebieg: dict) -> bool:
    return not (przebieg.get("pod") or "").startswith(PRZEDROSTEK_RECZNY)


# POWTORKA ALARMU O ZADANIU Z ROSNACYM ODSTEPEM - liczona od AWARII (klucz niesie
# id padnietego przebiegu), nie od objawu. Stala doba dawala przy tygodniowym
# treningu siedem identycznych alarmow o jednym zdarzeniu; doba, dwie, cztery,
# potem raz w tygodniu - tyle, ile realnie jest do zauwazenia.
ZADANIE_POWTORKA_H = 24
ZADANIE_POWTORKA_MAX_H = 168


def odstep_powtorki_zadania(zgloszen: int) -> timedelta:
    """Po ilu godzinach od ostatniego zgloszenia wolno zglosic ten sam klucz
    po raz `zgloszen + 1`. CZYSTA FUNKCJA."""
    h = ZADANIE_POWTORKA_H * 2 ** max(0, zgloszen - 1)
    return timedelta(hours=min(h, ZADANIE_POWTORKA_MAX_H))


# Nazwy w polach dnia tygodnia i miesiaca (`SUN`, `JAN`). Bez nich `40 4 * * SUN`
# dawalo ValueError -> None -> cichy powrot do samej mediany (N2-4).
_NAZWY_CRONA = {n: i for i, n in enumerate(
    ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))} | {n: i + 1 for i, n in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))}


def _pole_crona(pole: str, od: int, do: int) -> set[int]:
    """Jedno pole wyrazenia crona -> zbior wartosci. Obsluguje `*`, `a-b`,
    `/krok`, listy po przecinku i nazwy dni/miesiecy - tyle, ile uzywa chart."""
    pole = re.sub(r"[A-Za-z]{3}",
                  lambda m: str(_NAZWY_CRONA.get(m.group(0).upper(), m.group(0))), pole)
    wynik: set[int] = set()
    for czesc in pole.split(","):
        zakres, _, krok = czesc.partition("/")
        if zakres == "*":
            a, b = od, do
        elif "-" in zakres:
            a, b = (int(x) for x in zakres.split("-", 1))
        else:
            a = b = int(zakres)
            if krok:
                b = do
        wynik.update(range(a, b + 1, int(krok) if krok else 1))
    return wynik


def najdluzsza_przerwa_harmonogramu(cron: str) -> timedelta | None:
    """Najdluzsza przerwa miedzy kolejnymi uruchomieniami wedlug wyrazenia
    crona (5 pol). CZYSTA FUNKCJA. `None` dla wyrazenia, ktorego nie umiemy
    przeczytac albo ktore w ciagu 8 tygodni nie odpala sie dwa razy (np.
    zawieszone `0 0 31 2 *`) - wtedy zostaje sama mediana z danych.

    Liczone przejsciem minuta po minucie przez 8 tygodni od stalej daty: dosc
    na harmonogram tygodniowy i bez zaleznosci od strefy (przesuniecie czasu
    zmienia przerwe o godzine, a zapas ciszy liczy sie w dobach)."""
    try:
        mi, go, dm, mc, dt = cron.split()
        minuty, godziny = _pole_crona(mi, 0, 59), _pole_crona(go, 0, 23)
        dni_mies, miesiace = _pole_crona(dm, 1, 31), _pole_crona(mc, 1, 12)
        dni_tyg = {d % 7 for d in _pole_crona(dt, 0, 7)}
    except ValueError:
        return None
    # Semantyka crona: przy obu polach dnia ograniczonych wystarczy jedno.
    oba = dm != "*" and dt != "*"
    t = datetime(2026, 1, 5, tzinfo=timezone.utc)
    koniec = t + timedelta(weeks=8)
    poprzedni, przerwa = None, timedelta(0)
    while t < koniec:
        if t.hour in godziny and t.minute in minuty and t.month in miesiace:
            w_mies, w_tyg = t.day in dni_mies, (t.isoweekday() % 7) in dni_tyg
            if (w_mies or w_tyg) if oba else (w_mies and w_tyg):
                if poprzedni is not None:
                    przerwa = max(przerwa, t - poprzedni)
                poprzedni = t
            t += timedelta(minutes=1)
        elif t.hour in godziny:
            t += timedelta(minutes=1)
        else:
            t = t.replace(minute=0) + timedelta(hours=1)
    return przerwa or None


def ocen_zadania(historia: dict[str, list[dict]], teraz: datetime,
                 dziennik_od: datetime | None,
                 harmonogramy: dict[str, str] | None = None
                 ) -> list[tuple[str, str]]:
    """Ktore zadania zawiodly albo zamilkly. CZYSTA FUNKCJA.

    `historia`: nazwa -> przebiegi od najnowszego (`id`, `finished_at`,
    `exit_code`, `pod` - patrz `przebieg_planowy`). `dziennik_od`: najstarszy wiersz `risk.job_run` w ogole.
    Zwraca (klucz, opis); klucz niesie id przebiegu, wiec nowy nieudany
    przebieg jest nowym znaleziskiem, a ten sam - przypomnieniem.
    """
    braki = []
    for nazwa, przebiegi in sorted(historia.items()):
        if not przebiegi:
            if dziennik_od is not None and \
                    teraz - dziennik_od > timedelta(days=BRAK_WIERSZY_PO_DNIACH):
                braki.append((f"{nazwa}:brak",
                              f"{nazwa}: ani jednego zapisanego przebiegu od "
                              f"{dziennik_od:%Y-%m-%d}"))
            continue
        ostatni = przebiegi[0]
        if ostatni["exit_code"] != 0:
            braki.append((f"{nazwa}:{ostatni['id']}",
                          f"{nazwa}: ostatni przebieg "
                          f"{ostatni['finished_at']:%m-%d %H:%M} zakończony "
                          f"kodem {ostatni['exit_code']}"))
            continue
        # Ostatni przeszedl, ale byl reczny: rozstrzyga ostatni PLANOWY.
        planowy = next((p for p in przebiegi if przebieg_planowy(p)), None)
        if planowy is not None and planowy["exit_code"] != 0 \
                and not przebieg_planowy(ostatni):
            braki.append((f"{nazwa}:planowy:{planowy['id']}",
                          f"{nazwa}: ostatni PLANOWY przebieg "
                          f"{planowy['finished_at']:%m-%d %H:%M} zakończony kodem "
                          f"{planowy['exit_code']}. Ręczne powtórzenie "
                          f"{ostatni['pod']} ({ostatni['finished_at']:%m-%d %H:%M}) "
                          f"przeszło, ale harmonogram nadal zawodzi — następny "
                          f"planowy przebieg może paść z tego samego powodu"))
            continue
        z_planu = najdluzsza_przerwa_harmonogramu((harmonogramy or {}).get(nazwa, ""))
        if len(przebiegi) >= MIN_PRZEBIEGOW_ODSTEPU:
            konce = [p["finished_at"] for p in przebiegi]
            odstepy = sorted((a - b).total_seconds() for a, b in zip(konce, konce[1:]))
            typowy = odstepy[len(odstepy) // 2]
        elif z_planu is None:
            continue
        else:
            typowy = 0.0
        zrodlo = "typowy odstęp"
        prog = CISZA_KROTNOSC * typowy + CISZA_ZAPAS_MIN * 60
        if z_planu is not None and z_planu.total_seconds() >= typowy:
            typowy, zrodlo = z_planu.total_seconds(), "odstęp z harmonogramu"
            prog = typowy + min(max(CZESC_ZAPASU * typowy, CISZA_ZAPAS_MIN * 60),
                                ZAPAS_HARMONOGRAMU_MAX_H * 3600)
        cisza = (teraz - ostatni["finished_at"]).total_seconds()
        if cisza > prog:
            braki.append((f"{nazwa}:cisza:{ostatni['id']}",
                          f"{nazwa}: ostatni przebieg {cisza / 3600:.1f} h temu, "
                          f"{zrodlo} {typowy / 3600:.1f} h"))
    return braki


async def sprawdz_zadania(fetch_all, notify, sucho: bool = False, *,
                          zadania: list[str],
                          harmonogramy: dict[str, str] | None = None,
                          teraz: datetime | None = None) -> int:
    """Czy kazde wlaczone zadanie cykliczne skonczylo sie powodzeniem
    i czy w ogole chodzi."""
    teraz = teraz or datetime.now(timezone.utc)
    if not zadania:
        print("zadania: pusta lista zadań do sprawdzenia — chart nie podał "
              "żadnego, czujka nie ma czego oceniać")
        return 0
    wiersze = await fetch_all(
        "SELECT id, nazwa, finished_at, exit_code, pod FROM ("
        "  SELECT j.*, row_number() OVER (PARTITION BY nazwa"
        "                                 ORDER BY finished_at DESC) AS nr"
        "    FROM risk.job_run j WHERE nazwa = ANY(%s)) x"
        " WHERE nr <= %s ORDER BY nazwa, finished_at DESC",
        (list(zadania), PRZEBIEGOW_WSTECZ))
    od = await fetch_all("SELECT min(finished_at) AS od FROM risk.job_run")
    historia: dict[str, list[dict]] = {z: [] for z in zadania}
    for w in wiersze:
        historia[w["nazwa"]].append(w)
    for nazwa, przebiegi in sorted(historia.items()):
        if przebiegi:
            p = przebiegi[0]
            print(f"zadanie {nazwa}: ostatni przebieg {p['finished_at']:%m-%d %H:%M}"
                  f" kod {p['exit_code']} ({len(przebiegi)} w historii)")
        else:
            print(f"zadanie {nazwa}: brak zapisanych przebiegów")

    braki = ocen_zadania(historia, teraz, od[0]["od"] if od else None,
                         harmonogramy)
    if not braki:
        print("  każde zadanie skończyło się powodzeniem i chodzi w swoim rytmie")
        return 0
    for _, opis in braki:
        print(f"  ZADANIE: {opis}")
    if sucho:
        print("  Tryb suchy — powiadomienie nie wysylane.")
        return len(braki)

    nowe, trwale = 0, 0
    odbiorcy = None
    for klucz, opis in braki:
        # Jedno zgloszenie = jedna minuta (wszyscy odbiorcy w tym samym przebiegu).
        poprzednie = await fetch_all(
            "SELECT count(DISTINCT date_trunc('minute', created_at)) AS n,"
            "       max(created_at) AS ostatnie FROM notify.notification"
            " WHERE event_code IN ('job.nieudany', 'notify.alarm_nieprzyjety')"
            "   AND data->>'klucz' = %s", (klucz,))
        n = (poprzednie[0]["n"] or 0) if poprzednie else 0
        ostatnie = poprzednie[0]["ostatnie"] if poprzednie else None
        if n and ostatnie is not None and teraz - ostatnie < odstep_powtorki_zadania(n):
            nastepne = ostatnie + odstep_powtorki_zadania(n)
            print(f"  {klucz}: zgłoszone {n}×, ostatnio {ostatnie:%m-%d %H:%M} UTC; "
                  f"następne przypomnienie najwcześniej {nastepne:%m-%d %H:%M} UTC")
            trwale += 1
            continue
        if odbiorcy is None:
            odbiorcy = await fetch_all("SELECT id FROM app.\"user\" WHERE is_active AND role = 'owner'")
        nazwa = klucz.split(":", 1)[0]
        wyslane = 0
        for u in odbiorcy:
            if await notify(u["id"], "job.nieudany", f"Zadanie {nazwa} zawiodło",
                            f"{opis}.\n\nLog: `kubectl -n default get jobs | grep "
                            f"{nazwa}` i `kubectl logs job/<nazwa>`. Wiersze: "
                            f"`risk.job_run`.",
                            severity="warning", data={"klucz": klucz, "zadanie": nazwa}):
                wyslane += 1
        if not wyslane and odbiorcy:
            # `notify()` NIKOMU NIE ZAPISAL `job.nieudany` (2026-10-05: preferencja
            # wylaczona albo jej cooldown - jedyne drogi do `None`). Bez wiersza
            # nastepny przebieg nie wie, ze probowal: kod 1 co 10 minut, a do
            # czlowieka nie dochodzi nic. Druga droga, `always_on` (migracja
            # 0094), mowi wprost, ze to blad kanalu - i zostawia wiersz z tym
            # samym kluczem, wiec powtorki licza sie dalej tym samym rytmem.
            print(f"  BŁĄD KANAŁU: żaden z {len(odbiorcy)} odbiorców nie przyjął "
                  f"`job.nieudany` dla {klucz} — wysyłam `notify.alarm_nieprzyjety`")
            for u in odbiorcy:
                if await notify(u["id"], "notify.alarm_nieprzyjety",
                                f"Alarm o zadaniu {nazwa} nie został przyjęty",
                                f"{opis}.\n\nPowiadomienie `job.nieudany` nie zostało "
                                f"zapisane dla żadnego odbiorcy (wyłączona preferencja "
                                f"albo jej cooldown). Sprawdź preferencje powiadomień "
                                f"„Zadanie cykliczne nie powiodło się”.",
                                severity="warning",
                                data={"klucz": klucz, "zadanie": nazwa}):
                    wyslane += 1
        if wyslane:
            nowe += 1
        else:
            # Brak aktywnych odbiorcow (albo i druga droga nic nie zapisala) -
            # nie ma komu powiedziec, a kod 1 co 10 minut niczego nie zmieni.
            # Stan zostaje w logu przy kazdym przebiegu.
            print(f"  BŁĄD KANAŁU: {klucz} — nie zapisano żadnego powiadomienia "
                  f"(aktywnych odbiorców: {len(odbiorcy)})")
            trwale += 1
    return kod_wyjscia(nowe, trwale, "zadanie cykliczne zawiodło")


async def sprawdz_dryf(fetch_all, notify, sucho: bool = False, *,
                       engine: str = "crypto", bar: str = "4h",
                       teraz: datetime | None = None) -> int:
    """Czy rozklad cech odjechal od treningowego I TAK ZOSTAL.

    DLACZEGO TO JEST OSOBNA CZUJKA, A NIE KOLEJNY POWOD W `zmiany`

    `zmiany` odpowiada na pytanie "czy cos sie zmienilo od wczoraj" i to jest
    dobra regula: powiadomienie, ktore przychodzi zawsze, przestaje cokolwiek
    znaczyc. Ale stan trwale zly NIE JEST zmiana. Dryf, ktory wszedl wysoko
    i tam stoi, nie rosnie, wiec warunek z `zmiany` go nie przepuszcza - od
    drugiego przebiegu w gore panuje cisza nieodrozialna od spokoju.

    DLACZEGO ZDARZENIE `learning.drift_high`, A NIE `learning.state_changed`

    Typ `learning.drift_high` istnieje w katalogu od migracji 0011, jest
    wlaczony domyslnie dla wszystkich odbiorcow - i NIE BYL WYSYLANY ANI RAZU,
    bo zadna linia kodu go nie uzywala. Doklejenie dryfu do "zmiany stanu
    uczenia" mialoby ten sam skutek co przy kopiach zapasowych: alarm
    o niewlasciwej nazwie uczy czytac alarmy nieuwaznie, a preferencja
    odbiorcy dotyczaca dryfu nadal nie mialaby czego wlaczac.

    RYTM PRZYPOMINANIA JEST TU, A NIE W COOLDOWNIE ZDARZENIA

    Katalogowy cooldown to 86400 s, czyli dokladnie odstep miedzy przebiegami
    trenera - przepuscilby wiec kazdy. Wlasciwe tlumienie liczy sie z historii
    powiadomien (ten sam chwyt co przy `engine.down`: bez dodatkowej kolumny),
    per (engine, bar), i jest ostrzejsze niz katalogowe - wiec sie z nim nie
    bije.
    """
    historia = await fetch_all(
        "SELECT max_psi, run_at FROM learn.learning_state_log"
        " WHERE engine = %s AND bar = %s ORDER BY run_at DESC LIMIT %s",
        (engine, bar, DRYF_TRWALY_PRZEBIEGOW))
    if len(historia) < DRYF_TRWALY_PRZEBIEGOW:
        print(f"dziennik stanu ma {len(historia)} z {DRYF_TRWALY_PRZEBIEGOW}"
              f" potrzebnych przebiegow dla {engine}/{bar} — za wcześnie na werdykt")
        return 0

    # PER (engine, bar), bo rytm przypominania o swiecy 4h nie moze uciszac
    # swiecy 1h ani odwrotnie. `bar` siedzi w `data`, bo tabela powiadomien nie
    # ma na niego kolumny - i nie potrzebuje jej mieć dla jednego zdarzenia.
    poprzednie = await fetch_all(
        "SELECT created_at, data->>'dryf' AS dryf FROM notify.notification"
        " WHERE event_code = 'learning.drift_high' AND engine::text = %s"
        " AND data->>'bar' = %s ORDER BY created_at DESC LIMIT 1",
        (engine, bar))
    ostatnie = poprzednie[0]["created_at"] if poprzednie else None
    zgloszony = None
    if poprzednie and poprzednie[0]["dryf"] is not None:
        zgloszony = float(poprzednie[0]["dryf"])

    werdykt = ocen_dryf_trwaly(historia, ostatnie_zgloszenie=ostatnie,
                               zgloszony_dryf=zgloszony, teraz=teraz)

    biezacy = historia[0].get("max_psi") or 0.0
    print(f"Dryf {engine}/{bar}: {biezacy:.2f} "
          f"(próg trwałości {DRYF_TRWALY_PROG}, próg wzrostu {PSI_PROG})")
    if werdykt is None:
        # Cisza jest wynikiem, wiec musi byc widoczna w logu razem z powodem.
        # Inaczej nie da sie odroznic czujki, ktora nic nie znalazla, od czujki,
        # ktora padla - a to jest dokladnie ten blad, ktory ta czujka naprawia.
        if biezacy <= DRYF_TRWALY_PROG:
            print("  poniżej progu trwałości — cisza")
        elif ostatnie is not None:
            print(f"  zgłoszone {ostatnie:%Y-%m-%d %H:%M} UTC, przypomnienie "
                  f"co {DRYF_PRZYPOMNIENIE_DNI} dni — cisza")
        else:
            print("  stan jeszcze nie utrwalony (za mało przebiegów lub za "
                  "krótki odstęp między nimi) — cisza")
        return 0

    print(f"  • {werdykt['powod']}")

    # KTORE CECHY, a nie tylko ze maksimum wynosi tyle a tyle.
    #
    # UWAGA NA ZAKRES: `learn.drift_metric` jest kluczowana po model_version,
    # a nie po horyzoncie, a trener liczy dryf z `feat.feature_value` bez filtru
    # po swiecy. Ta rozpiska mowi wiec, ktore CECHY odjechaly - nie ktory
    # horyzont. To i tak jest jedyna dostepna odpowiedz na pytanie "co
    # naprawiac", a udawanie precyzji per-horyzont byloby gorsze niz jej brak.
    cechy = await fetch_all(
        "SELECT fd.name || '@' || fd.version AS feature, d.psi"
        " FROM learn.drift_metric d"
        " JOIN feat.feature_def fd ON fd.id = d.feature_def_id"
        " JOIN learn.model_version mv ON mv.id = d.model_version_id"
        " JOIN learn.model m ON m.id = mv.model_id"
        " WHERE m.engine = %s"
        "   AND d.event_time = (SELECT max(event_time) FROM learn.drift_metric"
        "                       WHERE model_version_id = d.model_version_id)"
        " ORDER BY d.psi DESC NULLS LAST LIMIT %s",
        (engine, DRYF_CECH_W_TRESCI))
    rozpiska = podsumuj_dryf_cech(cechy)
    for c in rozpiska:
        print(f"      {c}")

    if sucho:
        print("Tryb suchy — powiadomienie nie wysyłane.")
        return 1

    odbiorcy = await fetch_all("SELECT id FROM app.\"user\" WHERE is_active AND role = 'owner'")
    if not odbiorcy:
        print("Brak odbiorców — powiadomienie nie ma do kogo pójść.")
        return 1

    tytul = f"Dryf cech utrzymuje się ({engine}/{bar})"
    # MIARA NAZWANA PO IMIENIU. Odbiorca, ktory porowna te liczbe z progiem
    # 0,25 obiecanym w katalogu i na dashboardzie, wyciagnie zly wniosek -
    # to nie jest PSI i nie ma tej skali.
    tresc = (f"{werdykt['powod']}.\n\n"
             f"Miara to standaryzowane przesunięcie rozkładu cechy "
             f"(przesunięcie średniej w jednostkach historycznego odchylenia "
             f"plus zmiana rozrzutu), NIE PSI — próg {DRYF_TRWALY_PROG} to "
             f"„efekt średni”, {PSI_PROG} to „duży”.\n\n"
             + ("Cechy, które odjechały najbardziej:\n"
                + "\n".join(f"• {c}" for c in rozpiska) + "\n\n"
                if rozpiska else "")
             + "Handel NIE jest wstrzymany — to czujka, nie blokada.")
    for u in odbiorcy:
        await notify(u["id"], "learning.drift_high", tytul, tresc,
                     engine=engine,
                     data={"bar": bar, "dryf": round(werdykt["dryf"], 4),
                           "pasmo": werdykt["pasmo"],
                           "przebiegow": werdykt["przebiegow"],
                           "eskalacja": werdykt["eskalacja"],
                           "cechy": rozpiska})
    print(f"Powiadomiono odbiorców: {len(odbiorcy)}")

    # KOD NIEZEROWY TYLKO WTEDY, GDY COS WYSLANO - czyli najwyzej raz na
    # DRYF_PRZYPOMNIENIE_DNI. Ten sam kompromis co przy kill-switchu: stan znany
    # i czekajacy na reakcje nie moze malowac `kubectl get jobs` na czerwono co
    # noc, bo wtedy czerwony przestaje cokolwiek znaczyc. Ale przy samym
    # zgloszeniu drugi, niezalezny od kanalu powiadomien slad jest wart tyle
    # samo co przy martwym silniku.
    return 1


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--co", default="stan-nauki",
                    choices=("stan-nauki", "zywotnosc", "ryzyko", "kopie",
                             "swiezosc", "egzekucja", "poslizg", "instrumenty",
                             "workery", "dryf", "kanaly", "odlozone",
                             "decyzje", "zadania"),
                    help="stan-nauki: czy wynik treningu sie zmienil; "
                         "dryf: czy rozklad cech odjechal od treningowego I TAK "
                         "ZOSTAL - stan trwaly, ktorego 'stan-nauki' z definicji "
                         "nie widzi, bo tam zdarzeniem jest ZMIANA; "
                         "zywotnosc: czy silniki bija; "
                         "ryzyko: czy kill-switch zadzialal i CZY NADAL JEST "
                         "AKTYWNY - stan jest wypisywany w logu przy kazdym "
                         "przebiegu, a kod wyjscia jest niezerowy w tych "
                         "przebiegach, w ktorych poszlo powiadomienie; "
                         "kopie: czy jest swieza kopia zapasowa; "
                         "swiezosc: czy dane, na ktorych silnik decyduje, "
                         "nie sa przestarzale; "
                         "egzekucja: czy zlecenia dochodza do gieldy; "
                         "poslizg: ile NAPRAWDE kosztuje egzekucja - raport, "
                         "nie czujka; "
                         "instrumenty: czy uniwersum nadal istnieje u gieldy "
                         "(wycofanie, zawieszenie, nieaktualny odczyt statusu); "
                         "workery: czy procesy zbierajace dane domykaja cykle - "
                         "prog liczony z INTERWALU kazdego z nich; "
                         "decyzje: czy kazda swieca ma decyzje dla kazdego "
                         "instrumentu uniwersum, gdy cechy przyszly; "
                         "zadania: czy wlaczone CronJoby koncza sie "
                         "powodzeniem i chodza w swoim rytmie (risk.job_run)")
    ap.add_argument("--zadania", default="",
                    help="lista nazw zadan dla --co zadania, po przecinku; "
                         "podaje ja chart z flag `enabled`")
    ap.add_argument("--harmonogramy", default="",
                    help="dla --co zadania: `nazwa=<cron>` rozdzielone `;` - "
                         "`schedule` CronJobow z values.yaml; oczekiwany odstep "
                         "nie spada ponizej najdluzszej przerwy w harmonogramie")
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
                    help="okno w godzinach dla --co poslizg i --co odlozone; dla poslizgu domyslnie od poczatku, "
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
                         "powiadomienie - poza 3 (tor niegotowy), ktory daje "
                         "ostrzezenie dopiero po TOR_NIEGOTOWY_DNI dniach bez "
                         "udanego przebiegu, z tygodniowym rytmem")
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
        if args.co == "workery":
            return await sprawdz_puls_workerow(fetch_all, notify, sucho=args.sucho,
                                               prog_min=args.prog_min)
        if args.co == "ryzyko":
            return await sprawdz_ryzyko(fetch_all, notify, sucho=args.sucho)
        if args.co == "kopie":
            return await sprawdz_kopie(fetch_all, notify, sucho=args.sucho)
        if args.co == "kanaly":
            return await sprawdz_kanaly(fetch_all, notify, sucho=args.sucho)
        if args.co == "swiezosc":
            return await sprawdz_swiezosc(fetch_all, notify, sucho=args.sucho,
                                          engine=args.engine,
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
        if args.co == "dryf":
            return await sprawdz_dryf(fetch_all, notify, sucho=args.sucho,
                                      engine=args.engine, bar=args.bar)
        if args.co == "zadania":
            return await sprawdz_zadania(
                fetch_all, notify, sucho=args.sucho,
                zadania=[z for z in args.zadania.split(",") if z],
                harmonogramy=dict(h.split("=", 1) for h in
                                  args.harmonogramy.split(";") if "=" in h))
        if args.co == "decyzje":
            return await sprawdz_decyzje(fetch_all, notify, sucho=args.sucho,
                                         engine=args.engine, mode=args.mode,
                                         bar=args.bar)
        if args.co == "odlozone":
            # `--okno-h` ma domyslnie None (dla `--co poslizg` znaczy to "od
            # poczatku"), a tutaj None wywrocilo by zapytanie. Domyslne 48 h to
            # dwanascie swiec czterogodzinnych - dosc, zeby stan trwajacy dobe
            # byl widoczny, i za malo, zeby wracac do awarii sprzed tygodnia,
            # o ktorej ktos juz wie.
            return await sprawdz_odlozone_decyzje(
                fetch_all, notify, sucho=args.sucho, engine=args.engine,
                mode=args.mode, okno_h=args.okno_h or 48)

        if args.kod_trenera == KOD_TOR_NIEGOTOWY:
            # PRZED odczytem dziennika: tor, ktory nigdy nie przeszedl, nie ma
            # wierszy, a galaz nizej milczalaby wtedy „brak zapisanych
            # przebiegow". I nie jako „trener zakonczyl sie bledem" - kod 3 to
            # odmowa z powodu, nie awaria.
            return await sprawdz_tor_niegotowy(fetch_all, notify, sucho=args.sucho,
                                               engine=args.engine, bar=args.bar)

        # ZMIANA STRUKTURY MODELU (T8) - niezaleznie od tego, czy stan nauki
        # sie zmienil: nowa struktura moze przyjsc przy tym samym statusie.
        if not args.kod_trenera:
            await sprawdz_zmiane_struktury(fetch_all, notify, sucho=args.sucho,
                                           engine=args.engine, bar=args.bar)
        wiersze = await fetch_all(SQL_STAN_NAUKI,
                                  (args.engine, args.bar, STAN_NAUKI_WSTECZ))
        if not wiersze:
            print(f"brak zapisanych przebiegow dla {args.engine}/{args.bar}"
                  " — dozor nie ma czego porownac")
            return 0

        biezacy = wiersze[0]
        # POPRZEDNIK TA SAMA MIARA, nie po prostu drugi wiersz - patrz
        # komentarz przy `MIARA_PO_KARCIE`.
        powody, pominiete = ocen_stan_nauki(wiersze)
        if pominiete:
            print(f"pominięto {pominiete} przebieg(ów) liczonych inną miarą niż "
                  f"bieżący ({miara_przebiegu(biezacy)}) — dla odsetka, przedziału "
                  f"i znaku mediany; status, okna i dryf porównane z poprzednim wierszem")
        bezposredni = wiersze[1] if len(wiersze) > 1 else None
        if bezposredni is not None and (bezposredni.get("doplata_pb")
                                        != biezacy.get("doplata_pb")):
            # Codzienny pomiar spreadu zmienia doplate miedzy przebiegami. Zmiana
            # znaku mediany przy tej samej klasie miary moze wiec wynikac
            # z KOSZTU, nie z rynku - liczba w logu pozwala to rozroznic.
            def _pb(w):
                v = w.get("doplata_pb")
                return "brak" if v is None else f"{v:.2f} pb"
            print(f"dopłata z karty: {_pb(bezposredni)} → {_pb(biezacy)}")

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

        odbiorcy = await fetch_all("SELECT id FROM app.\"user\" WHERE is_active AND role = 'owner'")
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
