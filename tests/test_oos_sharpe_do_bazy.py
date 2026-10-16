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
dostaja 0, historyczne poprawia migracja 0062.
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
    _hash_konfiguracji, odchylenie_prob_z_bazy, przewaga_dsr, werdykt_dsr, zapisz_oos_sharpe,
)

KORZEN = Path(__file__).resolve().parents[1]
TRAIN = (KORZEN / "services" / "engine" / "train.py").read_text(encoding="utf-8")
MIGRACJA = KORZEN / "db" / "migrations" / "0062_n_windows_liczy_okna_oos.sql"


# ============================================ KIERUNEK ZMIANY DSR
#
# Nazwany wprost, bo gdyby wyszlo odwrotnie, byloby to objawem bledu, a nie
# dobra wiadomoscia.

def _zwroty(n: int = 1462, sr: float = 0.0823) -> list[float]:
    """Proba o zadanym Sharpe/transakcje, symetryczna: g3 = 0, g4 = 3 w granicy.

    Naprzemienne +a/-a ma kurtoze 1, wiec zamiast tego bierzemy deterministyczna
    probe normalna - wazne jest, zeby Sharpe wyszedl dokladnie taki, jak w
    renacode-infra: docs/projekty/trader/03-wynik-walk-forward.md, bo do tych liczb odnosi sie tabela DSR.
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
    _, opis_podstawione = werdykt_dsr(z, doplata=0.0, n=59,
                                      trials_kumulatywnie=12653,
                                      nieznane=("spread",))
    _, opis_zmierzone = werdykt_dsr(z, doplata=0.0, n=59,
                                    trials_kumulatywnie=12653,
                                    nieznane=("spread",),
                                    odchylenie_prob=0.0266,
                                    konfiguracji_z_oos=28)
    assert "NIE ZMIERZONY" in opis_podstawione
    assert str(MIN_PROB_ROZRZUTU) in opis_podstawione
    assert "ZMIERZONY" in opis_zmierzone and "28 konfiguracji" in opis_zmierzone
    # OBA opisy musza powiedziec, po CZYM liczymy rozrzut - inaczej czytelnik
    # nie ma jak odroznic tej wielkosci od rozrzutu miedzy oknami, ktory jest
    # dziesieciokrotnie wiekszy i przez jeden dzien stal w tym miejscu.
    for opis in (opis_podstawione, opis_zmierzone):
        assert "DISTINCT learn.model_version.params" in opis
        assert "OKNAMI" in opis
    assert "DOLNE" in opis_zmierzone, (
        "opis przestal mowic, ze zmierzony rozrzut jest dolnym oszacowaniem — "
        "bez tego zdania liczba czyta sie jak domkniety pomiar")


def test_opis_MOWI_gdy_pomiar_wypadl_pod_podloga():
    """Trzecia droga musi byc widoczna: to nie to samo, co brak pomiaru."""
    z = _zwroty()
    _, opis = werdykt_dsr(z, doplata=0.0, n=59, trials_kumulatywnie=12653,
                          nieznane=(), odchylenie_prob=0.0010,
                          konfiguracji_z_oos=28)
    assert "ODRZUCONY" in opis and "podlogi H0" in opis
    assert "CO NAJMNIEJ szumem" in opis

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


def test_migracja_0062_naprawia_historyczne_n_windows():
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


# ====================================== V[SR_n]: PO KONFIGURACJACH, NIE PO OKNACH
#
# WADA, KTOREJ TE TESTY PILNUJA, BYLA WDROZONA. `odchylenie_prob_z_bazy` czytalo
# `learn.hypothesis_trial.oos_sharpe`, gdzie kazdy wiersz to Sharpe z JEDNEGO
# okna. Rozrzut takich liczb to rozrzut miedzy OKNAMI - zmiennosc rynku plus
# szum estymacji z proby jednego okna - i jest DZIESIECIOKROTNIE wiekszy od
# rozrzutu miedzy konfiguracjami. Zmierzone na produkcji 2026-09-27, tor 4h:
# 0,2627 wobec 0,0266. Poprzeczka rosla z +0,062 na +0,615, DSR spadal z 0,70
# na 0,00 - z "nie przechodzi o wlos" na "nie przechodzi o rzad wielkosci".
#
# Dlatego kanarki nizej sprawdzaja WARTOSC, nie obecnosc zapytania: kazdy zasiewa
# OBA zbiory naraz - waski po konfiguracjach i dziesiec razy szerszy po oknach -
# i wymaga, zeby wyszedl waski.

# Rozrzut zasiewanych danych. Dobrany tak, zeby odpowiadal produkcji: 0,027
# miedzy konfiguracjami i 0,27 miedzy oknami.
SD_KONFIGURACJI = 0.027
SD_OKIEN = 0.27


def _wersja(cur, model_id: int, params: dict) -> int:
    cur.execute(
        "INSERT INTO learn.model_version (model_id, version, status, params)"
        " VALUES (%s, (SELECT COALESCE(max(version),0)+1 FROM learn.model_version"
        "              WHERE model_id=%s), 'training', %s) RETURNING id",
        (model_id, model_id, json.dumps(params)))
    return cur.fetchone()["id"]


def _dziennik(cur, mv_id: int, sharpe: float) -> None:
    cur.execute(
        "INSERT INTO learn.learning_state_log (engine, bar, status, windows_total,"
        " windows_positive, median_oos_sharpe, model_version_id)"
        " VALUES ('crypto','4h','learning',40,20,%s,%s)", (sharpe, mv_id))


def _okna(cur, mv_id: int, sharpes: list[float]) -> None:
    """Okna walk-forward z ich Sharpe'ami - zbior, ktorego NIE wolno tu uzyc."""
    cur.execute(
        "INSERT INTO learn.training_run (model_version_id, train_from, train_to,"
        " status) VALUES (%s, now() - interval '400 days', now(), 'done')"
        " RETURNING id", (mv_id,))
    run_id = cur.fetchone()["id"]
    for i, sh in enumerate(sharpes):
        cur.execute(
            "INSERT INTO learn.wf_window (training_run_id, idx, train_from,"
            " train_to, test_from, test_to, n_trades) VALUES (%s,%s,"
            " now() - interval '400 days', now() - interval '310 days',"
            " now() - interval '310 days', now() - interval '296 days', 35)"
            " RETURNING id", (run_id, i))
        w_id = cur.fetchone()["id"]
        cur.execute("INSERT INTO learn.wf_metric (wf_window_id, metric, value)"
                    " VALUES (%s,'sharpe',%s)", (w_id, sh))


