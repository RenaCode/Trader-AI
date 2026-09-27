"""N deflacji liczy PRZEBIEGI, KTORE DOSZLY DO WYNIKU — nie wiersze w tabeli.

DLACZEGO TO ISTNIEJE

`n_konfiguracji` liczylo do 2026-09-27 `count(DISTINCT params)` po CALEJ
`learn.model_version`, wiec wchodzily do N takze wersje, ktore zostaly
w statusie `training`: wiersze-sieroty po przebiegach zabitych i po `--sucho`.
Selekcja nie mogla sie na nich oprzec, bo nigdy nie oddaly wyniku — nikt ich
nie zobaczyl i nikt na ich podstawie niczego nie wybral. Liczenie ich w N
zawyzalo poprzeczke selekcji, czyli mylilo sie NA NIEKORZYSC strategii; nie
przepuszczalo wiec niczego, co nie powinno przejsc, ale podawalo czlowiekowi
jako pomiar liczbe, ktora pomiarem nie byla.

ZMIERZONE NA PRODUKCJI 2026-09-27, tor 4h, rozbicie 59 roznych `params`:

    28  ma zapisany median OOS Sharpe                         -> proba
    24  skonczone (active/retired), bez wiersza w dzienniku    -> proba (`--sucho`)
     7  wylacznie `training`, nigdy nie doszly do wyniku       -> NIE proba

    N = 59 -> poprzeczka +0,0622      N = 52 -> +0,0609      roznica 0,017 DSR

Tor 1h: 27 -> 26. Siedem `params` z `training` nie powtarza sie w zadnym innym
statusie, wiec filtr naprawde je usuwa, a nie tylko odsiewa duplikaty.

DRUGA POLOWA, I TO JEST TA, KTORA LATWO PRZEOCZYC

`train()` zaklada swoj wiersz w `learn.model_version` (INSERT ze statusem
domyslnym `training`) ZANIM dojdzie do bramki. Sam filtr statusu gubilby wiec
dokladnie te probe, ktora jest przedmiotem selekcji — a dla pierwszego
przebiegu nowego toru dawalby N = 0, czyli DSR NIEOBLICZALNY i promocje
wstrzymana na zawsze przy bramce fail-closed. Dwa tory stoja dzis dokladnie
w tym stanie (`crypto_signal_weighting_5m`, `equity_signal_weighting_1h`: po
jednej wersji, obie `training`). Dlatego `n_konfiguracji` przyjmuje `mv_id`
i dolicza wlasna wersje przebiegu OSOBNO.

CZEGO TE TESTY NIE ROBIA: nie podstawiaja atrapy kursora pod zapytanie o N.
Atrapa oddajaca podlozona liczbe niezaleznie od SQL przeszlaby przy KAZDYM
filtrze — tak wlasnie `test_N_liczy_ROZNE_params_z_model_version`
przechodzil przez caly czas, gdy N bylo liczone ze zlej populacji. Tu pyta sie
prawdziwego Postgresa o wiersze o znanych statusach.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from services.engine.train import (
    n_konfiguracji, opis_zrodla_n, opis_zrodla_odchylenia, przewaga_dsr,
)

KORZEN = Path(__file__).resolve().parents[1]
TRAIN = (KORZEN / "services" / "engine" / "train.py").read_text(encoding="utf-8")
ROUTER = (KORZEN / "services" / "api" / "routers"
          / "learning.py").read_text(encoding="utf-8")

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


def _wersja(cur, model_id: int, params: dict, status: str) -> int:
    cur.execute(
        "INSERT INTO learn.model_version (model_id, version, status, params)"
        " VALUES (%s, (SELECT COALESCE(max(version),0)+1 FROM learn.model_version"
        "              WHERE model_id=%s), %s, %s) RETURNING id",
        (model_id, model_id, status, json.dumps(params)))
    return cur.fetchone()["id"]


# ===================================================== PRAWDZIWA BAZA

@wymaga_bazy
def test_PRAWDZIWA_BAZA_wersje_ktore_zostaly_w_training_NIE_sa_probami():
    """Odtworzone rozbicie produkcyjne w miniaturze: 4 proby, 3 sieroty.

    Bez filtru wychodzi 7 — i to jest dokladnie ta liczba, ktora stala
    w werdykcie do 2026-09-27.
    """
    with _polacz() as conn, conn.cursor() as cur:
        model_id = _model(cur, "test_n_selekcji_sieroty")
        for i in range(3):
            _wersja(cur, model_id, {"w": i}, "retired")
        _wersja(cur, model_id, {"w": "aktywna"}, "active")
        # Sieroty: kazda o INNYCH `params`, zeby filtr musial je naprawde
        # usunac, a nie tylko zlac z istniejacymi przez DISTINCT.
        for i in range(3):
            _wersja(cur, model_id, {"w": f"sierota-{i}"}, "training")

        assert n_konfiguracji(cur, model_id) == 4, (
            "N liczy wersje, ktore nigdy nie wyszly ze statusu `training`. "
            "Taki przebieg nie oddal wyniku, wiec selekcja nie mogla sie na nim "
            "oprzec — a kazda doliczona sierota podnosi poprzeczke selekcji")
        cur.execute("SELECT count(DISTINCT params) AS n FROM learn.model_version"
                    " WHERE model_id = %s", (model_id,))
        assert cur.fetchone()["n"] == 7, "atrapa scenariusza sie rozjechala"
        conn.rollback()


@wymaga_bazy
def test_PRAWDZIWA_BAZA_przebieg_ktory_PADL_tez_nie_jest_proba():
    """`learn.mv_status` ma jeszcze `failed`. Dzis takich wierszy nie ma ani
    jednego, wiec na produkcyjna liczbe to nie wplywa — ale lista dodatnia
    statusow ma nie przepuscic ich po cichu, gdy sie pojawia."""
    with _polacz() as conn, conn.cursor() as cur:
        model_id = _model(cur, "test_n_selekcji_padly")
        _wersja(cur, model_id, {"w": "skonczona"}, "retired")
        _wersja(cur, model_id, {"w": "padla"}, "failed")
        assert n_konfiguracji(cur, model_id) == 1, (
            "przebieg ze statusem `failed` liczy sie jako proba, choc nie doszedl "
            "do wyniku dokladnie tak samo jak ten, ktory zostal w `training`")
        conn.rollback()


@wymaga_bazy
def test_PRAWDZIWA_BAZA_wlasna_wersja_biezacego_przebiegu_LICZY_SIE():
    """Kandydat, ktorego bramka wlasnie ocenia, jest proba — i stoi w `training`.

    To jest ta polowa poprawki, ktora latwo zgubic: filtr statusu bez `mv_id`
    usuwalby z N wlasnie te konfiguracje, ktora jest przedmiotem selekcji.
    """
    with _polacz() as conn, conn.cursor() as cur:
        model_id = _model(cur, "test_n_selekcji_wlasna")
        _wersja(cur, model_id, {"w": "stara"}, "retired")
        moja = _wersja(cur, model_id, {"w": "biezaca"}, "training")

        assert n_konfiguracji(cur, model_id) == 1
        assert n_konfiguracji(cur, model_id, moja) == 2, (
            "wlasna wersja biezacego przebiegu nie wchodzi do N. Bramka ocenia "
            "wlasnie ja, wiec jest proba — a bez niej N jest zanizone o 1, "
            "czyli w strone LATWIEJSZEJ bramki")
        conn.rollback()


@wymaga_bazy
def test_PRAWDZIWA_BAZA_pierwszy_przebieg_nowego_toru_ma_N_rowne_JEDEN():
    """Nowy tor ma tylko wlasna wersje. N = 0 znaczylo by DSR NIEOBLICZALNY
    i promocje wstrzymana przy bramce fail-closed — nie z powodu selekcji,
    a z powodu filtru. Dwa tory stoja dzis w tym stanie."""
    with _polacz() as conn, conn.cursor() as cur:
        model_id = _model(cur, "test_n_selekcji_nowy_tor")
        moja = _wersja(cur, model_id, {"w": "pierwsza"}, "training")
        assert n_konfiguracji(cur, model_id, moja) == 1, (
            "pierwszy przebieg nowego toru dostaje N = 0, czyli DSR "
            "nieobliczalny — bramka wstrzymuje promocje z powodu filtru, "
            "a nie z powodu wielokrotnosci prob")
        conn.rollback()


@wymaga_bazy
def test_PRAWDZIWA_BAZA_wlasna_wersja_o_ISTNIEJACYCH_params_nie_liczy_sie_dwa_razy():
    """`mv_id` dokleja sie przez OR, a nie przez dodanie jedynki — inaczej
    powtorzenie tej samej konfiguracji zawyzalo by N."""
    with _polacz() as conn, conn.cursor() as cur:
        model_id = _model(cur, "test_n_selekcji_powtorka")
        _wersja(cur, model_id, {"w": "ta sama"}, "retired")
        moja = _wersja(cur, model_id, {"w": "ta sama"}, "training")
        assert n_konfiguracji(cur, model_id, moja) == 1, (
            "ta sama konfiguracja policzona dwa razy — N ma liczyc ROZROZNIALNE "
            "`params`, a nie przebiegi")
        conn.rollback()


# ===================================================== SKLAD, BEZ BAZY

def test_train_podaje_bramce_WLASNA_WERSJE_przebiegu():
    wywolanie = re.search(r"dsr_ok, opis_dsr = werdykt_dsr\((.*?)\n\n", TRAIN, re.S)
    assert wywolanie, "train() nie wola `werdykt_dsr`"
    assert "n=n_konfiguracji(cur, model_id, mv_id)" in wywolanie.group(1), (
        "bramka dostaje N policzone BEZ wlasnej wersji przebiegu. Wiersz tego "
        "przebiegu stoi w `training` w chwili liczenia, wiec filtr statusu go "
        "pomija — i N jest zanizone o probe, ktora jest wlasnie oceniana")


def test_panel_i_bramka_filtruja_TE_SAME_statusy():
    """Rozjazd tych dwoch zapytan to ta sama wada, ktora kafelek „Uczciwosc
    pomiaru" mial 2026-09-27: panel pokazywalby 59 tam, gdzie promocje
    wstrzymuje 52."""
    i = TRAIN.index("def n_konfiguracji(")
    j = TRAIN.index("\ndef ", i + 1)
    cialo = "\n".join(w.split("#")[0] for w in TRAIN[i:j].splitlines())
    wzor = "status IN ('candidate','active','retired')"
    assert wzor in cialo, f"bramka nie filtruje statusow wzorem {wzor!r}"

    blok = ROUTER[ROUTER.index("async def state("):
                  ROUTER.index('@router.get("/weights")')]
    bez_komentarzy = "\n".join(w.split("#")[0] for w in blok.splitlines())
    assert "v." + wzor in bez_komentarzy or wzor in bez_komentarzy, (
        "`/state` liczy N po CALEJ `learn.model_version`, a bramka po wersjach, "
        "ktore doszly do wyniku. Panel pokazywalby inna liczbe niz ta, ktora "
        "wstrzymuje promocje")


def test_opis_N_mowi_ze_wersje_bez_wyniku_NIE_sa_probami():
    opis = opis_zrodla_n(52, 12613)
    assert "training" in opis, (
        "opis N nie mowi, ktore wersje sie NIE licza. Czytelnik nie ma jak "
        "sprawdzic tej liczby wlasnym zapytaniem")
    assert "52" in opis


def test_opis_rozrzutu_NAZYWA_OBIE_populacje_gdy_rozrzut_ZMIERZONY():
    """Rozrzut wychodzi z 28 konfiguracji, a N = 52. Roznica nie jest
    kosmetyczna: podstawienie sd z 28 jako sd z 52 jest ZALOZENIEM, i to
    zalozenie musi stac w tym samym zdaniu, co liczba."""
    import random
    import statistics
    rng = random.Random(11)
    surowe = [rng.gauss(0.0, 1.0) for _ in range(1500)]
    sr = statistics.fmean(surowe)
    sd = statistics.stdev(surowe)
    zwroty = [(x - sr) / sd + 0.0762 for x in surowe]

    wynik = przewaga_dsr(zwroty, 52, 0.0266)
    assert wynik["zrodlo_odchylenia"].startswith("pomiar"), (
        "test nie trafil w galez POMIARU — bez tego nie sprawdza tego zdania")
    opis = opis_zrodla_odchylenia(wynik, 28)
    assert "28" in opis and "52" in opis, (
        f"opis rozrzutu nie podaje OBU populacji (rozrzut z 28, N = 52). "
        f"Czytelnik bierze wtedy jedna liczbe za obie proby. Dostalem: {opis}")
    assert "ZALOZENIEM" in opis, (
        "opis nie mowi, ze przyjecie rozrzutu wezszej proby za rozrzut calej "
        "populacji N jest zalozeniem, nie pomiarem")


def test_opis_rozrzutu_NIE_zmysla_populacji_gdy_rozrzutu_NIE_ZMIERZONO():
    """Przy podstawieniu z H0 uzyta sd nie pochodzi z zadnej proby
    konfiguracji, wiec zdanie „rozrzut z N konfiguracji" byloby tam falszem —
    o dokladnie tym ksztalcie, ktory ten opis ma tepic."""
    import random
    import statistics
    rng = random.Random(12)
    surowe = [rng.gauss(0.0, 1.0) for _ in range(1500)]
    sr = statistics.fmean(surowe)
    sd = statistics.stdev(surowe)
    zwroty = [(x - sr) / sd + 0.0762 for x in surowe]

    wynik = przewaga_dsr(zwroty, 52, None)
    opis = opis_zrodla_odchylenia(wynik, 3)
    assert "NIE ZMIERZONY" in opis
    assert "POPULACJE SIE ROZNIA" not in opis, (
        "zdanie o dwoch populacjach doklejono do galezi, w ktorej rozrzutu "
        "NIE zmierzono — uzyta sd jest podloga H0 i nie pochodzi z zadnej "
        "proby konfiguracji")
