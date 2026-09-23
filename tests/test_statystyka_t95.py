"""`t95` miedzy wpisami tablicy MUSI brac wartosc SZERSZA, nie wezsza.

DLACZEGO TEN PLIK ISTNIEJE

Wartosc krytyczna t MALEJE wraz ze stopniami swobody. `t95` deklarowala
w docstringu, ze miedzy wpisami tablicy bierze "NAJBLIZSZY WIEKSZY df, czyli
zawsze ostrozniejsza (szersza) z dwoch" - i robila dokladnie odwrotnie:
wiekszy df to MNIEJSZA wartosc krytyczna, czyli przedzial WEZSZY.

Za waski przedzial nie jest drobna niescisloscia. `poslizg.py` liczy przedzial
zmierzonego poslizgu przez `przedzial_sredniej` i ALARMUJE, gdy zalozenie
POSLIZG_TAKER wypada z przedzialu (tests/test_poslizg.py). Przy 17
wypelnieniach (df=16) przedzial byl 1,6% za waski, wiec zalozenie lezace tuz
wewnatrz prawdziwego przedzialu wypadalo z policzonego: falszywy alarm
"poslizg rozjechal sie z zalozeniem" i pokusa, zeby zmienic stala kosztu na
podstawie szumu. To jest ten sam ksztalt, przed ktorym broni sie komentarz przy
samej tablicy T95: "za waski przedzial mowi »zmierzone i rozne od zalozenia«
o czyms, czego jeszcze nie widac".

Liczby nizej sa z tablicy rozkladu t Studenta, dwustronnie, poziom 95%.
Testy sa na KONKRETNYCH liczbach, a nie na samej wlasnosci "szerszy", bo
odwrocona petla tez spelnia jakas wlasnosc monotonicznosci - roznia sie
dopiero wartosciami.
"""
from __future__ import annotations

import pytest

from services.engine.statystyka import T95, przedzial_sredniej, t95

# df -> (co ma zwrocic t95, prawdziwa wartosc krytyczna dla tego df).
# Kazdy wiersz to df MIEDZY wpisami tablicy, czyli miejsce, w ktorym w ogole
# jest jakis wybor do zrobienia. Trzecia kolumna: co zwracal kod sprzed
# poprawki - zawsze ponizej prawdziwej wartosci, czyli za waski przedzial.
MIEDZY_WPISAMI = [
    # df,  oczekiwane, prawdziwe, sprzed poprawki
    (11,   2.228,      2.201,     2.179),
    (13,   2.179,      2.160,     2.131),
    (16,   2.131,      2.120,     2.086),
    (22,   2.086,      2.074,     2.060),
    (35,   2.042,      2.030,     2.021),
    (61,   2.000,      1.999,     1.980),
]


@pytest.mark.parametrize("df,oczekiwane,prawdziwe,sprzed_poprawki", MIEDZY_WPISAMI)
def test_miedzy_wpisami_bierze_wartosc_SZERSZA(df, oczekiwane, prawdziwe, sprzed_poprawki):
    assert t95(df) == oczekiwane
    assert t95(df) >= prawdziwe, (
        f"df={df}: {t95(df)} jest WEZSZE niz prawdziwe {prawdziwe} - przedzial "
        f"udaje dokladniejszy, niz jest")
    assert sprzed_poprawki < prawdziwe, (
        "wiersz testu bezuzyteczny: wartosc sprzed poprawki nie byla za waska")


@pytest.mark.parametrize("df,_o,_p,sprzed_poprawki", MIEDZY_WPISAMI)
def test_kod_sprzed_poprawki_by_tego_nie_przeszedl(df, _o, _p, sprzed_poprawki):
    assert t95(df) != sprzed_poprawki


@pytest.mark.parametrize("df", sorted(T95))
def test_na_wpisie_tablicy_zwraca_dokladnie_ten_wpis(df):
    assert t95(df) == T95[df]


def test_ponizej_najmniejszego_df_nie_wpada_w_1_96():
    # df=0 nie powinno tu trafic (przedzial_sredniej wymaga n>=2), ale gdyby
    # trafilo, ma byc najszersza wartoscia z tablicy, a nie najwezsza.
    assert t95(0) == T95[1] == 12.706


def test_powyzej_najwiekszego_df_zostaje_przy_1_980_a_nie_1_96():
    # 1,96 jest wartoscia asymptotyczna, czyli WEZSZA od kazdego wpisu tablicy.
    assert t95(500) == 1.980
    assert t95(500) > 1.96


def test_jest_nierosnaca_wraz_z_df():
    poprzednia = t95(1)
    for df in range(1, 200):
        biezaca = t95(df)
        assert biezaca <= poprzednia, f"df={df}: przedzial urosl przy WIEKSZEJ probie"
        poprzednia = biezaca


def test_przedzial_sredniej_na_17_obserwacjach_jest_szerszy_niz_byl():
    # 17 obserwacji -> df=16, czyli najwiekszy blad sprzed poprawki (1,6%).
    xs = [0.0, 1.0] * 8 + [0.5]
    lo, hi = przedzial_sredniej(xs)
    szerokosc = hi - lo
    # ta sama arytmetyka z wartoscia sprzed poprawki
    szerokosc_sprzed = szerokosc * (2.086 / 2.131)
    assert szerokosc > szerokosc_sprzed
    assert szerokosc / szerokosc_sprzed == pytest.approx(2.131 / 2.086, rel=1e-9)