def _zasiej_dwa_zbiory(cur, model_id: int, ile_konfiguracji: int) -> list[float]:
    """Waski rozrzut po konfiguracjach ORAZ szeroki po oknach i hipotezach.

    Zwraca wartosci per konfiguracja - to jest liczba, ktorej oczekujemy.
    """
    import statistics as st
    per_konfiguracja: list[float] = []
    for i in range(ile_konfiguracji):
        # Rownomiernie rozlozone wokol 0,12 - odchylenie probkowe wyjdzie
        # dokladnie policzalne, wiec test nie zalezy od generatora losowego.
        sh = 0.12 + SD_KONFIGURACJI * ((i - (ile_konfiguracji - 1) / 2)
                                       / max(1.0, (ile_konfiguracji - 1) / 2))
        mv = _wersja(cur, model_id, {"wybor": "ic", "wariant": i})
        _dziennik(cur, mv, sh)
        per_konfiguracja.append(sh)
        # Ten sam przebieg dostaje 40 okien o rozrzucie DZIESIEC RAZY wiekszym,
        # plus wiersze hipotez z tymi samymi szerokimi wartosciami.
        szerokie = [sh + SD_OKIEN * ((j - 19.5) / 19.5) for j in range(40)]
        _okna(cur, mv, szerokie)
        odcisk = _hipoteza(cur, model_id, {"range": {"rsi": float(i)}})
        zapisz_oos_sharpe(cur, model_id, {odcisk: szerokie})
    assert st.stdev(per_konfiguracja) < 0.3 * SD_OKIEN
    return per_konfiguracja


@wymaga_bazy
def test_PRAWDZIWA_BAZA_rozrzut_jest_PO_KONFIGURACJACH_a_nie_po_oknach():
    """KANAREK NA WARTOSC. Oba zbiory w bazie naraz, rozne dziesieciokrotnie.

    Jesli funkcja wroci do czytania `hypothesis_trial.oos_sharpe` albo
    `wf_metric`, dostanie ~0,27 zamiast ~0,027 i ten test padnie na LICZBIE,
    a nie na napisie.
    """
    import statistics as st
    with _polacz() as conn:
        cur = conn.cursor()
        m = _model(cur, "test_rozrzut_po_konfiguracjach")
        per_konfiguracja = _zasiej_dwa_zbiory(cur, m, 25)

        sd, ile = odchylenie_prob_z_bazy(cur, m)
        assert ile == 25
        assert sd == pytest.approx(st.stdev(per_konfiguracja), rel=1e-6), (
            "rozrzut nie jest odchyleniem Sharpe'ow per KONFIGURACJA")
        assert sd < 0.3 * SD_OKIEN, (
            f"rozrzut wyszedl {sd:.4f}, czyli w skali rozrzutu MIEDZY OKNAMI "
            f"({SD_OKIEN}) — funkcja czyta okna, nie konfiguracje; poprzeczka "
            f"deflacji bedzie dziesieciokrotnie za wysoka, a DSR zero")
        conn.rollback()


