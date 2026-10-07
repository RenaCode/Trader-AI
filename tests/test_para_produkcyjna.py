"""Produkcja gra PARA wag i progow, ktora walk-forward naprawde zmierzyl (2026-10-07).

Do tej daty produkcja dostawala MEDIANE wag po oknach i MEDIANE progow po
oknach. Kazdy prog okna to percentyl 0,88 wyniku liczonego wagami TEGO okna;
mediana wag to wektor, ktorego nie mialo zadne okno, a skleja sygnaly, ktore
sie wzajemnie znosza - wynik produkcji jest mniejszy niz wynik, na ktorym
liczono prog. Na produkcji (v171, `range`): prog 0,341 przy srednim |wyniku|
~0,049, 0 wejsc na 18 decyzji.

KANAREK: sztuczne okna, z ktorych kazde stawia na INNY sygnal. Odsetek
decyzji, w ktorych |score| (na spocie: score > 0) przekracza prog
produkcyjny przy WAGACH PRODUKCYJNYCH, ma byc zamiarem (12%) - na danych
z tego samego rozkladu, ktorych trener nie widzial. Na kodzie sprzed zmiany
(mediana wag + mediana progow) wychodzi okolo 1%.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.engine import train as tr  # noqa: E402
from services.engine.horizons import get as get_horizon  # noqa: E402
from services.engine.signals import SIGNALS, SignalOutput, combine  # noqa: E402

H4 = get_horizon("4h")
REZIM = "range"
NAZWY = list(SIGNALS)[:4]
SWIEC_NA_OKNO = 6000
OKIEN = 6
TOLERANCJA = 0.025          # +-2,5 p.p. przy ~6000 obserwacjach (sd ~0,4 p.p.)


def _swiece(rng: random.Random, n: int) -> list[dict[str, SignalOutput]]:
    """Cztery niezalezne sygnaly, kazdy ~N(0; 0,35) obciety do [-1, 1]."""
    return [{s: SignalOutput(max(-1.0, min(1.0, rng.gauss(0.0, 0.35))), 1.0, {})
             for s in NAZWY} for _ in range(n)]


def _wagi_okna(k: int) -> dict[str, dict[str, float]]:
    """Okno k stawia na sygnal k % 4 (0,85), reszta po 0,05 ze zmiennym znakiem
    - tak jak wagi IC kolejnych okien, ktore wybieraja rozne sygnaly."""
    w = {s: 0.0 for s in SIGNALS}
    for i, s in enumerate(NAZWY):
        w[s] = 0.85 if i == k % len(NAZWY) else (0.05 if (i + k) % 2 else -0.05)
    return {r: dict(w) for r in ("trend_up", "trend_down", "range", "high_vol",
                                  "news_shock")}


def _przebieg(tylko_dluga: bool) -> tr.Przebieg:
    rng = random.Random(20261007)
    z = tr.Przebieg()
    for k in range(OKIEN):
        swiece = _swiece(rng, SWIEC_NA_OKNO)
        panele = {"SYM": [{"t": i} for i in range(len(swiece))]}
        cache = {"SYM": [(REZIM, s) for s in swiece]}
        wagi = _wagi_okna(k)
        bramki = tr.bramki_okna(panele, wagi, {}, H4, cache, per_rezim=True,
                                tylko_dluga=tylko_dluga)
        z.dodaj_progi(bramki, wagi, set())
    return z


def _odsetek_wejsc(prod: tr.Produkcja, tylko_dluga: bool) -> float:
    """Na NOWYCH danych z tego samego rozkladu, tak jak petla (`loop.py`):
    `evaluate`/`combine` z `params["weights"][rezim]`, warunek pewnosci,
    porownanie z `enter_threshold_per_regime[rezim]`."""
    rng = random.Random(7)
    prog = prod.prod_progi[REZIM]
    wejscia = populacja = 0
    for s in _swiece(rng, 20000):
        sc, conf, _ = combine(s, REZIM, prod.prod_weights[REZIM],
                              with_contributions=False)
        if conf < tr.MIN_CONF:
            continue
        if tylko_dluga:
            if sc <= 0:
                continue
            populacja += 1
            wejscia += sc >= prog
        else:
            populacja += 1
            wejscia += abs(sc) >= prog
    return wejscia / populacja


@pytest.mark.parametrize("tylko_dluga", [False, True], ids=["obie_strony", "spot_H6"])
def test_odsetek_wejsc_pary_produkcyjnej_to_zamiar(tylko_dluga):
    prod = tr.parametry_produkcji(_przebieg(tylko_dluga))
    odsetek = _odsetek_wejsc(prod, tylko_dluga)
    zamiar = 1.0 - tr.ENTER_PCTL
    assert abs(odsetek - zamiar) <= TOLERANCJA, (
        f"odsetek wejść przy parze produkcyjnej {odsetek:.1%} wobec zamiaru "
        f"{zamiar:.0%} (próg {prod.prod_progi[REZIM]:.4f}) - produkcja gra "
        f"parą, której walk-forward nie symulował")


def test_para_to_wagi_i_progi_OSTATNIEGO_okna_a_mediany_zostaja_sladem():
    z = _przebieg(False)
    prod = tr.parametry_produkcji(z)
    assert prod.prod_weights[REZIM] == {k: round(v, 4)
                                        for k, v in z.weight_history[-1][REZIM].items()}
    assert prod.prod_progi[REZIM] == round(z.bramki_okien[-1].wejscie(REZIM), 6)
    assert prod.prod_proby[REZIM] == SWIEC_NA_OKNO
    # Slad: stary prog (mediana okien) jest zapisany i wypisany obok nowego.
    assert REZIM in prod.progi_mediana_okien
    assert any("mediana okien" in linia for linia in prod.linie)
    assert prod.wagi_mediana_okien[REZIM] != prod.prod_weights[REZIM]


def test_rezim_bez_progu_w_ostatnim_oknie_nie_dostaje_progu():
    """Prog z mediany wczesniejszych okien nie przechodzi do produkcji, gdy
    ostatnie okno go nie skalibrowalo - i jest to wypisane."""
    z = _przebieg(False)
    bez = tr.Bramki({}, 0.1, {REZIM: 10})
    z.dodaj_progi(bez, _wagi_okna(0), set())
    prod = tr.parametry_produkcji(z)
    assert REZIM not in prod.prod_progi
    assert REZIM in prod.bez_progu_w_produkcji
    assert any("BEZ progu w ostatnim" in linia for linia in prod.linie)


def test_skala_wyniku_niezalezna_od_normalizacji_wag():
    """Petla dostaje wagi zaokraglone do 4 miejsc; `combine` dzieli przez sume
    |wag| obecnych sygnalow, wiec skala wektora nie zmienia wyniku."""
    rng = random.Random(3)
    w = _wagi_okna(1)[REZIM]
    w2 = {k: 3.7 * v for k, v in w.items()}
    for s in _swiece(rng, 200):
        a = combine(s, REZIM, w, with_contributions=False)[0]
        b = combine(s, REZIM, w2, with_contributions=False)[0]
        assert a == pytest.approx(b, abs=1e-12)
