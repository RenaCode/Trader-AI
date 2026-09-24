"""Przewaga liczona tak, żeby dało się ją porównać z prawdziwymi pieniędzmi.

CO RAPORTOWAŁ WALK-FORWARD

    sharpe         0,0452   (-0,8531 .. 1,3273)
    avg_r          0,1376
    hit_rate       0,4872
    profit_factor  1,6419   (0 .. 129,2275)

Trzy z tych czterech liczb są ŚREDNIMI PO OKNACH z wielkości, których średnia
nie opisuje, a czwarta (`hit_rate`) jest jedyną, która znaczy to, co mówi.

CZEGO TU BRAKUJE NAJBARDZIEJ

Liczby, przy której przewaga znika. `avg_r = 0,1376%` to średni zwrot
z transakcji PO prowizji naliczonej w symulacji — ale symulacja nie nalicza
spreadu, niewypełnienia ani finansowania (patrz `koszty.karta_rundy`). Dopóki
tych składników nie zmierzono, jedyne uczciwe zdanie o przewadze brzmi:
„przeżyje, jeśli to, czego nie policzyliśmy, jest mniejsze niż X punktów
bazowych na rundę". X liczy się bez żadnego założenia — to po prostu średni
zwrot z transakcji — i jest liczbą, na którą właściciel może patrzeć.

DLACZEGO `koszt_progowy` NIE JEST TYM SAMYM CO `avg_r`

Bo `avg_r` jest średnią po OKNACH ze średnich po transakcjach, a koszt płaci
każda transakcja z osobna. Okno z pięcioma transakcjami waży w `avg_r` tyle
samo, co okno z dwustoma. Próg musi być liczony ze złączonej próby.
"""
import statistics

import pytest

from services.engine.train import podsumowanie_przewagi


def test_koszt_progowy_to_sredni_zwrot_ze_ZLACZONEJ_proby():
    """Nie średnia po oknach. Dwa okna o skrajnie różnej liczbie transakcji."""
    male = [0.01] * 2          # dwie transakcje po +1%
    duze = [-0.001] * 198      # sto dziewiecdziesiat osiem po -0,1%
    p = podsumowanie_przewagi(male + duze, [0.5, -0.1])
    # Zlaczona srednia: (2 x 0,01 + 198 x (-0,001)) / 200 = -0,00089.
    assert p["koszt_progowy"] == pytest.approx(-0.00089)
    # A srednia z DWOCH SREDNICH OKIENNYCH (+1% i -0,1%) wyszlaby +0,45%:
    # dodatnia, pieciokrotnie wieksza i z przeciwnym znakiem. Okno o dwoch
    # transakcjach wazy tam tyle samo, co okno o stu dziewiecdziesieciu osmiu.
    assert statistics.fmean([statistics.fmean(male), statistics.fmean(duze)]) > 0
    assert p["koszt_progowy"] < 0


def test_przewaga_ujemna_daje_koszt_progowy_UJEMNY_a_nie_zero():
    """Strategia, która już nie zarabia, nie ma zapasu na koszty — i musi to
    być widoczne jako liczba ujemna, nie jako zero ani None."""
    p = podsumowanie_przewagi([-0.002, -0.001, -0.003, 0.001], [-0.2])
    assert p["koszt_progowy"] < 0


def test_sharpe_po_doplacie_SPADA_a_sam_zwrot_maleje_o_doplate():
    zwroty = [0.01, -0.005, 0.02, 0.0, -0.01, 0.015, 0.005, -0.002]
    bez = podsumowanie_przewagi(zwroty, [0.1])
    z_doplata = podsumowanie_przewagi(zwroty, [0.1], doplata=0.002)
    assert z_doplata["sharpe_zbiorczy"] < bez["sharpe_zbiorczy"]
    assert z_doplata["sredni_zwrot"] == pytest.approx(bez["sredni_zwrot"] - 0.002)


def test_doplata_ROWNA_progowi_zeruje_przewage():
    """Definicja progu: koszt, przy którym średni zwrot wychodzi na zero."""
    zwroty = [0.01, -0.005, 0.02, 0.0, -0.01, 0.015, 0.005, -0.002]
    prog = podsumowanie_przewagi(zwroty, [0.1])["koszt_progowy"]
    assert podsumowanie_przewagi(zwroty, [0.1], doplata=prog)["sredni_zwrot"] \
        == pytest.approx(0.0, abs=1e-15)
    assert podsumowanie_przewagi(zwroty, [0.1], doplata=prog)["sharpe_zbiorczy"] \
        == pytest.approx(0.0, abs=1e-12)


def test_doplata_NIE_ZMIENIA_odchylenia_bo_koszt_jest_staly():
    """Kanarek: stała odjęta od każdej obserwacji przesuwa średnią i zostawia
    rozrzut. Gdyby ruszyła też rozrzut, byłby to błąd rachunku, nie koszt."""
    zwroty = [0.01, -0.005, 0.02, 0.0, -0.01, 0.015, 0.005, -0.002]
    a = podsumowanie_przewagi(zwroty, [0.1])
    b = podsumowanie_przewagi(zwroty, [0.1], doplata=0.003)
    assert a["odchylenie"] == pytest.approx(b["odchylenie"])


