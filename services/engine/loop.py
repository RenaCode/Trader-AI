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
from services.engine.risk import RiskKernel
from services.engine.uniwersum import odcisk
from services.engine.koszty import POSLIZG_MAKER, POSLIZG_TAKER, koszty
from services.engine.portfolio import _wypelnione
from services.engine.risk_rules import (
    MIN_CONFIDENCE, START_EQUITY as START_EQUITY_WSPOLNY, RiskVerdict,
    position_budget, udzial_budzetu,
)
from services.engine.signals import (SIGNALS, classify_regime, evaluate,
                                     pokrycie_wag)

# Progi awaryjne - obowiazuja WYLACZNIE, dopoki nie ma aktywnej wersji modelu.
# Docelowo pochodza z rejestru (learn.model_version.params), gdzie zapisuje je
# trener, wyliczone z rozkladu wynikow na oknach treningowych. Trzymanie ich
# w dwoch miejscach bylo pewna droga do rozjazdu trening-produkcja.
ENTER_THRESHOLD = 0.25
EXIT_THRESHOLD = 0.12
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

START_EQUITY = Decimal(str(START_EQUITY_WSPOLNY))


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
                "min_conf": MIN_CONFIDENCE, "version": None}
    p = row["params"]
    return {
        "weights": p["weights"],
        "enter": float(p.get("enter_threshold", ENTER_THRESHOLD)),
        "exit": float(p.get("exit_threshold", EXIT_THRESHOLD)),
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
                    feature_ids: list[int]) -> dict:
    """Odczyt point-in-time. Jedyna dozwolona sciezka - patrz komentarz modulu."""
    cur.execute(
        "SELECT feature_name, value FROM feat.as_of(%s, %s, %s, %s::interval)",
        (instrument_id, as_of, feature_ids or None, f"{h.max_staleness_min} minutes"))
    # feature_name ma postac 'rola__swieca@wersja'; do sygnalow idzie sama rola.
    return {Horizon.role_of(r["feature_name"]): r["value"] for r in cur.fetchall()}


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
    # ZASIEW TYLKO DLA DEMO. Na zywym rachunku wpisanie 100 000 jako punktu
    # wyjscia jest fikcja, od ktorej liczone sa potem wszystkie limity
    # procentowe i cale obsuniecie.
    if mode != "demo":
        return
    cur.execute("SELECT 1 FROM exec.equity_point WHERE engine=%s AND mode=%s LIMIT 1",
                (engine, mode))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO exec.equity_point (engine, mode, event_time, equity, cash,"
            " unrealized, realized_cum, exposure, nav, units, wplaty_netto)"
            " VALUES (%s,%s, now(), %s, %s, 0, 0, 0, 1.0, %s, %s)",
            (engine, mode, START_EQUITY, START_EQUITY, START_EQUITY, START_EQUITY))


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
    cur.execute(
        "INSERT INTO exec.equity_point (engine, mode, event_time, equity, cash, unrealized,"
        " realized_cum, exposure, nav, units, wplaty_netto)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (engine, mode, event_time) DO NOTHING",
        (engine, mode, as_of.replace(second=0, microsecond=0),
         equity, equity - exposure, unrealized, realized, exposure,
         nav, units, wplaty))
    return w


def record_decision(cur, engine: str, mode: str, instrument_id: int, as_of: datetime,
                    action: str, qty, price, confidence: float, regime: str,
                    features: dict, verdict, rationale: str, contributions: list[dict],
                    signal_ids: dict[str, int], latency_ms: int,
                    model_version_id: int | None = None,
                    pokrycie_sygnalow: float | None = None) -> int | None:
    # model_version_id mowi, KTORA wersja modelu podjela te decyzje. Kolumna
    # istniala z kluczem obcym od migracji 0006, ale nikt jej nie wypelnial -
    # bo do tej pory petla nie uzywala zadnego modelu, wiec nie bylo czego
    # zapisac. NULL znaczy "zalozenia poczatkowe, brak zwalidowanego modelu"
    # i jest to stan poprawny, nie brak danych.
    cur.execute(
        """
        INSERT INTO journal.decision (engine, mode, instrument_id, decided_at, action,
            target_qty, target_price, confidence, regime, feature_snapshot,
            risk_verdict, risk_reason, rationale, rationale_model, latency_ms,
            model_version_id, pokrycie_sygnalow)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'reguły',%s,%s,%s)
        ON CONFLICT (engine, instrument_id, decided_at) DO NOTHING
        RETURNING id
        """,
        (engine, mode, instrument_id, as_of, action, qty, price, confidence, regime,
         json.dumps({k: round(v, 6) for k, v in features.items()}),
         verdict.verdict, verdict.reason, rationale, latency_ms, model_version_id,
         pokrycie_sygnalow))
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


