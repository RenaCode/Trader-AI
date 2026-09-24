"""Miary, które dziś znaczą coś innego, niż sugeruje ich nazwa.

TRZY WADY, KAŻDA CIĄGNĄCA WYNIK W GÓRĘ

1. SHARPE JEST ŚREDNIĄ ILORAZÓW, NIE ILORAZEM.

   `portfolio.metrics` liczy `mean(zwroty_okna) / pstdev(zwroty_okna)` — Sharpe
   NA TRANSAKCJĘ, w obrębie jednego okna. Trener zbiera te liczby okno po oknie
   i podaje ich średnią. Okno z pięcioma transakcjami waży wtedy tyle samo, co
   okno z dwustoma, a okno o małym rozrzucie dostaje duży iloraz z samego
   mianownika. Średnia ilorazów nie jest ilorazem ze złączonej próby i nie ma
   powodu, żeby leżała blisko niej.

2. `pstdev` ZAMIAST `stdev` ZANIŻA MIANOWNIK.

   Odchylenie populacyjne dzieli przez n, próbkowe przez n−1. Przy `min_trades`
   rzędu ośmiu to zaniżenie o 6,5%, czyli zawyżenie Sharpe'a o tyle samo — na
   liczbie rzędu 0,045 mówimy o trzech tysięcznych, ale w jedną stronę: zawsze
   w górę, w każdym oknie.

3. `profit_factor == 0.0`, GDY NIE BYŁO ANI JEDNEJ STRATY.

   Okno bez strat jest najlepszym możliwym oknem i dostaje najgorszą możliwą
   wartość. Wchodzi potem do średniej z innymi i ciągnie ją w dół, podczas gdy
   pojedyncze okno o wartości 129 ciągnie ją w górę. Średnia z wielkości
   nieograniczonej z góry i uciętej z dołu nie opisuje niczego.

CZWARTA RZECZ, KTÓRA NIE JEST WADĄ RACHUNKU, TYLKO RAPORTU

Rozkład o skrajnej 129 przy średniej 1,64 jest skośny i średnia go nie opisuje.
Mediana i odsetek okien ujemnych opisują. `rozklad()` istnieje po to, żeby nie
dało się podać średniej bez nich.
"""
import math
import statistics

import pytest

from services.engine.statystyka import profit_factor, rozklad, sharpe


# --- Sharpe: probkowe odchylenie, nie populacyjne ---------------------------

def test_sharpe_uzywa_odchylenia_PROBKOWEGO():
    """Dzielnik n−1, nie n. Inaczej każdy Sharpe jest zawyżony."""
    zwroty = [0.01, -0.005, 0.02, 0.0, -0.01, 0.015, 0.005, -0.002]
    oczekiwane = statistics.fmean(zwroty) / statistics.stdev(zwroty)
    assert abs(sharpe(zwroty) - oczekiwane) < 1e-12
    # ...i jest MNIEJSZY niz ten liczony populacyjnie.
    assert sharpe(zwroty) < statistics.fmean(zwroty) / statistics.pstdev(zwroty)


def test_sharpe_z_jednej_obserwacji_to_None_a_NIE_liczba():
    """Jedna transakcja nie ma rozrzutu. Iloraz przez zero albo przez podłogę
    daje liczbę tym większą, im mniej o niej wiadomo."""
    assert sharpe([0.01]) is None
    assert sharpe([]) is None


def test_sharpe_przy_zerowym_rozrzucie_to_None_a_NIE_liczba_z_podlogi():
    """`portfolio.metrics` podstawia tu MIN_SD = 1e-4, więc osiem identycznych
    zwrotów po 1% daje Sharpe 100. To nie jest przewaga stukrotna, to dzielenie
    przez stałą wpisaną, żeby nie było dzielenia przez zero."""
    assert sharpe([0.01] * 8) is None


# --- Sharpe zbiorczy kontra srednia z okien ---------------------------------