def test_srednia_po_oknach_JEST_PODANA_OBOK_zbiorczej_a_nie_zamiast():
    """Obie liczby naraz — bo to one się rozjeżdżają i ten rozjazd jest
    wynikiem, a nie szczegółem rachunku."""
    p = podsumowanie_przewagi([0.01, -0.01, 0.02, -0.005], [1.3, -0.85, 0.05])
    assert p["sharpe_sredni_po_oknach"] == pytest.approx((1.3 - 0.85 + 0.05) / 3)
    assert "sharpe_zbiorczy" in p


def test_rozklad_okien_podaje_MEDIANE_i_odsetek_ujemnych():
    p = podsumowanie_przewagi([0.01, -0.01, 0.02, -0.005], [1.3, -0.85, 0.05])
    ok = p["okna"]
    assert ok["mediana"] == pytest.approx(0.05)
    assert ok["ujemnych"] == 1
    assert ok["udzial_ujemnych"] == pytest.approx(1 / 3)


def test_profit_factor_jest_ZBIORCZY_a_nie_sredni_z_okien():
    """Średnia z wielkości nieograniczonej z góry i uciętej z dołu nie opisuje
    niczego; iloraz ze złączonej próby opisuje."""
    p = podsumowanie_przewagi([0.03, -0.01, 0.02, -0.04], [0.1])
    assert p["profit_factor_zbiorczy"] == pytest.approx(0.05 / 0.05)


def test_pusta_proba_NIE_udaje_wyniku():
    p = podsumowanie_przewagi([], [])
    assert p["sharpe_zbiorczy"] is None
    assert p["koszt_progowy"] is None
    assert p["n_transakcji"] == 0


def test_jedna_transakcja_to_NIE_JEST_sharpe():
    p = podsumowanie_przewagi([0.01], [0.3])
    assert p["sharpe_zbiorczy"] is None
    # ...ale prog kosztowy JEST policzalny z jednej obserwacji i jest nia sama.
    assert p["koszt_progowy"] == pytest.approx(0.01)


# --- martwe stale ----------------------------------------------------------

def test_trener_NIE_MA_juz_wlasnych_stalych_kosztowych():
    """`FEE` i `SLIP` leżały w `train.py` nieużywane od czasu wydzielenia
    `koszty.py`. Druga kopia stawki, której nikt nie czyta, jest gorsza niż
    żadna: przy następnej zmianie cennika ktoś poprawi tę widoczną."""
    import services.engine.train as t

    assert not hasattr(t, "FEE")
    assert not hasattr(t, "SLIP")


# --- krotnosc spreadu, przy ktorej przewaga schodzi do zera ----------------
#
# Arkusz czytamy z hosta GLOBALNEGO, a zlecenia skladamy na bybit.eu - podmiocie
# o rzedu 0,1% tamtego wolumenu (migracja 0018). Zmierzony spread jest wiec
# PODLOGA naszego, a nie jego wartoscia. Pytanie „ile wynosi nasz spread" nie ma
# odpowiedzi; pytanie „ile razy szerszy musialby byc, zeby zjesc przewage" ma,
# i daje sie sprawdzic.

def test_krotnosc_to_iloraz_progu_i_zmierzonego_spreadu():
    from services.engine.train import krotnosc_zabojcza

    # prog 13,76 pb, spread 0,802 pb -> 17,2x
    assert krotnosc_zabojcza(0.001376, 0.802 / 1e4) == pytest.approx(17.156, rel=1e-3)


def test_szerszy_spread_daje_MNIEJSZA_krotnosc():
    from services.engine.train import krotnosc_zabojcza

    assert krotnosc_zabojcza(0.001376, 1.304 / 1e4) < \
        krotnosc_zabojcza(0.001376, 0.802 / 1e4)


def test_krotnosc_ponizej_jedynki_znaczy_ze_JUZ_po_przewadze():
    """Spread sam w sobie szerszy niż cały zapas — i to już na księdze,
    na której nie handlujemy."""
    from services.engine.train import krotnosc_zabojcza

    assert krotnosc_zabojcza(0.0001, 20.0 / 1e4) < 1.0


def test_brak_przewagi_albo_brak_spreadu_daje_None():
    from services.engine.train import krotnosc_zabojcza

    assert krotnosc_zabojcza(-0.001, 0.802 / 1e4) is None   # przewagi juz nie ma
    assert krotnosc_zabojcza(0.001376, None) is None        # spreadu nie zmierzono
    assert krotnosc_zabojcza(0.001376, 0.0) is None         # zerowy spread: brak skali
    assert krotnosc_zabojcza(None, 0.802 / 1e4) is None
