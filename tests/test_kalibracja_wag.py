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


# ------------------------------------------------------ wagi ze znakiem
def test_losowanie_produkuje_wagi_ujemne():
    """KANAREK na powrot ograniczenia do wag nieujemnych.

    Przy nieujemnych wagach zdanie 'ten sygnal trafnie porzadkuje sytuacje,
    tylko w druga strone' jest NIEWYRAZALNE - a pomiar IC pokazal, ze cztery
    z osmiu sygnalow niosa informacje z odwroconym znakiem.
    """
    import random

    from services.engine.train import random_weights

    rng = random.Random(20260828)
    ujemnych = 0
    for _ in range(20):
        w = random_weights(rng)
        ujemnych += sum(1 for v in w["range"].values() if v < 0)
    assert ujemnych > 0, "kalibracja musi móc odwrócić sygnał, nie tylko go wyciszyć"


def test_suma_wartosci_bezwzglednych_wag_wynosi_jeden():
    """Budzet wagi ma byc taki sam jak przy wagach nieujemnych."""
    import random

    from services.engine.train import random_weights

    rng = random.Random(1)
    for _ in range(10):
        for wagi in random_weights(rng).values():
            assert sum(abs(v) for v in wagi.values()) == pytest.approx(1.0, abs=2e-3)


def test_pewnosc_zostaje_dodatnia_przy_ujemnych_wagach():
    """Odwrocenie sygnalu zmienia KIERUNEK, nie sile.

    Gdyby pewnosc liczyla sie po wagach ze znakiem, ujemne wagi odejmowalyby
    ja i wynik mogl wyjsc ujemny albo wiekszy od jedynki.
    """
    from services.engine.horizons import get as get_horizon
    from services.engine.signals import combine, precompute

    h = get_horizon("4h")
    f = {"atr_pct": 1.0, "ema_ratio": 1.0, "rsi": 20.0, "range_pos": 0.1,
         "ret_short": -5.0, "ret_mid": -9.0, "ret_long": -15.0, "volume_z": 0.0,
         "news_sent": 0.0, "funding_rate": 0.0, "funding_z": 0.0, "rel_strength": -0.9}
    wagi = {k: (-0.2 if i % 2 else 0.2) for i, k in enumerate(SIGNALS)}
    _, pewnosc, _ = combine(precompute(f, h), "range", wagi)
    assert 0.0 <= pewnosc <= 1.0


def test_odwrocenie_wszystkich_wag_odwraca_wynik():
    """Wlasnosc, ktora musi zachodzic, jesli znak ma cokolwiek znaczyc."""
    from services.engine.horizons import get as get_horizon
    from services.engine.signals import combine, precompute

    h = get_horizon("4h")
    f = {"atr_pct": 1.0, "ema_ratio": 1.02, "rsi": 78.0, "range_pos": 0.95,
         "ret_short": 6.0, "ret_mid": 12.0, "ret_long": 30.0, "volume_z": 2.5,
         "news_sent": 0.0, "funding_rate": 0.0, "funding_z": 2.5, "rel_strength": 0.9}
    pc = precompute(f, h)
    wagi = {k: 1.0 / len(SIGNALS) for k in SIGNALS}
    a, _, _ = combine(pc, "range", wagi)
    b, _, _ = combine(pc, "range", {k: -v for k, v in wagi.items()})
    assert a == pytest.approx(-b, abs=1e-9)


def test_zerowanie_martwych_normalizuje_po_module():
    """Suma zwykla moglaby wyjsc bliska zeru i rozdmuchac pozostale wagi."""
    wagi = {"range": {k: (0.3 if i % 2 else -0.3) for i, k in enumerate(SIGNALS)}}
    out = _bez_martwych(wagi, {"news_shock"})
    assert sum(abs(v) for v in out["range"].values()) == pytest.approx(1.0, abs=2e-3)
    assert out["range"]["news_shock"] == 0.0
