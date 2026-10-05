#!/usr/bin/env python3
"""Petla decyzyjna silnika.

Jeden przebieg = jedna decyzja per instrument:
    odczyt cech point-in-time -> rezim -> sygnaly -> risk kernel -> zapis -> egzekucja

Cechy czytamy WYLACZNIE przez feat.as_of(). To nie jest wygoda, tylko jedyna
gwarancja, ze silnik na zywo i trening widza dokladnie to samo - osobna sciezka
odczytu "na szybko" po stronie live to definicja training/serving skew.

Egzekucja ma dwa tryby:
  internal_paper - symulacja po realnych cenach rynkowych (gdy brak kluczy)
  venue_demo     - zlecenia na koncie demo gieldy (gdy klucze sa wpiete)
Oba operuja na PRAWDZIWYCH cenach; roznica jest w tym, kto liczy wypelnienie.

Uzycie:
    DATABASE_URL=... python services/engine/loop.py --once
    DATABASE_URL=... python services/engine/loop.py --interval 300
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg
from psycopg.rows import dict_row

from services.engine.horizons import DEFAULT_BAR, HORIZONS, Horizon, get as get_horizon
from services.engine import stan_kapitalu, strategia_momentum
from services.engine.risk import RiskKernel
from services.engine.sesja import StanSesji, stan_sesji
from services.engine.zatrzymanie import Zatrzymanie
from services.ingest.features import wklady_newsow
from services.ingest import puls
from services.engine.uniwersum import (odcisk, opis_wykluczen,
                                       sql_uniwersum_decyzyjnego,
                                       srednie_spready, za_szeroki_spread)
from services.engine.koszty import POSLIZG_MAKER, POSLIZG_TAKER, koszty
from services.engine.portfolio import _wypelnione
from services.engine.risk_rules import (
    MIN_CONFIDENCE, START_EQUITY as START_EQUITY_WSPOLNY, RiskVerdict,
    position_budget, wielkosc_pozycji,
)
from services.engine.zlecenia import Filtry, do_kroku_w_dol
from services.engine.bramki import (
    PROG_WEJSCIA_POCZATKOWY, PROG_WYJSCIA_POCZATKOWY, bramki_z_modelu,
)
from services.engine.signals import (SIGNALS, classify_regime, evaluate,
                                     pokrycie_wag)

# Progi awaryjne - obowiazuja WYLACZNIE, dopoki nie ma aktywnej wersji modelu.
# Docelowo pochodza z rejestru (learn.model_version.params), gdzie zapisuje je
# trener, wyliczone z rozkladu wynikow na oknach treningowych. Trzymanie ich
# w dwoch miejscach bylo pewna droga do rozjazdu trening-produkcja.
# Same liczby stoja w `services/engine/bramki.py` razem z polityka progow -
# tutaj byly by druga kopia, a dwie kopie tej samej liczby sie rozjezdzaja.
ENTER_THRESHOLD = PROG_WEJSCIA_POCZATKOWY
EXIT_THRESHOLD = PROG_WYJSCIA_POCZATKOWY
# MIN_CONFIDENCE i START_EQUITY: patrz risk_rules. Byly tu zdefiniowane
# po raz drugi, obok tych samych wartosci w symulacji i w trenerze.

# PROWIZJA I POSLIZG POCHODZA Z services/engine/koszty.py, NIE Z TEGO PLIKU.
#
# Do 2026-09-06 stala byla tutaj: FEE_RATE = 0,00055 z komentarzem "prowizja
# taker Bybit spot". To bylo 0,055%, czyli stawka taker KONTRAKTOW WIECZYSTYCH -
# dokladnie ten sam blad, ktory docs/03 opisal i naprawil 2026-08-31 w symulacji.
# Poprawka poszla wtedy do koszty.py i portfolio.py, a petla decyzyjna - czyli
# to, co faktycznie prowadzi rachunek papierowy - zostala ze stara stala.
#
# Rozmiar rozjazdu: petla liczyla rundzie 0,190%, trener modelowal 0,200% przy
# zleceniach z limitem, a przy rynkowych na podmiocie unijnym runda wynosi
# 0,580%. Krzywa kapitalu mierzyla wiec strategie TANSZA TRZYKROTNIE od tej,
# ktora dalo by sie wykonac.
#
# Model egzekucji (rodzaj instrumentu + limit/rynek) przychodzi z wiersza
# polecen, tak samo jak w trenerze, i jest ustawiany z jednego miejsca w charcie.
#
# HISTORII SPRZED POPRAWKI NIE PRZELICZAMY - I TO JEST DECYZJA, NIE ZANIEDBANIE
#
# Zmierzone na produkcji 2026-09-09. Momentu przelaczenia stawki nie trzeba
# zgadywac, widac go w danych co do cyfry:
#
#     34 nogi po 0,055% (obrot 27 435,79)  ->  15,089687
#      2 nogi po 0,100% (obrot  2 345,62)  ->   2,345620   (wyjscia poz. 17 i 18)
#                                              17,435307  = sum(fees_paid)
#     gdyby 0,100% na calym obrocie:           29,781414
#     NIEDOBOR:                                12,346107
#
# Kapital jest wiec zawyzony o 12,35 USDC, a zysk +63,09 to naprawde +50,75
# - zawyzenie o 19,6%. TO JEST PRAWDZIWE i nie znika przez to, ze zapisu nie
# ruszamy.
#
# DLACZEGO MIMO TO NIE PRZEPISUJEMY WSTECZ
#
# 1. Bezpiecznik NIE dziala na zawyzonej liczbie - i to jest pomiar, nie
#    domysl. Wszystkie 34 tanie nogi byly zaksiegowane PRZED szczytem jednostki
#    (szczyt 2026-09-07 11:49; poz. 17 i 18 otwarte 2026-09-06, wiec ich
#    prowizje wejscia juz w nim siedzialy). Niedobor jest wiec STALYM
#    PRZESUNIECIEM POZIOMU, ktore skraca sie w liczniku `(szczyt - teraz)`
#    i zostaje tylko w mianowniku:
#
#        obsuniecie zapisane  0,0100884789%
#        obsuniecie prawdziwe 0,0100897236%
#        roznica              1,2e-06 punktu procentowego, przy progu 10 pp
#
#    Jest to wlasnosc TEJ historii, a nie prawo: gdyby tanie nogi wypadly PO
#    szczycie, licznik by sie przesunal. Dlatego jest tu liczba, a nie zasada.
#
# 2. Sama poprawka pozycji zostawilaby krzywa gorsza, niz jest teraz.
#    `exec.equity_point` to 3719 zapisanych migawek, a szczyt bierze sie
#    z `max(nav)` po nich (risk.py). Obnizenie `fees_paid` zmienia wylacznie
#    punkt BIEZACY, wiec w krzywej powstalby uskok 12,35 w dol w chwili
#    poprawki - i bezpiecznik przeczytalby go jako prawdziwe obsuniecie
#    z niczego. Zeby tego uniknac, trzeba by przepisac takze wszystkie 3719
#    punktow razem ze szczytem, czyli pamiec bezpiecznika o najgorszym stanie.
#
# 3. I tego przeliczenia NIE DA SIE ZROBIC UCZCIWIE. Migracja 0042 postawila
#    tu granice i ona wlasnie tedy przebiega: wolno odtworzyc wielkosc, ktora
#    jest DOKLADNIE WYLICZALNA z danych, ktore mamy (`nav = equity / units`,
#    `units` stale i znane) - a nie zgadnac. Prowizja tego warunku nie spelnia.
#    Znam stawke, ktora nalezalo policzyc; nie wiem, co zrobilby rachunek,
#    gdyby ja placil. Wyzsza prowizja to nizszy kapital, a `max_position_notional`
#    jest ulamkiem KAPITALU - wiec kazda nastepna pozycja bylaby innej
#    wielkosci i kazdy nastepny wynik inny. Przepisanie samych prowizji daje
#    krzywa, w ktorej pozycje sa wymierzone od jednego kapitalu, a zaksiegowane
#    przy drugim: nie rekonstrukcje, tylko sklejke dwoch roznych przebiegow,
#    ktora WYGLADA na kompletna. Dokladnie ten powod, dla ktorego 0042 zostawia
#    NULL tam, gdzie nie umie policzyc.
#
# 4. Zapis jest prawdziwy jako zapis. `fees_paid` mowi, ile ta petla NALICZYLA,
#    i mowi to zgodnie z prawda. Blad byl w stawce, nie w ksiegowaniu, jest
#    nazwany tutaj i w charts/trader/values.yaml. Nadpisanie zamienia usterke
#    udokumentowana i policzalna w niewidoczna.
#
# CO WOBEC TEGO TRZEBA WIEDZIEC, CZYTAJAC TE LICZBY
#
# Niedobor 12,35 USDC jest STALY - nie rosnie, bo od 2026-09-06 obie nogi
# ida po stawce z koszty.py. Dotyka dwoch wielkosci i tylko dwoch:
#
#   * ZYSKU. +63,09 znaczy +50,75. Zadna regula ryzyka tego nie czyta, czyta
#     to czlowiek - i to jest jedyna liczba, ktora naprawde klamie.
#   * PROGU `min_equity` (95 000, kill-switch). Prog jest kwota, wiec tu
#     przesuniecie sie NIE SKRACA: bufor zapisany 5063,09, prawdziwy 5050,75
#     - zawyzenie o 0,244%, zawsze w strone "za pozno". Dla porownania jedna
#     pozycja to 5% kapitalu, czyli okolo 5000: blad jest o trzy rzedy
#     wielkosci mniejszy niz ziarno, ktorym ten prog operuje.
#
# Gdyby demo mialo trwac przy tej historii dluzej albo gdyby prog byl ciasny,
# wlasciwa droga NIE jest edycja starych wierszy, tylko odciecie: nowy rachunek
# od czystej krzywej. Sklejone przebiegi nie robia sie prawdziwe przez to,
# ze sa dokladniejsze.

START_EQUITY = Decimal(str(START_EQUITY_WSPOLNY))

# Powody bezczynnosci, po jednym na galaz wyboru akcji w `run_once`.
#
# KLUCZE SA WPISANE W SCHEMAT (migracja 0040 ma CHECK na dokladnie ten zbior),
# wiec literowka konczy sie odrzuceniem INSERT-a przez baze, a nie cicho
# zapisanym napisem, ktorego pozniej nikt nie policzy. Opisy sluza wylacznie
# uzasadnieniu czytanemu przez czlowieka - zapytania analityczne maja chodzic
# po kolumnie, nie po tekscie.
OPIS_POWODU_HOLDU = {
    "slaby_wynik": "Nie wchodzimy: |wynik| poniżej progu wejścia.",
    "niska_pewnosc": "Nie wchodzimy: pewność poniżej progu.",
    "short_niewykonalny": "Nie wchodzimy: sygnał na spadek, a na tym "
                          "instrumencie nie da się zająć krótkiej pozycji.",
    "trzyma_pozycje": "Trzymamy otwartą pozycję: sygnał nadal w jej stronę "
                      "i powyżej progu wyjścia.",
    "wstrzymanie_ryzykiem": "Nie wchodzimy: wejścia wstrzymane przez ryzyko.",
    "rezim_bez_progu": "Nie wchodzimy: w tym reżimie próg wejścia NIE ZOSTAŁ "
                       "SKALIBROWANY — próba na oknach treningowych była za "
                       "mała, żeby percentyl cokolwiek znaczył.",
    "szeroki_spread": "Nie wchodzimy: średni spread arkusza z 7 dni przekracza "
                      "próg — runda kosztowałaby więcej niż zmierzona przewaga.",
    "decyzja_przeterminowana": "Nie wchodzimy: od zamknięcia świecy minęło "
                               "ponad 20 minut — sygnał opisuje rynek, którego "
                               "już nie ma (opóźniony potok danych).",
    # Strategia `momentum` (services/engine/strategia_momentum.py, migracja 0091).
    "filtr_trendu": "Nie wchodzimy: BTC pod EMA 50 d — filtr trendu każe "
                    "trzymać gotówkę.",
    "poza_skladem": "Nie wchodzimy: instrument poza trzema najsilniejszymi "
                    "po momentum 30 d.",
    "poza_decyzja_dobowa": "Nie wchodzimy: instrument jest w składzie, ale "
                           "wejście zapada tylko na zamknięciu doby UTC.",
}

# STRATEGIA -> wartosc `journal.decision.strategia` (CHECK w migracji 0091).
# `model` to ensemble z aktywnej wersji modelu (albo zalozenia poczatkowe),
# `momentum` - zamrozona regula R4 na demo (`strategia_momentum`).
STRATEGIE = {"model": "ensemble", "momentum": strategia_momentum.NAZWA}


def ensure_signal_defs(cur, engine: str) -> dict[str, int]:
    ids = {}
    for name, (_fn, family, label) in SIGNALS.items():
        cur.execute(
            "INSERT INTO sig.signal_def (name, version, engine, family, code_ref, description, enabled)"
            " VALUES (%s,1,%s,%s,%s,%s,true)"
            " ON CONFLICT (name, version) DO UPDATE SET description = EXCLUDED.description"
            " RETURNING id",
            (name, engine, family, f"services.engine.signals:{name}", label))
        ids[name] = cur.fetchone()["id"]
    return ids


def active_model(cur, engine: str, h: Horizon) -> dict:
    """Wagi i progi z AKTYWNEJ wersji modelu - albo zalozenia poczatkowe.

    Wersja o statusie 'candidate' (czyli taka, ktora nie przeszla walidacji)
    swiadomie NIE jest tu brana. Model, ktory nie wykazal przewagi out-of-sample,
    nie ma prawa sterowac produkcja - a jedyne, co go od niej dzieli, to ten
    warunek na status.
    """
    cur.execute(
        "SELECT mv.id, mv.version, mv.params FROM learn.model_version mv"
        " JOIN learn.model m ON m.id = mv.model_id"
        " WHERE m.name = %s AND mv.status = 'active'"
        " ORDER BY mv.version DESC LIMIT 1",
        (f"{engine}_signal_weighting_{h.bar}",))
    row = cur.fetchone()
    if not row or not (row["params"] or {}).get("weights"):
        return {"weights": None, "enter": ENTER_THRESHOLD, "exit": EXIT_THRESHOLD,
                "bramki": bramki_z_modelu(None),
                "min_conf": MIN_CONFIDENCE, "version": None}
    p = row["params"]
    return {
        "weights": p["weights"],
        # `enter` i `exit` ZOSTAJA, ale sa juz tylko liczbami zbiorczymi do logu
        # i do starszych czytnikow. O wejsciu rozstrzyga `bramki`, bo prog
        # zalezy od rezimu - jedna liczba nie wyraza jednego percentyla na
        # kilku skalach wyniku (patrz `services/engine/bramki.py`).
        "enter": float(p.get("enter_threshold", ENTER_THRESHOLD)),
        "exit": float(p.get("exit_threshold", EXIT_THRESHOLD)),
        "bramki": bramki_z_modelu(p),
        "min_conf": float(p.get("min_confidence", MIN_CONFIDENCE)),
        "version": row["version"],
        "model_version_id": row["id"],
    }


def horizon_feature_ids(cur, h: Horizon, symbole: list[str] | None = None) -> list[int]:
    """Identyfikatory cech NALEZACYCH DO TEGO HORYZONTU i do TEGO uniwersum.

    feat.as_of() z NULL zwrocilby komplet cech wszystkich horyzontow naraz -
    silnik 4-godzinny dostalby wtedy takze cechy 5-minutowe i podejmowal decyzje
    na mieszance, ktorej trener nigdy nie widzial.

    CECHY PRZEKROJOWE FILTRUJEMY ODCISKIEM UNIWERSUM. Ich wartosc zalezy od
    zbioru, wiec ta sama nazwa bez odcisku znaczy co innego po kazdej zmianie
    uniwersum - a zmiana taka juz raz zaszla (migracja 0023, pary USDT -> USDC).
    Cechy per instrument nie maja odcisku w `params` i przechodza zawsze.

    Bez podanych symboli cechy przekrojowe SA POMIJANE. Domysl jest zachowawczy:
    wolacz, ktory nie wie, jakim zbiorem handluje, nie moze uzywac cechy, ktora
    od tego zbioru zalezy.
    """
    fp = odcisk(symbole) if symbole else None
    cur.execute("SELECT array_agg(id) AS ids FROM feat.feature_def"
                " WHERE active AND params->>'bar' = %s"
                "   AND (params->>'uniwersum' IS NULL"
                "        OR params->>'uniwersum' = %s)", (h.bar, fp))
    return cur.fetchone()["ids"] or []


def latest_features(cur, instrument_id: int, as_of: datetime, h: Horizon,
                    feature_ids: list[int],
                    bar_start: datetime) -> tuple[dict, dict]:
    """Cechy JEDNEJ swiecy - tej, dla ktorej zapada decyzja. Point-in-time.

    Zwraca `(cechy tej swiecy, cechy ze swiec WCZESNIEJSZYCH)`. Druga wartosc
    nie idzie do zadnego wyniku; sluzy wylacznie do nazwania awarii.

    DWA BLEDY NARAZ, OBA CICHE - i oba bral na siebie sam prog `max_staleness`.

    PIERWSZY: DECYZJA DLA SWIECY T NA CECHACH SWIECY T-1. Prog dla swiecy 4 h
    wynosi 600 minut, czyli dwie i pol swiecy. Petla budzi sie o 12:00 i pyta
    o decyzje dla swiecy 08:00-12:00, ale cecha tej swiecy powstaje dopiero,
    gdy `history_worker` dociagnie swiece 1 h z 11:00 (co 1200 s), a `features`
    ja policzy (co 900 s, swiadomie pomijajac swiece otwarta). Realna dostepnosc
    to 12:20-12:35, czyli 20-35 minut PO cyklu z 12:00. Przy progu 600 minut
    `feat.as_of` oddawalo wtedy komplet cech swiecy 04:00-08:00 i decyzja
    zapadala na wektorze o jedna swiece za starym.

    DRUGI: MIESZANKA SWIEC W JEDNYM WEKTORZE. `feat.as_of` robi
    `DISTINCT ON (feature_def_id) ... ORDER BY event_time DESC` (migracja 0004),
    wiec KAZDA cecha bierze swoja najswiezsza wartosc NIEZALEZNIE. Cecha
    policzona wczesniej pochodzila ze swiecy T-1, wolniejsza ze swiecy T-2 -
    i szly do `evaluate()` jako jeden wektor. Trener sklei pary sztywno
    (`train.load_panel`: `FROM f JOIN p ON p.t = f.event_time`), wiec ani jedna
    obserwacja treningowa nie ma tego ksztaltu.

    ZADEN LICZNIK TEGO NIE WIDZIAL. `MIN_CECH` bylo spelnione (cechy T-1
    istnieja i jest ich komplet), prog 600 minut nie migal, a `decided_for_bar`
    zamykalo swiece po pierwszej takiej decyzji, wiec spoznione cechy nie mialy
    juz jak niczego poprawic.

    DLACZEGO OKNO ZAPYTANIA ZOSTAJE SZEROKIE, A FILTR JEST TUTAJ. Wezsze okno
    (jedna swieca) daje ten sam wynik decyzyjny, ale zwraca wtedy PUSTKE
    i pominiecie zapisuje sie jako "zadnej cechy" - nieodroznialnie od workera,
    ktory stoi. Szerokie okno pozwala powiedziec, ze cechy SA, tyle ze dla
    swiecy wczesniejszej: "potok spozniony" i "potok martwy" to dwie rozne
    awarie i dwie rozne reakcje.
    """
    cur.execute(
        "SELECT feature_name, value, event_time FROM feat.as_of(%s, %s, %s, %s::interval)",
        (instrument_id, as_of, feature_ids or None, f"{h.max_staleness_min} minutes"))
    # feature_name ma postac 'rola__swieca@wersja'; do sygnalow idzie sama rola.
    cechy, obce = {}, {}
    for r in cur.fetchall():
        rola = Horizon.role_of(r["feature_name"])
        if r["event_time"] == bar_start:
            cechy[rola] = r["value"]
        else:
            obce[rola] = r["event_time"]
    return cechy, obce


def current_bar(as_of: datetime, h: Horizon) -> datetime:
    """Poczatek swiecy, ktora wlasnie sie domknela."""
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    span = h.bar_minutes * 60
    n = int((as_of - epoch).total_seconds() // span)
    return epoch + timedelta(seconds=(n - 1) * span)


def okno_decyzji(bar_start: datetime, h: Horizon) -> datetime:
    """Od kiedy zapadaja decyzje DLA TEJ swiecy. CZYSTA FUNKCJA.

    Decyzja dla swiecy B zapada dopiero PO jej zamknieciu, czyli od chwili
    B + dlugosc swiecy. Wczesniej silnik rozstrzyga swiece POPRZEDNIA.
    """
    return bar_start + timedelta(minutes=h.bar_minutes)


# ILE PO ZAMKNIECIU SWIECY WOLNO JESZCZE WEJSC (2026-10-03, audyt F-4.4).
#
# Cena decyzyjna to zamkniecie swiecy, a sygnal opisuje rynek z tej chwili.
# Zmierzone przed poprawka potoku: decyzje 63-88 min po zamknieciu i kupna
# wypelniane 76-95 pb nad cena decyzji (NEAR #86, BTC #83). Prog cenowy
# (`zlecenia.prog_zwloki`) tego nie lapal, bo byl skalibrowany na zwloke
# 75 min. Teraz granica jest wprost CZASOWA: po 20 minutach wejscie odpada
# z wlasnym powodem holdu, a nie po cichu. Po skroceniu interwalow potoku
# (F-4.1) najgorszy przypadek to 15 min, wiec prog ma odcinac wylacznie
# awarie potoku - i liczba takich holdow jest jej miara.
#
# WYJSC TO NIE DOTYCZY. Spozniona decyzja o wyjsciu nadal zdejmuje ryzyko,
# ktorego decyzja juz nie popiera.
MAKS_ZWLOKA_WEJSCIA = timedelta(minutes=20)


def decyzja_przeterminowana(teraz: datetime, bar_start: datetime,
                            h: Horizon) -> bool:
    """Czy od zamkniecia swiecy decyzyjnej minelo wiecej niz `MAKS_ZWLOKA_WEJSCIA`.

    CZYSTA FUNKCJA. Koniec swiecy z `okno_decyzji` - tej samej, ktora mowi,
    od kiedy decyzja dla swiecy w ogole zapada.
    """
    return teraz - okno_decyzji(bar_start, h) > MAKS_ZWLOKA_WEJSCIA


def decided_for_bar(cur, engine: str, mode: str, instrument_id: int,
                    bar_start: datetime, h: Horizon) -> bool:
    """Czy dla tej swiecy podjeto juz decyzje.

    Petla chodzi czesciej niz zamykaja sie swiece (co 5 minut przy swiecy
    4-godzinnej). Bez tego warunku silnik podjalby do 48 decyzji na jedna
    swiece, a symulacja, na ktorej byl kalibrowany, zaklada dokladnie jedna.
    Rozjazd trening-produkcja bralby sie wtedy z samego harmonogramu.

    PROG LICZYMY OD ZAMKNIECIA SWIECY, NIE OD JEJ POCZATKU - i to jest tu cala
    rzecz. `decided_at` to czas zegarowy, a swieca, ktorej decyzja dotyczy,
    zamyka sie o `bar_start + dlugosc`. Decyzje dla swiecy B zapadaja wiec
    w oknie [B + dlugosc, B + 2*dlugosc).

    Pierwsza wersja porownywala z samym `bar_start`, przez co decyzje dla swiecy
    POPRZEDNIEJ - podjete w oknie [B, B + dlugosc) - liczyly sie jako decyzje
    dla biezacej. Silnik rozstrzygal wtedy CO DRUGA swiece.

    Zmierzone na produkcji 2026-09-07: decyzje zapadaly o 00:00, 08:00 i 16:00,
    a swiece 04:00, 12:00 i 20:00 byly pomijane. Silnik handlowal z POLOWA
    czestotliwosci, na ktorej kalibrowano walk-forward - czyli dokladnie tym
    rozjazdem z harmonogramu, przed ktorym ostrzega akapit wyzej. W logu
    wygladalo to na zdrowe odsiewanie duplikatow: "juz rozstrzygniete w tej
    swiecy 10".
    """
    cur.execute(
        "SELECT 1 FROM journal.decision WHERE engine=%s AND mode=%s AND instrument_id=%s"
        " AND decided_at >= %s LIMIT 1",
        (engine, mode, instrument_id, okno_decyzji(bar_start, h)))
    return cur.fetchone() is not None


# Ponizej tylu cech nie ma czego skladac w wynik: `evaluate()` dostalby wektor,
# w ktorym wiekszosc wag nie ma czym sie odezwac. Stala byla wpisana wprost
# w warunku, przez co komunikat o pominieciu nie mogl powiedziec, ILU cech
# zabraklo - a to jest cala roznica miedzy "worker cech stoi" a "jedna cecha
# sie nie liczy".
MIN_CECH = 4

# Ponizej tego POKRYCIA WAG decyzja jest ODKLADANA do nastepnego cyklu w oknie
# swiecy, a nie podejmowana. `MIN_CECH` liczy sztuki; to liczy, jaka czesc
# budzetu wag ma czym sie odezwac - i to jest wlasciwa miara. Zmierzone
# 2026-09-22: 8 cech pochodnych (funding, arkusz, long_ratio, oi, newsy)
# spelnialo MIN_CECH 20-35 min PRZED policzeniem cech cenowych, wiec silnik
# przez 11 dni decydowal na 2 z 9 sygnalow przy pokryciu 0,02-0,10 - strategia,
# ktorej trener nigdy nie modelowal (train.load_panel skleja komplet).
# 0,9, nie 1,0: pojedyncza cecha bez wartosci (np. brak newsow w oknie) nie ma
# odkladac decyzji bez konca. Patrz migracja 0048.
MIN_POKRYCIE = 0.9


def zapisz_pominiecie(cur, engine: str, mode: str, instrument_id: int,
                      bar_start: datetime, powod: str, szczegol: str) -> None:
    """POMINIECIE ZOSTAWIA SLAD. Patrz migracja 0041.

    Instrument, ktory wypadl z przebiegu przed ocena, nie zostawial w bazie
    NICZEGO - ani decyzji, ani informacji, ze decyzji miec nie mialo. Z zewnatrz
    bylo to nieodroznialne od przestoju silnika i od instrumentu, ktory
    rozwazono i odrzucono.

    JEDEN WIERSZ NA (instrument, swieca, powod), nie jeden na przebieg: petla
    budzi sie co 300 s, a swieca trwa 4 h, wiec bez klucza ten sam brak danych
    zapisalby sie kilkadziesiat razy. `ON CONFLICT DO NOTHING` zostawia wiec
    znacznik PIERWSZEGO wystapienia - czyli chwili, w ktorej problem sie zaczal.
    """
    cur.execute(
        "INSERT INTO journal.pominiecie (engine, mode, instrument_id, bar_start,"
        " powod, szczegol) VALUES (%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (engine, mode, instrument_id, bar_start, powod) DO NOTHING",
        (engine, mode, instrument_id, bar_start, powod, szczegol))


def atr_percentile_80(cur, instrument_id: int, as_of: datetime,
                      h: Horizon) -> float | None:
    """Prog wysokiej zmiennosci liczony z HISTORII TEGO instrumentu.

    Stala wartosc nie ma sensu: 0,3% na 5 min to spokoj dla SOL i szok dla SPY.
    Okno historii skaluje sie z horyzontem - na swiecy 4-godzinnej 14 dni to
    zaledwie 84 obserwacje, czyli za malo na stabilny percentyl.
    """
    cur.execute(
        """
        SELECT percentile_cont(0.8) WITHIN GROUP (ORDER BY fv.value) AS p80
        FROM feat.feature_value fv JOIN feat.feature_def fd ON fd.id = fv.feature_def_id
        WHERE fd.name = %s AND fv.instrument_id = %s
          AND fv.event_time BETWEEN %s - %s::interval AND %s
        """,
        (h.name("atr_pct"), instrument_id, as_of,
         f"{h.train_days} days", as_of))
    row = cur.fetchone()
    return float(row["p80"]) if row and row["p80"] is not None else None


# Ile moze miec cena odniesienia, zanim przestaniemy nazywac ja CENA.
#
# `market_worker` dociaga swiece 1m co 60 s (charts/trader/values.yaml), wiec
# najnowszy wiersz ma naturalnie do dwoch minut: jedna trwajaca swieca plus
# jeden odstep pobierania. Petla decyzyjna budzi sie co 300 s, wiec 15 minut to
# TRZY POMINIETE CYKLE - dokladnie ta sama logika, z ktorej wziete jest
# MARTWY_PO_MIN w dozorze: jedno spoznienie to wahanie, trzy to sygnal.
#
# Zapas nad zmierzona norma jest siedmiokrotny i to jest celowe. Ten prog nie
# tylko opisuje stan - on WSTRZYMUJE HANDEL, wiec prog, ktory miga, zatrzymywalby
# silnik po kilka razy dziennie bez powodu. Bramka, ktora miga, konczy tak samo
# jak czujka, ktora miga: ktos ja podniesie, zeby przestala przeszkadzac.
#
# ZASTRZEZENIE DLA SILNIKA GIELDOWEGO. Prog zaklada rynek dzialajacy bez przerwy.
# Akcja poza sesja nie ma swiec 1m i KAZDA pozycja bedzie tu "bez swiezej ceny" -
# co jest poprawne w tym sensie, ze wyceny naprawde nie ma, ale wstrzymanie wejsc
# przez cala noc jest bez sensu. Zanim silnik `equity` ruszy, prog musi pochodzic
# z kalendarza sesji, a nie z tej stalej. Do tego czasu zachowanie i tak jest
# bezpieczne: pozycja zostaje w ekspozycji, a nie znika z niej.
CENA_STARA_PO_MIN = 15


def cena_odniesienia(cur, instrument_id: int, as_of: datetime,
                     max_wiek_min: int = CENA_STARA_PO_MIN) -> dict:
    """Ostatnia cena minutowa RAZEM Z JEJ WIEKIEM.

    Wiek nie jest tu ozdoba. Zapytanie "ostatnia swieca <= as_of" odpowiada tak
    samo chetnie dla feedu sprzed minuty, jak dla feedu, ktory stanal trzy dni
    temu - i bez wieku tych dwoch przypadkow NIE DA SIE odroznic. Dopoki
    istniala jakakolwiek stara swieca, kapital liczyl sie po cenie sprzed
    dowolnie dlugiego czasu i wygladalo to na pomiar.

    Zwracamy oba pola, zeby zaden wolajacy nie mogl przez przeoczenie wziac
    starej ceny za biezaca: zeby dostac sama liczbe, trzeba jawnie przejsc przez
    `swieza`.
    """
    cur.execute(
        "SELECT close, EXTRACT(EPOCH FROM (%s - event_time)) / 60 AS wiek_min"
        " FROM market.candle WHERE instrument_id = %s AND timeframe='1m'"
        " AND event_time <= %s ORDER BY event_time DESC LIMIT 1",
        (as_of, instrument_id, as_of))
    row = cur.fetchone()
    if not row:
        return {"cena": None, "wiek_min": None, "swieza": False}
    wiek = float(row["wiek_min"])
    return {"cena": Decimal(row["close"]), "wiek_min": wiek,
            "swieza": wiek <= max_wiek_min}


def mark_price(cur, instrument_id: int, as_of: datetime,
               max_wiek_min: int = CENA_STARA_PO_MIN) -> Decimal | None:
    """Cena odniesienia, ale WYLACZNIE swieza. None znaczy "nie wiem".

    Wersja bez limitu wieku byla cicha awaria najgorszego rodzaju: instrument
    z zatrzymanym feedem dostawal cene sprzed doby i nikt sie o tym nie
    dowiadywal, bo funkcja zwracala liczbe, a liczba wyglada jak pomiar.
    """
    c = cena_odniesienia(cur, instrument_id, as_of, max_wiek_min)
    return c["cena"] if c["swieza"] else None


def open_position(cur, engine: str, mode: str, instrument_id: int) -> dict | None:
    cur.execute(
        "SELECT id, side, qty, avg_price, opened_at FROM exec.position"
        " WHERE engine=%s AND mode=%s AND instrument_id=%s AND closed_at IS NULL LIMIT 1",
        (engine, mode, instrument_id))
    return cur.fetchone()


class BrakKapitalu(RuntimeError):
    """Nie wiadomo, ile jest na rachunku. Zgadywanie jest tu gorsze niz postoj."""


def seed_equity(cur, engine: str, mode: str) -> None:
    """Punkt zerowy krzywej kapitalu - TYLKO dla silnika, ktory liczy ja SAM.

    ZASIEW TYLKO DLA DEMO. Na zywym rachunku wpisanie 100 000 jako punktu
    wyjscia jest fikcja, od ktorej liczone sa potem wszystkie limity procentowe
    i cale obsuniecie.

    I ZASIEW TYLKO DLA SILNIKA, KTOREGO KRZYWEJ NIE PISZE NIKT INNY. To jest
    poprawka z 2026-09-26 i powod, dla ktorego ta funkcja ma docstring.

    Do tego dnia zasiew wykonywal sie dla KAZDEGO silnika w trybie demo, w tym
    dla `equity` - ktorego krzywa od 2026-09-22 pisze `kapital_gieldy` z ksiag
    konta paper Alpaki. Seria dostawala wiec dwoch pisarzy z dwiema roznymi
    definicjami kapitalu, a `seed_equity` stoi w `run_once` PRZED bramka
    `--wymagaj-modelu`, wiec dzialal takze wtedy, gdy silnik nie mial prawa
    zawrzec ani jednej transakcji. Zasiane `nav = 1.0` wchodzilo do `max(nav)`
    liczonego bez ograniczenia czasowego, czyli zostawalo szczytem NA ZAWSZE -
    i przy `max_drawdown` 10% dawalo kill-switcha z arytmetyki, ktorego dla
    equity nie ma kto zwolnic (dozorca chodzi z `--engine crypto`).

    WYSCIG, KTORY CZYNIL TO NIEPRZEWIDYWALNYM: warunek "seria jest pusta"
    rozstrzygal sie miedzy dwoma procesami. Pierwszy cykl silnika po czystej
    bazie zasiewal, a `kapital-equity` co 900 s dopisywal odczyty do tej samej
    serii - albo odwrotnie. To, ktora definicja zaklada krzywa, zalezalo wiec
    od kolejnosci startu podow.

    MILCZY, NIE PADA. Brak zasiewu jest tu stanem NORMALNYM (krzywa pisze
    ksiegi), a nie awaria - wyjatek zatrzymalby cykl silnika za to, ze wszystko
    jest w porzadku.
    """
    if mode != "demo":
        return
    # Nie nasza krzywa - patrz `stan_kapitalu.KRZYWA_Z_KSIAG_GIELDY`.
    if engine in stan_kapitalu.KRZYWA_Z_KSIAG_GIELDY:
        return
    cur.execute("SELECT 1 FROM exec.equity_point WHERE engine=%s AND mode=%s LIMIT 1",
                (engine, mode))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO exec.equity_point (engine, mode, event_time, equity, cash,"
            " unrealized, realized_cum, exposure, nav, units, wplaty_netto, zrodlo)"
            " VALUES (%s,%s, now(), %s, %s, 0, 0, 0, 1.0, %s, %s, %s)",
            (engine, mode, START_EQUITY, START_EQUITY, START_EQUITY, START_EQUITY,
             stan_kapitalu.ZRODLO_ZASIEW))


def przeplywy(cur, engine: str, mode: str) -> tuple[Decimal, Decimal]:
    """Wplaty netto i liczba jednostek. Zrodlo prawdy o KAPITALE, nie o wyniku.

    Do 2026-09-07 kapital byl stala plus wynik handlu, wiec kazde dolozenie
    srodkow wygladalo jak zysk. Teraz stala nie ma - jest suma przeplywow.
    """
    cur.execute(
        "SELECT COALESCE(sum(amount), 0) AS wplaty, COALESCE(sum(units), 0) AS units"
        " FROM exec.cash_flow WHERE engine=%s AND mode=%s", (engine, mode))
    r = cur.fetchone()
    return Decimal(r["wplaty"] or 0), Decimal(r["units"] or 0)


@dataclass
class Wycena:
    """Wynik wyceny otwartych pozycji razem z JAKOSCIA tego pomiaru.

    `bez_swiezej_ceny` nie jest dodatkiem informacyjnym - to jest powod, dla
    ktorego caly ten obiekt istnieje zamiast pary liczb. Kapital policzony przy
    niepustej liscie jest kapitalem NIEPELNIE ZMIERZONYM i wolajacy musi miec
    jak sie tego dowiedziec, zamiast dostac liczbe nieodroznialna od dobrej.
    """
    unrealized: Decimal = Decimal(0)
    exposure: Decimal = Decimal(0)
    # EKSPOZYCJA ZE ZNAKIEM: dluga na plus, krotka na MINUS. Osobna od `exposure`,
    # bo obie odpowiadaja na inne pytanie i zlanie ich w jedna liczbe psuje
    # jedna z odpowiedzi.
    #
    # `exposure` (brutto) mierzy, ILE RYNKU DOTYKAMY - i temu sluzy w limitach
    # `max_total_exposure` oraz `max_position_notional`. Dla ryzyka krotka
    # pozycja jest tak samo grozna jak dluga, wiec sumuje sie ja na plus.
    #
    # `exposure_netto` mierzy, ILE GOTOWKI POSZLO NA RYNEK - i tylko tym da sie
    # odtworzyc saldo gotowkowe. Kupno gotowke ZABIERA, sprzedaz krotka ja
    # DODAJE, wiec ta druga musi wejsc z przeciwnym znakiem. Patrz `update_equity`.
    exposure_netto: Decimal = Decimal(0)
    # Ta sama ekspozycja w rozbiciu na instrument. Potrzebuje jej risk kernel do
    # limitu `max_position_notional`, ktory jest limitem NA INSTRUMENT - a bez
    # tego pola musialby wyliczac ekspozycje po raz drugi, wlasnym zapytaniem
    # i wlasna regula wyceny. Dwie reguly wyceny tej samej wielkosci to
    # dokladnie ten rozjazd, przed ktorym ostrzega docstring `wycen()`.
    per_instrument: dict[int, Decimal] = field(default_factory=dict)
    bez_swiezej_ceny: list[dict] = field(default_factory=list)

    @property
    def pomiar_pelny(self) -> bool:
        return not self.bez_swiezej_ceny

    def opis_brakow(self) -> str:
        """Jednolinijkowy opis, gotowy do logu i do powodu wstrzymania."""
        return "; ".join(
            f"{p['symbol']}: {p['powod']}" for p in self.bez_swiezej_ceny)


def wycen(pozycje: list[dict]) -> Wycena:
    """Niezrealizowany wynik i ekspozycja otwartych pozycji. CZYSTA FUNKCJA.

    NIE MA TU GALEZI, KTORA POMIJA POZYCJE - i to jest cala tresc tej funkcji.

    Poprzednia wersja robila `if px is None: continue`, wiec pozycja bez ceny
    nie wnosila ani niezrealizowanej straty, ani ekspozycji. Kierunek tego bledu
    byl najgorszy z mozliwych i dzialal w obie strony naraz:

      (a) obsuniecie i prog kapitalu przestawaly widziec strate na tej pozycji,
          wiec kill-switch nie mial czym zadzialac;
      (b) `exposure` spadala, wiec limit lacznej ekspozycji ZWALNIAL MIEJSCE na
          kolejne zakupy.

    Zatrzymany feed instrumentu, na ktorym siedzi stratna pozycja, zamienial
    zabezpieczenie w akcelerator - przy kapitale stabilnym, ekspozycji niskiej
    i bijacym heartbeacie. Brak pomiaru nie jest pomiarem zerowym.

    CZYM WYCENIAMY, GDY NIE MA SWIEZEJ CENY

      - jest cena stara -> bierzemy ja, bo to nadal NAJLEPSZE, co wiemy o tej
        pozycji. Zamrozona strata jest blizej prawdy niz strata skasowana.
      - nie ma zadnej swiecy -> bierzemy cene wejscia. Wynik niezrealizowany
        wychodzi wtedy zero, ale EKSPOZYCJA zostaje: `qty x avg_price` to
        kwota, ktora naprawde zostala zaangazowana, i nie przestaje byc
        zaangazowana przez to, ze potok danych padl.

    Ten sam wybor robi od zawsze symulacja walk-forward (portfolio.py: wycena
    spada na `p.entry`, a ekspozycja liczy sie z `qty * p.entry`). Produkcja
    pozycje po prostu wyrzucala - czyli obsuniecie mierzone bylo na innym
    systemie niz ten, ktory naprawde handlowal.

    ZADNA Z TYCH WYCEN NIE JEST POMIAREM i dlatego kazda z nich trafia na liste
    `bez_swiezej_ceny`. Odpowiedz na "co zrobic z niepelnym pomiarem" nie nalezy
    do tej funkcji - nalezy do tego, kto na jej podstawie chce handlowac.
    """
    w = Wycena()
    for pos in pozycje:
        qty, entry = Decimal(pos["qty"]), Decimal(pos["avg_price"])
        cena, swieza = pos.get("cena"), pos.get("swieza", False)

        if cena is None:
            px = entry
            powod = "brak jakiejkolwiek świecy — wycena po cenie wejścia"
        else:
            px = Decimal(cena)
            powod = (f"cena sprzed {pos['wiek_min']:.0f} min "
                     f"— wycena po cenie przeterminowanej")

        diff = (px - entry) if pos["side"] == "long" else (entry - px)
        w.unrealized += diff * qty
        w.exposure += qty * px
        w.exposure_netto += (qty * px) if pos["side"] == "long" else -(qty * px)
        iid = pos.get("instrument_id")
        if iid is not None:
            w.per_instrument[iid] = w.per_instrument.get(iid, Decimal(0)) + qty * px
        if not swieza:
            w.bez_swiezej_ceny.append({
                "instrument_id": pos.get("instrument_id"),
                "symbol": pos.get("symbol") or str(pos.get("instrument_id")),
                "wiek_min": pos.get("wiek_min"),
                "powod": powod,
            })
    return w


def pozycje_do_wyceny(cur, engine: str, mode: str, as_of: datetime,
                      max_wiek_min: int = CENA_STARA_PO_MIN) -> list[dict]:
    """Otwarte pozycje wzbogacone o cene i JEJ WIEK. Wejscie dla `wycen()`.

    Symbol dociagamy razem z pozycja, bo komunikat "3 pozycje bez świeżej ceny"
    nie mowi nikomu, ktory potok danych naprawic.
    """
    cur.execute(
        "SELECT p.id, p.instrument_id, p.side, p.qty, p.avg_price, i.symbol"
        " FROM exec.position p JOIN ref.instrument i ON i.id = p.instrument_id"
        " WHERE p.engine=%s AND p.mode=%s AND p.closed_at IS NULL", (engine, mode))
    pozycje = cur.fetchall()
    return [dict(p, **cena_odniesienia(cur, p["instrument_id"], as_of, max_wiek_min))
            for p in pozycje]


def update_equity(cur, engine: str, mode: str, as_of: datetime,
                  max_wiek_min: int = CENA_STARA_PO_MIN) -> Wycena:
    """Przelicza kapital: gotowka + niezrealizowany wynik otwartych pozycji.

    WYNIK ZREALIZOWANY LICZYMY Z POZYCJI OTWARTYCH TAKZE, nie tylko zamknietych.

    Wyjscie czesciowe ksieguje `realized_pnl` na wierszu, ktory POZOSTAJE
    otwarty - zmniejsza sie `qty`, a `closed_at` jest nadal puste. Warunek
    `closed_at IS NOT NULL` pomijal wiec ten wynik calkowicie, a `unrealized`
    liczy sie juz z pomniejszonej ilosci. Strata na sprzedanej czesci znikala
    z krzywej kapitalu.

    Kierunek bledu jest najgorszy z mozliwych: OBSUNIECIE BYLO ZANIZONE, wiec
    kill-switch nie strzelal w sytuacji, dla ktorej istnieje. Pozycja 10 sztuk
    po 200, wyjscie przy 150 wypelnione w 6 - realizacja -300 nie wchodzila
    do kapitalu, a niezrealizowane pokazywalo -200 zamiast -500.

    Przy okazji domyka to drugi ubytek: prowizja WEJSCIA na pozycji otwartej
    tez nie wchodzila do kapitalu, wiec byl on zawyzony o koszt kazdego
    trwajacego wejscia.

    POZYCJA BEZ SWIEZEJ CENY NIE ZNIKA - patrz `wycen()`. Zwracamy `Wycena`,
    a nie None, zeby wolajacy dowiedzial sie, ze ten punkt krzywej kapitalu
    powstal z NIEPELNEGO POMIARU. Sam wiersz w bazie tego nie powie: liczba
    z zamrozonej ceny wyglada dokladnie tak samo jak liczba z ceny biezacej.
    """
    cur.execute(
        "SELECT COALESCE(sum(realized_pnl - fees_paid), 0) AS realized FROM exec.position"
        " WHERE engine=%s AND mode=%s", (engine, mode))
    realized = Decimal(cur.fetchone()["realized"] or 0)

    w = wycen(pozycje_do_wyceny(cur, engine, mode, as_of, max_wiek_min))
    unrealized, exposure = w.unrealized, w.exposure

    # KAPITAL Z PRZEPLYWOW, nie ze stalej. Brak wierszy w exec.cash_flow oznacza
    # rachunek, ktorego nikt nie zasilil - na demo spadamy wtedy na kapital
    # startowy, zeby proces nie wywracal sie na swiezej bazie przed migracja
    # zasiewajaca.
    #
    # NA ZYWYM RACHUNKU TAKIEGO DOMYSLU BYC NIE MOZE. Kolejnosc zdarzen jest
    # naturalna: silnik startuje, wlasciciel przelewa 10 000, nikt jeszcze nie
    # zarejestrowal wplaty - i system przyjmuje 100 000. Limity sa procentowe,
    # wiec pozycja "5% kapitalu" staje sie polowa rachunku, a prog min_equity
    # 95 000 nie zadziala nigdy, bo equity "wynosi" 100 000. Jest to dokladnie
    # ta awaria, przed ktora powstala migracja 0028, wprowadzona z powrotem
    # tylnymi drzwiami przez wartosc domyslna.
    wplaty, units = przeplywy(cur, engine, mode)
    if wplaty == 0:
        if mode != "demo":
            raise BrakKapitalu(
                f"tryb {mode} nie ma zadnego przeplywu w exec.cash_flow — "
                f"zamiast zgadywac kapital 100 000, zatrzymuje sie. Zarejestruj "
                f"wplate przed uruchomieniem handlu")
        wplaty, units = START_EQUITY, START_EQUITY

    equity = wplaty + realized + unrealized
    # WARTOSC JEDNOSTKI. Wplata kupuje jednostki po biezacym nav i nav sie nie
    # rusza; wynik handlu rusza nav i nie rusza liczby jednostek. Dzieki temu
    # obsuniecie mierzy strategie, a nie przelewy.
    nav = (equity / units) if units > 0 else Decimal(1)
    # GOTOWKA LICZY SIE Z EKSPOZYCJI NETTO, NIE Z BRUTTO.
    #
    # Bylo tu `equity - exposure`. Dla pozycji dlugiej to tozsamosc: kupno
    # zamienilo gotowke na aktywo, wiec gotowka to kapital minus wartosc
    # rynkowa tego, co trzymamy.
    #
    # DLA KROTKIEJ TO ZDANIE JEST FALSZYWE, i to o dwie ekspozycje. Sprzedaz
    # krotka gotowke na rachunek WPLACA, a aktywo staje sie zobowiazaniem:
    #
    #     gotowka = wplaty + realized + notional_wejscia
    #     equity  = gotowka - ekspozycja       =>   gotowka = equity + ekspozycja
    #
    # Stary wzor odejmowal wiec dokladnie to, co sprzedaz dodala. Zmierzone na
    # produkcji 2026-09-01 12:06 (same pozycje krotkie): equity 99 843,03,
    # ekspozycja 1009,67, zapisana `cash` 98 833,37 przy rzeczywistej gotowce
    # 100 852,70 - zanizenie o 2019,33, czyli o 2 x ekspozycje.
    #
    # `exposure_netto` ma dluga na plus, krotka na minus, wiec jedno odejmowanie
    # obsluguje oba kierunki i portfel mieszany takze.
    #
    # DO ZADNEJ REGULY RYZYKA TA LICZBA NIE WCHODZI - czyta ja kafelek
    # w services/api/routers/overview.py. Blad nie otwieral wiec dziury
    # w bezpieczniku, tylko klamal wlascicielowi, i dlatego nie wolno go bylo
    # zostawic jako "nieszkodliwy": kafelek "gotowka" jest jedynym miejscem,
    # w ktorym czlowiek sprawdza, czy rachunek zgadza sie z giełdą.
    # TRZECI PISARZ TEJ SAMEJ SERII - I DLATEGO TEN WARUNEK.
    #
    # Dla silnika, ktorego krzywa idzie z ksiag gieldy, ta rekonstrukcja jest
    # DRUGA definicja kapitalu w jednej serii: liczy `wplaty + zrealizowany +
    # niezrealizowany` z NASZEGO dziennika, a ksiegi licza to samo po stronie
    # gieldy i te dwie liczby nie musza sie zgadzac (prowizje, korporacyjne
    # zdarzenia, czesciowe wypelnienia znane gieldzie wczesniej niz nam).
    # Czytelnicy biora `max` i ostatni punkt bez pytania o zrodlo, wiec rozjazd
    # nie objawilby sie bledem - objawilby sie obsunieciem liczonym miedzy
    # dwiema roznymi ksiegowosciami.
    #
    # WYCENA ZOSTAJE POLICZONA I ZWROCONA. Wolajacy potrzebuje `Wycena`
    # (ekspozycja per instrument, braki swiezych cen) do decyzji i do limitow -
    # to nie jest krzywa kapitalu i nie ma powodu tego pomijac. Pomijamy sam
    # INSERT, czyli dokladnie to, co tworzy druga definicje.
    #
    # DOTYCZY TO TAKZE PRZYSZLOSCI, nie tylko dzisiejszego braku handlu: gdy
    # equity kiedys zacznie handlowac, ksiegi Alpaki beda zawierac te transakcje
    # z definicji, wiec rekonstrukcja nadal nie bedzie miala co dolozyc.
    if engine not in stan_kapitalu.KRZYWA_Z_KSIAG_GIELDY:
        cur.execute(
            "INSERT INTO exec.equity_point (engine, mode, event_time, equity, cash,"
            " unrealized, realized_cum, exposure, nav, units, wplaty_netto, zrodlo)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (engine, mode, event_time) DO NOTHING",
            (engine, mode, as_of.replace(second=0, microsecond=0),
             equity, equity - w.exposure_netto, unrealized, realized, exposure,
             nav, units, wplaty, stan_kapitalu.ZRODLO_PETLA))
    return w


def record_decision(cur, engine: str, mode: str, instrument_id: int, as_of: datetime,
                    action: str, qty, price, confidence: float, regime: str,
                    features: dict, verdict, rationale: str, contributions: list[dict],
                    signal_ids: dict[str, int], latency_ms: int,
                    model_version_id: int | None = None,
                    pokrycie_sygnalow: float | None = None,
                    powod_holdu: str | None = None,
                    prog_wejscia: float | None = None,
                    prog_wyjscia: float | None = None,
                    bar_start: datetime | None = None,
                    strategia: str | None = None) -> int | None:
    # PROGI ZAPISUJEMY PRZY DECYZJI, a nie zostawiamy ich w wersji modelu.
    #
    # Wersja modelu zyje krocej niz decyzja: purge_demo.py kasuje cala tabele
    # learn.model_version, a journal.decision jest niemutowalna i wieczna. Bez
    # tych dwoch liczb pytanie "czy ten wynik byl ponad progiem" traci
    # odpowiedz w chwili, w ktorej ktos posprzata rejestr modeli.
    # model_version_id mowi, KTORA wersja modelu podjela te decyzje. Kolumna
    # istniala z kluczem obcym od migracji 0006, ale nikt jej nie wypelnial -
    # bo do tej pory petla nie uzywala zadnego modelu, wiec nie bylo czego
    # zapisac. NULL znaczy "zalozenia poczatkowe, brak zwalidowanego modelu"
    # i jest to stan poprawny, nie brak danych.
    # `strategia` mowi, KTORA REGULA podjela decyzje (0091): przy `momentum`
    # model_version_id jest NULL, a mimo to decyzja nie jest "zalozeniami
    # poczatkowymi" - i bez tej kolumny jedno od drugiego nie daloby sie
    # odroznic. Pomiar egzekucji (`poslizg.py --strategia`) filtruje po niej.
    cur.execute(
        """
        INSERT INTO journal.decision (engine, mode, instrument_id, decided_at, action,
            target_qty, target_price, confidence, regime, feature_snapshot,
            risk_verdict, risk_reason, rationale, rationale_model, latency_ms,
            model_version_id, pokrycie_sygnalow, powod_holdu,
            prog_wejscia, prog_wyjscia, strategia)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'reguły',%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (engine, instrument_id, decided_at) DO NOTHING
        RETURNING id
        """,
        (engine, mode, instrument_id, as_of, action, qty, price, confidence, regime,
         json.dumps({k: round(v, 6) for k, v in features.items()}),
         verdict.verdict, verdict.reason, rationale, latency_ms, model_version_id,
         pokrycie_sygnalow, powod_holdu, prog_wejscia, prog_wyjscia, strategia))
    row = cur.fetchone()
    if not row:
        return None
    did = row["id"]

    rows = []
    for c in contributions:
        sid = signal_ids.get(c["name"])
        if sid:
            rows.append((did, "signal", sid, c["label"], c["score"], c["weight"],
                         c["contribution"]))
    # Cecha nie ma wlasnego identyfikatora w tej tabeli (klucz glowny to
    # decision_id+kind+ref_id), wiec wyprowadzamy go z nazwy. Swiadomie crc32,
    # a NIE wbudowany hash(): ten jest losowany przy kazdym starcie interpretera
    # (PYTHONHASHSEED), wiec ta sama cecha dostawalaby inne ref_id po restarcie,
    # a ON CONFLICT przestalby dzialac.
    for name, value in features.items():
        rows.append((did, "feature", zlib.crc32(name.encode()) % 2147483647,
                     name, value, 0.0, 0.0))

    # ARTYKULY, KTORE ZLOZYLY SIE NA `news_sent` TEJ SWIECY.
    #
    # Do 2026-09-24 ten rodzaj wkladu NIE BYL ZAPISYWANY ANI RAZU, mimo ze
    # `CHECK (kind IN ('feature','signal','news'))` i komentarz `ref_id =
    # article_id` czekaly na niego od migracji 0005. Skutkiem bylo pytanie bez
    # odpowiedzi: dziennik mowil "sentyment newsowy -0,21", ale nie mowil,
    # KTORY artykul to powiedzial - wiec przy zlej decyzji nie dalo sie
    # odroznic zlej wiadomosci od zlej interpretacji dobrej.
    #
    # `bar_start`, NIE `as_of`. Decyzja zapada po chwili `as_of`, ale liczby,
    # na ktorych zapada, pochodza ze swiecy `bar_start` (patrz `cechy_i_obce`:
    # do `cechy` wchodzi wylacznie `event_time == bar_start`). Atrybucja liczona
    # z `as_of` opisywalaby okno przesuniete wzgledem tego, co model widzial.
    #
    # WAGA I WKLAD ZEREM, tak samo jak przy cechach: artykul jest WEJSCIEM do
    # cechy, a nie osobnym skladnikiem wyniku. Niezerowa wartosc podwajalaby go
    # w kazdym zestawieniu sumujacym `contribution`.
    if bar_start is not None:
        for a in wklady_newsow(cur, instrument_id, bar_start):
            rows.append((did, "news", a["article_id"],
                         (a["title"] or a["url"])[:200], a["wklad"], 0.0, 0.0))

    cur.executemany(
        "INSERT INTO journal.decision_contribution (decision_id, kind, ref_id, label,"
        " value, weight, contribution) VALUES (%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT DO NOTHING", rows)
    return did


def zakres_swiecy(cur, instrument_id: int, h: Horizon,
                  poczatek: datetime) -> dict | None:
    """Najwyzszy, najnizszy i zamkniecie swiecy zaczynajacej sie o `poczatek`.

    Agregacja jest ta sama, ktorej uzywa trener przy budowaniu panelu: swieca
    horyzontu powstaje z `source_timeframe` przez time_bucket. Gdyby produkcja
    liczyla ja inaczej, sprawdzenie wypelnienia dotyczyloby innej swiecy niz ta,
    na ktorej model byl kalibrowany.

    Zwraca None, gdy swieca SIE JESZCZE NIE DOMKNELA. Warunek jest czasowy,
    a nie oparty na liczbie swiec zrodlowych, i to jest celowe: przerwa w danych
    gieldy nie powinna w nieskonczonosc blokowac rozstrzygniecia, a swieca
    trwajaca nie powinna byc oceniana NIGDY - jej zakres jeszcze rosnie.

    Ochrona jest tutaj, a nie tylko u wywolujacego. Przy przestoju silnika
    warunek czasowy w rozstrzygnij_plany przestaje wystarczac (mija termin
    waznosci planu), wiec funkcja musi bronic sie sama.
    """
    koniec = poczatek + timedelta(minutes=h.bar_minutes)
    if koniec > datetime.now(timezone.utc):
        return None
    cur.execute(
        "SELECT max(high) AS high, min(low) AS low,"
        " (array_agg(close ORDER BY event_time DESC))[1] AS close,"
        " count(*) AS n"
        " FROM market.candle WHERE instrument_id = %s AND timeframe = %s"
        "   AND event_time >= %s AND event_time < %s",
        (instrument_id, h.source_timeframe, poczatek, koniec))
    r = cur.fetchone()
    if not r or not r["n"] or r["high"] is None:
        return None
    return {"high": float(r["high"]), "low": float(r["low"]),
            "close": Decimal(r["close"]), "n": r["n"]}


def stawki(kind: str, limit: bool) -> tuple[Decimal, Decimal]:
    """Prowizja i poslizg dla przyjetego modelu egzekucji - z koszty.py.

    Zlecenie z limitem placi stawke maker i nie ma poslizgu, bo nie przechodzi
    przez spread; placi za to ryzykiem niewypelnienia, ktorego ta petla NIE
    modeluje (patrz zastrzezenie przy execute_paper).
    """
    k = koszty(kind)
    return (Decimal(str(k.maker if limit else k.taker)),
            Decimal(str(POSLIZG_MAKER if limit else POSLIZG_TAKER)))


def _czas_wypelnienia(czas_wykonania: datetime | None, as_of: datetime) -> datetime:
    """`exec.fill.event_time`: czas GIELDY, gdy go znamy (2026-10-03, audyt 3 T3).

    Do tej daty kolumna dostawala `as_of`, czyli chwile KSIEGOWANIA przy
    rozstrzyganiu planu - nawet dwie godziny po wypelnieniu (zlecenie 81:
    `event_time` 07:21:27 = `ingested_at` co do milisekundy). Opoznienia
    wypelnienia nie dalo sie przez to zmierzyc, a symulacja zaklada wypelnienie
    przy dotknieciu ceny. Chwila ksiegowania zostaje w `exec.fill.ingested_at`.

    Czas gieldy pozniejszy niz ksiegowanie jest niemozliwy (rozjazd zegarow) -
    wtedy `as_of`, zeby wypelnienie nie wyprzedzalo wlasnego zapisu.
    """
    if czas_wykonania is None or czas_wykonania > as_of:
        return as_of
    return czas_wykonania


def zapisz_wejscie(cur, engine: str, mode: str, instrument_id: int, decision_id: int,
                   side: str, qty: Decimal, cena: Decimal, prowizja: Decimal,
                   as_of: datetime, quote_ccy: str, meta: dict, *,
                   planned_order_id: int | None = None,
                   venue_order_id: str | None = None,
                   client_order_id: str | None = None,
                   venue_fill_id: str | None = None,
                   order_id: int | None = None,
                   prowizja_ccy: str | None = None,
                   prowizja_surowa: Decimal | None = None,
                   czas_wykonania: datetime | None = None) -> int:
    """Zapis wejscia w pozycje. JEDEN ZAPIS DLA OBU ZRODEL.

    `czas_wykonania` to chwila wypelnienia wg ZEGARA GIELDY i idzie wylacznie
    do `exec.fill.event_time` (patrz `_czas_wypelnienia`). None = nie znamy
    (rachunek papierowy) i zostaje `as_of`, jak dotad.

    DWIE LICZBY PROWIZJI, BO DWIE KOLUMNY ODPOWIADAJA NA DWA ROZNE PYTANIA

    `prowizja` idzie do `exec.position.fees_paid` i MUSI byc w walucie
    kwotowanej: `update_equity` sumuje `fees_paid` jako liczby bez jednostki,
    wiec 0,0003395 wpisane wprost odejmowaloby od kapitalu 0,00034 zamiast
    0,93 — zanizenie ~2700x, i to w strone, ktora ZAWYZA kapital, wiec
    obsuniecie wychodzi mniejsze, a kill-switch strzela pozniej.

    `prowizja_surowa` idzie do `exec.fill.fee` i MUSI byc w tej walucie, ktora
    stoi obok w `exec.fill.fee_ccy`. Ta para jest czytana jako para:
    `gielda_exec.sprzatanie` odtwarza z niej ilosc BRUTTO wzorem
    `f.qty + f.fee WHEN upper(f.fee_ccy) = upper(i.base_ccy)`. Wpisanie tam
    kwoty przeliczonej na walute kwotowana obok `fee_ccy` = ETH dalo by dla
    produkcyjnego wejscia ETHUSDC brutto 0,3392 + 0,9308 = 1,27 ETH przeciwko
    `cumExecQty` 0,3395 — czyli ogromna, nieistniejaca „pozycje poza
    dziennikiem" z czujki, ktora istnieje po to, zeby lapac prawdziwe.

    Do 2026-09-23 obie kolumny dostawaly te sama liczbe i nic sie nie dzialo,
    bo `fee_ccy` bylo ZAWSZE walutą kwotowaną — przez wade, ktora wlasnie
    zniknela (`gielda_exec.prowizja_z_rejestru`). Rachunek papierowy nalicza
    prowizje w walucie kwotowanej i dla niego obie liczby sa rowne, dlatego
    `prowizja_surowa=None` zostawia zachowanie dokladnie takie jak dotad.

    DLACZEGO TO JEST WSPOLNE, A NIE SKOPIOWANE

    Symulacja i produkcja roznia sie WYLACZNIE tym, SKAD biora ilosc, cene
    i prowizje: symulacja wylicza je z zalozonych stawek, produkcja odczytuje
    z odpowiedzi gieldy. Sposob zapisu musi byc ten sam, bo dwie kopie tej samej
    reguly rozjechalyby sie przy pierwszej zmianie - a roznica byla by
    niewidoczna dokladnie tam, gdzie backtest ma mierzyc produkcje.

    `meta` niesie, ktore zrodlo wpisalo ten wiersz. Bez tego pierwsze pytanie
    przy analizie wyniku - "czy to bylo naprawde, czy policzone" - nie ma
    odpowiedzi w danych.

    `order_id` PODAJEMY, GDY ZLECENIE JUZ ISTNIEJE. Na gieldzie wiersz zlecenia
    powstaje przy ZLOZENIU, a ta funkcja jest wolana dopiero przy rozstrzygnieciu
    - wiec bez tego parametru jeden plan zostawialby DWA wiersze w exec.order.
    Nie jest to szczegol ksiegowy: `exec.order` przestaje wtedy znaczyc "jedno
    zlecenie", uzgodnienie liczy podwojnie, a czujka zdrowia egzekucji widzi
    dwa razy wiecej przyjec, niz bylo. Rachunek papierowy zadnego zlecenia
    wczesniej nie sklada, wiec tam wiersz powstaje tutaj.
    """
    if order_id is None:
        cur.execute(
            "INSERT INTO exec.order (decision_id, planned_order_id, engine, mode,"
            " instrument_id, venue_order_id, client_order_id, side, order_type, qty,"
            " limit_price, status, submitted_at, raw)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'filled',%s,%s) RETURNING id",
            (decision_id, planned_order_id, engine, mode, instrument_id,
             venue_order_id, client_order_id or f"tai-{decision_id}", side,
             meta.get("order", "market"), qty, cena, as_of, json.dumps(meta)))
        oid = cur.fetchone()["id"]
    else:
        oid = order_id
        # `qty` I `limit_price` ZOSTAJA NIETKNIETE - TO SA FAKTY SPRZED
        # WYPELNIENIA.
        #
        # Wiersz powstal przy ZLOZENIU i trzyma to, co giełda dostała: ilosc
        # wyslana i cene wyslana. Nadpisanie ich cena i iloscia WYKONANIA
        # kasowalo jedyny zapis naszego zobowiazania, i kosztowalo trzy razy:
        #
        #   * `poslizg.cena_odniesienia` bierze w pierwszej kolejnosci
        #     `o.limit_price` (`poslizg.py`, ZAPYTANIE: `o.limit_price AS
        #     cena_zlecenia`), a dwie linie nizej ta sama `cena` trafia do
        #     `exec.fill.price`. Poslizg kazdego wejscia gieldowego wychodzil
        #     wiec dokladnie (x - x)/x = 0. Silnik sklada PostOnly, czesc wejsc
        #     przechodzi przez spread jako taker po gorszej cenie, a modul
        #     meldowal „mediana +0.00 pb — NIEODROZNIALNE OD ZALOZENIA", czyli
        #     formalne potwierdzenie `POSLIZG_MAKER = 0` zbudowane z echa
        #     wlasnego UPDATE-a. Jest to ta sama klasa wady, przed ktora
        #     ostrzega naglowek `poslizg.py` przy rachunku papierowym:
        #     liczba wygladajaca na pomiar egzekucji, a bedaca pomiarem nas
        #     samych;
        #   * `poslizg.rozjazd_ksiag` liczyl dla wejsc plan kontra cena
        #     WYPELNIENIA zamiast plan kontra cena WYSLANA - czyli mierzyl co
        #     innego, niz ma w nazwie;
        #   * `RiskKernel._open_exposure` (krok 2) wycenia zlecenia
        #     nierozstrzygniete przez `o.qty * o.limit_price`, wiec podmiana
        #     obu liczb naraz przesuwala tez ekspozycje.
        #
        # Cena i ilosc wykonania maja swoje miejsce - `exec.fill.price`
        # i `exec.fill.qty`, zakladane dwie instrukcje nizej. Rachunek papierowy
        # zadnego zlecenia wczesniej nie sklada, wiec jego wiersz powstaje
        # galezia wyzej i tam obie liczby sa cenami wypelnienia z zalozenia.
        cur.execute(
            "UPDATE exec.order SET status='filled',"
            " venue_order_id = COALESCE(%s, venue_order_id), updated_at=%s,"
            " raw = raw || %s WHERE id=%s",
            (venue_order_id, as_of, json.dumps(meta), oid))
    cur.execute(
        "INSERT INTO exec.fill (order_id, venue_fill_id, qty, price, fee, fee_ccy, event_time)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s)",
        # WALUTA PROWIZJI Z GIELDY, gdy ja podala. Rachunek papierowy nalicza
        # prowizje w walucie kwotowanej i tak zostaje; produkcja na spocie placi
        # ja czesto w kupionym aktywie i wpisanie tam quote_ccy zanizaloby koszt
        # wejscia o dwa rzedy wielkosci.
        #
        # KWOTA I WALUTA MUSZA POCHODZIC Z TEGO SAMEGO ODCZYTU - patrz docstring.
        (oid, venue_fill_id or f"paper-{oid}", qty, cena,
         prowizja if prowizja_surowa is None else prowizja_surowa,
         prowizja_ccy or quote_ccy, _czas_wypelnienia(czas_wykonania, as_of)))
    cur.execute(
        "INSERT INTO exec.position (engine, mode, instrument_id, side, qty, avg_price,"
        " opened_at, fees_paid, open_decision_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (engine, mode, instrument_id, "long" if side == "buy" else "short",
         qty, cena, as_of, prowizja, decision_id))
    return oid


def trwale_niesprzedawalna(ilosc: Decimal, f: Filtry) -> bool:
    """Czy tej ilosci nie da sie sprzedac przy ZADNEJ cenie. CZYSTA FUNKCJA.

    KRYTERIUM MUSI BYC TRWALE, A NIE CHWILOWE — i to jest cala tresc tej funkcji

    Odpowiedz „nie da sie sprzedac" kosztuje pozycje: wolajacy domyka na jej
    podstawie wiersz i spisuje resztke na strate po cenie wejscia. Wolno wiec
    odpowiedziec TAK tylko wtedy, gdy zadna zmiana rynku tego nie odwroci:

      * ilosc znika po zaokragleniu do kroku (`lot_size`) — krok jest regula
        instrumentu i cena go nie rusza,
      * ilosc lezy ponizej minimum ILOSCI gieldy — tak samo.

    `min_notional` (minimum WARTOSCI) tu NIE WCHODZI, choc `sprawdz_zlecenie`
    go sprawdza i choc odrzuci dzis takie zlecenie. Resztka lezaca na kroku,
    dzis warta za malo, jutro po wzroscie ceny sprzeda sie normalnie —
    a spisanie jej na strate byloby wyrzuceniem zywych pieniedzy. Przy regulach
    produkcyjnych nie jest to drobiazg: ADAUSDC ma krok 0,1 i `min_notional`
    5 USDC, wiec przy cenie 0,36 prog wartosci to 13,9 ADA, czyli 139 PELNYCH
    krokow. Kryterium wartosciowe spisywaloby na strate resztki do ~5 USDC przy
    KAZDYM wyjsciu czesciowym, a aktywo zostawaloby na gieldzie poza ewidencja
    — czyli pozycja-widmo stworzona wlasna reka (naglowek `uzgodnienie.py`).
    Nadzor by tego nie zglosil: prog `uzgodnienie.progi_bezwzgledne` to
    `max(lot_size, min_notional/cena)`, czyli DOKLADNIE ten sam prog, ktory
    resztke spisal.

    BRAK REGUL TO NIE JEST ODPOWIEDZ „NIE DA SIE". Niekompletne `Filtry`
    (gole `Filtry()`, ktore `gielda_exec` podstawia za instrument nieobecny
    w mapie regul, albo `min_notional` NULL w `ref.instrument`) sprawia, ze
    `sprawdz_zlecenie` odmawia KAZDEJ ilosci — a przez to kazde wyjscie
    czesciowe domykaloby pozycje i spisywalo na strate CALA reszte, bez
    ograniczenia co do wielkosci. Zmierzone na funkcjach produkcyjnych:
    pozycja 1996,7013 ADA, wyjscie czesciowe 1000,0, reguly nieznane ->
    `realized_pnl` += -338,85 zamiast +10,00, a 996,7 ADA (~359 USDC) znika
    z ksiag i zostaje na gieldzie.

    JEDNA FUNKCJA DLA DWOCH WOLAJACYCH. `gielda_exec.zloz_plan` zadaje to samo
    pytanie o CALA pozycje przed wyslaniem, a `zapisz_wyjscie` o resztke po
    sprzedazy. Dwie wlasne kopie rozjechalyby sie przy pierwszej zmianie regul
    gieldy — i wlasnie tak bylo: kopia w `gielda_exec` pytala o kryterium
    trwale, a `zapisz_wyjscie` wolalo `czy_zamykalna`, czyli kryterium
    chwilowe, bez straznika na niekompletne reguly.
    """
    if f is None or not f.kompletne:
        return False
    i = do_kroku_w_dol(ilosc, f.krok_ilosci)
    return i <= 0 or (f.min_ilosc is not None and i < f.min_ilosc)


def zapisz_wyjscie(cur, position: dict, cena: Decimal, prowizja: Decimal,
                   as_of: datetime, decision_id: int,
                   ilosc: Decimal | None = None, *,
                   meta: dict | None = None,
                   order_id: int | None = None,
                   planned_order_id: int | None = None,
                   venue_order_id: str | None = None,
                   client_order_id: str | None = None,
                   venue_fill_id: str | None = None,
                   prowizja_ccy: str | None = None,
                   filtry: Filtry | None = None,
                   czas_wykonania: datetime | None = None) -> Decimal:
    """Zamkniecie pozycji, w calosci albo w czesci. Wspolne dla obu zrodel.

    WYJSCIE CZESCIOWE JEST NAJGORSZYM MOMENTEM NA UPROSZCZENIE. Zlecenie
    sprzedazy, ktore weszlo w polowie, zostawia na gieldzie polowe pozycji.
    Zamkniecie calego wiersza byloby wtedy zapisaniem, ze nie mamy niczego -
    czyli stworzeniem pozycji-widma, i to najgorszego rodzaju, bo o pozycji,
    ktorej przestalibysmy pilnowac. Risk kernel nie liczylby z niej ekspozycji,
    a stop na obsunieciu nie zadzialalby na cos, czego nie widzi.

    Czesc pozostala ZACHOWUJE `avg_price`. Cena wejscia nie zmienia sie od tego,
    ze sprzedalismy polowe - zmienia sie tylko ilosc.

    WYJSCIE ZOSTAWIA WIERSZ W `exec.order` I `exec.fill`, TAK SAMO JAK WEJSCIE

    Do 2026-09-09 ta funkcja dotykala wylacznie `exec.position`. Skutek zmierzony
    na produkcji: 18 pozycji zamknietych, 18 wierszy `exec.order` i 18 `exec.fill`
    - SAME WEJSCIA. Prowizji wyjsc bylo 9,3335 z 17,4353 USDC, czyli 53,5%
    calego kosztu, i nie istnialy w zadnym wierszu wypelnienia.

    To nie jest brak wygodnego widoku, tylko dziura w rejestrze faktow. Wszystko,
    co mierzy egzekucje, stoi na `exec.fill`, bo tylko tam jest zapisane, PO ILE
    naprawde poszlo - `exec.position` trzyma cene wejscia i wynik, nie cene
    wyjscia. Trzy rzeczy widzialy wiec polowe transakcji i nie mialy jak sie
    o tym dowiedziec:

      * pomiar poslizgu (services/engine/poslizg.py) - liczyl poslizg wejsc
        i nazywal go kosztem, choc runda ma dwie nogi;
      * uzgodnienie z gielda - odtwarza pozycje z wypelnien, wiec kazde wyjscie
        bylo dla niego rozjazdem sald bez przyczyny w naszej ksiedze;
      * czujka zdrowia egzekucji - liczyla polowe zdarzen.

    Cena wyjscia nie da sie odtworzyc wstecz z niczego innego: `realized_pnl`
    jest po prowizji i po zaokragleniu, a przy wyjsciu czesciowym sumuje sie
    z kilku wypelnien.

    SKAD `engine`, `mode` I `instrument_id`: DOCZYTUJEMY JE Z WIERSZA POZYCJI,
    a nie bierzemy z parametru. Wolajacy podaje `position` w roznym ksztalcie
    (sciezka gieldowa i papierowa czytaja inne kolumny), wiec przepisanie tych
    trzech pol do sygnatury oznaczaloby, ze zapis zlecenia zalezy od tego, kto
    go wola. Tabela pozycji jest jedynym zrodlem prawdy o tym, czyja jest pozycja,
    i pozostanie nim takze wtedy, gdy ktos doda trzecia sciezke wyjscia.

    Osobny SELECT, a nie `INSERT ... SELECT` w jednej instrukcji, i to jest
    wybor, nie niedopatrzenie. Atrapa bazy (tests/atrapa_bazy.py) sprawdza
    NOT NULL i `UNIQUE (mode, client_order_id)` wiazac parametry pozycyjne
    z kolumnami z migracji - a wartosci schowanych w SELECT-cie nie widzi
    zadna. Zapis w formie, ktorej atrapa nie potrafi zweryfikowac, wypadlby
    spod tej ochrony po cichu: testy nadal by przechodzily, a wiersz odrzucalaby
    dopiero produkcja. To jest dokladnie ta klasa bledu, dla ktorej ta atrapa
    powstala.

    `order_id` PODAJEMY, GDY ZLECENIE JUZ ISTNIEJE - dokladnie jak w
    `zapisz_wejscie`. Na gieldzie wiersz zlecenia powstaje przy ZLOZENIU, wiec
    bez tego parametru jedno wyjscie zostawialoby DWA wiersze w `exec.order`.
    Rachunek papierowy zadnego zlecenia wczesniej nie sklada, wiec tam wiersz
    powstaje tutaj.

    `filtry` DOMYKA POZYCJE, Z KTOREJ ZOSTAL PYL NIE DO SPRZEDANIA

    Warunek `ile >= calosc` zaklada, ze da sie sprzedac DOKLADNIE tyle, ile
    stoi na pozycji. Na spocie jest to nieprawda i to nie sporadycznie, tylko
    ZA KAZDYM RAZEM. Kupno placi prowizje w kupionym aktywie, wiec
    `egzekucja.rozlicz_prowizje` zapisuje na `exec.position.qty` ilosc NETTO,
    ktora z definicji NIE lezy na kroku ilosci gieldy. Wyjscie planuje cala te
    ilosc, a `zlecenia.sprawdz_zlecenie` zaokragla ja W DOL do kroku (w gore
    lamaloby limit risk kernela) - czyli sprzedajemy zawsze mniej, niz mamy.

    Zmierzone na produkcyjnych funkcjach, ADAUSDC, `lot_size` 0.1, maker 0,1%:

        wypelnienie wejscia 1998.7  ->  exec.position.qty = 1996.7013
        wyjscie: wysylamy 1996.7    ->  reszta na pozycji  0.0013
        nastepna swieca: sprawdz_zlecenie(0.0013) -> "ilosc po zaokragleniu
            do kroku 0.1 wynosi zero" -> plan odrzucony

    `ile >= calosc` jest wtedy falszem, `closed_at` zostaje NULL NA ZAWSZE,
    `open_position` dalej oddaje ten wiersz, a petla decyzyjna przy niepustej
    pozycji wybiera wylacznie `exit` albo `hold` - NOWEGO WEJSCIA NA TYM
    INSTRUMENCIE NIE PODEJMIE JUZ NIGDY. Po jednej rundzie uniwersum handlowe
    jest puste, a w logu wyglada to na rynek bez sygnalow. Nadzor tego nie
    zglosi: `uzgodnienie.progi_bezwzgledne` istnieje wlasnie po to, zeby
    resztke ponizej `lot_size` wyciszyc - pyl jest wiec jednoczesnie
    NIEWIDOCZNY dla nadzoru i BLOKUJACY dla silnika.

    Poprzednia poprawka (zapis netto zamiast brutto) usunela pierwsze wcielenie
    tego bledu - pozycje, ktorej gielda odmawiala sprzedazy za brak salda -
    i stworzyla drugie, o krok dalej. Kryterium musi wiec byc NIE "czy
    sprzedalismy wszystko", tylko "czy to, co zostalo, da sie jeszcze
    sprzedac". Odpowiada na to `zlecenia.czy_zamykalna` - ta sama funkcja,
    ktorej `egzekucja.co_z_wypelnieniem` uzywa do rozpoznania wypelnienia
    `nie_do_zamkniecia`. Dwie wlasne kopie tego pytania rozjechalyby sie przy
    pierwszej zmianie regul gieldy.

    BEZ `filtry` ZACHOWANIE JEST STARE. Regul gieldy nie wolno zgadywac:
    domyslne `Filtry()` uznaloby kazda resztke za niezamykalna i domykalo
    pozycje, z ktorych da sie jeszcze wyjsc. Wolajacy, ktory regul nie ma
    (rachunek papierowy sprzedaje calosc, wiec ich nie potrzebuje), dostaje
    dokladnie to, co dostawal.

    PYL JEST SPISYWANY NA STRATE, a nie znika po cichu. `update_equity` liczy
    kapital jako `wplaty + realized - fees + unrealized`, gdzie `unrealized`
    bierze sie WYLACZNIE z pozycji otwartych. Samo `closed_at` bez zapisu
    wyniku skasowaloby wiec niezrealizowana strate na pyle i PODNIOSLO kapital
    w chwili, w ktorej cos tracimy - czyli obsuniecie wyszloby mniejsze,
    a kill-switch strzelalby pozniej. Ksiegujemy zatem pyl po cenie wejscia:
    zaplacilismy za niego i nie dostaniemy za niego nic.

    TYLKO POZYCJE DLUGIE. Resztka pozycji krotkiej to ZOBOWIAZANIE, ktorego nie
    zdolalismy odkupic - zamkniecie wiersza zdjeloby je z ksiag, choc nadal
    istnieje. Na spocie krotkich nie ma (`koszty(kind).krotka_pozycja`), wiec
    to zastrzezenie nic dzis nie kosztuje, a bez niego pierwsza pozycja krotka
    ginelaby z ewidencji.
    """
    calosc = Decimal(position["qty"])
    ile = calosc if ilosc is None else min(ilosc, calosc)
    entry = Decimal(position["avg_price"])
    diff = (cena - entry) if position["side"] == "long" else (entry - cena)
    pnl = diff * ile

    # ZAPIS WYPELNIENIA IDZIE PRZED ZMIANA POZYCJI, zeby `INSERT ... SELECT`
    # czytal wiersz pozycji w stanie sprzed zamkniecia. Po `closed_at` pozycja
    # nadal istnieje, ale kolejnosc "najpierw fakt, potem jego skutek" jest
    # tutaj takze porzadkiem czytania.
    #
    # ZEROWEJ ILOSCI NIE KSIEGUJEMY. Wypelnienie na 0 nie jest zdarzeniem, a
    # wiersz w `exec.fill` z qty=0 zanizalby kazda srednia liczona po tej tabeli
    # i podbijal licznik wypelnien czujki zdrowia egzekucji.
    oid = order_id
    if ile > 0:
        meta = dict(meta or {})
        cur.execute(
            "SELECT p.engine, p.mode, p.instrument_id, p.side, i.quote_ccy"
            " FROM exec.position p JOIN ref.instrument i ON i.id = p.instrument_id"
            " WHERE p.id=%s", (position["id"],))
        p = cur.fetchone()
        if oid is None:
            cur.execute(
                "INSERT INTO exec.order (decision_id, planned_order_id, engine, mode,"
                " instrument_id, venue_order_id, client_order_id, side, order_type, qty,"
                " limit_price, status, submitted_at, raw)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'filled',%s,%s) RETURNING id",
                (decision_id, planned_order_id, p["engine"], p["mode"],
                 p["instrument_id"], venue_order_id,
                 client_order_id or f"tai-wyjscie-{decision_id}",
                 # STRONA ZLECENIA JEST ODWROTNA DO STRONY POZYCJI. Wyjscie
                 # z pozycji dlugiej to sprzedaz, z krotkiej - odkup. Wpisanie
                 # tu strony pozycji zrobiloby z kazdego wyjscia drugie wejscie:
                 # uzgodnienie odtwarza salda z wypelnien, wiec liczyloby nam
                 # ekspozycje podwojna zamiast zerowej.
                 "sell" if p["side"] == "long" else "buy",
                 meta.get("order", "market"), ile, cena, as_of, json.dumps(meta)))
            oid = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO exec.fill (order_id, venue_fill_id, qty, price, fee, fee_ccy,"
            " event_time) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            # WALUTA PROWIZJI: z gieldy, gdy ja podala, inaczej waluta kwotowana
            # instrumentu. Zaden z wolajacych wyjscie jej nie zna, a NULL w tym
            # miejscu zmienialby prowizje w liczbe bez jednostki - czyli w cos,
            # czego nie da sie zsumowac z pozostalymi kosztami.
            (oid, venue_fill_id or f"paper-{oid}", ile, cena, prowizja,
             prowizja_ccy or p["quote_ccy"],
             _czas_wypelnienia(czas_wykonania, as_of)))

    # CZY TO, CO ZOSTAJE NA POZYCJI, DA SIE JESZCZE SPRZEDAC.
    #
    # `pyl` jest ustawiany tylko wtedy, gdy odpowiedz brzmi NIE - i tylko wtedy
    # domykamy wiersz mimo niepelnej sprzedazy. Warunek `ile > 0` jest tu
    # istotny: wywolanie z iloscia zero nie jest wyjsciem, a bez niego
    # zamkneloby zywa pozycje, ktorej nikt nie probowal sprzedac.
    #
    # KRYTERIUM Z `trwale_niesprzedawalna`, A NIE Z `czy_zamykalna`. To drugie
    # przewraca sie takze na `min_notional`, czyli na CENIE — a resztka warta
    # dzis za malo jutro sprzeda sie normalnie. Odpowiada tez „nie da sie" na
    # reguly NIEZNANE, wiec gole `Filtry()` domykalo pozycje z dowolnie duza
    # reszta. Pelny rachunek obu skutkow: docstring `trwale_niesprzedawalna`.
    pyl = None
    if (ile > 0 and ile < calosc
            and position["side"] == "long"
            and trwale_niesprzedawalna(calosc - ile, filtry)):
        pyl = calosc - ile

    # PYL PO CENIE WEJSCIA IDZIE W WYNIK. Uzasadnienie w docstringu: samo
    # `closed_at` skasowaloby niezrealizowana strate na resztce i zawyzylo
    # kapital dokladnie w chwili, w ktorej cos tracimy.
    wynik = pnl if pyl is None else pnl - entry * pyl

    if ile >= calosc or pyl is not None:
        cur.execute(
            "UPDATE exec.position SET closed_at=%s,"
            " realized_pnl = COALESCE(realized_pnl,0) + %s,"
            " fees_paid = fees_paid + %s, close_decision_id=%s WHERE id=%s",
            (as_of, wynik, prowizja, decision_id, position["id"]))
        if pyl is not None and oid is not None:
            # SLAD ZOSTAJE W WIERSZU ZLECENIA, nie tylko na ekranie.
            #
            # `exec.position` nie ma kolumny na uwagi (migracja 0005), a
            # dolozenie jej to osobna decyzja - wiersz zlecenia wyjscia jest
            # jedynym miejscem, w ktorym ten fakt da sie dzis zapisac trwale
            # i w ktorym ktokolwiek go szuka. Bez tego zapisu domkniecie
            # pozycji z resztka wygladaloby identycznie jak domkniecie pelne,
            # a roznica miedzy nimi to aktywo, ktore nadal lezy na gieldzie.
            #
            # COALESCE na obu kolumnach, zeby nie skasowac powodu wpisanego
            # wczesniej (kill-switch, nieudane anulowanie) ani nie wywrocic
            # sie na `raw` bedacym NULL-em.
            cur.execute(
                "UPDATE exec.order SET reject_reason = COALESCE(reject_reason, %s),"
                " raw = COALESCE(raw, '{}'::jsonb) || %s WHERE id=%s",
                (f"pozycja domknięta z resztką {pyl} — tej ilości nie da się "
                 f"sprzedać na tej giełdzie (krok {filtry.krok_ilosci}, minimum "
                 f"wartości {filtry.min_wartosc}); resztka zostaje na giełdzie "
                 f"i jest spisana na stratę po cenie wejścia {entry}",
                 json.dumps({"resztka_nierozliczalna": str(pyl),
                             "resztka_spisana_po": str(entry)}), oid))
    else:
        cur.execute(
            "UPDATE exec.position SET qty = qty - %s,"
            " realized_pnl = COALESCE(realized_pnl,0) + %s,"
            " fees_paid = fees_paid + %s WHERE id=%s",
            (ile, pnl, prowizja, position["id"]))
    # ZWRACAMY TO, CO NAPRAWDE ZAKSIEGOWALISMY. Przy domknieciu z pylem `wynik`
    # jest mniejszy od `pnl` o spisana resztke; oddanie `pnl` pokazywaloby
    # wolajacemu inna liczbe niz ta, ktora trafila do `realized_pnl`.
    return wynik


def execute_paper(cur, engine: str, mode: str, instrument_id: int, decision_id: int,
                  side: str, qty: Decimal, price: Decimal, as_of: datetime,
                  kind: str, limit: bool, quote_ccy: str,
                  planned_order_id: int | None = None) -> None:
    """Symulacja wypelnienia po realnej cenie, z prowizja i poslizgiem.

    Przy egzekucji z limitem ta funkcja jest wolana DOPIERO po stwierdzeniu, ze
    zlecenie sie wypelnilo (rozstrzygnij_plany), a cena wypelnienia to cena
    limitu - bez poslizgu, bo zlecenie nie przechodzilo przez spread.
    """
    fee_rate, slip = stawki(kind, limit)
    fill = price * (Decimal(1) + slip) if side == "buy" else price * (Decimal(1) - slip)
    fee = qty * fill * fee_rate
    zapisz_wejscie(
        cur, engine, mode, instrument_id, decision_id, side, qty, fill, fee,
        as_of, quote_ccy,
        {"execution": "internal_paper", "kind": kind,
         "order": "limit" if limit else "market",
         "fee_rate": str(fee_rate), "slippage": str(slip),
         "wypelnienie": ("sprawdzone na swiecy T+1 (plan "
                         f"{planned_order_id})" if planned_order_id
                        else "natychmiastowe")},
        planned_order_id=planned_order_id)


def close_paper(cur, position: dict, price: Decimal, as_of: datetime,
                decision_id: int, kind: str, limit: bool,
                planned_order_id: int | None = None) -> Decimal:
    """Symulacja wyjscia. Lustro `execute_paper` - z ta sama `meta`.

    `meta` NIE JEST OZDOBA. Pomiar poslizgu odrzuca wiersze rachunku papierowego
    po `raw->>'execution'`, bo cena wypelnienia jest tam z zalozenia rowna
    `cena * (1 +- POSLIZG)` - policzenie z nich poslizgu oddaloby stala, ktora
    sami wpisalismy, i wygladalo na jej potwierdzenie. Wyjscie bez tego znacznika
    weszloby do pomiaru jako obserwacja gieldowa i unieważniłoby go.
    """
    fee_rate, slip = stawki(kind, limit)
    qty = Decimal(position["qty"])
    exit_px = price * (Decimal(1) - slip) if position["side"] == "long" \
        else price * (Decimal(1) + slip)
    fee = qty * exit_px * fee_rate
    return zapisz_wyjscie(
        cur, position, exit_px, fee, as_of, decision_id,
        planned_order_id=planned_order_id,
        meta={"execution": "internal_paper", "kind": kind,
              "order": "limit" if limit else "market",
              "fee_rate": str(fee_rate), "slippage": str(slip),
              "wypelnienie": ("sprawdzone na swiecy T+1 (plan "
                              f"{planned_order_id})" if planned_order_id
                              else "natychmiastowe")})


def zamknij_pozycje_papierowe(conn, engine: str, mode: str, kind: str,
                              now: datetime | None = None) -> list[dict]:
    """Zamyka NA PAPIERZE pozycje otwarte na rachunku papierowym, zanim silnik
    zacznie wysylac zlecenia na gielde. Zwraca liste zamknietych.

    DLACZEGO TO ISTNIEJE. Pozycja z rachunku papierowego istnieje tylko
    w naszej bazie - gielda jej nie zna. Silnik przelaczony na `--egzekucja
    gielda` widzialby ja jak kazda inna: wyslalby wyjscie, gielda odrzucilaby
    sprzedaz aktywa, ktorego konto nie ma, a uzgodnienie zglosiloby
    pozycje-widmo i zatrzymalo wejscia. 13.09.2026 dokladnie ten stan skonczyl
    sie zejsciem CALEGO silnika z powrotem na papier i wyciszeniem uzgodnienia
    w demo - czyli utrata jedynego pomiaru poslizgu, dla ktorego tryb demo
    w ogole istnieje.

    CO ZAMYKA: wylacznie pozycje, ktorych zlecenie otwierajace ma
    `raw->>'execution' = 'internal_paper'`. Pozycja otwarta NA GIELDZIE zostaje
    nietknieta - gielda ja ma i to ona ja zamknie.

    JAK: po ostatniej cenie minutowej, przez `close_paper` (ta sama sciezka co
    zwykle wyjscie papierowe, z tym samym znacznikiem `internal_paper`, wiec
    pomiar poslizgu nadal to odrzuci). Decyzja w dzienniku jest jawna: akcja
    `exit`, uzasadnienie mowi wprost, ze to zamkniecie administracyjne przy
    przelaczeniu egzekucji, `risk_reason` tak samo. Dziennik jest append-only
    i ma pozostac prawdziwy: ta pozycja NIE zostala zamknieta przez model.
    """
    now = now or datetime.now(timezone.utc)
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        "SELECT p.*, i.symbol"
        " FROM exec.position p"
        " JOIN ref.instrument i ON i.id = p.instrument_id"
        " WHERE p.engine::text = %s AND p.mode::text = %s AND p.closed_at IS NULL"
        "   AND EXISTS (SELECT 1 FROM exec.\"order\" o"
        "               WHERE o.decision_id = p.open_decision_id"
        "                 AND o.raw->>'execution' = 'internal_paper')"
        " ORDER BY p.id",
        (engine, mode))
    pozycje = cur.fetchall()
    zamkniete: list[dict] = []
    for p in pozycje:
        cena = cena_odniesienia(cur, p["instrument_id"], now)
        if cena["cena"] is None:
            # Bez ceny nie ma uczciwego zamkniecia - lepiej zostawic pozycje
            # i powiedziec o tym glosno, niz zaksiegowac wyjscie po zerze.
            print(f"  {p['symbol']}: brak ceny minutowej - pozycji papierowej "
                  f"NIE zamykam, silnik nie wystartuje")
            raise RuntimeError(
                f"pozycja papierowa {p['symbol']} (id={p['id']}) bez ceny - "
                f"nie da sie jej zamknac przed przelaczeniem na gielde")
        cur.execute(
            """
            INSERT INTO journal.decision (engine, mode, instrument_id, decided_at, action,
                target_qty, target_price, confidence, regime, feature_snapshot,
                risk_verdict, risk_reason, rationale, rationale_model, latency_ms)
            VALUES (%s,%s,%s,%s,'exit',%s,%s,0,'n/d','{}','allow',
                    'zamkniecie administracyjne - przelaczenie egzekucji papier -> gielda',
                    %s, 'administracyjne', 0)
            RETURNING id
            """,
            (engine, mode, p["instrument_id"], now, p["qty"], cena["cena"],
             f"Pozycja otwarta na rachunku papierowym ({p['side']} {p['qty']} "
             f"{p['symbol']} @ {p['avg_price']}) zamknieta administracyjnie po "
             f"ostatniej cenie minutowej {cena['cena']} (wiek {cena['wiek_min']:.0f} min), "
             f"bo silnik przechodzi na egzekucje gieldowa, a gielda tej pozycji nie ma. "
             f"To NIE jest decyzja modelu."))
        did = cur.fetchone()["id"]
        close_paper(cur, p, cena["cena"], now, did, kind, limit=False)
        conn.commit()
        zamkniete.append({"id": p["id"], "symbol": p["symbol"], "side": p["side"],
                          "qty": p["qty"], "cena": cena["cena"], "decision_id": did})
        print(f"  {p['symbol']}: pozycja papierowa {p['side']} {p['qty']} zamknieta "
              f"administracyjnie @ {cena['cena']} (decyzja {did})")
    return zamkniete


def zapisz_wyjscie_bez_pozycji(cur, engine: str, mode: str, plan: dict,
                               cena: Decimal, wypelniono: datetime,
                               now: datetime) -> str:
    """Slad po wyjsciu, ktore rozstrzygnelo sie, a nie mialo czego zamknac.

    TEN SAM STAN CO `wyjscie_bez_pozycji` W `gielda_exec.rozstrzygnij_plan`,
    nazwany tak samo. Rozjazd nazw miedzy rachunkiem papierowym a produkcja
    ukrylby ten przypadek dokladnie tam, gdzie sie go szuka: w porownaniu obu
    sciezek. Trener porownuje wynik z rachunkiem papierowym, wiec kazdy stan,
    ktory produkcja umie nazwac, a symulacja przemilcza, przesuwa porownanie
    w strone "symulacja wyglada lepiej".

    DLACZEGO WPIS DO `exec.order`, A NIE DO `exec.fill` I `exec.position`

    Doksiegowac sie tego NIE DA i NIE WOLNO ZGADYWAC. Zamkniecie dowolnej innej
    otwartej pozycji na tym instrumencie przypisaloby sprzedaz cudzej decyzji
    i sfalszowalo jej wynik. Zapisujemy wiec tylko to, co wiemy, tam, gdzie
    ktos tego szuka - w wierszu zlecenia, ze statusem 'rejected' ("nigdy sie
    nie zaksiegowalo") i z powodem czytelnym bez zagladania w kod.

    `client_order_id` jest WYPROWADZONY Z PLANU i rozny od `tai-{decision_id}`
    uzywanego przez `zapisz_wejscie`: gdyby ta sama decyzja zdazyla wczesniej
    otworzyc pozycje, drugi wiersz zlamalby UNIQUE (mode, client_order_id)
    i wywrocil cala transakcje cyklu - czyli anomalia jednego instrumentu
    kasowalaby rozstrzygniecie wszystkich pozostalych planow.
    """
    powod = ("wyjście rozstrzygnięte BEZ otwartej pozycji (position_id="
             f"{plan['trigger'].get('position_id')}): plan {plan['id']} "
             f"na {plan['qty']} @ {cena} — nie ma czego zamknąć")
    cur.execute(
        "INSERT INTO exec.order (decision_id, planned_order_id, engine, mode,"
        " instrument_id, client_order_id, side, order_type, qty, limit_price,"
        " status, reject_reason, submitted_at, raw)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,'limit',%s,%s,'rejected',%s,%s,%s)",
        (plan["decision_id"], plan["id"], engine, mode, plan["instrument_id"],
         f"tai-p{plan['id']}-bez-pozycji", plan["side"],
         Decimal(plan["qty"]), cena, powod, now,
         json.dumps({"execution": "internal_paper",
                     "anomalia": "wyjscie_bez_pozycji",
                     "position_id": plan["trigger"].get("position_id"),
                     "cena_wypelnienia": str(cena),
                     "wypelniono_o": wypelniono.isoformat()})))
    # PLAN JEST ROZSTRZYGNIETY, wiec nie moze zostac 'pending'. Swieca, ktora go
    # rozstrzygnela, jest juz domknieta i przy kazdym nastepnym cyklu dalaby te
    # sama odpowiedz - plan wracalby tu w kolko, bez szansy na zmiane wyniku.
    # 'expired' klamaloby w druga strone ("limit nie zostal trafiony"), wiec
    # zostaje 'triggered', tak samo jak na sciezce gieldowej.
    cur.execute("UPDATE journal.planned_order SET status='triggered',"
                " resolved_at=%s WHERE id=%s", (now, plan["id"]))
    return powod


def zaplanuj(cur, decision_id: int, instrument_id: int, side: str, qty: Decimal,
             limit_price: Decimal, h: Horizon, bar_start: datetime,
             rodzaj: str, position_id: int | None = None) -> None:
    """Zlecenie z limitem NIE jest wypelnieniem - jest planem na nastepna swiece.

    DLACZEGO ODROCZENIE, A NIE ZAPIS OD RAZU

    Do 2026-09-06 petla zapisywala wypelnienie natychmiast, PRZYJMUJAC, ze limit
    sie wypelnil. To jedyne miejsce, w ktorym rachunek papierowy byl
    optymistyczny - i to systematycznie w jedna strone, bo zlecenia, ktore sie
    nie wypelniaja, to zwykle te, przy ktorych rynek uciekl w nasza strone,
    czyli najbardziej zyskowne.

    Symulacja walk-forward modelowala to od poczatku (portfolio._wypelnione),
    produkcja nie - czyli krzywa kapitalu i wynik kalibracji opisywaly dwie
    rozne strategie.

    `journal.planned_order` istnieje w schemacie od migracji 0005 z polami
    `limit_price`, `valid_until` i statusem pending/triggered/expired.
    Zaprojektowano ja dokladnie na to i nigdy nie uzyto.
    """
    koniec_nastepnej = bar_start + timedelta(minutes=2 * h.bar_minutes)
    cur.execute(
        "INSERT INTO journal.planned_order (decision_id, instrument_id, side, qty,"
        " order_type, limit_price, trigger, valid_until, status)"
        " VALUES (%s,%s,%s,%s,'limit',%s,%s,%s,'pending')",
        (decision_id, instrument_id, side, qty, limit_price,
         json.dumps({"rodzaj": rodzaj, "position_id": position_id,
                     "bar": bar_start.isoformat(),
                     "regula": "kupno gdy low <= limit, sprzedaz gdy high >= limit"}),
         koniec_nastepnej))


def termin_rozstrzygniecia(plan: dict, h: Horizon,
                           now: datetime) -> tuple[datetime, bool]:
    """Ktora swieca rozstrzyga plan i czy juz na nia czas. CZYSTA FUNKCJA.

    WSPOLNA DLA RACHUNKU PAPIEROWEGO I DLA GIELDY. Obie sciezki musza pytac
    o los planu w tej samej chwili: gdyby jedna rozstrzygala wczesniej, backtest
    i produkcja ogladalyby inny wycinek swiecy - a roznica byla by niewidoczna,
    bo obie wersje wygladalyby na poprawne.

    Zwraca (swieca_rozstrzygajaca, czy_jeszcze_czekamy).
    """
    bar = datetime.fromisoformat(plan["trigger"]["bar"])
    sprawdzana = bar + timedelta(minutes=h.bar_minutes)
    czekamy = (sprawdzana + timedelta(minutes=h.bar_minutes) > now
               and now < plan["valid_until"])
    return sprawdzana, czekamy


def rozstrzygnij_plany(cur, engine: str, mode: str, h: Horizon, now: datetime,
                       kind: str, *, verbose: bool,
                       tylko_wyjscia: bool = False) -> dict:
    """Sprawdza, czy plany z poprzedniej swiecy doczekaly sie wypelnienia.

    Reguly nie powtarzamy - uzywamy `portfolio._wypelnione`, tej samej funkcji,
    ktora rozstrzyga to w symulacji. Dwie kopie tej samej reguly rozjechalyby
    sie przy pierwszej zmianie, a roznica bylaby niewidoczna.

    `verbose` I `tylko_wyjscia` SA TYLKO NAZWANE. Oba sa bool-ami stojacymi obok
    siebie na koncu listy, wiec `..., "spot", False, True)` nie dawalo sie
    przeczytac bez zagladania do definicji, a ich zamiana miejscami jest
    pomylka, ktorej nic nie zglasza: kill-switch przestaje zdejmowac plany
    wejscia i jednoczesnie milknie. Cichy bezpiecznik zamiast gadatliwego logu.

    `tylko_wyjscia` ma TE SAMA SEMANTYKE co w `gielda_exec.zloz_oczekujace`:
    stan po zadziałaniu kill-switcha. Wyjścia rozstrzygamy normalnie (kill-switch
    ma wyprowadzać z ryzyka, nie więzić w nim), a plany wejścia zdejmujemy —
    dokładnie tak, jak `gielda_exec.anuluj_wejscia` zdejmuje je z arkusza giełdy.
    """
    # `wyjscie_bez_pozycji` W SLOWNIKU OD POCZATKU, z zerem - tak samo jak na
    # sciezce gieldowej. Klucz pojawiajacy sie dopiero przy anomalii znaczylby,
    # ze jego brak i jego zero to dwie rozne rzeczy, a sa tym samym.
    #
    # `anulowane_wejscia` z tego samego powodu: zero znaczy "kill-switch nie
    # zdjal zadnego planu", a brak klucza znaczylby "nikt tego nie liczy".
    st = {"wypelnione": 0, "niewypelnione": 0, "czekaja": 0,
          "wyjscie_bez_pozycji": 0, "anulowane_wejscia": 0}
    # SILNIK I TRYB Z DECYZJI, KTORA PLAN ZRODZILA.
    #
    # `journal.planned_order` nie ma kolumn engine/mode (0005, linia 68), wiec
    # sam status 'pending' wybieral KAZDY plan w bazie - takze cudzego silnika
    # i cudzego trybu, ktory zaksiegowalby sie tutaj jako NASZA pozycja
    # (`execute_paper` dostaje `engine`/`mode` z argumentow petli, nie z planu).
    # Asymetria byla jednostronna: `RiskKernel._open_exposure` dochodzi do tych
    # samych wierszy przez `JOIN journal.decision d ... WHERE d.engine=%s AND
    # d.mode=%s` (services/engine/risk.py:393-402), wiec limit ekspozycji widzi
    # wylacznie WLASNE plany, a rozstrzyganie bralo WSZYSTKIE.
    #
    # KSZTALT JOIN-a JEST CELOWO TEN SAM CO W `_open_exposure`, zeby nie powstala
    # druga regula obok istniejacej. Denormalizacja engine/mode do
    # `planned_order` byloby prostsze w zapisie, ale to osobna decyzja
    # (migracja + backfill), a nie poprawka przy okazji.
    cur.execute(
        "SELECT p.id, p.decision_id, p.instrument_id, p.side, p.qty, p.limit_price,"
        "       p.trigger, p.valid_until, i.symbol, i.quote_ccy"
        " FROM journal.planned_order p JOIN ref.instrument i ON i.id = p.instrument_id"
        " JOIN journal.decision d ON d.id = p.decision_id"
        " WHERE d.engine = %s AND d.mode = %s"
        "   AND p.status = 'pending' ORDER BY p.id", (engine, mode))
    for plan in cur.fetchall():
        # KILL-SWITCH ZDEJMUJE PLANY WEJSCIA TAKZE NA RACHUNKU PAPIEROWYM.
        #
        # Sciezka gieldowa robi to przez `anuluj_wejscia`; papierowa nie robila
        # tego wcale, wiec po zadzialaniu kill-switcha wypelnialy sie tu plany
        # wejscia z poprzedniej swiecy - do `len(instrumenty)` nowych pozycji
        # JUZ PO tym, jak system uznal, ze handlowac nie wolno.
        #
        # Rozjazd szedl w strone "symulacja wyglada lepiej": rachunek papierowy
        # handlowal dalej tam, gdzie produkcja stawala, a to wlasnie symulacja
        # jest wzorcem kalibracji dla trenera.
        #
        # 'cancelled', A NIE 'expired'. `expired` znaczy w tej funkcji dokladnie
        # jedno: swieca nie dotknela limitu. Uzycie go tutaj wpisywaloby do
        # dziennika, ze limit nie zostal trafiony - czego nie sprawdzilismy
        # i co wprost klamie o przyczynie. To samo slowo, co na sciezce
        # gieldowej, bo to samo zdarzenie.
        if tylko_wyjscia and plan["trigger"].get("rodzaj") != "wyjscie":
            cur.execute("UPDATE journal.planned_order SET status='cancelled',"
                        " resolved_at=%s WHERE id=%s", (now, plan["id"]))
            st["anulowane_wejscia"] += 1
            if verbose:
                print(f"  [anulowane]    {plan['symbol']} {plan['side']}"
                      f" @ {float(plan['limit_price']):.6g} — wejścia wstrzymane")
            continue

        sprawdzana, czekamy = termin_rozstrzygniecia(plan, h, now)
        if czekamy:
            st["czekaja"] += 1
            continue

        swieca = zakres_swiecy(cur, plan["instrument_id"], h, sprawdzana)
        limit_px = float(plan["limit_price"])
        trafiony = bool(swieca) and _wypelnione(limit_px, swieca, kupno=plan["side"] == "buy")

        if not trafiony:
            # NIEWYPELNIENIE JEST WYNIKIEM, NIE BLEDEM. Przy wyjsciu oznacza, ze
            # pozycja ZOSTAJE OTWARTA - i to jest najdrozszy przypadek w calym
            # modelu, bo dotyczy rynku, ktory ucieka przeciwko nam.
            cur.execute("UPDATE journal.planned_order SET status='expired',"
                        " resolved_at=%s WHERE id=%s", (now, plan["id"]))
            st["niewypelnione"] += 1
            if verbose:
                print(f"  [niewypełnione] {plan['symbol']} {plan['side']} @ {limit_px:.6g}"
                      f" — {plan['trigger']['rodzaj']}")
            continue

        cena = Decimal(str(limit_px))
        # DATA WYPELNIENIA to zamkniecie swiecy, w ktorej limit zostal dotkniety,
        # a nie chwila, w ktorej petla to zauwazyla. Przy przestoju silnika
        # roznica bylaby dowolnie duza, a `opened_at` pozycji wchodzi do
        # dziennika i do liczenia czasu trzymania.
        wypelniono = sprawdzana + timedelta(minutes=h.bar_minutes)
        if plan["trigger"]["rodzaj"] == "wyjscie":
            poz = None
            if plan["trigger"].get("position_id"):
                cur.execute("SELECT id, side, qty, avg_price FROM exec.position"
                            " WHERE id=%s AND closed_at IS NULL",
                            (plan["trigger"]["position_id"],))
                poz = cur.fetchone()
            if poz:
                close_paper(cur, poz, cena, wypelniono, plan["decision_id"], kind, True,
                            planned_order_id=plan["id"])
            else:
                # WYJSCIE, KTORE NIE MA CZEGO ZAMKNAC. Pozycji nie ma, bo
                # zamknal ja w miedzyczasie ktos inny (dozor, kill-switch)
                # albo plan nigdy nie niosl `position_id`.
                #
                # Samo `if poz:` bez `else` zapisywalo plan jako 'triggered',
                # czyli "rozstrzygniety pomyslnie", doliczalo go do
                # `wypelnione` i nie zostawialo ani linijki na ekranie, ani
                # wiersza w bazie. Cisza jest tu grozniejsza niz na gieldzie:
                # rachunek papierowy jest wzorcem, z ktorym trener porownuje
                # produkcje, wiec przemilczany przypadek przesuwa porownanie
                # w strone "symulacja wyglada lepiej" - a taki rozjazd nie
                # wyglada na awarie, tylko na lepszy wynik.
                powod = zapisz_wyjscie_bez_pozycji(
                    cur, engine, mode, plan, cena, wypelniono, now)
                # NIE DOLICZAMY DO `wypelnione`, inaczej niz na sciezce
                # gieldowej - i ta roznica jest zamierzona. Tam aktywo NAPRAWDE
                # wyszlo z rachunku, wiec wypelnienie jest faktem mimo braku
                # pozycji. Tutaj nie powstal ani wiersz w `exec.fill`, ani
                # zmiana w `exec.position` - nic sie nie wypelnilo w zadnej
                # ksiedze, a policzenie tego jako wypelnienia zawyzaloby
                # licznik rachunku papierowego wobec produkcji.
                st["wyjscie_bez_pozycji"] += 1
                if verbose:
                    print(f"  [wyjscie_bez_pozycji] {plan['symbol']} "
                          f"{plan['side']} @ {limit_px:.6g} — {powod}")
                continue
        else:
            execute_paper(cur, engine, mode, plan["instrument_id"], plan["decision_id"],
                          plan["side"], Decimal(plan["qty"]), cena, wypelniono,
                          kind, True, plan["quote_ccy"], planned_order_id=plan["id"])
        cur.execute("UPDATE journal.planned_order SET status='triggered',"
                    " resolved_at=%s WHERE id=%s", (now, plan["id"]))
        st["wypelnione"] += 1
        if verbose:
            print(f"  [wypełnione]   {plan['symbol']} {plan['side']} @ {limit_px:.6g}"
                  f" — {plan['trigger']['rodzaj']}")
    return st


async def sprawdz(conn, engine: str = "crypto", h: Horizon | None = None) -> int:
    """Przebieg KONTROLNY: wykonuje wszystkie odczyty, nie zapisuje niczego.

    DLACZEGO ISTNIEJE

    Test dymny sprawdza warstwe webowa. Petla decyzyjna nie miala zadnego testu
    i blad w nazwie kolumny (`event_time` zamiast `decided_at`) wywracal ja przy
    kazdym przebiegu przez kilkanascie minut - jedynym sladem byl wpis w logu
    poda, ktorego nikt nie czytal.

    Ten tryb wykonuje dokladnie te same zapytania co produkcja, w tej samej
    kolejnosci, i konczy sie kodem niezerowym, gdy ktorekolwiek zawiedzie. Nie
    zastepuje testu jednostkowego - wylapuje inna klase bledow: rozjazd miedzy
    kodem a schematem bazy, ktorego zadne przenosne testy nie widza.

    Uzycie:  DATABASE_URL=... python services/engine/loop.py --sprawdz
    """
    h = h or get_horizon(DEFAULT_BAR)
    cur = conn.cursor()
    now = datetime.now(timezone.utc)
    bar_start = current_bar(now, h)
    bledy: list[str] = []

    def probuj(nazwa, fn):
        try:
            wynik = fn()
            print(f"  [ok  ] {nazwa}")
            return wynik
        except Exception as e:                                # noqa: BLE001
            print(f"  [BŁĄD] {nazwa}: {type(e).__name__}: {e}")
            bledy.append(nazwa)
            conn.rollback()
            return None

    print(f"Kontrola pętli decyzyjnej — silnik {engine}, świeca {h.bar}\n")
    risk = RiskKernel(conn, engine, "demo")
    probuj("limity ryzyka konta", risk.check_account)
    ids = probuj("identyfikatory cech horyzontu", lambda: horizon_feature_ids(cur, h))
    model = probuj("aktywna wersja modelu", lambda: active_model(cur, engine, h))

    # WARUNEK Z `uniwersum.py`, nie przepisany tutaj. Kontrola ma ogladac TEN
    # SAM zbior, ktory obstawia petla — inaczej „sprawdz" sprawdzaloby co innego.
    # CALE uniwersum, nie LIMIT 3: kontrola strategii momentum nizej liczy
    # ranking przekrojowy. Kontrole per instrument nadal na pierwszych trzech.
    cur.execute(sql_uniwersum_decyzyjnego(), (engine,))
    uniwersum = cur.fetchall()
    for inst in uniwersum[:3]:
        iid, sym = inst["id"], inst["symbol"]
        probuj(f"{sym}: decyzja w tej świecy?",
               lambda i=iid: decided_for_bar(cur, engine, "demo", i, bar_start, h))
        # KONTROLA PYTA O TO SAMO, CO PRODUKCJA: o cechy TEJ swiecy, nie
        # o najswiezsze jakiekolwiek. Kontrola czytajaca szerszym oknem
        # meldowalaby [ok] dokladnie w tym stanie, ktory wstrzymuje decyzje.
        para = probuj(f"{sym}: odczyt cech point-in-time",
                      lambda i=iid: latest_features(cur, i, now, h, ids or [],
                                                    bar_start))
        f, obce = para if para else (None, {})
        if not f and obce:
            print(f"         UWAGA: brak cech świecy {bar_start:%m-%d %H:%M}, "
                  f"ale {len(obce)} czeka z wcześniejszych — potok spóźniony")
        # KONTROLA MOWI O WIEKU, nie tylko o tym, ze zapytanie sie wykonalo.
        # `mark_price` przy przeterminowanej cenie zwraca None bez wyjatku, wiec
        # sam `probuj` wypisalby [ok] dla instrumentu z martwym feedem - czyli
        # dokladnie ten ksztalt awarii, ktory ta zmiana usuwa. Nie podnosimy
        # tego do bledu: swiezo wdrozony pod nie ma jeszcze zadnej swiecy,
        # a kontrola bramkuje start silnika (`--sprawdz && exec ...`).
        def cena_kontrolna(i):
            c = cena_odniesienia(cur, i, now)
            if c["cena"] is None:
                print("         UWAGA: brak jakiejkolwiek świecy 1m")
            elif not c["swieza"]:
                print(f"         UWAGA: cena sprzed {c['wiek_min']:.0f} min "
                      f"(próg {CENA_STARA_PO_MIN} min) — pozycja na tym "
                      f"instrumencie wstrzyma wejścia")
            return c["cena"]

        probuj(f"{sym}: cena odniesienia", lambda i=iid: cena_kontrolna(i))
        probuj(f"{sym}: próg zmienności", lambda i=iid: atr_percentile_80(cur, i, now, h))
        probuj(f"{sym}: otwarta pozycja", lambda i=iid: open_position(cur, engine, "demo", i))
        if f:
            regime, _ = classify_regime(f, None)
            probuj(f"{sym}: ocena sygnałów ({len(f)} cech, reżim {regime})",
                   # `weights` bywa jawnie None (brak aktywnego modelu, patrz
                   # active_model) - `.get("weights", {})` zwracalo wtedy None
                   # i kontrola wywracala start silnika. Ukryte, dopoki ostatnia
                   # swieca nie miala cech (do 2026-10-02 byla pomijana).
                   lambda ff=f, r=regime: evaluate(
                       ff, r, ((model or {}).get("weights") or {}).get(r), h))

    # STRATEGIA MOMENTUM: ten sam odczyt co `run_once --strategia momentum`,
    # na CALYM uniwersum (ranking jest przekrojowy). Zawsze, a nie tylko przy
    # tej strategii: zapytanie jest tanie, a blad w nim ma zatrzymac start
    # przed przelaczeniem w charcie, a nie po nim.
    if h.bar_minutes == 240:
        cel = probuj(f"strategia {strategia_momentum.OPIS}: cel dobowy",
                     lambda: strategia_momentum.cel_z_bazy(cur, uniwersum,
                                                           bar_start, h))
        if cel is not None:
            print(f"         {cel.chwila:%Y-%m-%d} UTC: "
                  + (f"odłożona — {cel.powod}" if not cel.kompletny else
                     f"filtr BTC {'TAK' if cel.filtr else 'NIE'}, skład: "
                     f"{', '.join(cel.sklad) or 'gotówka'}"
                     + (f" ({cel.powod})" if cel.powod else "")))

    # KONTROLA STRUKTURALNA: czy wszystkie cechy horyzontu siedza na siatce swiec.
    # Cechy liczone na wlasnej siatce (funding, news) potrafily wyladowac na
    # znacznikach typu 16:19:49 i wtedy NIGDY nie wchodzily do panelu trenera,
    # dzialajac jednoczesnie w produkcji przez feat.as_of(). Rozjazd
    # trening-produkcja nie do wykrycia po samych wynikach.
    def kontrola_siatki():
        krok = h.bar_minutes * 60
        cur.execute(
            """
            SELECT fd.name,
                   count(*) AS n,
                   sum(CASE WHEN extract(epoch FROM fv.event_time)::bigint %% %s = 0
                            THEN 1 ELSE 0 END) AS na_siatce
            FROM feat.feature_value fv
            JOIN feat.feature_def fd ON fd.id = fv.feature_def_id
            WHERE fd.params->>'bar' = %s
              AND fv.event_time > now() - interval '7 days'
            GROUP BY 1
            """,
            (krok, h.bar),
        )
        krzywe = [(r["name"], r["n"], r["na_siatce"]) for r in cur.fetchall()
                  if r["n"] and r["na_siatce"] < r["n"]]
        if krzywe:
            raise RuntimeError(
                "cechy poza siatką świec: " +
                ", ".join(f"{n} ({na}/{tot})" for n, tot, na in krzywe))
        return True

    probuj("cechy leżą na siatce świec", kontrola_siatki)

    # KONTROLA STRUKTURALNA: czy nie liczymy cech z jeszcze OTWARTEJ swiecy.
    # time_bucket() zwraca biezaca swiece z niepelnymi danymi, a przez
    # ON CONFLICT DO NOTHING taka czastkowa wartosc zostaje w bazie na zawsze -
    # pozniejszy przebieg nie nadpisze istniejacego wiersza.
    def kontrola_otwartej_swiecy():
        biezaca = current_bar(datetime.now(timezone.utc), h) + timedelta(
            minutes=h.bar_minutes)
        cur.execute(
            """
            SELECT count(*) AS n FROM feat.feature_value fv
            JOIN feat.feature_def fd ON fd.id = fv.feature_def_id
            WHERE fd.params->>'bar' = %s AND fv.event_time >= %s
            """,
            (h.bar, biezaca),
        )
        n = cur.fetchone()["n"]
        if n:
            raise RuntimeError(
                f"{n} cech policzonych dla świecy {biezaca:%H:%M}, która jeszcze trwa")
        return True

    probuj("brak cech z niezamkniętej świecy", kontrola_otwartej_swiecy)

    # KONTROLA STRUKTURALNA: czy datowanie nie twierdzi wczesniejszej wiedzy,
    # niz bylo mozliwe. Cecha swiecy 04:00-08:00 nie mogla byc znana o 04:00.
    #
    # I DRUGIE PYTANIE, KTOREGO `ingested_at` NIE UMIE ZADAC: czy decyzja wziela
    # wartosc, ktora potem zrewidowano. Potok przy poprawce zostawia
    # `ingested_at = znane_od`, wiec pierwszy warunek przepuszcza rewizje
    # z definicji; `revised_at` (migracja 0070) to zegar poprawki. Tolerancja
    # TA SAMA co w `train.load_panel` (dwie swiece), zeby obie strony liczyly
    # ten sam rozjazd. To NIE jest blad - poprawka swiecy jest uprawniona,
    # a blokowanie startu silnika za nia nie naprawia niczego - wiec UWAGA, nie
    # [BŁĄD]. Zapytanie odwoluje sie jednak do kolumny wprost: baza bez 0070
    # wywraca te kontrole, a z nia start silnika (`--sprawdz && exec ...`) -
    # tak samo, jak wywraca zapis w `features.insert_values`.
    def kontrola_datowania():
        cur.execute(
            """
            SELECT count(*) FILTER (
                       WHERE fv.ingested_at < fv.event_time + %s::interval) AS n,
                   count(*) FILTER (
                       WHERE fv.revised_at > fv.event_time + 2 * %s::interval)
                       AS zrewidowane,
                   count(*) AS wszystkie
            FROM feat.feature_value fv
            JOIN feat.feature_def fd ON fd.id = fv.feature_def_id
            WHERE fd.params->>'bar' = %s
              AND fv.event_time > now() - interval '7 days'
            """,
            (f"{h.bar_minutes} minutes", f"{h.bar_minutes} minutes", h.bar),
        )
        r = cur.fetchone()
        if r["n"]:
            raise RuntimeError(
                f"{r['n']} cech datowanych PRZED zamknięciem własnej świecy")
        if r["zrewidowane"]:
            print(f"         UWAGA: {r['zrewidowane']} z {r['wszystkie']} wartości "
                  f"cech z 7 dni zrewidowano po czasie decyzji — decyzje z tych "
                  f"świec widziały inną liczbę niż ta, na której uczy się trener")
        return True

    probuj("datowanie nie wyprzedza zamknięcia świecy", kontrola_datowania)

    conn.rollback()
    print()
    if bledy:
        print(f"NIEPOWODZENIE: {len(bledy)} zapytań zawiodło")
        for b in bledy:
            print(f"  - {b}")
        return 1
    print("Wszystkie zapytania pętli decyzyjnej wykonały się poprawnie.")
    return 0


def nowe_stats(**flagi) -> dict:
    """Komplet licznikow przebiegu, WSZYSTKIE zalozone zerem. JEDNO ZRODLO.

    `risk.engine_heartbeat` jest nadpisywana co przebieg (ON CONFLICT (engine)
    DO UPDATE), wiec licznik zakladany dopiero przy pierwszym wystapieniu daje
    puls, w ktorym "nic nie pominieto" jest nieodrozialne od "ta wersja silnika
    w ogole tego nie liczy".

    DLACZEGO FUNKCJA, A NIE TRZY LITERALY

    Cykl przerwany bramka sesji albo brakiem modelu TEZ zapisuje `detail`, wiec
    tez musi miec pelny ksztalt - inaczej kazda noc silnika equity kasowalaby
    z pulsu liczniki, ktore panel czyta, a "poza sesja" znaczyloby przy okazji
    "ta wersja nie liczy pominiec". Trzy kopie tej listy rozjechalyby sie przy
    pierwszym nowym liczniku, a rozjazd objawilby sie brakiem klucza w JSON-ie,
    nie awaria.

    `flagi` dokladaja POWOD przerwania (`poza_sesja`, `bez_modelu`). Powod nigdy
    nie nadpisuje licznika: to dwie rozne rzeczy i czytelnik pulsu ma prawo
    widziec obie naraz.
    """
    stats = {"rozwazone": 0, "wejscia": 0, "wyjscia": 0, "wstrzymane": 0,
             "zablokowane": 0, "poza_swieca": 0, "bez_swiecy": 0,
             "wypelnione": 0, "niewypelnione": 0, "czekaja": 0,
             # Ile decyzji zapadlo na NIEPELNYM komplecie sygnalow. Idzie do
             # heartbeatu, bo poza tym stanu tego nie widac z zewnatrz w ogole:
             # od czasu renormalizacji wyniku (services/engine/signals.combine)
             # decyzja z trzech sygnalow ma te sama skale co decyzja z osmiu.
             "niepelne_sygnaly": 0,
             # ZERO TEZ JEST WYNIKIEM. Licznik byl wczesniej zakladany przez
             # `stats.get(...) + 1`, wiec klucz pojawial sie w heartbeacie
             # WYLACZNIE w cyklach, w ktorych byl niezerowy - a `detail` jest
             # nadpisywany co przebieg (ON CONFLICT (engine) DO UPDATE). Brak
             # klucza znaczyl wtedy jednoczesnie "zadnego shorta nie odrzucono"
             # i "ten silnik w ogole tego nie liczy", czyli nie znaczyl nic.
             "short_niewykonalny": 0,
             # Ile decyzji zapadlo w REZIMIE BEZ SKALIBROWANEGO PROGU wejscia.
             # Ten sam powod, co przy liczniku wyzej: bez niego rezim wypadniety
             # z handlu wyglada w pulsie tak samo jak rezim, w ktorym nic sie nie
             # dzialo. A wypadniecie jest tu decyzja projektowa (patrz
             # `services/engine/bramki.py`), wiec musi byc widoczne z zewnatrz.
             "rezim_bez_progu": 0,
             # Wejscia zablokowane srednim spreadem z 7 dni (F-1.5,
             # `uniwersum.za_szeroki_spread`).
             "szeroki_spread": 0,
             # Wejscia odrzucone, bo od zamkniecia swiecy minelo ponad
             # `MAKS_ZWLOKA_WEJSCIA`. Niezerowe znaczy: potok danych sie spoznia.
             "decyzja_przeterminowana": 0,
             # POMINIECIA Z ROZBICIEM NA POWOD. Obie te sciezki konczyly sie
             # samym `continue`: instrument wypadal z przebiegu, nie zostawiajac
             # ani wiersza, ani liczby. Tu odpowiadaja na pytanie "ile pominieto
             # TERAZ i dlaczego"; na pytanie "co sie stalo WTEDY" odpowiada
             # journal.pominiecie (0041), bo heartbeat ma `engine` w kluczu
             # glownym i kazdy przebieg kasuje poprzedni.
             "pominiete_bez_cech": 0, "pominiete_bez_ceny": 0,
             # Decyzje ODLOZONE przez niepelne pokrycie wag - patrz MIN_POKRYCIE.
             "odlozone_niepelne": 0,
             # POTOK SPOZNIONY, A NIE MARTWY - podzbior `pominiete_bez_cech`.
             #
             # Rozroznienie jest tu cala trescią licznika. "Brak cech" znaczy
             # "worker stoi" i konczy sie alarmem zywotnosci. "Cechy sa, ale
             # dla swiecy wczesniejszej" znaczy "worker zyje, tylko nie zdazyl"
             # - zywotnosc milczy, wszystko bije, a silnik nie handluje.
             # Bez osobnej liczby obie awarie wygladaja w heartbeacie
             # identycznie, a reakcja na nie jest inna.
             "cechy_z_poprzedniej_swiecy": 0,
             # Rozmiar uniwersum decyzyjnego. Bez tej liczby przebieg na PUSTEJ
             # liscie instrumentow - `tradeable` zdjete pomylkowo, migracja
             # w polowie - wyglada w heartbeacie identycznie jak przebieg,
             # w ktorym po prostu nie bylo czego robic: same zera.
             "instrumenty": 0,
             # CO ZROBILA LIKWIDACJA PO KILL-SWITCHU. `None` znaczy "do
             # likwidacji ten cykl nie doszedl" - bramka sesji, brak modelu,
             # egzekucja papierowa albo wylaczony przelacznik - i jest to INNY
             # stan niz slownik z zerami, ktory znaczy "likwidacja patrzyla
             # i nie mialo czego zamykac". Klucz jest obecny w obu, bo
             # `risk.engine_heartbeat.detail` jest nadpisywany co przebieg:
             # brak klucza nie dalby sie odroznic od wersji, ktora tego nie
             # liczy (ta sama zasada co przy licznikach wyzej).
             "likwidacja": None,
             # ID KILL-SWITCHA, KTORY W TYM CYKLU WSTRZYMAL WEJSCIA (U-12).
             # Ogniwo 3 proby pozarowej czyta stad stan bramki - slad
             # w dzienniku decyzji powstaje tylko przy zamiarze wejscia, a tego
             # w oknie proby zwykle nie ma. None: bramka otwarta albo zamknieta
             # z innego powodu niz kill-switch.
             "wstrzymanie_killswitch_id": None,
             # Ktora regula wybierala akcje (`STRATEGIE`). None: cykl skonczyl
             # sie na bramce przed wyborem akcji.
             "strategia": None,
             # Decyzje strategii momentum ODLOZONE, bo swieca BTC konczaca dobe
             # nie ma jeszcze wszystkich swiec 1h (`strategia_momentum.cel_z_bazy`).
             "odlozone_momentum": 0}
    stats.update(flagi)
    return stats


def id_wstrzymania_killswitchem(wejscia_wstrzymane: bool,
                                killswitch: dict | None) -> int | None:
    """Id zdarzenia, ktore zamknelo bramke wejsc w tym cyklu. CZYSTA FUNKCJA."""
    return killswitch["id"] if wejscia_wstrzymane and killswitch else None

def zapisz_puls(cur, engine: str, mode: str, stats: dict) -> None:
    """Puls silnika po cyklu. JEDNO miejsce zapisu, bo wyjsc z `run_once` jest kilka.

    Cykl przerwany bramka sesji albo brakiem modelu TEZ bije. Gdyby nie bil,
    `dozor.py --co zywotnosc` uznalby kazda noc za smierc silnika equity
    i wysylal alarm krytyczny o 22:00 kazdego dnia roboczego - a czujka, ktora
    krzyczy codziennie o tej samej porze bez powodu, konczy wyciszona. Wyciszony
    kanal jest gorszym stanem niz brak czujki (patrz `dozor.sprawdz_swiezosc`).

    Powod przerwania idzie do `detail`, wiec panel i `overview.py` odrozniaja
    "silnik spi, bo gielda zamknieta" od "silnik pracuje" bez zagladania w logi.
    """
    cur.execute(
        "INSERT INTO risk.engine_heartbeat (engine, mode, last_beat_at, status, detail)"
        " VALUES (%s,%s, now(), 'running', %s)"
        " ON CONFLICT (engine) DO UPDATE SET last_beat_at=now(), status='running',"
        " detail=EXCLUDED.detail",
        (engine, mode, json.dumps(stats)))


async def run_once(conn, engine: str = "crypto", mode: str = "demo",
                   kind_kosztow: str = "spot", limit: bool = False,
                   verbose: bool = True, h: Horizon | None = None,
                   gielda=None, kategoria: str = "spot",
                   wymagaj_modelu: bool = False,
                   sesja: StanSesji | None = None,
                   likwiduj: bool = False,
                   strategia: str = "model") -> dict:
    """Jeden przebieg silnika.

    `gielda` PRZELACZA ZRODLO PRAWDY O WYPELNIENIU, a nie sposob decydowania.
    Bez niej plan rozstrzyga swieca (rachunek papierowy); z nia - odpowiedz
    gieldy. Decyzje, limity i dziennik sa w obu przypadkach te same, bo to nie
    egzekucja ma o nich stanowic.

    `strategia` PRZELACZA WYLACZNIE WYBOR AKCJI I WIELKOSC WEJSCIA: `model`
    to ensemble z aktywnej wersji modelu, `momentum` to zamrozona regula R4
    (`strategia_momentum`). Bramki, kill-switch, risk kernel, egzekucja i puls
    sa wspolne - pomiar egzekucji ma dotyczyc tej samej sciezki, ktora handluje.
    """
    if strategia not in STRATEGIE:
        raise ValueError(f"nieznana strategia {strategia!r} (znane: {sorted(STRATEGIE)})")
    momentum = strategia == "momentum"
    h = h or get_horizon(DEFAULT_BAR)
    if momentum and h.bar_minutes != 240:
        # Parametry H-3 sa zmierzone na swiecach 4h i decyzji na ich zamknieciu
        # o polnocy UTC; inny horyzont bylby inna regula.
        raise ValueError(f"strategia momentum wymaga świecy 4h (dostała {h.bar})")
    # `None`, a nie pusty slownik: "likwidacja nie ruszyla w tym cyklu" to inny
    # stan niz "ruszyla i nie miala o czym mowic". Patrz `nowe_stats`.
    likwidacja_stat: dict | None = None
    cur = conn.cursor()
    signal_ids = ensure_signal_defs(cur, engine)
    seed_equity(cur, engine, mode)
    conn.commit()

    risk = RiskKernel(conn, engine, mode)

    # Limity konta sprawdzamy RAZ NA CYKL, zanim cokolwiek rozwazymy.
    # Wczesniej risk.check() wolane bylo wylacznie przy probie wejscia, wiec
    # obsuniecie kapitalu podczas samego trzymania pozycji nie bylo oceniane
    # przez nikogo - kill-switch nie mogl zadzialac dokladnie w tej sytuacji,
    # dla ktorej istnieje.
    konto = risk.check_account()
    conn.commit()

    # BRAMKI CYKLU STOJA ZA POMIAREM RYZYKA, A NIE PRZED NIM.

    # Pierwsza wersja (2026-09-24) miala je wyzej, przed `RiskKernel`. Czujka
    # `--co ryzyko` zglosila to przy PIERWSZYM cyklu silnika equity:
    # "BEZPIECZNIK NIE RAPORTUJE equity/demo: ANI JEDNEGO zapisanego pomiaru
    # limitow konta w calej historii". Sluszznie - kazdy cykl konczyl sie na
    # bramce sesji, wiec `check_account` nie wykonal sie ani razu.

    # PODLOGA KAPITALU NIE PRZESTAJE OBOWIAZYWAC, GDY GIELDA ZAMYKA. Pozycje
    # zostaja otwarte przez noc i weekend, a `kapital-equity` aktualizuje
    # krzywa kapitalu z ksiag Alpaki co 15 minut niezaleznie od sesji. Obsuniecie
    # moze wiec przekroczyc limit o 03:00 i nie byloby przez nikogo ocenione.

    # `check_account` jest tu bezpieczny poza sesja: czyta `exec.equity_point`,
    # a ekspozycji SWIADOMIE nie liczy (`exposure=None` w `risk.check_account`),
    # wiec nie siega po swieze swiece, ktorych poza sesja nie ma.

    # Bramki nadal wstrzymuja WSZYSTKO, co nastepuje dalej: decyzje, zlecenia
    # i kontakt z gielda. Zmienia sie tylko to, ze pomiar ryzyka dzieje sie
    # przed nimi - bo pomiar nie jest handlem.
    # ---------------------------------------------------------- BRAMKI CYKLU
    #
    # Obie sa PRZED jakimkolwiek odczytem cen i przed `RiskKernel`, i obie
    # koncza cykl pulsem. Powod jest ten sam w obu przypadkach: stan, w ktorym
    # silnik NIE MA PRAWA decydowac, musi byc odrozialny od stanu, w ktorym
    # decyduje i nic mu nie wychodzi. Bez tego jedna linia "rozwazone 21,
    # wejscia 0" znaczylaby jednoczesnie "rynek spi", "nie ma modelu"
    # i "sygnaly nie przekroczyly progu" - czyli nie znaczylaby nic.

    # BRAMKA 1: SESJA GIELDY.
    #
    # `stan_sesji` zwraca trzy stany, nie dwa, i `None` NIE jest otwarciem -
    # patrz naglowek `services/engine/sesja.py`. Dla rynku ciaglego bramka
    # konczy sie na `is_24_7` i nie kosztuje ani jednego zapytania do gieldy,
    # wiec silnik crypto nie placi za nia niczym.
    if sesja is not None and not sesja.wolno_decydowac:
        stats = nowe_stats(poza_sesja=True, sesja_zrodlo=sesja.zrodlo,
                           sesja_znana=sesja.otwarta is not None)
        if verbose:
            print(f"  {sesja.opis()} — cykl bez decyzji")
        zapisz_puls(cur, engine, mode, stats)
        conn.commit()
        return stats

    # BRAMKA 2: WYMAGANY ZWALIDOWANY MODEL.
    #
    # `active_model` przy braku aktywnej wersji NIE przerywa - cofa sie do
    # zalozen poczatkowych i handluje dalej. Dla silnika, ktory dopiero rusza,
    # to jest najgorszy mozliwy domysl: krzywa kapitalu zaczyna mierzyc
    # heurystyke, a wyglada jak wynik modelu, i nikt tego nie odrozni po fakcie.
    #
    # DLACZEGO PRZELACZNIK, A NIE REGULA DLA OBU SILNIKOW. Crypto ma aktywny
    # model i chodzi tak od miesiecy; zmiana jego zachowania przy okazji
    # uruchamiania equity byla by zmiana produkcji, o ktora nikt nie prosil.
    # Wlaczenie nalezy do chartu - tak samo jak wlaczenie egzekucji gieldowej.
    if wymagaj_modelu:
        m = active_model(cur, engine, h)
        if not m.get("version"):
            stats = nowe_stats(
                bez_modelu=True,
                model_nazwa=f"{engine}_signal_weighting_{h.bar}")
            if verbose:
                print(f"  BRAK ZWALIDOWANEGO MODELU "
                      f"({engine}_signal_weighting_{h.bar}, status 'active') — "
                      f"cykl bez decyzji. Handel na założeniach początkowych "
                      f"jest tu wyłączony świadomie (--wymagaj-modelu).")
            zapisz_puls(cur, engine, mode, stats)
            conn.commit()
            return stats


    # POZYCJA BEZ SWIEZEJ CENY WSTRZYMUJE WEJSCIA - i to jest druga polowa
    # naprawy z `wycen()`.
    #
    # Sama poprawna wycena nie wystarczy. Gdy feed instrumentu stanie, wszystkie
    # limity procentowe (obsuniecie, strata dzienna, prog kapitalu, laczna
    # ekspozycja) licza sie z liczby, ktorej NIKT NIE ZMIERZYL - z ceny
    # zamrozonej albo z ceny wejscia. Wchodzenie w nowe pozycje na podstawie
    # takiego stanu to handel z wylaczonym pomiarem ryzyka.
    #
    # DLACZEGO WSTRZYMANIE, A NIE KILL-SWITCH. Kill-switch wymaga recznego
    # zwolnienia, bo opisuje strate, ktora sie wydarzyla. Martwy feed sam wraca
    # razem z podem `market_worker` - zatrzymanie na stale wymagaloby wtedy
    # czlowieka o trzeciej w nocy do awarii, ktora minela sama.
    #
    # WYJSCIA IDA DALEJ. Tak samo jak przy kill-switchu: bezpiecznik ma
    # wyprowadzac z ryzyka, a nie zamykac w nim. Wyjscie z pozycji, ktorej nie
    # umiemy wycenic, jest ruchem ZMNIEJSZAJACYM to, czego nie mierzymy.
    pomiar = wycen(pozycje_do_wyceny(cur, engine, mode, datetime.now(timezone.utc)))

    # UNIWERSUM I BUDZET LICZYMY PRZED ZEBRANIEM POWODOW WSTRZYMANIA, bo brak
    # budzetu JEST jednym z tych powodow i musi trafic do tej samej listy.
    # Uniwersum to czysty odczyt `ref.instrument` - nie zalezy od niczego, co
    # dzieje sie nizej, wiec przesuniecie go tutaj niczego nie zmienia poza tym,
    # ze wszystkie warunki wejscia zapadaja w JEDNYM miejscu.
    #
    # `tradeable`, nie `active`: historię zbieramy szerzej, niż handlujemy.
    # Obstawianie instrumentu, którego giełda z kontem nie oferuje, to decyzja
    # niewykonalna - a trener kalibruje wagi na tym samym zbiorze (0020).
    # WARUNEK Z `uniwersum.py`, bo to on wyznacza zbior, z ktorego liczy sie
    # `odcisk()` — a `horizon_feature_ids` filtruje po odcisku SCISLE. Wlasna
    # kopia warunku tutaj rozjechalaby sie z `features.py` przy pierwszej
    # zmianie definicji uniwersum, a sygnal `rel_strength` zniknalby BEZ SLADU.
    cur.execute(sql_uniwersum_decyzyjnego("id, symbol, kind, quote_ccy"),
                (engine,))
    instrumenty = cur.fetchall()
    # WYKLUCZENIE ZE WZGLEDU NA SPREAD (audyt Fable F-1.5) - WYLACZNIE Z WEJSC.
    # Sredni spread arkusza z 7 dni ponad prog: instrument nie dostaje nowej
    # pozycji, ale wychodzi normalnie, a `instrumenty` (odcisk, przekroj,
    # budzet) zostaja na pelnym zbiorze. Uzasadnienie w `uniwersum.py`.
    szerokie_spready = za_szeroki_spread(srednie_spready(
        cur, [i["id"] for i in instrumenty], h.bar, datetime.now(timezone.utc)))
    if szerokie_spready and verbose:
        for linia in opis_wykluczen(szerokie_spready,
                                    {i["id"]: i["symbol"] for i in instrumenty}):
            print(f"  {linia}")

    # BUDZET NA POZYCJE LICZY SIE Z KAPITALU, NIE ZE STALEJ.
    #
    # Do 2026-09-09 `position_budget` wolane bylo bez kapitalu i spadalo wewnatrz
    # na START_EQUITY = 100 000. Na demo tego nie bylo widac, bo kapital (100 063)
    # byl przypadkiem rowny tej stalej - profil ryzyka trzymal sie wiec na
    # ZBIEZNOSCI DWOCH LICZB, a nie na kontroli. Na zapowiadanym rachunku 10 000
    # dawalo to budzet 1500 przy limicie lacznej ekspozycji rownym 1500, czyli
    # dziesiec zlecen po 500 (obcietych przez `max_position_notional`) i 50%
    # rachunku w rynku zamiast 15%.
    #
    # Kapital bierzemy z `risk.kapital()`, czyli z DOKLADNIE tego samego wiersza,
    # ktorym `evaluate_limits` mierzy potem oba limity procentowe. Dwa zrodla tej
    # samej liczby byly cala trescia tej wady.
    kapital = risk.kapital()
    baza = position_budget(risk.limits, len(instrumenty), kapital)

    powody_wstrzymania = []
    if konto.verdict != "allow":
        powody_wstrzymania.append(konto.reason)
    if not pomiar.pomiar_pelny:
        powody_wstrzymania.append(
            f"pomiar niepełny: {len(pomiar.bez_swiezej_ceny)} "
            f"pozycji bez świeżej ceny ({pomiar.opis_brakow()})")
    if baza is None:
        # BRAK POMIARU WSTRZYMUJE WEJSCIA - ta sama sciezka i z tego samego
        # powodu co pozycja bez swiezej ceny wyzej. `position_budget` oddaje
        # None tylko wtedy, gdy wielkosci zlecenia NIE DA SIE wyprowadzic:
        # nie ma punktu krzywej kapitalu, kapital jest niedodatni albo w
        # `risk.limit_def` nie ma limitu lacznej ekspozycji, z ktorego budzet
        # powstaje. Kazda liczba wstawiona tu zamiast wstrzymania bylaby
        # zgadnieta, a zgadnieta wielkosc zlecenia to zgadniete ryzyko.
        powody_wstrzymania.append(
            "budżetu na pozycję nie da się wyprowadzić: "
            + ("brak pomiaru kapitału (exec.equity_point pusty)" if kapital is None
               else f"kapitał {kapital} nie jest dodatni" if kapital <= 0
               else "brak limitu max_total_exposure w risk.limit_def"))
        # Zero, a nie stala: nizej `baza` mnozy sie przez udzial budzetu, a przy
        # wstrzymanych wejsciach ta liczba i tak nigdzie nie idzie. Zero sprawia,
        # ze gdyby kiedys poszla, bylaby zleceniem pustym, a nie zgadnietym.
        baza = Decimal(0)

    # Budzet wejscia strategii momentum: 15% / 3 kapitalu (wariant P z H-3).
    # Z TEGO SAMEGO `kapital` co `baza`; przy braku pomiaru zero, a wejscia
    # i tak sa wstrzymane powodem dopisanym wyzej.
    baza_momentum = (strategia_momentum.budzet_pozycji(kapital)
                     if momentum and kapital is not None and kapital > 0
                     and baza > 0 else Decimal(0))

    wejscia_wstrzymane = bool(powody_wstrzymania)
    powod_wstrzymania = " | ".join(powody_wstrzymania)
    # Werdykt zapisywany do dziennika musi mowic PRAWDE o tym, co zatrzymalo
    # wejscie. Podstawienie tu `konto` (ktore przy samym niepelnym pomiarze ma
    # verdict 'allow') opisywaloby w dzienniku decyzje jako dozwolona, choc
    # system jej wlasnie odmowil - czyli ten sam ksztalt bledu, ktory naprawiamy.
    werdykt_wstrzymania = konto if konto.verdict != "allow" else RiskVerdict(
        verdict="block", reason=powod_wstrzymania)
    if wejscia_wstrzymane and verbose:
        print(f"  UWAGA: wejścia wstrzymane — {powod_wstrzymania}")

    # KILL-SWITCH ZDEJMUJE ZLECENIA Z GIELDY, a nie tylko wstrzymuje decyzje.
    #
    # Wstrzymanie wejsc zatrzymuje podejmowanie NOWYCH decyzji. Zlecenie zlozone
    # minute wczesniej nadal lezy w arkuszu i nadal moze sie wypelnic - juz po
    # tym, jak system uznal, ze handlowac nie wolno. Pozycja powstalaby wtedy
    # wbrew dzialajacemu zabezpieczeniu i bez zadnej decyzji za soba.
    if wejscia_wstrzymane and gielda is not None:
        from services.engine.gielda_exec import anuluj_wejscia
        zdjete, wypelnione = await anuluj_wejscia(
            cur, gielda, kategoria=kategoria, engine=engine, mode=mode,
            powod=f"wstrzymanie wejść: {powod_wstrzymania}", verbose=verbose)
        conn.commit()
        # KOMUNIKAT ZAWSZE, TAKZE PRZY ZERZE. Wczesniej pojawial sie wylacznie
        # przy `zdjete > 0`, wiec kill-switch, ktoremu NIE UDALO SIE zdjac ani
        # jednego zlecenia, wygladal identycznie jak kill-switch, ktory nie mial
        # czego zdejmowac.
        if verbose:
            print(f"  wstrzymanie wejść: zdjęto z giełdy {zdjete} zleceń"
                  + (f", {wypelnione} było już wypełnionych — zostają "
                     f"do zaksięgowania" if wypelnione else ""))

    # KILL-SWITCH LIKWIDUJE OTWARTE POZYCJE — ZLECENIEM RYNKOWYM.
    #
    # DLACZEGO TO NIE JEST TA SAMA BRAMKA CO `anuluj_wejscia` WYŻEJ.
    # Tamta rusza na KAŻDYM powodzie wstrzymania wejść, w tym na powodach,
    # które mijają same: stary pomiar kapitału, pozycja bez świeżej ceny, brak
    # budżetu. Zdjęcie niewypełnionego zlecenia z arkusza jest wtedy tanie
    # i odwracalne — złożymy je znowu, gdy stan minie. Zlikwidowanie pozycji
    # NIE JEST odwracalne i kosztuje pełną rundę plus poślizg takera, więc
    # wolno je wywołać wyłącznie temu jednemu stanowi, który opisuje stratę
    # WYDARZONĄ i wymaga człowieka do zwolnienia: czynnemu kill-switchowi.
    # Rozstrzyga o tym `likwidacja.ocen_zdarzenie`, czytając
    # `risk.killswitch_event` — a nie lista `powody_wstrzymania`.
    #
    # PRZEŁĄCZNIK, NIE DOMYŚLNOŚĆ, i to z tego samego powodu co przy
    # `--egzekucja gielda`: pierwsze prawdziwe uruchomienie ścieżki zlecenia
    # rynkowego (`execution='gielda' AND order_type='market'` ma w tej bazie
    # ZERO wierszy) ma być decyzją wpisaną w chart, a nie skutkiem wdrożenia
    # obrazu. Domyślne `False` znaczy: kill-switch zachowuje się dokładnie tak,
    # jak zachowywał się do 2026-09-27.
    if likwiduj and gielda is not None:
        from services.engine.likwidacja import likwiduj_po_killswitchu
        likwidacja_stat = await likwiduj_po_killswitchu(
            cur, gielda, engine=engine, mode=mode, kategoria=kategoria,
            verbose=verbose)
        conn.commit()

    now = datetime.now(timezone.utc)
    bar_start = current_bar(now, h)
    # STRATEGIA MOMENTUM NIE CZYTA MODELU - ani aktywnej wersji, ani kandydata.
    # Atrapa z `None` w miejscu wag i progow, zeby nic nizej nie siegnelo po
    # ensemble przypadkiem: kazde takie siegniecie wywroci cykl, a nie po cichu
    # domiesza wagi modelu do reguly.
    model = (active_model(cur, engine, h) if not momentum else
             {"version": None, "model_version_id": None, "bramki": None,
              "weights": None, "min_conf": None})
    if momentum and verbose:
        print(f"  strategia: {strategia_momentum.OPIS} (zamrożone parametry H-3: "
              f"{dict(strategia_momentum.PARAMETRY)}, budżet "
              f"{strategia_momentum.UDZIAL_LACZNY:.0%} kapitału, "
              f"{strategia_momentum.UDZIAL_POZYCJI:.0%} na pozycję = "
              f"{baza_momentum} przy kapitale {kapital}; model nieużywany)")
    elif verbose:
        print(f"  wagi: {'wersja modelu ' + str(model['version']) if model['version'] else 'założenia początkowe (brak zwalidowanego modelu)'}")
        print(f"  progi wejścia: {model['bramki'].opis()}")
    # KOMUNIKAT BEZ `verbose`, bo mowi o rozjezdzie wersji, nie o przebiegu.
    #
    # Wersja modelu zapisana przed 2026-09-27 nie ma progow per rezim i dostaje
    # DAWNE zachowanie: jeden prog dla wszystkich rezimow. Zmierzylismy, ze taki
    # prog przepuszcza 7,0% sygnalow w `range` i 34,0% w `high_vol` przy
    # zamiarze 12%, wiec silnik handluje wtedy inna czestotliwoscia niz
    # zaprojektowana - i musi to powiedziec, a nie zrobic po cichu.
    if not momentum and model["bramki"].powod == "wersja_modelu_bez_progow_per_rezim":
        print(f"  UWAGA: wersja modelu {model['version']} NIE MA progów per reżim "
              f"(wytrenowana przed 2026-09-27). Obowiązuje jeden próg "
              f"{model['enter']:.4f} dla wszystkich reżimów — zamierzone 12% "
              f"najsilniejszych sygnałów NIE JEST dotrzymane w żadnym z nich. "
              f"Naprawia to dopiero kolejny trening.")
    stats = nowe_stats()
    # WYNIK LIKWIDACJI IDZIE DO PULSU. Bez tego jedynym sladem po zamknieciu
    # calej ekspozycji zleceniem rynkowym bylaby linia w logu poda, a logu poda
    # nikt nie czyta o trzeciej w nocy. `None` zostaje, gdy likwidacja nie
    # ruszyla - patrz `nowe_stats`.
    stats["likwidacja"] = likwidacja_stat
    stats["wstrzymanie_killswitch_id"] = id_wstrzymania_killswitchem(
        wejscia_wstrzymane, risk.killswitch_active())

    # ROZSTRZYGNIECIE PLANOW IDZIE PRZED OCENA, nie po niej.
    #
    # Plan z poprzedniej swiecy moze otworzyc albo zamknac pozycje, a ocena
    # musi widziec stan PO tym rozstrzygnieciu - inaczej silnik podejmowalby
    # decyzje na podstawie pozycji, ktorej juz nie ma albo ktora wlasnie
    # powstala.
    if limit and gielda is not None:
        from services.engine.gielda_exec import rozstrzygnij_oczekujace
        stats.update(await rozstrzygnij_oczekujace(
            cur, gielda, engine=engine, mode=mode, h=h, now=now,
            kategoria=kategoria, kind=kind_kosztow, verbose=verbose))
        conn.commit()
    elif limit:
        # `tylko_wyjscia` TAK SAMO JAK NA SCIEZCE GIELDOWEJ. Tam kill-switch
        # zdejmuje zlecenia wejscia z arkusza (`anuluj_wejscia`, wyzej);
        # tutaj nie ma arkusza, wiec jego odpowiednikiem jest zdjecie planu.
        # Bez tego warunku rachunek papierowy otwieral pozycje po zadzialaniu
        # bezpiecznika, ktory produkcje wlasnie zatrzymal.
        stats.update(rozstrzygnij_plany(cur, engine, mode, h, now,
                                        kind_kosztow, verbose=verbose,
                                        tylko_wyjscia=wejscia_wstrzymane))
        conn.commit()

    stats["instrumenty"] = len(instrumenty)

    # IDENTYFIKATORY CECH DOPIERO TERAJ, bo cechy przekrojowe sa filtrowane
    # odciskiem uniwersum - a uniwersum znamy dopiero po tym zapytaniu.
    feature_ids = (horizon_feature_ids(cur, h, [i["symbol"] for i in instrumenty])
                   if not momentum else [])
    stats["strategia"] = strategia
    # CEL STRATEGII MOMENTUM - liczony RAZ na cykl i tylko wtedy, gdy jest
    # co decydowac (pierwszy instrument bez decyzji w tej swiecy). Patrz
    # `strategia_momentum.cel_z_bazy`.
    cel: strategia_momentum.Cel | None = None
    decyzyjna = momentum and strategia_momentum.swieca_decyzyjna(bar_start, h)

    # PRZEBIEG PIERWSZY: ocena wszystkich instrumentow, bez podejmowania akcji.
    #
    # Rozdzielenie oceny od dzialania jest tu konieczne, bo przy niedoborze
    # kapitalu kolejnosc rozpatrywania decyduje o tym, kto go dostanie.
    # Poprzednia wersja przetwarzala instrumenty alfabetycznie i przez to
    # systematycznie faworyzowala ADAUSDT wzgledem XRPUSDT - o przydziale
    # decydowala nazwa, nie sila sygnalu.
    oceny = []
    for inst in instrumenty:
        iid, symbol = inst["id"], inst["symbol"]
        if decided_for_bar(cur, engine, mode, iid, bar_start, h):
            stats["poza_swieca"] += 1
            continue
        if momentum:
            # REGULA ZAMIAST CECH: momentum nie czyta `feat.*` ani reżimu.
            # Niedomknieta swieca BTC = decyzja ODLOZONA do nastepnego cyklu
            # w oknie swiecy (jak `MIN_POKRYCIE`), bez wiersza w dzienniku.
            if cel is None:
                # Na domkniecie swiecy BTC czekamy tylko w oknie wejscia
                # swiecy decyzyjnej - potem czekanie wiezi wyjscia.
                cel = strategia_momentum.cel_z_bazy(
                    cur, instrumenty, bar_start, h,
                    czekaj=decyzyjna and not decyzja_przeterminowana(now, bar_start, h))
                if not cel.kompletny:
                    print(f"  strategia {strategia_momentum.OPIS}: decyzja "
                          f"odłożona — {cel.powod}")
                elif cel.powod:
                    print(f"  UWAGA strategia {strategia_momentum.OPIS}: {cel.powod}")
            if not cel.kompletny:
                stats["odlozone_momentum"] += 1
                continue
        features, obce = (latest_features(cur, iid, now, h, feature_ids, bar_start)
                          if not momentum else ({}, {}))
        if not momentum and len(features) < MIN_CECH:
            # SLAD ZANIM `continue`. Bez tego wiersza instrument znikal
            # z przebiegu bezglosnie i z bazy nie dalo sie odroznic "pominiety
            # z braku cech" od "silnik w ogole nie chodzil".
            #
            # DECYZJA ODLOZONA, NIE ODWOLANA - i to jest cala roznica.
            # `current_bar` oddaje te sama swiece przez cale jej nastepne
            # okno, a `decided_for_bar` blokuje dopiero po ZAPADNIECIU decyzji.
            # Petla budzi sie co 300 s, wiec przy potoku spoznionym o 20-35
            # minut decyzja dla swiecy 08:00-12:00 zapada w cyklu 12:20-12:35,
            # nadal w oknie [12:00, 16:00) i nadal po cenie zamkniecia tej
            # swiecy (`zakres_swiecy(..., bar_start)`). Kosztem jest opoznienie
            # zlozenia zlecenia, nie utrata transakcji.
            #
            # KOSZT JEST REALNY DOPIERO, GDY POTOK SPOZNIA SIE O WIECEJ NIZ
            # SWIECE - wtedy zaden cykl w oknie nie zobaczy kompletu i silnik
            # przestaje handlowac CALKOWICIE. Z zewnatrz wyglada to jak rynek
            # bez sygnalow, wiec ta galaz musi zostawiac liczbe: wiersz
            # w journal.pominiecie na kazda (swieca, instrument) ORAZ licznik
            # w heartbeacie ponizej.
            stats["pominiete_bez_cech"] += 1
            if obce:
                stats["cechy_z_poprzedniej_swiecy"] += 1
                najswiezsza = max(obce.values())
                szczegol = (
                    f"{len(features)} z wymaganych {MIN_CECH} dla świecy "
                    f"{bar_start:%Y-%m-%d %H:%M}; potok SPÓŹNIONY — "
                    f"{len(obce)} cech czeka z wcześniejszych świec, "
                    f"najświeższa {najswiezsza:%Y-%m-%d %H:%M}")
            else:
                szczegol = (f"{len(features)} z wymaganych {MIN_CECH} dla świecy "
                            f"{bar_start:%Y-%m-%d %H:%M}: "
                            + (", ".join(sorted(features)) if features else "żadnej"))
            zapisz_pominiecie(cur, engine, mode, iid, bar_start,
                              "za_malo_cech", szczegol)
            if verbose:
                print(f"  {symbol:10s} pominięty — {szczegol}")
            continue
        # CENA DECYZYJNA TO ZAMKNIECIE SWIECY, nie ostatnia cena minutowa.
        #
        # Symulacja walk-forward stawia limit i liczy wielkosc pozycji po
        # ZAMKNIECIU swiecy (portfolio: limit_px = cur["row"]["price"]).
        # Petla budzi sie co 300 s, wiec mark_price(now) oddaje cene nawet piec
        # minut pozniejsza - a to jest inna cena, nie zaokraglenie tej samej.
        #
        # Zmierzone na dziesieciu instrumentach uniwersum (2026-09-06, dane
        # minutowe, po naprawie swiec godzinowych):
        #
        #     po 1 min   mediana 0,0240%   90 centyl 0,2186%
        #     po 3 min   mediana 0,0530%   90 centyl 0,2741%
        #     po 5 min   mediana 0,0808%   90 centyl 0,3195%
        #
        # Runda kosztowa wynosi 0,200%, wiec mediana rozjazdu po pieciu minutach
        # to 40% calego budzetu, a dziewiaty decyl poltora budzetu. Cena limitu
        # wprost decyduje o tym, czy zlecenie sie wypelni, wiec rozjazd tej
        # wielkosci zmienia mierzona strategie, a nie tylko jej dokladnosc.
        swieca_dec = zakres_swiecy(cur, iid, h, bar_start)
        if swieca_dec:
            price = swieca_dec["close"]
        else:
            # Brak domknietej swiecy oznacza, ze dane rynkowe dla tego
            # instrumentu sa niekompletne. Decydujemy po ostatniej cenie, ale
            # GLOSNO - cicha zamiana zrodla ceny byla dokladnie tym, co
            # rozjechalo produkcje z kalibracja.
            price = mark_price(cur, iid, now)
            stats["bez_swiecy"] += 1
            if verbose:
                print(f"  UWAGA {symbol}: brak domkniętej świecy {h.bar} "
                      f"{bar_start:%m-%d %H:%M} — cena z ostatniej minuty")
        if price is None:
            # NAJCICHSZA ze wszystkich sciezek w tej petli: bez licznika, bez
            # wiersza i bez komunikatu nawet przy `--verbose`. Instrument, dla
            # ktorego gielda przestala oddawac ceny, wypadal z uniwersum
            # decyzyjnego tak, ze nie bylo tego widac NIGDZIE.
            stats["pominiete_bez_ceny"] += 1
            zapisz_pominiecie(
                cur, engine, mode, iid, bar_start, "brak_ceny",
                f"brak świecy {h.bar} {bar_start:%Y-%m-%d %H:%M} "
                f"i brak ceny minutowej na {now:%Y-%m-%d %H:%M}")
            if verbose:
                print(f"  {symbol:10s} pominięty — BRAK CENY "
                      f"(świeca {h.bar} {bar_start:%m-%d %H:%M} i minutówka)")
            continue
        if momentum:
            # Cena ta sama co dla ensemble (zamkniecie swiecy decyzyjnej), cechy
            # zastepuje sklad celu - to on trafia do `feature_snapshot`.
            stats["rozwazone"] += 1
            oceny.append({"iid": iid, "symbol": symbol, "kind": inst["kind"],
                          "quote_ccy": inst["quote_ccy"],
                          "features": cel.snapshot(symbol), "price": price,
                          "regime": None, "regime_conf": None,
                          "score": cel.sygnaly.get(symbol, 0.0),
                          "confidence": 1.0, "contributions": [],
                          "pokrycie": None,
                          "position": open_position(cur, engine, mode, iid)})
            continue
        stats["rozwazone"] += 1
        regime, regime_conf = classify_regime(features, atr_percentile_80(cur, iid, now, h))
        weights = (model["weights"] or {}).get(regime)
        score, confidence, contributions = evaluate(features, regime, weights, h)
        # Pokrycie liczymy z TEGO SAMEGO rozbicia, ktore trafia do dziennika,
        # a nie z osobnego wywolania precompute() - inaczej dwa przebiegi tych
        # samych sygnalow mogly by sie rozjechac i pokrycie opisywaloby
        # decyzje, ktorej nie podjeto.
        pokrycie = pokrycie_wag([c["name"] for c in contributions], regime, weights)
        if pokrycie < MIN_POKRYCIE:
            # DECYZJA ODLOZONA, NIE ODWOLANA - ta sama zasada co przy `MIN_CECH`
            # wyzej, tylko z miara, ktora naprawde rozstrzyga. `decided_for_bar`
            # nie zamyka swiecy, wiec cykl 20-35 min pozniej zobaczy komplet
            # i zdecyduje po tej samej cenie zamkniecia. Ślad pod WLASNYM
            # powodem, zeby "potok spozniony" dalo sie policzyc.
            stats["odlozone_niepelne"] += 1
            brakujace = sorted(set(SIGNALS) - {c["name"] for c in contributions})
            zapisz_pominiecie(
                cur, engine, mode, iid, bar_start, "niepelne_sygnaly",
                f"pokrycie wag {pokrycie:.2f} < {MIN_POKRYCIE} dla świecy "
                f"{bar_start:%Y-%m-%d %H:%M}; bez sygnałów: {', '.join(brakujace)}")
            if verbose:
                print(f"  {symbol:10s} odłożony — pokrycie wag {pokrycie:.2f}, "
                      f"czekam na komplet cech (bez: {', '.join(brakujace)})")
            continue
        if pokrycie < 1.0 - 1e-9:
            stats["niepelne_sygnaly"] += 1
        oceny.append({"iid": iid, "symbol": symbol, "kind": inst["kind"],
                      "quote_ccy": inst["quote_ccy"],
                      "features": features, "price": price,
                      "regime": regime, "regime_conf": regime_conf, "score": score,
                      "confidence": confidence, "contributions": contributions,
                      "pokrycie": pokrycie,
                      "position": open_position(cur, engine, mode, iid)})

    # SLAD POMINIEC ZATWIERDZAMY OD RAZU, nie razem z decyzjami.
    #
    # Drugi przebieg sklada zlecenia i wola gielde, wiec potrafi sie wywrocic.
    # Gdyby wiersze pominiec czekaly na wspolny commit, wyjatek w drugim
    # przebiegu kasowalby jedyny slad po tym, ze pierwszy przebieg cos odrzucil -
    # czyli awaria zacieralaby dowod na inna awarie. Tu nie ma czego cofac:
    # pominiecie JUZ zaszlo, niezaleznie od tego, co stanie sie dalej.
    if stats["pominiete_bez_cech"] or stats["pominiete_bez_ceny"]:
        conn.commit()

    # CISZA CALEGO UNIWERSUM MA BYC GLOSNA.
    #
    # Pojedynczy instrument bez cech to zdarzenie; CALE uniwersum bez cech tej
    # swiecy to potok, ktory sie zatrzymal - a silnik, ktory nie handluje, bo
    # nie ma na czym, wyglada dokladnie tak samo jak silnik, ktory nie handluje,
    # bo rynek jest spokojny. Komunikat idzie BEZ `verbose`, bo jest to jedyne
    # miejsce, w ktorym ta roznica pada wprost do logu zadania.
    do_oceny = stats["instrumenty"] - stats["poza_swieca"]
    if do_oceny > 0 and stats["cechy_z_poprzedniej_swiecy"] == do_oceny:
        print(f"  UWAGA: ŻADEN z {do_oceny} instrumentów nie ma kompletu cech "
              f"świecy {bar_start:%Y-%m-%d %H:%M} — potok cech spóźniony. "
              f"Decyzje odłożone do następnego cyklu; jeśli opóźnienie "
              f"przekracza {h.bar_minutes} min, silnik NIE HANDLUJE. "
              f"Sprawdź: kubectl logs deploy/trader-features --tail=30")

    # PRZEBIEG DRUGI: najpierw wyjscia (nie konkuruja o kapital i zwalniaja go),
    # potem wejscia w kolejnosci malejacej sily sygnalu.
    def priorytet(o):
        wychodzi = o["position"] is not None
        return (0 if wychodzi else 1, -abs(o["score"]), o["symbol"])

    for ocena in sorted(oceny, key=priorytet):
        started = time.perf_counter()
        iid, symbol = ocena["iid"], ocena["symbol"]
        features, price = ocena["features"], ocena["price"]
        regime, regime_conf = ocena["regime"], ocena["regime_conf"]
        score, confidence = ocena["score"], ocena["confidence"]
        contributions, pokrycie = ocena["contributions"], ocena["pokrycie"]

        position = ocena["position"]

        # --- wybor akcji ----------------------------------------------------
        #
        # KAZDA GALAZ KONCZACA SIE HOLDEM USTAWIA `powod_holdu`.
        #
        # Bez tego dziennik zapisywal 782 holdy na 818 decyzji i o zadnym z nich
        # nie mowil, dlaczego. Najgorszy przypadek: wynik -0,644 przy progu
        # 0,278 - dwa i pol raza ponad progiem - z werdyktem ryzyka 'allow'
        # i pustym powodem, bo blokada krotkiej pozycji podmienia akcje na hold
        # PRZED galezia ryzyka. Powod jest wiec czescia WYBORU AKCJI, a nie
        # czescia werdyktu ryzyka, i musi powstawac tutaj, w tym samym `if`.
        powod_holdu = None
        # PROGI TEGO REZIMU. `None` przy wejsciu znaczy "reżim bez skalibrowanego
        # progu" i jest odpowiedzia inna niz jakakolwiek liczba - patrz
        # `services/engine/bramki.py`. Prog WYJSCIA jest zawsze liczba.
        if momentum:
            # Regula bez progow: NULL w dzienniku, bo progu nie bylo.
            prog_wejscia_rezimu = prog_wyjscia_rezimu = None
        else:
            prog_wejscia_rezimu = model["bramki"].wejscie(regime)
            prog_wyjscia_rezimu = model["bramki"].wyjscie(regime)
        if momentum:
            # Cala regula w `strategia_momentum.akcja` (czysta funkcja, tam
            # testy galezi). Powody holdu z jej zbioru stoja w CHECK 0091.
            action, powod_holdu = strategia_momentum.akcja(cel, position, symbol,
                                                           decyzyjna)
        elif position:
            same_way = (position["side"] == "long" and score > 0) or \
                       (position["side"] == "short" and score < 0)
            if not same_way or abs(score) < prog_wyjscia_rezimu:
                action = "exit"
            else:
                action = "hold"
                powod_holdu = "trzyma_pozycje"
        elif prog_wejscia_rezimu is None:
            # REZIM BEZ PROGU NIE HANDLUJE. Gałąź stoi PRZED porownaniem
            # z progiem, bo nie ma z czym porownywac; `slaby_wynik` bylby tu
            # zdaniem nieprawdziwym - wynik moze byc dowolnie mocny.
            action = "hold"
            powod_holdu = "rezim_bez_progu"
            stats["rezim_bez_progu"] += 1
        elif abs(score) >= prog_wejscia_rezimu and confidence >= model["min_conf"]:
            action = "enter_long" if score > 0 else "enter_short"
            # Krotkiej pozycji na spocie nie da sie zajac bez pozyczki marzowej.
            # Silnik podejmowal je swobodnie - 8 z 12 pozycji w dzienniku to byly
            # shorty na instrumentach spotowych, czyli transakcje niewykonalne.
            if action == "enter_short" and not koszty(ocena["kind"]).krotka_pozycja:
                action = "hold"
                powod_holdu = "short_niewykonalny"
                stats["short_niewykonalny"] += 1
        elif abs(score) < prog_wejscia_rezimu:
            # SLABY WYNIK MA PIERWSZENSTWO PRZED NISKA PEWNOSCIA.
            #
            # Warunek wyzej laczy oba progi jednym `and`, wiec sama akcja nie
            # mowi, ktory z nich nie przeszedl. Rozdzielamy je w tej samej
            # kolejnosci, w ktorej czyta je czlowiek: najpierw pytamy, czy
            # sygnal byl dosc silny, a dopiero potem, czy byl dosc pewny.
            # Gdy nie przeszly oba, powodem jest slaby wynik - bo to on jest
            # warunkiem, ktory na produkcji wiaze (progu pewnosci nie naruszyla
            # ani jedna z 818 decyzji).
            action = "hold"
            powod_holdu = "slaby_wynik"
        else:
            action = "hold"
            powod_holdu = "niska_pewnosc"

        # SZEROKI SPREAD BLOKUJE WEJSCIE, NIE WYJSCIE (F-1.5). Po wyborze akcji,
        # bo powod mowi wtedy prawde: sygnal BYL dosc mocny, wejscie zatrzymal
        # koszt. Przed galezia ryzyka - to wlasnosc instrumentu, nie konta.
        if action.startswith("enter") and iid in szerokie_spready:
            action = "hold"
            powod_holdu = "szeroki_spread"
            stats["szeroki_spread"] += 1

        # Wielkosc pozycji rosnie z sila sygnalu, ale nigdy powyzej limitu -
        # o tym decyduje risk kernel, nie ten kod. Wzor i zaokraglenia z
        # `risk_rules.wielkosc_pozycji` - tej samej funkcji liczy symulacja (U-3).
        qty = Decimal(0)
        notional = Decimal(0)
        kwota_momentum = ekspozycja_momentum = None
        if action in ("enter_long", "enter_short"):
            if momentum:
                # ROWNE WAGI PRZY WEJSCIU (H-3): 5% kapitalu niezaleznie od
                # sily sygnalu - stad udzial 1,0 zamiast `score`. Bez kapitalu
                # `baza` jest juz zerem, a wejscia wstrzymane (wyzej).
                #
                # RESZTA DO SUFITU (T-8, 2026-10-04): gdy trzymane pozycje
                # urosly i pelne 5% przekroczyloby 15%, wejscie idzie za reszte
                # do sufitu (`kwota_wejscia`), a nie odpada w calosci.
                kwota_momentum = baza_momentum
                if baza_momentum > 0:
                    ekspozycja_momentum = risk.ekspozycja()
                    kwota_momentum = strategia_momentum.kwota_wejscia(
                        kapital, ekspozycja_momentum)
                notional, qty = wielkosc_pozycji(kwota_momentum, 1.0, price)
            else:
                notional, qty = wielkosc_pozycji(baza, score, price)

        if action.startswith("enter") and wejscia_wstrzymane:
            # Kill-switch, przekroczony limit konta albo niepelny pomiar
            # kapitalu - wejscie odpada, ale decyzje i tak zapisujemy, zeby
            # dziennik pokazywal, ze system CHCIAL wejsc i co go zatrzymalo.
            verdict = werdykt_wstrzymania
            action = "hold"
            powod_holdu = "wstrzymanie_ryzykiem"
        elif action.startswith("enter") and decyzja_przeterminowana(now, bar_start, h):
            # PO WSTRZYMANIU RYZYKIEM, NIE PRZED NIM. Proba pozarowa
            # (`proba_killswitcha`) rozpoznaje dzialajace ogniwo 3 po
            # `wstrzymanie_ryzykiem`; zamiar wejscia przy spoznionym potoku
            # w oknie proby musi nadal je pokazac.
            verdict = type("V", (), {"verdict": "allow", "reason": None, "qty": qty,
                                     "violations": [], "usage": []})()
            action = "hold"
            powod_holdu = "decyzja_przeterminowana"
            stats["decyzja_przeterminowana"] += 1
        elif action.startswith("enter"):
            if momentum and (notional <= 0 or not strategia_momentum.miesci_sie(
                    kapital, ekspozycja_momentum, notional)):
                # WLASNY SUFIT STRATEGII (15%), na tej samej ekspozycji co
                # `max_total_exposure` (pozycje po cenie rynkowej + zlecenia
                # i plany wejsc). Limit konta moze kiedys urosnac; budzet
                # pomiaru nie. Odmowa jak z risk kernela: `block` z powodem.
                # `notional <= 0` = reszta do sufitu ponizej 1% kapitalu.
                # `ekspozycja_momentum` None = budzet zero (brak pomiaru), a wtedy
                # wejscia i tak sa wstrzymane wyzej - reszta nie ma znaczenia.
                reszta = (strategia_momentum.sufit(kapital) - ekspozycja_momentum
                          if ekspozycja_momentum is not None else Decimal(0))
                verdict = RiskVerdict(
                    verdict="block",
                    reason=f"Budżet strategii {strategia_momentum.OPIS}: reszta do "
                           f"sufitu {strategia_momentum.UDZIAL_LACZNY:.0%} kapitału "
                           f"({strategia_momentum.sufit(kapital):.0f}) to "
                           f"{max(reszta, Decimal(0)):.2f}, mniej niż 1% kapitału")
            else:
                verdict = risk.check(iid, notional, qty)
        else:
            verdict = type("V", (), {"verdict": "allow", "reason": None, "qty": qty,
                                     "violations": [], "usage": []})()

        top = sorted(contributions, key=lambda c: -abs(c["contribution"]))[:2]
        top_txt = ", ".join(f"{c['label'].lower()} ({c['contribution']:+.3f})" for c in top)
        rationale = (
            strategia_momentum.uzasadnienie(cel, symbol)
            + (f" Wejście {kwota_momentum} (budżet {baza_momentum}, reszta do "
               f"sufitu {strategia_momentum.sufit(kapital) - ekspozycja_momentum:.2f})."
               if kwota_momentum is not None and 0 < kwota_momentum < baza_momentum
               else "")
            + (f" Ryzyko: {verdict.reason}." if verdict.reason else "")
            + (f" {OPIS_POWODU_HOLDU[powod_holdu]}" if powod_holdu else "")
        ) if momentum else (
            f"Reżim: {regime}. Wynik złożony {score:+.3f} przy pewności {confidence:.0%}. "
            f"Największy wkład: {top_txt}."
            # Niepelny komplet mowimy WPROST w uzasadnieniu, bo wynik go juz nie
            # zdradza: po renormalizacji +0,45 z trzech sygnalow wyglada tak samo
            # jak +0,45 z osmiu. Czytelnik "Ruchow" ma prawo wiedziec, ile
            # sygnalow za ta liczba stalo.
            + (f" Niepełny komplet sygnałów: {pokrycie:.0%} budżetu wag."
               if pokrycie < 1.0 - 1e-9 else "")
            + (f" Ryzyko: {verdict.reason}." if verdict.reason else "")
            # POWOD BEZCZYNNOSCI TAKZE SLOWAMI, nie tylko w kolumnie.
            #
            # Uzasadnienie jest tym, co widzi czlowiek w zakladce "Ruchy",
            # i do tej pory przy holdzie konczylo sie na wyniku i pewnosci -
            # czyli na liczbach, ktore CZESTO PRZECZYLY decyzji. Wpis "wynik
            # -0,644 przy pewnosci 70%" pod progiem 0,278 czytalo sie jak
            # awarie, a byla to poprawnie odrzucona krotka pozycja.
            + (f" {OPIS_POWODU_HOLDU[powod_holdu]}" if powod_holdu else "")
        )

        latency = int((time.perf_counter() - started) * 1000)
        did = record_decision(cur, engine, mode, iid, now, action,
                              verdict.qty if action.startswith("enter") else (
                                  position["qty"] if position else None),
                              price, confidence, regime, features, verdict,
                              rationale, contributions, signal_ids, latency,
                              model.get("model_version_id"), pokrycie,
                              # DO DZIENNIKA IDA PROGI TEGO REZIMU, nie liczby
                              # zbiorcze modelu: kolumny `prog_wejscia`
                              # i `prog_wyjscia` sa jedynym sladem, po ktorym
                              # da sie pozniej sprawdzic, z czym ten wynik
                              # porownano. Przy rezimie bez progu zostaje NULL -
                              # bo progu nie bylo, a 0,25 udawaloby, ze byl.
                              powod_holdu, prog_wejscia_rezimu,
                              prog_wyjscia_rezimu,
                              bar_start=bar_start,
                              strategia=STRATEGIE[strategia])

        risk.record_usage(verdict.usage)
        risk.record_violations(verdict.violations, did)

        if did and verdict.verdict != "block":
            # Model egzekucji z konfiguracji, nie z domyslu w tym pliku: `kind`
            # wyznacza cennik gieldy, `limit` - czy placimy stawke maker bez
            # poslizgu, czy taker ze spreadem. Te same dwie wartosci dostaje
            # trener, z jednego miejsca w charcie.
            # Z LIMITEM: plan na nastepna swiece. RYNKOWE: wypelnienie od razu.
            #
            # Zlecenie z limitem zlozone na zamknieciu swiecy T nie jest faktem,
            # tylko zamiarem - rozstrzyga sie w swiecy T+1 i moze sie nie
            # wypelnic. Zapisywanie go od razu jako wypelnienia zawyzalo krzywa
            # kapitalu systematycznie w jedna strone.
            if action == "exit" and position:
                if limit:
                    zaplanuj(cur, did, iid, "sell" if position["side"] == "long" else "buy",
                             Decimal(position["qty"]), price, h, bar_start,
                             "wyjscie", position["id"])
                else:
                    close_paper(cur, position, price, now, did, kind_kosztow, limit)
                stats["wyjscia"] += 1
            elif action.startswith("enter") and verdict.qty and verdict.qty > 0:
                strona = "buy" if action == "enter_long" else "sell"
                if limit:
                    zaplanuj(cur, did, iid, strona, verdict.qty, price, h, bar_start,
                             "wejscie")
                else:
                    execute_paper(cur, engine, mode, iid, did, strona,
                                  verdict.qty, price, now,
                                  kind_kosztow, limit, ocena["quote_ccy"])
                stats["wejscia"] += 1
            else:
                stats["wstrzymane"] += 1
        elif verdict.verdict == "block":
            stats["zablokowane"] += 1

        if verbose:
            print(f"  {symbol:10s} {regime or '-':11s} wynik={score:+.3f} pewność={confidence:.0%} "
                  f"-> {action:12s} [{verdict.verdict}]"
                  + (f" kwota {notional}" if action.startswith("enter") else "")
                  + (f" ({powod_holdu})" if momentum and powod_holdu else ""))
        conn.commit()

    # WYSYLKA NA KONIEC PRZEBIEGU, PO ZATWIERDZENIU WSZYSTKICH DECYZJI.
    #
    # Wysylanie kazdego planu od razu po jego powstaniu oznaczaloby, ze przerwany
    # w polowie przebieg zostawia czesc zlecen na gieldzie, a czesc decyzji
    # nierozwazonych - stan, ktorego zadna strona nie potrafi opisac. Tutaj albo
    # decyzje sa komplete, albo nie wysylamy nic.
    #
    # PRZY WSTRZYMANYCH WEJSCIACH WYSYLAMY DALEJ WYJSCIA.
    #
    # Kill-switch blokuje wylacznie wejscia (patrz wyzej) - plany wyjscia
    # POWSTAJA nadal. Niewyslanie ich na gielde uwiezilo by pozycje dokladnie
    # w chwili, w ktorej system stwierdzil, ze ryzyko jest za duze. Bezpiecznik
    # ma wyprowadzac z ryzyka, a nie zamykac w nim.
    # SPRZATANIE PRZED WYSYLKA, nie po niej. Zlecenie, ktore przezylo swoj plan,
    # zajmuje miejsce w limicie zlecen na godzine i moze sie wypelnic obok
    # decyzji - a nowe zlecenia nie maja pierwszenstwa przed posprzataniem po
    # starych.
    if limit and gielda is not None:
        from services.engine.gielda_exec import posprzataj_zlecenia
        stats.update(await posprzataj_zlecenia(
            cur, gielda, engine=engine, mode=mode, kategoria=kategoria,
            verbose=verbose))
        conn.commit()

    if limit and gielda is not None:
        from services.engine.gielda_exec import zloz_oczekujace
        from services.engine.gielda_exec import ponow_nieznane

        # STAN SESJI CZYTAMY PONOWNIE, TU, NA KONCU PRZEBIEGU.
        #
        # BRAMKA 1 wyzej pyta o sesje na POCZATKU cyklu, a wysylka dzieje sie na
        # jego koncu - miedzy jednym a drugim mija caly przebieg dla calego
        # uniwersum. Sesja zamknieta w tym czasie nie byla przez nikogo
        # zauwazana: cykl wystartowany 15:59 czasu Nowego Jorku przechodzil
        # bramke jako "otwarta" i wysylal zlecenia po 16:00, gdy arkusz jest juz
        # zamrozony. Prog wieku arkusza tego nie lapie, bo drugie zrodlo ceny
        # (`cena_odniesienia`) nie ma znacznika czasu i poza sesja SIE UDAJE -
        # wiec zlecenie szlo po cenie zamkniecia, a Alpaca kolejkowala je `day`
        # do nastepnego otwarcia (patrz `gielda_exec.zloz_plan`).
        #
        # KOSZT: jedno zapytanie `/v2/clock` na cykl dla equity i ZERO dla
        # krypto - `stan_sesji` konczy pytanie na `is_24_7`, nie siegajac po
        # zegar. To ten sam rachunek, ktory stoi w migracji 0055.
        #
        # STAN NIEZNANY NIE PRZERYWA TU CYKLU (inaczej niz przed `run_once`,
        # gdzie podnosi sie do wyjatku): decyzje sa juz zapisane, a `zloz_plan`
        # wstrzymuje na tym stanie WEJSCIA i przepuszcza WYJSCIA. Przerwanie
        # zabralo by wyjsciom wysylke, czyli wiezilo pozycje przy awarii odczytu
        # zegara.
        sesja_wysylki = await stan_sesji(cur, engine, gielda)
        if verbose and not sesja_wysylki.wolno_decydowac:
            print(f"  wysyłka: {sesja_wysylki.opis()} — wejścia WSTRZYMANE, "
                  f"wyjścia idą dalej")
        # KILL-SWITCH CZYTANY PONOWNIE PRZED WYSYLKA (przeglad 2026-10-04, T-2).
        # `wejscia_wstrzymane` policzono na poczatku cyklu, a dozorca (co 60 s)
        # albo wlasciciel z panelu moze zapisac zatrzymanie w trakcie przebiegu.
        # Bez tego odczytu wejscia z tej swiecy szly na gielde PO wlaczeniu
        # bezpiecznika. Zmienia sie wylacznie wysylka - decyzje w dzienniku
        # zostaja takie, jakie zapadly; nastepny cykl i tak je zdejmie
        # (`anuluj_wejscia`).
        wysylka_tylko_wyjsc = wejscia_wstrzymane
        if not wysylka_tylko_wyjsc:
            nowy = risk.killswitch_active()
            if nowy:
                wysylka_tylko_wyjsc = True
                if verbose:
                    print(f"  wysyłka: kill-switch id={nowy['id']} zapisany W TRAKCIE "
                          f"cyklu ({nowy['reason']}) — wysyłam tylko wyjścia")
        # WIERSZE `unknown` WAZNYCH PLANOW PRZED WYSYLKA - patrz
        # `gielda_exec.ponow_nieznane`. Zwolniony plan wraca do `zloz_oczekujace`
        # jako plan bez wiersza i idzie na gielde pod tym samym id.
        stats.update(await ponow_nieznane(
            cur, gielda, engine=engine, mode=mode, kategoria=kategoria,
            verbose=verbose, tylko_wyjscia=wysylka_tylko_wyjsc))
        stats.update(await zloz_oczekujace(
            cur, gielda, engine=engine, mode=mode, kategoria=kategoria, h=h,
            verbose=verbose, tylko_wyjscia=wysylka_tylko_wyjsc,
            sesja=sesja_wysylki))
        conn.commit()

    # LICZBA POZYCJI BEZ SWIEZEJ CENY IDZIE DO HEARTBEATU.
    #
    # `risk.engine_heartbeat.detail` jest jedynym miejscem, w ktorym stan
    # przebiegu widac z zewnatrz procesu. Bez tej liczby silnik handlujacy na
    # zamrozonych cenach bije dokladnie tak samo jak silnik zdrowy - a wlasnie
    # o to, zeby awaria przestala wygladac jak spokoj, tu chodzi.
    pomiar_koncowy = update_equity(cur, engine, mode, now)
    stats["bez_swiezej_ceny"] = len(pomiar_koncowy.bez_swiezej_ceny)
    if not pomiar_koncowy.pomiar_pelny and verbose:
        print(f"  UWAGA: kapitał policzony z NIEPEŁNEGO pomiaru — "
              f"{pomiar_koncowy.opis_brakow()}")
    zapisz_puls(cur, engine, mode, stats)
    conn.commit()
    return stats


# JEDNO ZAPYTANIE O KLUCZ - dla budowy adaptera i dla sprawdzenia, czy klucz
# sie nie zmienil (`odcisk_klucza`). Dwie kopie rozjechalyby sie przy pierwszej
# zmianie reguly wyboru, a wtedy silnik "wykrywalby rotacje" co cykl albo nigdy.
SQL_KLUCZE = (
    "SELECT c.id, c.key_hint, c.key_enc, c.secret_enc,"
    "       v.code AS venue_code, v.demo_host, v.live_host"
    "  FROM app.api_credential c JOIN ref.venue v ON v.id = c.venue_id"
    " WHERE v.engine::text = %s AND c.mode::text = %s AND c.active"
    " ORDER BY c.created_at DESC, c.id DESC")


def odcisk_klucza(conn, engine: str, mode: str) -> int | None:
    """Id klucza, ktory WYBRALBY `zbuduj_adapter` - albo None, gdy nie ma zadnego.

    DLACZEGO CO CYKL (audyt 2026-10-03, G3)

    Adapter powstaje raz na proces, a proces zyje tygodniami. Klucz wymieniony
    w Ustawieniach (nowy wiersz, stary dezaktywowany) nie docieral do silnika
    az do restartu poda: silnik dalej podpisywal starym, gielda odbijala 10003,
    a do 2026-10-03 kazde takie zlozenie zapisywalo sie jako `unknown`, czyli
    "nie wiemy" zamiast "klucz nie dziala". Porownanie id kosztuje jedno
    zapytanie do bazy na cykl i nic po stronie gieldy.
    """
    w = conn.execute(SQL_KLUCZE, (engine, mode)).fetchone()
    return w["id"] if w else None


class DrugaPetla(RuntimeError):
    """Inny proces trzyma cykl tego silnika i trybu - ten cykl sie NIE odbywa."""


def klucz_cyklu(engine: str, mode: str) -> str:
    return f"engine:{engine}:{mode}"


def zajmij_cykl(conn, engine: str, mode: str) -> None:
    """Blokada doradcza w bazie: JEDEN cykl silnika (engine, mode) naraz.

    DLACZEGO NIE WYSTARCZA `replicas: 1` I `Recreate`

    Chronia wylacznie przy zwyklym rolloucie. Dwa realne scenariusze ich nie
    dotycza: reczne `loop.py --once --limit` przez `kubectl exec` obok
    dzialajacego poda i pod zawieszony w `Terminating`, ktory dalej chodzi.
    Klucz zlecenia `tai-p{id planu}` nie chroni wtedy przed niczym: dwie petle
    zapisuja DWIE decyzje (`decided_at` to zegar, wiec ON CONFLICT w
    `record_decision` nie zadziala), z nich DWA plany o roznych id, i na
    gielde ida dwa zlecenia - podwojna pozycja, ktora w dzienniku wyglada jak
    strategia wchodzaca mocniej, niz zakladal risk kernel (audyt 2026-10-03, G1).

    SESYJNA, NIE TRANSAKCYJNA. `run_once` commituje wiele razy, a blokada
    transakcyjna puszczalaby przy pierwszym commicie. Sesyjna zyje tyle, co
    polaczenie - a `main` otwiera je NA KAZDY CYKL, wiec zamkniecie polaczenia
    (takze po wyjatku i po smierci procesu) zwalnia ja bez sprzatania.

    `try`, NIE CZEKANIE. Druga petla ma sie dowiedziec i odpuscic, a nie stanac
    w kolejce za pierwsza i wykonac ten sam cykl chwile pozniej. Odmowa jest
    wyjatkiem, zeby wpadla w licznik pieciu awarii pod rzad: proces, ktory nigdy
    nie dostaje cyklu, ma sie skonczyc widocznie, a nie bic puls bez pracy.
    """
    wolne = conn.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS wolne",
                         (klucz_cyklu(engine, mode),)).fetchone()["wolne"]
    if not wolne:
        raise DrugaPetla(
            f"cykl silnika {engine}/{mode} trzyma INNY proces (blokada "
            f"doradcza {klucz_cyklu(engine, mode)!r}) — ten cykl się nie "
            f"odbywa. Sprawdź `kubectl get pods` (pod w Terminating?) i ręczne "
            f"`loop.py --once` obok działającego silnika")


async def zbuduj_adapter(url: str, mode: str, engine: str = "crypto"):
    """Adapter gieldy dla trybu I SILNIKA, z hostem WZIETYM Z ref.venue.

    Gielda wynika z silnika: `crypto` -> bybit, `equity` -> alpaca (0046).
    Wybor klasy adaptera siedzi w `services.exchanges.fabryka` - tu tylko
    klucz i host.

    HOST Z TABELI, NIE Z TABLICY W ADAPTERZE. Podmiot unijny ma wlasne hosty,
    a klucz z bybit.eu odbity od hosta globalnego wraca z kodem 10003 -
    komunikatem nie do odroznienia od zlego klucza. Ten projekt wszedl juz
    w te pulapke raz, w module uzgodnienia, mimo ze komentarz przy
    credentials.py przed nia ostrzegal.
    """
    from services.api.security import SecretBox
    from services.exchanges.fabryka import zbuduj_adapter_gieldy

    with psycopg.connect(url, row_factory=dict_row) as conn:
        # NAJNOWSZY KLUCZ, NIE NAJSTARSZY. Rotacja zostawia na chwile dwa
        # aktywne wiersze; przy `ORDER BY c.id` wygrywal ten, ktory wlasnie
        # zastapiono - i to bez sladu. Objaw pojawial sie dopiero w dniu
        # wygasniecia starego klucza i wygladal jak awaria uwierzytelnienia
        # bez przyczyny, bo nowy klucz w bazie byl przez caly czas poprawny.
        # `c.id` zostaje jako rozstrzygniecie remisu: dwa klucze wgrane w tej
        # samej milisekundzie maja dawac ten sam wybor przy kazdym starcie,
        # inaczej silnik po restarcie handluje z innego rachunku.
        klucze = conn.execute(SQL_KLUCZE, (engine, mode)).fetchall()
    cred = klucze[0] if klucze else None
    # NIEDOKONCZONA ROTACJA MA BYC SLYSZALNA. Wybor jest juz jednoznaczny,
    # ale dwa aktywne klucze naraz to stan, ktorego nikt nie zaplanowal:
    # albo dezaktywacji starego zapomniano, albo ktos wgral klucz do zlego
    # trybu. Cisza w tym miejscu byla polowa poprzedniego bledu.
    if len(klucze) > 1:
        print(f"  UWAGA: {len(klucze)} aktywnych kluczy dla trybu {mode} — "
              f"używam najnowszego ({cred['key_hint']}); "
              f"pozostałe dezaktywuj po zakończonej rotacji")
    if not cred:
        # BRAK KLUCZA PRZY WLACZONEJ EGZEKUCJI TO BLAD KONFIGURACJI, nie powod
        # do cichego zejscia na rachunek papierowy. Silnik udajacy, ze handluje,
        # jest gorszy niz silnik, ktory nie wstal.
        sys.exit(f"egzekucja na giełdzie wymaga aktywnego klucza dla silnika {engine} "
                 f"i trybu {mode}")

    box = SecretBox()
    return zbuduj_adapter_gieldy(
        cred["venue_code"], box.decrypt(bytes(cred["key_enc"])),
        box.decrypt(bytes(cred["secret_enc"])), mode=mode,
        demo_host=cred["demo_host"], live_host=cred["live_host"])


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--engine", default="crypto")
    # Tryb byl dotad zaszyty w domysle run_once. Przy egzekucji na gieldzie
    # decyduje o tym, KTORY KLUCZ i ktory host - wiec musi byc jawny.
    ap.add_argument("--mode", default="demo", choices=("demo", "live"))
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--bar", default=DEFAULT_BAR, choices=sorted(HORIZONS))
    ap.add_argument("--sprawdz", action="store_true",
                    help="wykonaj wszystkie odczyty bez zapisu i zakończ")
    # MODEL EGZEKUCJI - te same dwa przelaczniki co w trenerze.
    #
    # Rachunek papierowy musi kosztowac tyle, ile kosztuje strategia, ktora
    # kalibrujemy. Rozjazd nie objawia sie awaria: krzywa kapitalu po prostu
    # rosnie szybciej, niz powinna, i wyglada jak wynik.
    # "stock" dolaczylo 2026-09-24 razem z petla equity. Cennik akcji na Alpace
    # to zero prowizji (koszty.py) - i to jest liczba, nie brak liczby; skladniki,
    # ktorych zero nie obejmuje, wymienia `karta_rundy` z nazwy.
    ap.add_argument("--kind", default="spot",
                    choices=("spot", "spot_eu", "perp", "stock"),
                    help="cennik giełdy dla rachunku papierowego")
    # ZWALIDOWANY MODEL JAKO WARUNEK HANDLU. Patrz "BRAMKA 2" w `run_once`.
    # Domysl `False` zostaje SWIADOMIE: wlaczenie tej reguly dla silnika, ktory
    # chodzi na produkcji od miesiecy, byloby zmiana jego zachowania przy okazji.
    ap.add_argument("--wymagaj-modelu", action="store_true", dest="wymagaj_modelu",
                    help="bez aktywnej wersji modelu silnik NIE handluje "
                         "(zamiast cofać się do założeń początkowych)")
    ap.add_argument("--limit", action="store_true",
                    help="zlecenia z limitem: stawka maker, bez poślizgu")
    # EGZEKUCJA JEST PRZELACZNIKIEM ZRODLA PRAWDY, nie sposobu decydowania.
    #
    # `papier` rozstrzyga plan patrzac na swiece: jesli cena dotknela limitu,
    # uznaje zlecenie za wypelnione w calosci. To zalozenie jest optymistyczne
    # w dwie strony naraz - zaklada wypelnienie, ktorego moglo nie byc, i calosc
    # tam, gdzie weszla czesc.
    #
    # `gielda` pyta gielde. Ilosc, cena i prowizja przestaja byc naszym
    # zalozeniem. Domysl zostaje przy papierze SWIADOMIE: wlaczenie egzekucji
    # ma byc decyzja wpisana w chart, a nie skutkiem przeoczenia flagi.
    ap.add_argument("--egzekucja", default="papier", choices=("papier", "gielda"),
                    help="papier: wypełnienie ze świecy; gielda: prawdziwe zlecenia")
    # LIKWIDACJA POZYCJI PO KILL-SWITCHU — ZLECENIEM RYNKOWYM.
    #
    # DOMYSL `False` JEST CZESCIA ZABEZPIECZENIA, nie ostroznoscia na zapas.
    # Sciezka zlecenia rynkowego na gielde nie zostala w tym systemie uzyta ANI
    # RAZU (`execution='gielda' AND order_type='market'` to zero wierszy),
    # a poslizg takera nie jest zmierzony (`POSLIZG_TAKER` ma n=0). Wlaczenie
    # jej przez wdrozenie obrazu znaczyloby, ze pierwsze prawdziwe zlecenie
    # rynkowe w historii tego systemu idzie bez wiedzy czlowieka, w nocy,
    # na calej ekspozycji naraz. To ma byc linia w `values.yaml`, tak samo jak
    # `--egzekucja gielda`.
    #
    # BEZ TEJ FLAGI KILL-SWITCH ROBI DOKLADNIE TO, CO ROBIL: wstrzymuje
    # wejscia i zdejmuje niewypelnione wejscia z arkusza. Otwarte pozycje
    # zostaja, a wyjscia ida `PostOnly` - czyli moga sie nie wypelnic.
    ap.add_argument("--likwiduj-po-killswitchu", action="store_true",
                    dest="likwiduj",
                    help="po zadziałaniu kill-switcha zamknij otwarte pozycje "
                         "ZLECENIEM RYNKOWYM (koszt: stawka taker plus "
                         "NIEZMIERZONY poślizg); bez tej flagi kill-switch "
                         "tylko wstrzymuje wejścia")
    # STRATEGIA - KTORA REGULA WYBIERA AKCJE. Domysl `model` zostaje, zeby
    # przelaczenie bylo linia w `values.yaml`, a nie skutkiem wdrozenia obrazu
    # (ta sama zasada co przy `--egzekucja gielda`). `momentum` = decyzja
    # wlasciciela "A" z 2026-10-04: zamrozona regula R4 na demo, pomiar
    # egzekucji (docs/hipotezy.md, E-1).
    ap.add_argument("--strategia", default="model", choices=sorted(STRATEGIE),
                    help="model: ensemble z aktywnej wersji modelu; momentum: "
                         "momentum + filtr trendu (zamrożone parametry H-3, "
                         "15%% kapitału)")
    args = ap.parse_args()
    if args.strategia == "momentum" and args.mode != "demo":
        # Regula bez przewagi (H-3 STOP) chodzi WYLACZNIE po to, zeby zmierzyc
        # egzekucje. Na koncie z prawdziwymi pieniedzmi nie ma czego tu mierzyc
        # za te cene - i to ma byc odmowa startu, nie ostrzezenie w logu.
        sys.exit("--strategia momentum tylko z --mode demo (pomiar egzekucji, "
                 "nie strategia z przewagą)")

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    h = get_horizon(args.bar)

    if args.sprawdz:
        with psycopg.connect(url, row_factory=dict_row) as conn:
            return await sprawdz(conn, args.engine, h)

    gielda = None
    klucz_gieldy: int | None = None
    if args.egzekucja == "gielda":
        with psycopg.connect(url, row_factory=dict_row) as conn:
            klucz_gieldy = odcisk_klucza(conn, args.engine, args.mode)
        gielda = await zbuduj_adapter(url, args.mode, args.engine)
        print(f"EGZEKUCJA NA GIEŁDZIE ({args.mode}) — zlecenia idą naprawdę.")
        # POZYCJE PAPIEROWE NIE PRZECHODZA NA GIELDE - patrz `zamknij_pozycje_papierowe`.
        with psycopg.connect(url, row_factory=dict_row) as conn:
            zamkniete = zamknij_pozycje_papierowe(conn, args.engine, args.mode, args.kind)
        if zamkniete:
            print(f"Zamknięto administracyjnie {len(zamkniete)} pozycji z rachunku "
                  f"papierowego — giełda ich nie miała.")

    from services.engine.koszty import runda
    print(f"Silnik {args.engine}, świeca {h.bar} — jedna decyzja na świecę.")
    if args.strategia == "momentum":
        print(f"STRATEGIA: {strategia_momentum.OPIS} ({strategia_momentum.NAZWA}) — "
              f"parametry zamrożone z H-3 {dict(strategia_momentum.PARAMETRY)}, "
              f"budżet {strategia_momentum.UDZIAL_LACZNY:.0%} kapitału "
              f"({strategia_momentum.UDZIAL_POZYCJI:.0%} na pozycję), wejścia tylko "
              f"na zamknięciu doby UTC. Model NIEUŻYWANY. Pomiar egzekucji, "
              f"nie strategia z przewagą (H-1, H-3: STOP).")
    else:
        print("STRATEGIA: ensemble z aktywnej wersji modelu.")
    print(f"Rachunek papierowy: {args.kind}, zlecenia "
          f"{'z limitem' if args.limit else 'rynkowe'}, "
          f"koszt rundy {runda(args.kind, args.limit) * 100:.3f}%")
    if args.limit:
        print("Zlecenia z limitem są PLANOWANE na świecę T i rozstrzygane na T+1 "
              "(kupno gdy low ≤ limit, sprzedaż gdy high ≥ limit).\n")
    else:
        print()
    # STAN TEGO PRZELACZNIKA WYPISUJEMY ZAWSZE, W OBIE STRONY. Kill-switch,
    # ktory pozycji NIE zamyka, wyglada z zewnatrz identycznie jak ten, ktory
    # zamyka i nie mial czego - a to rozstrzyga o tym, ile wlasciciel ryzykuje
    # przez noc. Zdanie o wylaczeniu jest wiec rownie potrzebne jak o wlaczeniu.
    if args.likwiduj:
        print("KILL-SWITCH LIKWIDUJE POZYCJE: po jego zadziałaniu otwarte "
              "pozycje idą ZLECENIEM RYNKOWYM\n  (stawka taker + poślizg, "
              "który w tym systemie NIGDY nie został zmierzony). Próba "
              "pożarowa\n  tego nie wyzwala — rozstrzyga znacznik "
              "`risk.limit_def.proba_do`.\n")
    else:
        print("Kill-switch NIE zamyka otwartych pozycji (brak "
              "--likwiduj-po-killswitchu):\n  wstrzymuje wejścia i zdejmuje "
              "niewypełnione wejścia z arkusza, a wyjścia idą `PostOnly`,\n  "
              "czyli mogą się NIE wypełnić.\n")

    # POLACZENIE NA KAZDY CYKL, NIE NA CALY PROCES.
    #
    # Pierwsza wersja otwierala je raz, przed petla. Po restarcie bazy kazdy
    # kolejny cykl padal na tym samym zerwanym polaczeniu, a proces szedl dalej
    # w nieskonczonosc - silnik byl martwy i wygladal na zywy. Koszt jednego
    # polaczenia na piec minut jest przy tym zaden.
    #
    # LICZNIK AWARII POD RZAD KONCZY PROCES. Wypisanie bledu do logu nie jest
    # widocznoscia - logu poda nikt nie czyta o trzeciej w nocy. Restart poda
    # jest widoczny: rosnie licznik restartow, pod wpada w CrashLoopBackOff
    # i widac to bez zagladania do niczego.
    POD_RZAD_MAX = 5
    pod_rzad = 0
    # Sonda zywotnosci (`puls.zyje`, templates/workers.yaml): na starcie i po
    # kazdym obrocie petli, takze nieudanym.
    puls.zyje(args.interval)
    # LAGODNE ZATRZYMANIE (P-T4): SIGTERM konczy petle na granicy cyklu, a nie
    # w srodku transakcji - patrz services/engine/zatrzymanie.py.
    zatrzymanie = Zatrzymanie().zarejestruj()
    while True:
        if zatrzymanie.ustawione:
            return 0
        t0 = datetime.now(timezone.utc)
        try:
            with psycopg.connect(url, row_factory=dict_row) as conn:
                # JEDNA PETLA NA SILNIK I TRYB - patrz `zajmij_cykl`. Blokada
                # zyje tyle, co to polaczenie.
                zajmij_cykl(conn, args.engine, args.mode)
                # KLUCZ ZMIENIONY W UI -> NOWY ADAPTER, BEZ RESTARTU. Patrz
                # `odcisk_klucza`. Brak klucza to awaria cyklu (licznik pieciu),
                # tak jak brak klucza przy starcie jest awaria startu.
                if gielda is not None:
                    teraz_klucz = odcisk_klucza(conn, args.engine, args.mode)
                    if teraz_klucz is None:
                        raise RuntimeError(
                            f"egzekucja na giełdzie bez aktywnego klucza dla "
                            f"{args.engine}/{args.mode} — klucz usunięty w trakcie "
                            f"pracy silnika")
                    if teraz_klucz != klucz_gieldy:
                        print(f"[{t0:%H:%M:%S}] klucz giełdy zmieniony "
                              f"({klucz_gieldy} -> {teraz_klucz}) — przebudowuję adapter")
                        nowa = await zbuduj_adapter(url, args.mode, args.engine)
                        try:
                            await gielda.close()
                        except Exception as e:                # noqa: BLE001
                            print(f"  (stary adapter nie zamknął się czysto: {e})")
                        gielda, klucz_gieldy = nowa, teraz_klucz
                # STAN SESJI LICZY SIE CO CYKL, NIE RAZ PRZED PETLA. Proces
                # zyje tygodniami; otwarcie i zamkniecie wypadaja w jego
                # srodku, a swieta gieldowe nie pytaja o restart poda.
                sesja = await stan_sesji(conn.cursor(), args.engine, gielda, teraz=t0)
                # "NIE WIEM" JEST AWARIA, A NIE CICHYM POMINIECIEM.
                #
                # `run_once` na tym stanie i tak nie handluje (bramka 1), ale
                # samo pominiecie byloby niewidoczne: silnik bilby puls
                # "running" i nie robil nic - tygodniami, bez jednej czerwonej
                # linii. Podniesienie tego do wyjatku wpina stan w licznik
                # pieciu awarii pod rzad, ktory KONCZY PROCES - a restart poda
                # widac w `kubectl get pods`, czego wpisu w logu nie.
                if sesja.otwarta is None:
                    raise RuntimeError(sesja.opis())
                s = await run_once(conn, args.engine, mode=args.mode,
                                   kind_kosztow=args.kind, limit=args.limit,
                                   verbose=not args.quiet, h=h, gielda=gielda,
                                   wymagaj_modelu=args.wymagaj_modelu,
                                   sesja=sesja, likwiduj=args.likwiduj,
                                   strategia=args.strategia)
            # CYKL ZABLOKOWANY BRAMKA MA WLASNA LINIE, NIE SAME ZERA.
            #
            # "rozwazone 0, wejscia 0, wyjscia 0..." przez cala noc wyglada
            # identycznie jak silnik, ktory patrzy i nic nie znajduje. Powod
            # blokady jest jedyna informacja w takim cyklu, wiec jest jedyna
            # rzecza, ktora ta linia ma zawierac.
            if s.get("poza_sesja"):
                print(f"[{t0:%H:%M:%S}] {sesja.opis()} — bez decyzji")
            elif s.get("bez_modelu"):
                print(f"[{t0:%H:%M:%S}] BEZ ZWALIDOWANEGO MODELU "
                      f"({s['model_nazwa']}) — bez decyzji")
            else:
                plany = (f", plany: wypełnione {s['wypelnione']}, "
                         f"niewypełnione {s['niewypelnione']}, czekają {s['czekaja']}"
                         if args.limit else "")
                if s.get("bez_swiecy"):
                    plany += f", BEZ ŚWIECY {s['bez_swiecy']}"
                # POMINIECIA W JEDNEJ LINII PODSUMOWANIA, a nie tylko w komunikatach
                # `--verbose` wewnatrz przebiegu: przy `--quiet` (tak chodzi produkcja)
                # tamtych nie ma wcale, a ta linia jest zawsze.
                if s.get("odlozone_momentum"):
                    plany += f", ODŁOŻONE (świeca BTC niedomknięta) {s['odlozone_momentum']}"
                if s.get("pominiete_bez_cech") or s.get("pominiete_bez_ceny"):
                    plany += (f", POMINIĘTE: bez cech {s['pominiete_bez_cech']}, "
                              f"bez ceny {s['pominiete_bez_ceny']}")
                print(f"[{t0:%H:%M:%S}] rozważone {s['rozwazone']}, wejścia {s['wejscia']}, "
                      f"wyjścia {s['wyjscia']}, wstrzymane {s['wstrzymane']}, "
                      f"zablokowane {s['zablokowane']}, "
                      f"już rozstrzygnięte w tej świecy {s['poza_swieca']}{plany}")
            pod_rzad = 0
        except Exception as e:                                # noqa: BLE001
            pod_rzad += 1
            # BLAD PRZEBIEGU WYPISUJEMY ZAWSZE, TAKZE PRZY --quiet. `--quiet`
            # wycisza rutynowy postep, a nie awarie.
            print(f"[{t0:%H:%M:%S}] przebieg przerwany ({pod_rzad}/{POD_RZAD_MAX}): "
                  f"{type(e).__name__}: {e}")
            if pod_rzad >= POD_RZAD_MAX:
                print(f"{POD_RZAD_MAX} przebiegów pod rząd zakończonych błędem — "
                      f"kończę proces, żeby restart był WIDOCZNY zamiast cichy")
                return 1
        # Obrot petli, takze nieudany - sonda zywotnosci (`puls.zyje`).
        puls.zyje(args.interval)
        if args.once:
            # Kod wyjscia MOWI, jak poszedl ten jeden cykl. Do 2026-10-02 stalo
            # tu `return 0` takze po przebiegu przerwanym wyjatkiem - kazde
            # wywolanie z `--once` (reczne sprawdzenie, Job, skrypt) widzialo
            # sukces tam, gdzie cykl sie wywrocil.
            return 1 if pod_rzad else 0
        if await zatrzymanie.czekaj(args.interval):
            return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
