"""Testy sygnalow - ze szczegolnym naciskiem na to, ze sa BEZWYMIAROWE.

Pierwsza wersja dzielila zwroty przez wpisane na sztywno procenty dobrane pod
swiece 5-minutowe. Te testy pilnuja, zeby ten blad nie wrocil: ten sam uklad
rynku ma dawac ten sam wynik niezaleznie od horyzontu i od instrumentu.
"""
import pytest

from services.engine.horizons import get as get_horizon
from services.engine.signals import (
    SIGNALS, classify_regime, cross_section, evaluate, momentum_multi,
)

H4 = get_horizon("4h")
H5 = get_horizon("5m")


def cechy(**kw) -> dict:
    baza = {"atr_pct": 1.0, "ema_ratio": 1.0, "rsi": 50.0, "range_pos": 0.5,
            "ret_short": 0.0, "ret_mid": 0.0, "ret_long": 0.0, "volume_z": 0.0,
            "news_sent": 0.0, "funding_rate": 0.0, "funding_z": 0.0,
            "rel_strength": 0.0}
    return baza | kw


# ------------------------------------------------------ sila wzgledna
def test_najmocniejszy_daje_sygnal_kupna():
    out = cross_section(cechy(rel_strength=1.0), H4)
    assert out.score > 0.9


def test_najslabszy_daje_sygnal_sprzedazy():
    out = cross_section(cechy(rel_strength=-1.0), H4)
    assert out.score < -0.9


def test_srodek_stawki_jest_martwy():
    """Bez martwej strefy sygnal handlowalby przy kazdym drobnym przetasowaniu."""
    for rs in (-0.4, 0.0, 0.3, 0.49):
        out = cross_section(cechy(rel_strength=rs), H4)
        assert out.score == 0.0, f"rel_strength={rs} nie powinien nic mowic"
        assert out.confidence <= 0.1


def test_brak_cechy_daje_brak_sygnalu():
    f = cechy()
    del f["rel_strength"]
    assert cross_section(f, H4) is None


def test_sila_rosnie_monotonicznie_poza_martwa_strefa():
    poprzedni = 0.0
    for rs in (0.55, 0.7, 0.85, 1.0):
        s = cross_section(cechy(rel_strength=rs), H4).score
        assert s > poprzedni, f"sila ma rosnac, przy {rs} spadla"
        poprzedni = s


# ------------------------------------------------------ bezwymiarowosc
def test_momentum_nie_zalezy_od_horyzontu():
    """Ten sam ruch mierzony w jednostkach zmiennosci ma dac ten sam wynik.

    KANAREK NA POWROT STALYCH. Jesli ktos wpisze z powrotem 'zwrot / 1.5%',
    ten sam uklad na swiecy 4-godzinnej i 5-minutowej przestanie sie zgadzac.
    """
    def f_dla(h):
        return cechy(ret_short=1.0 * (h.ret_short ** 0.5),
                     ret_mid=1.0 * (h.ret_mid ** 0.5),
                     ret_long=1.0 * (h.ret_long ** 0.5))
    a = momentum_multi(f_dla(H4), H4)
    b = momentum_multi(f_dla(H5), H5)
    assert a.score == pytest.approx(b.score, abs=1e-9)


def test_momentum_nie_zalezy_od_zmiennosci_instrumentu():
    """SOL jest trzy razy bardziej zmienny niz BTC - ten sam ruch w jednostkach
    zmiennosci ma dac ten sam sygnal."""
    def f_dla(atr):
        return cechy(atr_pct=atr,
                     ret_short=0.8 * atr * (H4.ret_short ** 0.5),
                     ret_mid=0.8 * atr * (H4.ret_mid ** 0.5),
                     ret_long=0.8 * atr * (H4.ret_long ** 0.5))
    spokojny = momentum_multi(f_dla(0.5), H4)
    zmienny = momentum_multi(f_dla(1.5), H4)
    assert spokojny.score == pytest.approx(zmienny.score, abs=1e-9)


def test_rezim_trendu_tez_jest_bezwymiarowy():
    """Prog rozjazdu srednich skalowany zmiennoscia - inaczej 'trend' znaczylby
    co innego na BTC i co innego na SOL.

    Mnoznik 0,003 dobrany tak, zeby po przeskalowaniu KAZDY instrument mial
    dev = 0,3 (czyli powyzej progu 0,25), a bez skalowania instrument spokojny
    (atr 0,4) wypadal na 0,12 i lądował w 'range'. Pierwsza wersja tego testu
    uzywala mnoznika, przy ktorym oba warianty dawaly ten sam wynik - i przez
    to przechodzila takze po usunieciu skalowania.
    """
    rezimy = set()
    for atr in (0.4, 1.0, 2.5):
        f = cechy(atr_pct=atr, ema_ratio=1.0 + 0.00003 * atr * 100, ret_long=5.0)
        rezim, _ = classify_regime(f, atr_p80=99.0)
        rezimy.add(rezim)
        assert rezim == "trend_up", f"przy atr={atr} rezim to {rezim}, nie trend_up"
    assert rezimy == {"trend_up"}, "reżim ma być ten sam niezależnie od zmienności"


# ------------------------------------------------------ skladanie
def test_evaluate_zwraca_wszystkie_sygnaly_takze_zerowe():
    """Dashboard ma pokazywac, co zostalo ROZWAZONE, nie tylko co zadzialalo."""
    _, _, wklady = evaluate(cechy(), "range", None, H4)
    assert len(wklady) == len(SIGNALS)


def test_wynik_zlozony_miesci_sie_w_zakresie():
    skrajne = cechy(rsi=2.0, range_pos=0.0, rel_strength=-1.0, funding_z=5.0,
                    ret_short=-50.0, ret_mid=-80.0, ret_long=-99.0, volume_z=6.0)
    wynik, pewnosc, _ = evaluate(skrajne, "high_vol", None, H4)
    assert -1.0 <= wynik <= 1.0
    assert 0.0 <= pewnosc <= 1.0
