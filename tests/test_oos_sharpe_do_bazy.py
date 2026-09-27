"""`learn.hypothesis_trial.oos_sharpe`: zapisywany, i to Sharpe OUT-OF-SAMPLE.

CO BYLO NIE TAK

Kolumna istnieje od migracji 0006 i byla pusta w 16 050 wierszach z 16 050
(zmierzone na produkcji 2026-09-27; miesiac wczesniej 0 z 12 613). Komentarz
przy samej migracji wskazywal ja jako zrodlo licznika do deflowanego Sharpe.
Bez niej `statystyka.deflowany_sharpe` podstawia w miejsce `V[SR_n]` Baileya
wariancje SAMEGO ESTYMATORA pod hipoteza zerowa - czyli zaklada, ze
konfiguracje roznia sie wylacznie bledem pomiaru. Konfiguracje roznia sie
wiecej, wiec podstawienie ZANIZA poprzeczke selekcji i ZAWYZA DSR.

PULAPKA, KTORA TE TESTY PILNUJA

`zapisz_hipoteze` wola sie w `train()` w momencie, w ktorym znany jest wynik
kandydata na oknie TRENINGOWYM (`ocen(cand, train_slices, cache_tr)`). Zapisanie
tamtej liczby wypelniloby kolumne wartosciami in-sample, ktore WYGLADAJA jak
pomiar OOS. To jest gorsze niz NULL: NULL nie klamie, a rozrzut policzony
z liczb in-sample zawiera klatwe zwyciezcy i przestawia poprzeczke selekcji
w strone, ktorej nikt nie zobaczy.

CZEGO NIE UDALO SIE ZMIERZYC, I TO JEST POWIEDZIANE WPROST

Sharpe OOS jest liczony dla JEDNEGO kandydata na okno - tego, ktory wygral.
Pozostali (`trials_per_window` losowan, zalozenia poczatkowe, wagi z IC) nie
maja wyniku OOS i nigdy go nie mieli; policzenie im go oznacza inny protokol,
nie inny zapis. Zmierzony rozrzut jest wiec rozrzutem ZWYCIEZCOW, czyli proby
wybranej po gornym ogonie - a to zanizenie dziala na korzysc strategii.

n_windows BYL WPISYWANY JAKO 1 I NIGDY NIE AKTUALIZOWANY. Przy `oos_sharpe`
rownym NULL dwie kolumny tego samego wiersza mowily co innego. Nowe wiersze
dostaja 0, historyczne poprawia migracja 0061.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from services.engine.statystyka import (
    MIN_PROB_ROZRZUTU, deflowany_sharpe, oczekiwany_maksymalny_sharpe,
    rozrzut_sharpeow,
)
from services.engine.train import (
    _hash_konfiguracji, odchylenie_prob_z_bazy, opis_zrodla_odchylenia,
    przewaga_dsr, werdykt_dsr, zapisz_oos_sharpe,
)

KORZEN = Path(__file__).resolve().parents[1]
TRAIN = (KORZEN / "services" / "engine" / "train.py").read_text(encoding="utf-8")
MIGRACJA = KORZEN / "db" / "migrations" / "0061_n_windows_liczy_okna_oos.sql"


# ============================================ KIERUNEK ZMIANY DSR
#
# Nazwany wprost, bo gdyby wyszlo odwrotnie, byloby to objawem bledu, a nie
# dobra wiadomoscia.

def _zwroty(n: int = 1462, sr: float = 0.0823) -> list[float]:
    """Proba o zadanym Sharpe/transakcje, symetryczna: g3 = 0, g4 = 3 w granicy.

    Naprzemienne +a/-a ma kurtoze 1, wiec zamiast tego bierzemy deterministyczna
    probe normalna - wazne jest, zeby Sharpe wyszedl dokladnie taki, jak w
    docs/03, bo do tych liczb odnosi sie tabela DSR.
    """
    import random
    import statistics
    rng = random.Random(7)
    x = [rng.gauss(0.0, 1.0) for _ in range(n)]
    m, sd = statistics.fmean(x), statistics.stdev(x)
    return [(v - m) / sd * 0.01 + sr * 0.01 for v in x]


def test_ZMIERZONY_rozrzut_PODNOSI_poprzeczke_i_OBNIZA_dsr():
    """To jest caly sens zmiany i jedyny dopuszczalny kierunek.

    Podstawienie z H0 daje sd rzedu 1/sqrt(T-1) = 0,026. Rozrzut Sharpe'ow OOS
    zmierzony na produkcji (2 353 okna toru 4h) to 0,2627, czyli dziesiec razy
    wiecej. Poprzeczka rosnie liniowo w sd, wiec DSR spada.
    """
    z = _zwroty()
    podstawione = przewaga_dsr(z, 57)
    zmierzone = przewaga_dsr(z, 57, 0.2627)

    assert podstawione["odchylenie_prob"] < zmierzone["odchylenie_prob"]
    assert zmierzone["prog"] > podstawione["prog"], (
        "zmierzony rozrzut obnizyl poprzeczke selekcji — przy rozrzucie "
        "wiekszym od wariancji estymatora jest to arytmetycznie niemozliwe, "
        "wiec jest to objaw bledu, nie dobra wiadomosc")
    assert zmierzone["dsr"] < podstawione["dsr"]


def test_wynik_NAZYWA_zrodlo_odchylenia():
    """Bez tego pola pomiar i podstawienie wygladaja identycznie."""
    z = _zwroty()
    assert przewaga_dsr(z, 57)["zrodlo_odchylenia"].startswith("podstawienie")
    assert przewaga_dsr(z, 57, 0.2627)["zrodlo_odchylenia"].startswith("pomiar")


def test_opis_werdyktu_MOWI_ktora_droga_poszla():
    """Napis idzie do logu trenera i do statusu wersji modelu."""
    z = _zwroty()
    _, opis_podstawione = werdykt_dsr(z, doplata=0.0, n=57,
                                      trials_kumulatywnie=16050,
                                      nieznane=("spread",))
    _, opis_zmierzone = werdykt_dsr(z, doplata=0.0, n=57,
                                    trials_kumulatywnie=16050,
                                    nieznane=("spread",),
                                    odchylenie_prob=0.2627,
                                    konfiguracji_z_oos=40)
    assert "NIE ZMIERZONY" in opis_podstawione
    assert str(MIN_PROB_ROZRZUTU) in opis_podstawione
    assert "ZMIERZONY" in opis_zmierzone and "40 konfiguracji" in opis_zmierzone
    assert "ZWYCIĘZCACH" in opis_zmierzone, (
        "opis przestal mowic, ze rozrzut mierzymy na zwyciezcach okien — "
        "bez tego zdania liczba czyta sie jak rozrzut calego zbioru prob")


# ============================================ PROG PROBY

def test_za_mala_proba_NIE_JEST_pomiarem():
    """Odchylenie z trzech liczb wchodzi do poprzeczki liniowo i skacze."""
    assert rozrzut_sharpeow([0.1, -0.2, 0.3]) is None
    assert rozrzut_sharpeow([0.1] * (MIN_PROB_ROZRZUTU - 1)) is None


def test_proba_od_progu_JEST_pomiarem():
    """Strona odwrotna - inaczej prog moglby odrzucac zawsze."""
    probka = [0.1 * i for i in range(MIN_PROB_ROZRZUTU)]
    assert rozrzut_sharpeow(probka) is not None


def test_proba_bez_rozrzutu_oddaje_None_a_nie_zero():
    """Zero podstawione w to miejsce ZDJELOBY cala poprawke na selekcje.

    `oczekiwany_maksymalny_sharpe` przy sd = 0 oddaje None, a `bramka_dsr`
    wstrzymuje wtedy promocje. Zero udajace pomiar dawaloby poprzeczke na
    wysokosci zera, czyli DSR rowny zwyklemu PSR - i to po cichu.
    """
    assert rozrzut_sharpeow([0.1] * MIN_PROB_ROZRZUTU) is None
    assert oczekiwany_maksymalny_sharpe(57, 0.0) is None


def test_przy_braku_pomiaru_ZOSTAJE_dzisiejsze_podstawienie():
    """None nie moze zatrzymac rachunku - ma go zostawic tam, gdzie byl."""
    z = _zwroty()
    w = deflowany_sharpe(z, 57, None)
    assert w["dsr"] is not None and w["odchylenie_prob"] > 0
    assert w["zrodlo_odchylenia"].startswith("podstawienie")


# ============================================ SKAD BIERZE SIE LICZBA W train()
#
# Zapis do kolumny jest jednym UPDATE-em i nie ma jak sam powiedziec, CZY
# nakarmiono go wynikiem z okna testowego. Te dwa testy pytaja o to wprost
# w miejscu, w ktorym decyzja jest podjeta.

def test_do_bazy_idzie_Sharpe_z_SYMULACJI_OOS_a_nie_wynik_z_okna_wyboru():
    """`oos_m` pochodzi z `simulate_portfolio` na `test_slices`, nie na `train_slices`."""
    po_oos = TRAIN.split("oos_m = portfolio_metrics(oos_res, h.min_trades)")[1]
    zapis = po_oos.split("oos_wg_konfiguracji.setdefault")[0]
    assert "oos_wg_konfiguracji.setdefault" in po_oos, (
        "zbieranie Sharpe'a OOS przestalo lezec PO symulacji poza oknem — "
        "wszystko, co jest przed nia, jest wynikiem in-sample")
    assert 'oos_m["sharpe"]' in po_oos.split("conn.commit")[0]
    assert "best_score" not in zapis, (
        "do `oos_wg_konfiguracji` trafia wynik z okna WYBORU (`best_score`) — "
        "kolumna byla by wypelniona wartosciami in-sample, ktore wygladaja "
        "jak pomiar OOS")


def test_zapisuje_sie_konfiguracja_WYBRANA_a_nie_kazdy_kandydat():
    """OOS liczy sie dla jednego kandydata na okno i trzeba to nazwac."""
    fragment = TRAIN.split("oos_wg_konfiguracji.setdefault")[1].split(")")[0]
    assert "best_w" in fragment, (
        "zapisujemy odcisk innej konfiguracji niz ta, dla ktorej policzono "
        "symulacje OOS — wiersz dostal by Sharpe cudzego wektora wag")


def test_odcisk_konfiguracji_jest_LICZONY_W_JEDNYM_MIEJSCU():
    """Dwa niezalezne sha256 rozjechalyby sie cicho i UPDATE trafial by w zero."""
    assert TRAIN.count("hashlib.sha256(json.dumps(cand") == 1, (
        "odcisk wektora wag liczy sie w wiecej niz jednym miejscu — gdy jedno "
        "zgubi `sort_keys`, UPDATE nie trafi w zaden wiersz i kolumna zostanie "
        "pusta dokladnie tak, jak byla")


def test_INSERT_wpisuje_n_windows_ZERO():
    """W chwili INSERT-u zadne okno OOS nie jest policzone."""
    assert "n_windows) VALUES (%s,%s,%s,0)" in TRAIN, (
        "wrocilo `n_windows = 1` przy INSERT-cie: wiersz twierdzi wtedy, ze "
        "jedno okno OOS policzono, przy `oos_sharpe` rownym NULL")


def test_migracja_0061_naprawia_historyczne_n_windows():
    tresc = MIGRACJA.read_text(encoding="utf-8")
    assert "SET n_windows = 0" in tresc and "oos_sharpe IS NULL" in tresc
    assert "COMMENT ON COLUMN learn.hypothesis_trial.oos_sharpe" in tresc, (
        "migracja nie zapisuje w bazie, CO znaczy ta kolumna — a znaczenie "
        "bylo dotad wylacznie w komentarzu w kodzie")


# ============================================ PRAWDZIWY POSTGRES
#
# `UNIQUE (model_id, config_hash)` i dopasowanie po `bytea` zyja w bazie.
# Atrapa kursora przepuscilaby UPDATE, ktory nie trafia w zaden wiersz - czyli
# dokladnie te awarie, przez ktora kolumna byla by pusta nadal.

TEST_DB_URL = os.environ.get("TEST_DB_URL")
wymaga_bazy = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="ustaw TEST_DB_URL na jednorazowa baze z zastosowanymi migracjami")


def _polacz():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(TEST_DB_URL, row_factory=dict_row)


def _model(cur, nazwa: str) -> int:
    cur.execute(
        "INSERT INTO learn.model (name, engine, kind, description)"
        " VALUES (%s,'crypto','signal_weighting','test') RETURNING id", (nazwa,))
    return cur.fetchone()["id"]


def _hipoteza(cur, model_id: int, cand: dict) -> bytes:
    odcisk = _hash_konfiguracji(cand)
    cur.execute(
        "INSERT INTO learn.hypothesis_trial (model_id, config_hash, params,"
        " n_windows) VALUES (%s,%s,%s,0)", (model_id, odcisk, json.dumps(cand)))
    return odcisk


@wymaga_bazy
def test_PRAWDZIWA_BAZA_update_TRAFIA_w_wiersz_wstawiony_przez_zapisz_hipoteze():
    """Odcisk musi sie zgadzac miedzy INSERT-em i UPDATE-em - inaczej cisza."""
    with _polacz() as conn:
        cur = conn.cursor()
        m = _model(cur, "test_oos_trafia")
        cand = {"trend_up": {"rsi": 0.5, "ema": 0.5}}
        odcisk = _hipoteza(cur, m, cand)

        zmienione = zapisz_oos_sharpe(cur, m, {odcisk: [0.11, 0.09]})
        assert zmienione == 1, (
            "UPDATE nie trafil w zaden wiersz — kolumna `oos_sharpe` zostaje "
            "pusta dokladnie tak, jak byla, i nic tego nie zglasza")

        cur.execute("SELECT oos_sharpe, n_windows FROM learn.hypothesis_trial"
                    " WHERE model_id = %s AND config_hash = %s", (m, odcisk))
        w = cur.fetchone()
        assert w["oos_sharpe"] == pytest.approx(0.10)
        assert w["n_windows"] == 2, (
            "n_windows nie policzyl okien — dzielnik nie zgadza sie wtedy "
            "z licznikiem sredniej")
        conn.rollback()


@wymaga_bazy
def test_PRAWDZIWA_BAZA_srednia_po_oknach_a_nie_wartosc_ostatniego():
    """Konfiguracja, ktora wygrala trzy okna, ma JEDNA wartosc - ich srednia."""
    with _polacz() as conn:
        cur = conn.cursor()
        m = _model(cur, "test_oos_srednia")
        odcisk = _hipoteza(cur, m, {"range": {"rsi": 1.0}})
        zapisz_oos_sharpe(cur, m, {odcisk: [0.30, 0.00, -0.30]})
        cur.execute("SELECT oos_sharpe, n_windows FROM learn.hypothesis_trial"
                    " WHERE model_id = %s", (m,))
        w = cur.fetchone()
        assert w["oos_sharpe"] == pytest.approx(0.0) and w["n_windows"] == 3
        conn.rollback()


@wymaga_bazy
def test_PRAWDZIWA_BAZA_odchylenie_z_bazy_respektuje_PROG():
    """Ponizej progu None, od progu liczba - i to liczone z tych samych wierszy."""
    with _polacz() as conn:
        cur = conn.cursor()
        m = _model(cur, "test_oos_prog")
        for i in range(MIN_PROB_ROZRZUTU - 1):
            odcisk = _hipoteza(cur, m, {"range": {"rsi": float(i)}})
            zapisz_oos_sharpe(cur, m, {odcisk: [0.01 * i]})
        sd, ile = odchylenie_prob_z_bazy(cur, m)
        assert ile == MIN_PROB_ROZRZUTU - 1 and sd is None

        odcisk = _hipoteza(cur, m, {"range": {"rsi": 999.0}})
        zapisz_oos_sharpe(cur, m, {odcisk: [0.5]})
        sd, ile = odchylenie_prob_z_bazy(cur, m)
        assert ile == MIN_PROB_ROZRZUTU and sd is not None and sd > 0
        conn.rollback()


@wymaga_bazy
def test_PRAWDZIWA_BAZA_wiersze_bez_OOS_nie_wchodza_do_rozrzutu():
    """NULL nie jest zerem. Wliczony jako zero zanizal by rozrzut i DSR by wzrosl."""
    with _polacz() as conn:
        cur = conn.cursor()
        m = _model(cur, "test_oos_null")
        for i in range(MIN_PROB_ROZRZUTU):
            odcisk = _hipoteza(cur, m, {"range": {"rsi": float(i)}})
            zapisz_oos_sharpe(cur, m, {odcisk: [0.30 + 0.01 * i]})
        # Pieciuset kandydatow odrzuconych na oknie treningowym - bez OOS.
        for i in range(500):
            _hipoteza(cur, m, {"range": {"ema": float(i)}})

        sd, ile = odchylenie_prob_z_bazy(cur, m)
        assert ile == MIN_PROB_ROZRZUTU, (
            "do rozrzutu weszly wiersze bez policzonego OOS — NULL zostal "
            "potraktowany jak zero, co zanizyloby rozrzut i PODNIOSLO DSR")
        assert sd == pytest.approx(rozrzut_sharpeow(
            [0.30 + 0.01 * i for i in range(MIN_PROB_ROZRZUTU)]))
        conn.rollback()