@wymaga_bazy
def test_PRAWDZIWA_BAZA_ta_sama_konfiguracja_w_kilku_przebiegach_to_JEDNA_proba():
    """Dwa kolejne przebiegi dziela 39 z 40 okien (rho ~0,97).

    Policzone osobno zawyzalyby n i zanizaly rozrzut o czynnik zalezny od tego,
    ile nocy minelo od wdrozenia — czyli poprzeczka deflacji spadalaby sama
    z kalendarza.
    """
    import statistics as st
    with _polacz() as conn:
        cur = conn.cursor()
        m = _model(cur, "test_jedna_konfiguracja_jedna_proba")
        wartosci = []
        for i in range(MIN_PROB_ROZRZUTU):
            sh = 0.10 + 0.001 * i
            mv = _wersja(cur, m, {"wybor": "ic", "wariant": i})
            _dziennik(cur, mv, sh)
            wartosci.append(sh)
        # Jedna z konfiguracji wraca w dziesieciu kolejnych przebiegach - inna
        # wersja modelu, te same `params`.
        powtarzana = {"wybor": "ic", "wariant": 0}
        for _ in range(10):
            mv = _wersja(cur, m, powtarzana)
            _dziennik(cur, mv, 0.10)

        sd, ile = odchylenie_prob_z_bazy(cur, m)
        assert ile == MIN_PROB_ROZRZUTU, (
            f"ta sama konfiguracja policzona jako {ile} prob zamiast "
            f"{MIN_PROB_ROZRZUTU} — n rosnie kalendarzem, nie mnogoscia")
        assert sd == pytest.approx(st.stdev(wartosci), rel=1e-6)
        conn.rollback()


@wymaga_bazy
def test_PRAWDZIWA_BAZA_odchylenie_z_bazy_respektuje_PROG():
    """Ponizej progu None, od progu liczba - liczone z tych samych wierszy."""
    with _polacz() as conn:
        cur = conn.cursor()
        m = _model(cur, "test_rozrzut_prog")
        for i in range(MIN_PROB_ROZRZUTU - 1):
            _dziennik(cur, _wersja(cur, m, {"wariant": i}), 0.10 + 0.002 * i)
        sd, ile = odchylenie_prob_z_bazy(cur, m)
        assert ile == MIN_PROB_ROZRZUTU - 1 and sd is None

        _dziennik(cur, _wersja(cur, m, {"wariant": 999}), 0.18)
        sd, ile = odchylenie_prob_z_bazy(cur, m)
        assert ile == MIN_PROB_ROZRZUTU and sd is not None and sd > 0
        conn.rollback()


@wymaga_bazy
def test_PRAWDZIWA_BAZA_przebiegi_bez_Sharpe_nie_wchodza_do_rozrzutu():
    """NULL nie jest zerem. Przebieg sucho/przerwany nie ma median_oos_sharpe.

    Wliczony jako zero rozdmuchalby rozrzut (zero przy sredniej 0,12 lezy piec
    odchylen od niej), czyli PODNIOSLBY poprzeczke na podstawie braku danych.
    """
    import statistics as st
    with _polacz() as conn:
        cur = conn.cursor()
        m = _model(cur, "test_rozrzut_null")
        wartosci = []
        for i in range(MIN_PROB_ROZRZUTU):
            sh = 0.12 + 0.001 * i
            _dziennik(cur, _wersja(cur, m, {"wariant": i}), sh)
            wartosci.append(sh)
        for i in range(50):
            _dziennik(cur, _wersja(cur, m, {"sucho": i}), None)

        sd, ile = odchylenie_prob_z_bazy(cur, m)
        assert ile == MIN_PROB_ROZRZUTU, (
            "przebiegi bez policzonego Sharpe'a weszly do rozrzutu")
        assert sd == pytest.approx(st.stdev(wartosci), rel=1e-6)
        conn.rollback()


@wymaga_bazy
def test_PRAWDZIWA_BAZA_rozrzut_liczy_sie_dla_TEGO_modelu():
    """Drugi model w tej samej bazie nie moze wejsc do rozrzutu pierwszego.

    Filtr `v.model_id` zyje w SQL-u, wiec sprawdza go Postgres, a nie atrapa.
    """
    with _polacz() as conn:
        cur = conn.cursor()
        a = _model(cur, "test_rozrzut_model_a")
        b = _model(cur, "test_rozrzut_model_b")
        for i in range(MIN_PROB_ROZRZUTU):
            _dziennik(cur, _wersja(cur, a, {"wariant": i}), 0.12 + 0.001 * i)
        for i in range(MIN_PROB_ROZRZUTU):
            _dziennik(cur, _wersja(cur, b, {"wariant": i}), 0.50 * i)

        sd_a, ile_a = odchylenie_prob_z_bazy(cur, a)
        assert ile_a == MIN_PROB_ROZRZUTU and sd_a < 0.01, (
            "wiersze innego modelu weszly do rozrzutu tego")
        conn.rollback()