def test_srednia_z_okien_ROZNI_SIE_od_sharpe_ze_zlaczonej_proby():
    """Sedno wady 1. Dwa okna: jedno spokojne i krótkie, drugie zmienne
    i długie. Średnia ilorazów mówi co innego niż iloraz z całości."""
    spokojne = [0.001, 0.0011, 0.0009, 0.0012, 0.0008, 0.001, 0.0011, 0.0009]
    zmienne = [0.05, -0.06, 0.04, -0.05, 0.03, -0.04, 0.02, -0.03] * 10

    srednia_okien = statistics.fmean([sharpe(spokojne), sharpe(zmienne)])
    zbiorczy = sharpe(spokojne + zmienne)

    # Ciagnie je okno o malym rozrzucie: osiem transakcji po jednej dziesiatej
    # procenta daje iloraz ponad 6, bo mianownik jest mikroskopijny.
    assert srednia_okien > 1.0
    # Zlaczona proba ma przewage UJEMNA. Nie chodzi wiec o skale, tylko o znak:
    # te same transakcje, dwie miary, dwa przeciwne wnioski.
    assert zbiorczy < 0
    assert srednia_okien > 5 * abs(zbiorczy)


# --- profit factor ----------------------------------------------------------

def test_brak_strat_daje_None_a_NIE_zero():
    """Najlepsze możliwe okno nie może dostawać najgorszej możliwej wartości."""
    assert profit_factor([0.01, 0.02, 0.03]) is None


def test_brak_zyskow_daje_ZERO_i_to_jest_prawda():
    assert profit_factor([-0.01, -0.02]) == 0.0


def test_profit_factor_liczy_sie_normalnie_gdy_sa_obie_strony():
    assert abs(profit_factor([0.03, -0.01]) - 3.0) < 1e-12


def test_pusta_lista_to_None():
    assert profit_factor([]) is None


# --- rozklad ----------------------------------------------------------------

def test_rozklad_podaje_mediane_i_odsetek_ujemnych_obok_sredniej():
    """Rozkład skośny: jedna wartość 129 przy dziewięciu w okolicy jedności.
    Średnia 14, mediana 1,1 — i to mediana opisuje typowe okno."""
    xs = [0.9, 1.0, 1.1, 1.2, 0.8, 1.05, 1.15, 0.95, 1.1, 129.0]
    r = rozklad(xs)
    assert r["n"] == 10
    assert r["srednia"] > 10
    assert 1.0 < r["mediana"] < 1.2
    assert r["najwiekszy"] == 129.0
    assert r["udzial_ujemnych"] == 0.0


def test_rozklad_liczy_ujemne_bo_to_one_decyduja():
    xs = [-0.5, -0.2, 0.1, 0.3, -0.1, 0.4]
    r = rozklad(xs)
    assert r["ujemnych"] == 3
    assert abs(r["udzial_ujemnych"] - 0.5) < 1e-12


def test_rozklad_ma_kwartyle_a_nie_sam_zakres():
    """Zakres opisuje dwie skrajne obserwacje. Kwartyle opisują resztę."""
    r = rozklad([float(i) for i in range(1, 101)])
    assert r["q1"] < r["mediana"] < r["q3"]
    assert r["q1"] > r["najmniejszy"]
    assert r["q3"] < r["najwiekszy"]


def test_rozklad_pustej_proby_NIE_udaje_zer():
    r = rozklad([])
    assert r["n"] == 0
    assert r["srednia"] is None and r["mediana"] is None
    assert r["udzial_ujemnych"] is None


@pytest.mark.parametrize("xs", [[1.0], [1.0, 2.0]])
def test_rozklad_malej_proby_nadal_daje_mediane(xs):
    """Kwartyle bywają wtedy None, mediana nie — i ona jest tu potrzebna."""
    r = rozklad(xs)
    assert r["mediana"] is not None
    assert r["n"] == len(xs)


# --- zgodnosc z tym, co liczy produkcja -------------------------------------

def test_sharpe_NIE_JEST_anualizowany_i_moduł_tego_NIE_UKRYWA():
    """Nazwa `sharpe` w tym projekcie znaczy „na transakcję", nie „rocznie".
    Docstring musi to mówić, bo różnica to czynnik rzędu pierwiastka z liczby
    transakcji w roku — czyli kilkanaście razy."""
    assert "transakcj" in sharpe.__doc__.lower()
    assert not math.isnan(sharpe([0.01, -0.01, 0.02, -0.005]))
