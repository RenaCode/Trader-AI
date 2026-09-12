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
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Prog i komplet limitow koniecznych czytamy z risk kernela, a nie przepisujemy
# tutaj: druga kopia rozjechalaby sie z pierwsza przy pierwszej zmianie rytmu
# pomiaru, a czujka pilnowalaby wtedy progu, ktorego nikt juz nie stosuje.
# Uzywa ich `sprawdz_cisze_pomiaru` nizej. Import musi byc PO sys.path.insert,
# bo ten plik bywa uruchamiany jako skrypt.
from services.engine.risk import LIMITY_KONIECZNE, POMIAR_CO_S  # noqa: E402


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
    komentarz w `sprawdz_instrumenty`.
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

    # WARUNEK WZROSTU ZOSTAJE - ale dopiero teraz jest bezpieczny.
    #
    # `pb > pp` jest tu poprawny dla czujki ZMIANY: dryf, ktory opada, sam sie
    # cofa i nie wymaga uwagi. Dopoki jednak byl to JEDYNY tor zgloszenia dryfu,
    # ten sam warunek uciszal stan trwale zly - bo stan, ktory stoi, nie rosnie.
    # Za "nadal jest zle" odpowiada `ocen_dryf_trwaly`; tutaj zostaje wylacznie
    # "wlasnie sie pogorszylo".
    pp, pb = poprzedni.get("max_psi") or 0.0, biezacy.get("max_psi") or 0.0
    if pb > PSI_PROG and pb > pp:
        powody.append(f"dryf cech {pb:.2f} powyzej progu {PSI_PROG} i rosnie")

    return powody


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

    if not martwe:
        return 0

    if sucho:
        print("Tryb suchy — powiadomienie nie wysyłane.")
        return 1

    odbiorcy = await fetch_all('SELECT id FROM app."user" WHERE is_active')
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
            odbiorcy = await fetch_all('SELECT id FROM app."user" WHERE is_active')
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
    zdarzenia = await fetch_all(
        "SELECT k.id, k.engine::text AS engine, k.mode::text AS mode, k.reason,"
        "       k.triggered_at,"
        "       round(extract(epoch from (now() - k.triggered_at))/60) AS wiek_min"
        " FROM risk.killswitch_event k"
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
    trwale = 0

    if not zdarzenia:
        print("kill-switch: brak aktywnych zdarzeń")
        return zgloszone

    odbiorcy = await fetch_all('SELECT id FROM app."user" WHERE is_active')
    for z in zdarzenia:
        wiek_min = float(z["wiek_min"])
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
        ostatnie = await fetch_all(
            "SELECT round(extract(epoch from (now() - max(created_at)))/60)"
            "         AS od_zgloszenia_min"
            " FROM notify.notification"
            " WHERE event_code = 'killswitch.triggered'"
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
            trwale += 1
            print(f"    (zgłoszone {od_min:.0f} min temu, przypomnienie co "
                  f"{odstep_przypomnienia_ks(wiek_min)} h — cisza w kanale "
                  f"powiadomień; handel STOI, stan opisany w linii wyżej)")
            continue

        zgloszone += 1
        if sucho:
            print(f"    {werdykt['powod']} — tryb suchy, powiadomienie nie wysyłane.")
            continue

        godzin = wiek_min / 60
        if werdykt["pierwsze"]:
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
        tresc = (f"{wstep}{z['reason']}\n\nSilnik NIE WEJDZIE w żadną pozycję do "
                 f"czasu ręcznego zwolnienia — kill-switch nie wygasa sam, bo "
                 f"automatyczne wznowienie po obsunięciu to najprostsza droga do "
                 f"powtórzenia tej samej straty.\n\nZwolnienie: zakładka Ryzyko "
                 f"w interfejsie.")
        for u in odbiorcy:
            await notify(u["id"], "killswitch.triggered", tytul, tresc,
                         engine=z["engine"], severity="critical",
                         data={"reason": z["reason"], "event_id": z["id"],
                               "wiek_min": wiek_min,
                               "przypomnienie": not werdykt["pierwsze"],
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
    zgloszone = trwale = 0
    for kod, tytul, tresc in powody:
        print(f"  {tytul}")
        if sucho:
            zgloszone += 1
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
    # NIE JEST `always_on`: ma katalogowy cooldown 3600 s, wiec z 144 wywolan
    # `notify()` na dobe realnie wychodzi 24, a pozostale sa po cichu odrzucane
    # w warstwie powiadomien. Kod wyjscia liczy tu wiec PROBY, a nie wyslania,
    # i przy trwale zatrzymanym `services/ingest/instrumenty.py` zostaje
    # niezerowy przy kazdym przebiegu.
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
        if pracuje:
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

    odbiorcy = await fetch_all('SELECT id FROM app."user" WHERE is_active')
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

    odbiorcy = await fetch_all('SELECT id FROM app."user" WHERE is_active')
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
                             "workery", "dryf"),
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
                         "prog liczony z INTERWALU kazdego z nich")
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
        if args.co == "workery":
            return await sprawdz_puls_workerow(fetch_all, notify, sucho=args.sucho,
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
        if args.co == "dryf":
            return await sprawdz_dryf(fetch_all, notify, sucho=args.sucho,
                                      engine=args.engine, bar=args.bar)

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
