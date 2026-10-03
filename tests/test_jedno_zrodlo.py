"""Symulacja, trener i pętla decyzyjna czytają JEDNĄ definicję.

DLACZEGO TO JEST OSOBNY PLIK

`risk_rules.py` powstał dokładnie po to — jego docstring mówi wprost, że druga
implementacja „na potrzeby symulacji" byłaby gwarancją rozjazdu przy pierwszej
zmianie reguł. Mimo to kapitał początkowy, próg pewności i wzór na wielkość
zlecenia były zdefiniowane osobno w trzech miejscach.

Wartości się zgadzały, więc nic nie było widać. Tego samego dnia (2026-09-06)
znaleziono w projekcie dwa rozjazdy dokładnie tego rodzaju: stawkę prowizji
zapisaną osobno w pętli (0,055% — stawka perpetuałów na instrumentach
spotowych, tydzień po naprawieniu tego samego w symulacji) oraz regułę
wypełnienia limitu, którą omal nie przepisano po raz drugi.

Test nie sprawdza więc, czy liczby są równe — sprawdza, czy istnieje JEDNA.
"""
import re
from decimal import Decimal as D
from pathlib import Path

import pytest

from services.engine import loop, portfolio, train
from services.engine.risk_rules import (MIN_CONFIDENCE, START_EQUITY,
                                        udzial_budzetu, wielkosc_pozycji)


def test_kapital_poczatkowy_ma_jedno_zrodlo():
    assert float(loop.START_EQUITY) == START_EQUITY
    assert portfolio.START_EQUITY is START_EQUITY


def test_prog_pewnosci_ma_jedno_zrodlo():
    assert loop.MIN_CONFIDENCE is MIN_CONFIDENCE
    assert train.MIN_CONF is MIN_CONFIDENCE


def test_wielkosc_zlecenia_ma_jedno_zrodlo():
    """Symulacja i pętla muszą angażować ten sam udział budżetu."""
    for score in (0.0, 0.1, 0.25, 0.49, 0.5, 0.8, 1.0, -0.7):
        assert portfolio._sizing(score, 100.0, 1000.0)[0] == 1000.0 * udzial_budzetu(score)


# ------------------------------------------- wielkość pozycji: float == Decimal

# Budżety z `position_budget` przy rachunkach, które ten system naprawdę miał
# albo zapowiada (100 000 / 10, 10 000 / 10, 100 063 / 12), i ceny od groszowej
# pary do BTC - tam zaokrąglenie ilości do 1e-6 waży najwięcej.
PRZYPADKI = [(b, s, c)
             for b in ("1500", "150", "1250.7875")
             for s in (0.0, 0.13, 0.2501, 0.3333, -0.41, 0.5, 0.9)
             for c in ("0.3127", "2.5", "143.91", "61234.5")]


@pytest.mark.parametrize("baza,score,cena", PRZYPADKI)
def test_wielkosc_pozycji_float_ROWNA_SIE_decimal(baza, score, cena):
    """Audyt Fable U-3: symulacja (float) i pętla (Decimal) dostają te same
    liczby co do bitu po konwersji, a nie „prawie te same"."""
    n_d, q_d = wielkosc_pozycji(D(baza), score, D(cena))
    n_f, q_f = wielkosc_pozycji(float(baza), score, float(cena))

    assert isinstance(n_d, D) and isinstance(q_d, D)
    assert isinstance(n_f, float) and isinstance(q_f, float)
    assert (n_f, q_f) == (float(n_d), float(q_d))
    # Ta sama funkcja pod nazwą, której używa symulacja.
    assert portfolio._sizing(score, float(cena), float(baza)) == (n_f, q_f)


def test_wielkosc_pozycji_to_arytmetyka_PETLI_z_przed_U3():
    """Kanarek na zachowanie produkcyjne: dokładnie wzór, który do 2026-10-03
    stał w `loop.run_once` (Decimal, kwota do groszy, ilość do 1e-6)."""
    for baza, score, cena in PRZYPADKI:
        b, c = D(baza), D(cena)
        notional = (b * D(str(udzial_budzetu(score)))).quantize(D("0.01"))
        qty = (notional / c).quantize(D("0.000001"))
        assert wielkosc_pozycji(b, score, c) == (notional, qty)
    assert wielkosc_pozycji(D("1500"), 0.3, D("123.45")) == (D("900.00"), D("7.290401"))


def test_wielkosc_pozycji_bez_ceny_oddaje_sama_kwote():
    """Tak woła ją trener przy szacowaniu pyłu - zna budżet, nie cenę."""
    assert wielkosc_pozycji(D("1500"), 0.25) == (D("750.00"), None)
    assert wielkosc_pozycji(1500.0, 0.25) == (750.0, None)


def test_wzor_nie_jest_przepisany_w_zadnym_module():
    """Kanarek na powrót kopii. Wzór `abs(score) * 2` ma istnieć raz."""
    wystapienia = 0
    for plik in ("services/engine/loop.py", "services/engine/portfolio.py",
                 "services/engine/train.py", "services/engine/risk_rules.py"):
        zrodlo = Path(plik).read_text()
        kod = "\n".join(l for l in zrodlo.splitlines() if not l.strip().startswith("#"))
        wystapienia += len(re.findall(r"abs\(score\)\s*\*\s*2", kod))
    assert wystapienia == 1, f"wzór na wielkość zlecenia występuje {wystapienia} razy"


def test_udzial_jest_ograniczony_do_calego_budzetu():
    """Kanarek na sens samego wzoru: sygnał dowolnej siły nie może zaangażować
    więcej niż cały budżet pozycji."""
    for score in (0.5, 1.0, 5.0, -5.0):
        assert udzial_budzetu(score) == 1.0
    assert udzial_budzetu(0.0) == 0.0
