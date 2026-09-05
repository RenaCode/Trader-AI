"""Werdykt dozoru: kiedy warto sie odezwac, a kiedy zamilknac.

DLACZEGO TO JEST TESTOWANE OSOBNO I DOKLADNIE

Powiadomienie, ktore przychodzi zawsze, przestaje cokolwiek znaczyc — odbiorca
uczy sie je pomijac wlasnie wtedy, gdy przyjdzie to jedno wazne. Prog musi wiec
byc ostry w obie strony: przepuszczac zmiany istotne i milczec przy szumie.

Skala szumu jest w tym projekcie ZMIERZONA: cztery kolejne przebiegi tego samego
kodu, rozniace sie wylacznie przesunieciem okna o dzien, daly 52,5% -> 57,5% ->
60,0% -> 62,5%. Dziesiec punktow bez zadnej zmiany. Stad prog na przedziale
ufnosci poprzedniego przebiegu, a nie na roznicy odsetka.
"""
import pytest

from services.engine.dozor import zmiany

BAZA = {
    "status": "learning",
    "windows_total": 40,
    "windows_positive": 28,          # 70%
    "positive_ci_low": 0.546,
    "positive_ci_high": 0.819,
    "median_oos_sharpe": 0.094,
    "max_psi": 0.4,
}


def wariant(**zmiana):
    return {**BAZA, **zmiana}


# --- cisza ------------------------------------------------------------------
def test_powtorzony_wynik_to_cisza():
    assert zmiany(BAZA, wariant()) == []


def test_ruch_w_granicach_szumu_to_cisza():
    """Kanarek na caly sens tego modulu.

    30/40 to 75% wobec 70% poprzednio — piec punktow procentowych. Zmierzony szum
    dobowy siega dziesieciu, a 75% miesci sie w przedziale 54,6–81,9%. Gdyby to
    wywolalo powiadomienie, dozor odzywalby sie niemal co noc.
    """
    assert zmiany(BAZA, wariant(windows_positive=30)) == []


def test_dryf_ponizej_progu_to_cisza():
    assert zmiany(BAZA, wariant(max_psi=0.9)) == []


def test_dryf_powyzej_progu_ale_MALEJACY_to_cisza():
    """Dryf, ktory opada, nie wymaga uwagi — problem sam sie cofa."""
    assert zmiany(wariant(max_psi=1.8), wariant(max_psi=1.2)) == []


# --- zmiana istotna ---------------------------------------------------------
def test_zmiana_statusu_zawsze_przechodzi():
    p = zmiany(BAZA, wariant(status="degraded"))
    assert any("status" in x for x in p)


def test_odsetek_ponizej_przedzialu_przechodzi():
    # 20/40 = 50%, ponizej dolnej granicy 54,6%
    p = zmiany(BAZA, wariant(windows_positive=20))
    assert any("poza przedzialem" in x for x in p)


def test_odsetek_powyzej_przedzialu_tez_przechodzi():
    """Poprawa poza przedzialem to rowniez zdarzenie — moze oznaczac blad,
    a nie sukces. Ten projekt mial juz wynik zawyzony o 74% przez prowizje
    liczona poza miara."""
    p = zmiany(BAZA, wariant(windows_positive=35))     # 87,5% > 81,9%
    assert any("poza przedzialem" in x for x in p)


def test_ubytek_okien_przechodzi_nawet_gdy_odsetek_rosnie():
    """Najwazniejszy przypadek w calym module.

    26 okien zamiast 40 przy WYZSZYM odsetku wyglada jak poprawa, a jest utrata
    jednej trzeciej dowodow — zwykle dlatego, ze kalibracja znalazla uklad wag,
    ktory prawie nie handluje. Dokladnie tak zachowywalo sie losowe
    przeszukiwanie, zanim je usunieto.
    """
    p = zmiany(BAZA, wariant(windows_total=26, windows_positive=20))   # 76,9%
    assert any("ubylo okien" in x for x in p)


def test_zmiana_znaku_mediany_przechodzi():
    p = zmiany(BAZA, wariant(median_oos_sharpe=-0.011))
    assert any("zmienila znak" in x for x in p)


def test_dryf_powyzej_progu_i_rosnacy_przechodzi():
    p = zmiany(BAZA, wariant(max_psi=1.4))
    assert any("dryf" in x for x in p)


# --- przypadki brzegowe -----------------------------------------------------
def test_pierwszy_przebieg_odzywa_sie_raz():
    p = zmiany(None, BAZA)
    assert len(p) == 1 and "pierwszy" in p[0]


def test_przebieg_bez_okien_informatywnych_to_alarm():
    """Zero okien to nie jest wynik ujemny — to brak wyniku, czyli awaria
    ciagu danych albo kalibracji. Musi sie odezwac natychmiast."""
    p = zmiany(BAZA, wariant(windows_total=0, windows_positive=0))
    assert len(p) == 1 and "ani jednego okna" in p[0]


def test_brak_przedzialu_u_poprzednika_nie_wywraca():
    stary = wariant(positive_ci_low=None, positive_ci_high=None)
    assert zmiany(stary, wariant(windows_positive=20)) == []


def test_kilka_powodow_naraz_jest_raportowanych_lacznie():
    p = zmiany(BAZA, wariant(status="degraded", windows_positive=12,
                             median_oos_sharpe=-0.05))
    assert len(p) >= 3


# --- kontrola swiezosci cech (services/engine/train.py) ---------------------
def test_prog_swiezosci_jest_ten_sam_co_w_petli_decyzyjnej():
    """Trener i pętla decyzyjna muszą używać JEDNEJ liczby.

    Osobna stała dobrana w trenerze rozjechałaby się z progiem, którego pętla
    używa przy odczycie point-in-time — i wtedy trener uczyłby się na cechach,
    których produkcja już nie przyjmuje.
    """
    from services.engine.horizons import HORIZONS

    for bar, h in HORIZONS.items():
        assert h.max_staleness_min > 0, bar
        # Prog musi byc dluzszy niz swieca, inaczej kazdy przebieg tuz po
        # zamknieciu bariery falszywie zglaszalby awarie.
        assert h.max_staleness_min > h.bar_minutes, (
            f"{bar}: prog {h.max_staleness_min} min nie jest dluzszy niz swieca "
            f"{h.bar_minutes} min")
