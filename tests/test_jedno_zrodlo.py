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
from pathlib import Path

from services.engine import loop, portfolio, train
from services.engine.risk_rules import MIN_CONFIDENCE, START_EQUITY, udzial_budzetu


def test_kapital_poczatkowy_ma_jedno_zrodlo():
    assert float(loop.START_EQUITY) == START_EQUITY
    assert portfolio.START_EQUITY is START_EQUITY


def test_prog_pewnosci_ma_jedno_zrodlo():
    assert loop.MIN_CONFIDENCE is MIN_CONFIDENCE
    assert train.MIN_CONF is MIN_CONFIDENCE


def test_wielkosc_zlecenia_ma_jedno_zrodlo():
    """Symulacja i pętla muszą angażować ten sam udział budżetu."""
    for score in (0.0, 0.1, 0.25, 0.49, 0.5, 0.8, 1.0, -0.7):
        _, _ = portfolio._sizing(score, 100.0, 1000.0)
        assert portfolio._sizing(score, 100.0, 1000.0)[0] == 1000.0 * udzial_budzetu(score)


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
