"""Testy wykluczania martwych sygnalow z kalibracji wag.

Sygnal, ktory na danym oknie zawsze zwraca zero - jak news_shock bez klucza
Anthropic - nie moze dostawac wagi. Nie chodzi o estetyke: taka waga zjada
budzet innych sygnalow i ZANIZA PEWNOSC, bo martwy sygnal wchodzi do mianownika
z wlasna pewnoscia 0,1.
"""
import pytest

from services.engine.signals import DEFAULT_WEIGHTS, SIGNALS, SignalOutput
from services.engine.train import _bez_martwych, martwe_sygnaly


def wpis(**score_per_sygnal):
    """Buduje wpis pamieci podrecznej: (rezim, {nazwa: SignalOutput})."""
    return ("range", {
        name: SignalOutput(score_per_sygnal.get(name, 0.5), 0.5, {})
        for name in SIGNALS
    })


def test_wykrywa_sygnal_zawsze_zerowy():
    cache = {"AAA": [wpis(news_shock=0.0) for _ in range(200)]}
    assert "news_shock" in martwe_sygnaly(cache)


def test_nie_uznaje_za_martwy_sygnalu_ktory_czasem_dziala():
    """Kanarek. Sygnal odzywajacy sie rzadko to nie to samo co sygnal martwy."""
    wpisy = [wpis(news_shock=0.0) for _ in range(100)]
    wpisy += [wpis(news_shock=0.8) for _ in range(10)]     # ~9% swiec
    assert "news_shock" not in martwe_sygnaly({"AAA": wpisy})


def test_wszystkie_zywe_gdy_kazdy_sie_zmienia():
    cache = {"AAA": [wpis() for _ in range(50)]}
    assert martwe_sygnaly(cache) == set()


def test_pusta_pamiec_nie_wywraca():
    assert martwe_sygnaly({}) == set()
    assert martwe_sygnaly({"AAA": []}) == set()


def test_zerowanie_renormalizuje_do_jednosci():
    w = _bez_martwych(DEFAULT_WEIGHTS, {"news_shock"})
    for rezim, wagi in w.items():
        assert wagi["news_shock"] == 0.0
        assert sum(wagi.values()) == pytest.approx(1.0, abs=1e-3)
        assert set(wagi) == set(SIGNALS), "komplet sygnałów musi zostać zachowany"


def test_zerowanie_zwieksza_wagi_pozostalych():
    """Budzet zwolniony przez martwy sygnal ma trafic do zywych, nie zniknac."""
    przed = DEFAULT_WEIGHTS["range"]["momentum_multi"]
    po = _bez_martwych(DEFAULT_WEIGHTS, {"news_shock"})["range"]["momentum_multi"]
    assert po > przed


def test_brak_martwych_nie_zmienia_niczego():
    assert _bez_martwych(DEFAULT_WEIGHTS, set()) is DEFAULT_WEIGHTS