def zapisz_wejscie(cur, engine: str, mode: str, instrument_id: int, decision_id: int,
                   side: str, qty: Decimal, cena: Decimal, prowizja: Decimal,
                   as_of: datetime, quote_ccy: str, meta: dict, *,
                   planned_order_id: int | None = None,
                   venue_order_id: str | None = None,
                   client_order_id: str | None = None,
                   venue_fill_id: str | None = None,
                   order_id: int | None = None,
                   prowizja_ccy: str | None = None) -> int:
    """Zapis wejscia w pozycje. JEDEN ZAPIS DLA OBU ZRODEL.

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
        cur.execute(
            "UPDATE exec.order SET status='filled', qty=%s, limit_price=%s,"
            " venue_order_id = COALESCE(%s, venue_order_id), updated_at=%s,"
            " raw = raw || %s WHERE id=%s",
            (qty, cena, venue_order_id, as_of, json.dumps(meta), oid))
    cur.execute(
        "INSERT INTO exec.fill (order_id, venue_fill_id, qty, price, fee, fee_ccy, event_time)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s)",
        # WALUTA PROWIZJI Z GIELDY, gdy ja podala. Rachunek papierowy nalicza
        # prowizje w walucie kwotowanej i tak zostaje; produkcja na spocie placi
        # ja czesto w kupionym aktywie i wpisanie tam quote_ccy zanizaloby koszt
        # wejscia o dwa rzedy wielkosci.
        (oid, venue_fill_id or f"paper-{oid}", qty, cena, prowizja,
         prowizja_ccy or quote_ccy, as_of))
    cur.execute(
        "INSERT INTO exec.position (engine, mode, instrument_id, side, qty, avg_price,"
        " opened_at, fees_paid, open_decision_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (engine, mode, instrument_id, "long" if side == "buy" else "short",
         qty, cena, as_of, prowizja, decision_id))
    return oid


def zapisz_wyjscie(cur, position: dict, cena: Decimal, prowizja: Decimal,
                   as_of: datetime, decision_id: int,
                   ilosc: Decimal | None = None) -> Decimal:
    """Zamkniecie pozycji, w calosci albo w czesci. Wspolne dla obu zrodel.

    WYJSCIE CZESCIOWE JEST NAJGORSZYM MOMENTEM NA UPROSZCZENIE. Zlecenie
    sprzedazy, ktore weszlo w polowie, zostawia na gieldzie polowe pozycji.
    Zamkniecie calego wiersza byloby wtedy zapisaniem, ze nie mamy niczego -
    czyli stworzeniem pozycji-widma, i to najgorszego rodzaju, bo o pozycji,
    ktorej przestalibysmy pilnowac. Risk kernel nie liczylby z niej ekspozycji,
    a stop na obsunieciu nie zadzialalby na cos, czego nie widzi.

    Czesc pozostala ZACHOWUJE `avg_price`. Cena wejscia nie zmienia sie od tego,
    ze sprzedalismy polowe - zmienia sie tylko ilosc.
    """
    calosc = Decimal(position["qty"])
    ile = calosc if ilosc is None else min(ilosc, calosc)
    entry = Decimal(position["avg_price"])
    diff = (cena - entry) if position["side"] == "long" else (entry - cena)
    pnl = diff * ile

    if ile >= calosc:
        cur.execute(
            "UPDATE exec.position SET closed_at=%s,"
            " realized_pnl = COALESCE(realized_pnl,0) + %s,"
            " fees_paid = fees_paid + %s, close_decision_id=%s WHERE id=%s",
            (as_of, pnl, prowizja, decision_id, position["id"]))
    else:
        cur.execute(
            "UPDATE exec.position SET qty = qty - %s,"
            " realized_pnl = COALESCE(realized_pnl,0) + %s,"
            " fees_paid = fees_paid + %s WHERE id=%s",
            (ile, pnl, prowizja, position["id"]))
    return pnl


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
                decision_id: int, kind: str, limit: bool) -> Decimal:
    fee_rate, slip = stawki(kind, limit)
    qty = Decimal(position["qty"])
    exit_px = price * (Decimal(1) - slip) if position["side"] == "long" \
        else price * (Decimal(1) + slip)
    fee = qty * exit_px * fee_rate
    return zapisz_wyjscie(cur, position, exit_px, fee, as_of, decision_id)


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
                       kind: str, verbose: bool) -> dict:
    """Sprawdza, czy plany z poprzedniej swiecy doczekaly sie wypelnienia.

    Reguly nie powtarzamy - uzywamy `portfolio._wypelnione`, tej samej funkcji,
    ktora rozstrzyga to w symulacji. Dwie kopie tej samej reguly rozjechalyby
    sie przy pierwszej zmianie, a roznica bylaby niewidoczna.
    """
    # `wyjscie_bez_pozycji` W SLOWNIKU OD POCZATKU, z zerem - tak samo jak na
    # sciezce gieldowej. Klucz pojawiajacy sie dopiero przy anomalii znaczylby,
    # ze jego brak i jego zero to dwie rozne rzeczy, a sa tym samym.
    st = {"wypelnione": 0, "niewypelnione": 0, "czekaja": 0,
          "wyjscie_bez_pozycji": 0}
    cur.execute(
        "SELECT p.id, p.decision_id, p.instrument_id, p.side, p.qty, p.limit_price,"
        "       p.trigger, p.valid_until, i.symbol, i.quote_ccy"
        " FROM journal.planned_order p JOIN ref.instrument i ON i.id = p.instrument_id"
        " WHERE p.status = 'pending' ORDER BY p.id")
    for plan in cur.fetchall():
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
                close_paper(cur, poz, cena, wypelniono, plan["decision_id"], kind, True)
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

    cur.execute("SELECT id, symbol FROM ref.instrument WHERE engine=%s AND active"
                " AND tradeable AND kind <> 'perp' ORDER BY symbol LIMIT 3", (engine,))
    for inst in cur.fetchall():
        iid, sym = inst["id"], inst["symbol"]
        probuj(f"{sym}: decyzja w tej świecy?",
               lambda i=iid: decided_for_bar(cur, engine, "demo", i, bar_start, h))
        f = probuj(f"{sym}: odczyt cech point-in-time",
                   lambda i=iid: latest_features(cur, i, now, h, ids or []))
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
                   lambda ff=f, r=regime: evaluate(
                       ff, r, (model or {}).get("weights", {}).get(r), h))

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
    def kontrola_datowania():
        cur.execute(
            """
            SELECT count(*) AS n FROM feat.feature_value fv
            JOIN feat.feature_def fd ON fd.id = fv.feature_def_id
            WHERE fd.params->>'bar' = %s
              AND fv.event_time > now() - interval '7 days'
              AND fv.ingested_at < fv.event_time + %s::interval
            """,
            (h.bar, f"{h.bar_minutes} minutes"),
        )
        n = cur.fetchone()["n"]
        if n:
            raise RuntimeError(
                f"{n} cech datowanych PRZED zamknięciem własnej świecy")
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


async def run_once(conn, engine: str = "crypto", mode: str = "demo",
                   kind_kosztow: str = "spot", limit: bool = False,
                   verbose: bool = True, h: Horizon | None = None,
                   gielda=None, kategoria: str = "spot") -> dict:
    """Jeden przebieg silnika.

    `gielda` PRZELACZA ZRODLO PRAWDY O WYPELNIENIU, a nie sposob decydowania.
    Bez niej plan rozstrzyga swieca (rachunek papierowy); z nia - odpowiedz
    gieldy. Decyzje, limity i dziennik sa w obu przypadkach te same, bo to nie
    egzekucja ma o nich stanowic.
    """
    h = h or get_horizon(DEFAULT_BAR)
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

    powody_wstrzymania = []
    if konto.verdict != "allow":
        powody_wstrzymania.append(konto.reason)
    if not pomiar.pomiar_pelny:
        powody_wstrzymania.append(
            f"pomiar niepełny: {len(pomiar.bez_swiezej_ceny)} "
            f"pozycji bez świeżej ceny ({pomiar.opis_brakow()})")

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
            powod=f"wstrzymanie wejść: {powod_wstrzymania}")
        conn.commit()
        # KOMUNIKAT ZAWSZE, TAKZE PRZY ZERZE. Wczesniej pojawial sie wylacznie
        # przy `zdjete > 0`, wiec kill-switch, ktoremu NIE UDALO SIE zdjac ani
        # jednego zlecenia, wygladal identycznie jak kill-switch, ktory nie mial
        # czego zdejmowac.
        if verbose:
            print(f"  wstrzymanie wejść: zdjęto z giełdy {zdjete} zleceń"
                  + (f", {wypelnione} było już wypełnionych — zostają "
                     f"do zaksięgowania" if wypelnione else ""))

    now = datetime.now(timezone.utc)
    bar_start = current_bar(now, h)
    model = active_model(cur, engine, h)
    if verbose:
        print(f"  wagi: {'wersja modelu ' + str(model['version']) if model['version'] else 'założenia początkowe (brak zwalidowanego modelu)'}"
              f", próg wejścia {model['enter']:.4f}")
    stats = {"rozwazone": 0, "wejscia": 0, "wyjscia": 0, "wstrzymane": 0,
             "zablokowane": 0, "poza_swieca": 0, "bez_swiecy": 0,
             "wypelnione": 0, "niewypelnione": 0, "czekaja": 0,
             # Ile decyzji zapadlo na NIEPELNYM komplecie sygnalow. Idzie do
             # heartbeatu, bo poza tym stanu tego nie widac z zewnatrz w ogole:
             # od czasu renormalizacji wyniku (services/engine/signals.combine)
             # decyzja z trzech sygnalow ma te sama skale co decyzja z osmiu.
             "niepelne_sygnaly": 0}

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
        stats.update(rozstrzygnij_plany(cur, engine, mode, h, now,
                                        kind_kosztow, verbose))
        conn.commit()

    # `tradeable`, nie `active`: historię zbieramy szerzej, niż handlujemy.
    # Obstawianie instrumentu, którego giełda z kontem nie oferuje, to decyzja
    # niewykonalna - a trener kalibruje wagi na tym samym zbiorze (0020).
    cur.execute("SELECT id, symbol, kind, quote_ccy FROM ref.instrument WHERE engine=%s"
                " AND active AND tradeable AND kind <> 'perp' ORDER BY symbol", (engine,))
    instrumenty = cur.fetchall()

    # IDENTYFIKATORY CECH DOPIERO TERAJ, bo cechy przekrojowe sa filtrowane
    # odciskiem uniwersum - a uniwersum znamy dopiero po tym zapytaniu.
    feature_ids = horizon_feature_ids(cur, h, [i["symbol"] for i in instrumenty])

    # Budzet na pozycje wyprowadzony z limitu lacznej ekspozycji i liczby
    # obserwowanych instrumentow. Stala 3000 przy limicie 15 000 i dwunastu
    # instrumentach oznaczala, ze system chce zaangazowac 36 000 - dwa i pol
    # raza ponad limit - i realnie miescilo sie piec pozycji zamiast dwunastu.
    baza = position_budget(risk.limits, len(instrumenty))

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
        features = latest_features(cur, iid, now, h, feature_ids)
        if len(features) < 4:
            if verbose:
                print(f"  {symbol:10s} pominięty — za mało cech ({len(features)})")
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
        if pokrycie < 1.0 - 1e-9:
            stats["niepelne_sygnaly"] += 1
        oceny.append({"iid": iid, "symbol": symbol, "kind": inst["kind"],
                      "quote_ccy": inst["quote_ccy"],
                      "features": features, "price": price,
                      "regime": regime, "regime_conf": regime_conf, "score": score,
                      "confidence": confidence, "contributions": contributions,
                      "pokrycie": pokrycie,
                      "position": open_position(cur, engine, mode, iid)})

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
        if position:
            same_way = (position["side"] == "long" and score > 0) or \
                       (position["side"] == "short" and score < 0)
            if not same_way or abs(score) < model["exit"]:
                action = "exit"
            else:
                action = "hold"
        elif abs(score) >= model["enter"] and confidence >= model["min_conf"]:
            action = "enter_long" if score > 0 else "enter_short"
            # Krotkiej pozycji na spocie nie da sie zajac bez pozyczki marzowej.
            # Silnik podejmowal je swobodnie - 8 z 12 pozycji w dzienniku to byly
            # shorty na instrumentach spotowych, czyli transakcje niewykonalne.
            if action == "enter_short" and not koszty(ocena["kind"]).krotka_pozycja:
                action = "hold"
                stats["short_niewykonalny"] = stats.get("short_niewykonalny", 0) + 1
        else:
            action = "hold"

        # Wielkosc pozycji rosnie z sila sygnalu, ale nigdy powyzej limitu -
        # o tym decyduje risk kernel, nie ten kod.
        qty = Decimal(0)
        notional = Decimal(0)
        if action in ("enter_long", "enter_short"):
            notional = (baza * Decimal(str(udzial_budzetu(score)))).quantize(Decimal("0.01"))
            qty = (notional / price).quantize(Decimal("0.000001"))

        if action.startswith("enter") and wejscia_wstrzymane:
            # Kill-switch, przekroczony limit konta albo niepelny pomiar
            # kapitalu - wejscie odpada, ale decyzje i tak zapisujemy, zeby
            # dziennik pokazywal, ze system CHCIAL wejsc i co go zatrzymalo.
            verdict = werdykt_wstrzymania
            action = "hold"
        elif action.startswith("enter"):
            verdict = risk.check(iid, notional, qty)
        else:
            verdict = type("V", (), {"verdict": "allow", "reason": None, "qty": qty,
                                     "violations": [], "usage": []})()

        top = sorted(contributions, key=lambda c: -abs(c["contribution"]))[:2]
        top_txt = ", ".join(f"{c['label'].lower()} ({c['contribution']:+.3f})" for c in top)
        rationale = (
            f"Reżim: {regime}. Wynik złożony {score:+.3f} przy pewności {confidence:.0%}. "
            f"Największy wkład: {top_txt}."
            # Niepelny komplet mowimy WPROST w uzasadnieniu, bo wynik go juz nie
            # zdradza: po renormalizacji +0,45 z trzech sygnalow wyglada tak samo
            # jak +0,45 z osmiu. Czytelnik "Ruchow" ma prawo wiedziec, ile
            # sygnalow za ta liczba stalo.
            + (f" Niepełny komplet sygnałów: {pokrycie:.0%} budżetu wag."
               if pokrycie < 1.0 - 1e-9 else "")
            + (f" Ryzyko: {verdict.reason}." if verdict.reason else "")
        )

        latency = int((time.perf_counter() - started) * 1000)
        did = record_decision(cur, engine, mode, iid, now, action,
                              verdict.qty if action.startswith("enter") else (
                                  position["qty"] if position else None),
                              price, confidence, regime, features, verdict,
                              rationale, contributions, signal_ids, latency,
                              model.get("model_version_id"), pokrycie)

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
            print(f"  {symbol:10s} {regime:11s} wynik={score:+.3f} pewność={confidence:.0%} "
                  f"-> {action:12s} [{verdict.verdict}]")
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
        stats.update(await zloz_oczekujace(
            cur, gielda, engine=engine, mode=mode, kategoria=kategoria,
            verbose=verbose, tylko_wyjscia=wejscia_wstrzymane))
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
    cur.execute(
        "INSERT INTO risk.engine_heartbeat (engine, mode, last_beat_at, status, detail)"
        " VALUES (%s,%s, now(), 'running', %s)"
        " ON CONFLICT (engine) DO UPDATE SET last_beat_at=now(), status='running',"
        " detail=EXCLUDED.detail",
        (engine, mode, json.dumps(stats)))
    conn.commit()
    return stats


async def zbuduj_adapter(url: str, mode: str):
    """Adapter gieldy dla trybu, z hostem WZIETYM Z ref.venue.

    HOST Z TABELI, NIE Z TABLICY W ADAPTERZE. Podmiot unijny ma wlasne hosty,
    a klucz z bybit.eu odbity od hosta globalnego wraca z kodem 10003 -
    komunikatem nie do odroznienia od zlego klucza. Ten projekt wszedl juz
    w te pulapke raz, w module uzgodnienia, mimo ze komentarz przy
    credentials.py przed nia ostrzegal.
    """
    from services.api.security import SecretBox
    from services.exchanges.bybit import BybitAdapter

    with psycopg.connect(url, row_factory=dict_row) as conn:
        # NAJNOWSZY KLUCZ, NIE NAJSTARSZY. Rotacja zostawia na chwile dwa
        # aktywne wiersze; przy `ORDER BY c.id` wygrywal ten, ktory wlasnie
        # zastapiono - i to bez sladu. Objaw pojawial sie dopiero w dniu
        # wygasniecia starego klucza i wygladal jak awaria uwierzytelnienia
        # bez przyczyny, bo nowy klucz w bazie byl przez caly czas poprawny.
        # `c.id` zostaje jako rozstrzygniecie remisu: dwa klucze wgrane w tej
        # samej milisekundzie maja dawac ten sam wybor przy kazdym starcie,
        # inaczej silnik po restarcie handluje z innego rachunku.
        klucze = conn.execute(
            "SELECT c.id, c.key_hint, c.key_enc, c.secret_enc,"
            "       v.demo_host, v.live_host"
            "  FROM app.api_credential c JOIN ref.venue v ON v.id = c.venue_id"
            " WHERE v.code = 'bybit' AND c.mode::text = %s AND c.active"
            " ORDER BY c.created_at DESC, c.id DESC", (mode,)).fetchall()
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
        sys.exit(f"egzekucja na giełdzie wymaga aktywnego klucza dla trybu {mode}")

    box = SecretBox()
    return BybitAdapter(api_key=box.decrypt(bytes(cred["key_enc"])),
                        api_secret=box.decrypt(bytes(cred["secret_enc"])),
                        mode=mode,
                        host=cred["demo_host"] if mode == "demo" else cred["live_host"])


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
    ap.add_argument("--kind", default="spot", choices=("spot", "spot_eu", "perp"),
                    help="cennik giełdy dla rachunku papierowego")
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
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    h = get_horizon(args.bar)

    if args.sprawdz:
        with psycopg.connect(url, row_factory=dict_row) as conn:
            return await sprawdz(conn, args.engine, h)

    gielda = None
    if args.egzekucja == "gielda":
        gielda = await zbuduj_adapter(url, args.mode)
        print(f"EGZEKUCJA NA GIEŁDZIE ({args.mode}) — zlecenia idą naprawdę.")

    from services.engine.koszty import runda
    print(f"Silnik {args.engine}, świeca {h.bar} — jedna decyzja na świecę.")
    print(f"Rachunek papierowy: {args.kind}, zlecenia "
          f"{'z limitem' if args.limit else 'rynkowe'}, "
          f"koszt rundy {runda(args.kind, args.limit) * 100:.3f}%")
    if args.limit:
        print("Zlecenia z limitem są PLANOWANE na świecę T i rozstrzygane na T+1 "
              "(kupno gdy low ≤ limit, sprzedaż gdy high ≥ limit).\n")
    else:
        print()

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
    while True:
        t0 = datetime.now(timezone.utc)
        try:
            with psycopg.connect(url, row_factory=dict_row) as conn:
                s = await run_once(conn, args.engine, mode=args.mode,
                                   kind_kosztow=args.kind, limit=args.limit,
                                   verbose=not args.quiet, h=h, gielda=gielda)
            plany = (f", plany: wypełnione {s['wypelnione']}, "
                     f"niewypełnione {s['niewypelnione']}, czekają {s['czekaja']}"
                     if args.limit else "")
            if s.get("bez_swiecy"):
                plany += f", BEZ ŚWIECY {s['bez_swiecy']}"
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
        if args.once:
            return 0
        await asyncio.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
