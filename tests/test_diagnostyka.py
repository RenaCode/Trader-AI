"""Testy statystyki uzywanej w diagnostyce sygnalow.

Wspolczynnik informacji jest jedyna liczba, na podstawie ktorej ocenimy, czy
w ogole warto dalej dobierac wagi. Blad w jego liczeniu prowadzilby do decyzji
projektowych opartych na niczym - stad testy takze na przypadki zdegenerowane.
"""
import random

import pytest

from services.engine.diagnose_signals import rangi, spearman


def test_rangi_usredniaja_remisy():
    assert rangi([3.0, 1.0, 1.0, 2.0]) == [4.0, 1.5, 1.5, 3.0]


def test_rangi_dla_wszystkich_rownych():
    assert rangi([5.0] * 4) == [2.5] * 4


def test_idealna_zgodnosc():
    xs = [float(i) for i in range(100)]
    assert spearman(xs, xs) == pytest.approx(1.0)


def test_idealna_przeciwnosc():
    xs = [float(i) for i in range(100)]
    assert spearman(xs, [-x for x in xs]) == pytest.approx(-1.0)


def test_monotoniczna_ale_nieliniowa_daje_jeden():
    """Korelacja RANGOWA mierzy uporzadkowanie, nie skale.

    Gdyby ktos podmienil ja na Pearsona, ten test padnie - a to istotne,
    bo wyniki sygnalow sa przycinane do zakresu -1..1 i skala jest umowna.
    """
    xs = [float(i) for i in range(1, 101)]
    assert spearman(xs, [x ** 3 for x in xs]) == pytest.approx(1.0)


def test_losowe_daje_okolo_zera():
    random.seed(7)
    xs = [random.random() for _ in range(2000)]
    ys = [random.random() for _ in range(2000)]
    assert abs(spearman(xs, ys)) < 0.06


def test_za_mala_proba_zwraca_none():
    """Lepiej brak liczby niz liczba bez znaczenia."""
    assert spearman([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) is None


def test_stala_kolumna_zwraca_none():
    """Sygnal, ktory nie zmienia sie wcale, nie ma jak korelowac."""
    xs = [1.0] * 100
    ys = [float(i) for i in range(100)]
    assert spearman(xs, ys) is None
