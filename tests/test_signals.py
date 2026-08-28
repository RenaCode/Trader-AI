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


def test_szybka_sciezka_daje_ten_sam_wynik():
    """Pominiecie rozbicia na wklady nie moze zmieniac wyniku ani pewnosci.

    Rozbicie jest potrzebne tylko do dziennika i interfejsu; symulacja go nie
    czyta. Ale optymalizacja, ktora po cichu zmienia wynik, jest gorsza niz brak
    optymalizacji - stad ten test.
    """
    from services.engine.signals import combine, precompute

    for f in (
        cechy(rsi=78.0, rel_strength=0.9, ret_short=12.0, ret_mid=25.0, ret_long=60.0),
        cechy(rsi=18.0, rel_strength=-0.85, range_pos=0.03, funding_z=-3.1),
        cechy(),                                   # wszystko neutralne
        cechy(volume_z=4.0, ret_short=-9.0),
    ):
        pc = precompute(f, H4)
        for rezim in ("trend_up", "range", "high_vol"):
            pelne = combine(pc, rezim, None, with_contributions=True)
            szybkie = combine(pc, rezim, None, with_contributions=False)
            assert pelne[0] == szybkie[0], f"wynik rozjechany w reżimie {rezim}"
            assert pelne[1] == szybkie[1], f"pewność rozjechana w reżimie {rezim}"
            assert len(pelne[2]) == len(SIGNALS) and szybkie[2] == []


# ------------------------------------------------------ siatka swiec
@pytest.mark.parametrize("wejscie,oczekiwane", [
    ("2026-08-27T16:19:49.408934+00:00", "2026-08-27T16:00:00+00:00"),
    ("2026-08-27T00:00:00+00:00", "2026-08-27T00:00:00+00:00"),
    ("2026-08-27T03:59:59+00:00", "2026-08-27T00:00:00+00:00"),
    ("2026-08-27T23:59:59.999999+00:00", "2026-08-27T20:00:00+00:00"),
])
def test_wyrownanie_do_siatki_swiec(wejscie, oczekiwane):
    """Cechy liczone na wlasnej siatce MUSZA trafiac w granice swiec.

    Bez tego funding i news laduly na znacznikach typu 16:19:49 i nigdy nie
    wchodzily do panelu trenera, dzialajac jednoczesnie w produkcji przez
    feat.as_of() - czyli rozjazd trening-produkcja nie do wykrycia po wynikach.
    """
    from datetime import datetime

    from services.ingest.features import na_siatce

    got = na_siatce(datetime.fromisoformat(wejscie), H4)
    assert got == datetime.fromisoformat(oczekiwane)


def test_wyrownanie_jest_idempotentne():
    from datetime import datetime

    from services.ingest.features import na_siatce

    t = datetime.fromisoformat("2026-08-27T16:19:49.408934+00:00")
    raz = na_siatce(t, H4)
    assert na_siatce(raz, H4) == raz


def test_wyrownanie_zalezy_od_horyzontu():
    from datetime import datetime

    from services.ingest.features import na_siatce

    t = datetime.fromisoformat("2026-08-27T16:19:49+00:00")
    assert na_siatce(t, H4).hour == 16
    assert na_siatce(t, H5).minute == 15      # świeca 5-minutowa


# ------------------------------------------------------ datowanie cech
def test_cecha_jest_znana_dopiero_po_zamknieciu_swiecy():
    """Znacznik cechy to POCZATEK swiecy, ale wartosc pochodzi z jej ZAMKNIECIA.

    Wczesniejsza wersja datowala cechy na `poczatek + 3 sekundy`, twierdzac,
    ze wartosc byla znana trzy sekundy po OTWARCIU swiecy - choc pochodzila
    z zamkniecia cztery godziny pozniej. To unieważniało straznik bitemporalny:
    feat.as_of() przepuszczalo wartosc, ktorej w tamtej chwili nie dalo sie
    policzyc.
    """
    from datetime import datetime, timedelta, timezone

    from services.ingest.features import znane_od

    t = datetime(2026, 8, 28, 4, 0, tzinfo=timezone.utc)
    znane = znane_od(t, H4)
    assert znane >= t + timedelta(hours=4), \
        "cecha nie może być znana przed zamknięciem swojej świecy"
    assert znane < t + timedelta(hours=5), "opóźnienie ma być małe, nie dowolne"


def test_datowanie_skaluje_sie_z_horyzontem():
    from datetime import datetime, timedelta, timezone

    from services.ingest.features import znane_od

    t = datetime(2026, 8, 28, 4, 0, tzinfo=timezone.utc)
    assert znane_od(t, H4) - t >= timedelta(hours=4)
    assert znane_od(t, H5) - t >= timedelta(minutes=5)
    assert znane_od(t, H5) - t < timedelta(minutes=6)
