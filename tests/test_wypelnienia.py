"""Wypełnienie częściowe — trzeci stan, którego symulacja nie zna."""
from decimal import Decimal as D

import pytest

from services.engine.egzekucja import co_z_wypelnieniem, rozjazd_z_symulacja
from services.engine.zlecenia import Filtry

F = Filtry(krok_ilosci=D("0.01"), min_ilosc=D("0.01"),
           min_wartosc=D("5"), krok_ceny=D("0.01"))


def test_czesciowe_NIE_jest_traktowane_jak_pelne():
    """Pozycja w dzienniku większa niż faktyczna blokuje wejścia, których nic
    nie blokuje — a przy wyjściu sprzedaje ilość, której nie mamy."""
    r = co_z_wypelnieniem(D("4"), D("10"), D("100"), F)
    assert r.rodzaj == "czesciowe"
    assert r.ilosc == D("4")


def test_czesciowe_NIE_jest_traktowane_jak_brak():
    """Zapisanie „nie weszło" o czymś, co weszło, tworzy pozycję-widmo —
    naszą własną ręką."""
    r = co_z_wypelnieniem(D("4"), D("10"), D("100"), F)
    assert r.rodzaj != "brak"
    assert r.ilosc > 0
    assert r.anuluj_reszte is True


def test_wypelnienie_ponizej_minimum_jest_NAZWANE_a_nie_odrzucone():
    """Aktywo jest już nasze niezależnie od tego, co zapiszemy. Odrzucenie tego
    wypełnienia byłoby stworzeniem pozycji-widma, a nie uniknięciem jej."""
    r = co_z_wypelnieniem(D("0.02"), D("10"), D("100"), F)
    assert r.rodzaj == "nie_do_zamkniecia"
    assert r.ilosc == D("0.02")           # przyjmujemy, mimo ze klopotliwe
    assert "NIE DA SIĘ" in r.powod


def test_ulamek_mierzy_zawyzenie_backtestu():
    """Symulacja przyjmuje 1.0 dla każdego zlecenia, które dotknęło limitu.
    Średnia z ułamków faktycznych jest bezpośrednią miarą tego zawyżenia."""
    r = rozjazd_z_symulacja([
        co_z_wypelnieniem(D("10"), D("10"), D("100"), F),   # 1.0
        co_z_wypelnieniem(D("4"), D("10"), D("100"), F),    # 0.4
        co_z_wypelnieniem(D("0"), D("10"), D("100"), F),    # nie liczy sie
    ])
    assert r["n"] == 2
    assert r["sredni_ulamek"] == D("0.7")
    assert r["czesciowych"] == 1


def test_brak_wypelnien_nie_udaje_ulamka_zerowego():
    """None znaczy „nie zmierzono", zero znaczy „zmierzono i wyszło zero".
    Zero w tym miejscu wyglądałoby jak katastrofalna jakość egzekucji."""
    r = rozjazd_z_symulacja([co_z_wypelnieniem(D("0"), D("10"), D("100"), F)])
    assert r["n"] == 0
    assert r["sredni_ulamek"] is None


def test_nadwyzka_ponad_plan_nie_powieksza_pozycji():
    """Giełda nie powinna dać więcej, niż zamówiono, ale gdyby dała, pozycja
    ma zostać taka, jaką zatwierdził risk kernel."""
    r = co_z_wypelnieniem(D("12"), D("10"), D("100"), F)
    assert r.rodzaj == "pelne"
    assert r.ilosc == D("10")


def test_plan_o_zerowej_ilosci_nie_przechodzi_po_cichu():
    with pytest.raises(ValueError):
        co_z_wypelnieniem(D("0"), D("0"), D("100"), F)


# ------------------------------- prowizja pobrana w kupionym aktywie

def test_prowizja_w_aktywie_ZMNIEJSZA_posiadana_ilosc():
    """Zlecenie na 1,0 SOL przy makerze 0,1% daje nam 0,999 SOL — Bybit potrąca
    prowizję z otrzymanej ilości.

    Zapisanie 1,0 tworzy pozycję, KTÓREJ NIE DA SIĘ SPRZEDAĆ: sygnał wyjścia
    planuje sprzedaż całości, giełda odrzuca ją za brak salda, plan wygasa,
    następna świeca powtarza to samo. Nic tej pętli nie przerywa, bo każdy
    przebieg wygląda identycznie jak poprzedni.
    """
    from services.engine.egzekucja import rozlicz_prowizje

    ilosc, koszt = rozlicz_prowizje(D("1.0"), D("140"), D("0.001"),
                                    "SOL", "SOL", "USDC")
    assert ilosc == D("0.999")
    assert koszt == D("0.140")


def test_prowizja_w_aktywie_JEST_PRZELICZANA_na_walute_kwotowana():
    """`update_equity` sumuje `fees_paid` jako liczby bez jednostki. Prowizja
    0,001 SOL wpisana wprost odejmowała od kapitału 0,001 zamiast 0,14 —
    zaniżenie stukrotne.

    Kierunek jest najgorszy z możliwych: ZAWYŻA kapitał, więc obsunięcie wychodzi
    mniejsze i kill-switch strzela później, niż powinien.
    """
    from services.engine.egzekucja import rozlicz_prowizje

    _, koszt = rozlicz_prowizje(D("1.0"), D("140"), D("0.001"),
                                "SOL", "SOL", "USDC")
    assert koszt == D("0.14")
    assert koszt != D("0.001")


def test_prowizja_w_walucie_kwotowanej_NIE_rusza_ilosci():
    """Przy sprzedaży prowizja idzie w walucie kwotowanej — odjęcie jej od
    ilości byłoby wtedy odejmowaniem jabłek od gruszek."""
    from services.engine.egzekucja import rozlicz_prowizje

    ilosc, koszt = rozlicz_prowizje(D("1.0"), D("140"), D("0.14"),
                                    "USDC", "SOL", "USDC")
    assert ilosc == D("1.0")
    assert koszt == D("0.14")


def test_NIEZNANA_waluta_prowizji_nie_zmienia_tego_co_uwazamy_za_posiadane():
    """Domysł może być potrzebny przy koszcie, ale nie przy ilości: pomyłka
    w drugą stronę stworzyłaby pozycję-widmo z niczego."""
    from services.engine.egzekucja import rozlicz_prowizje

    ilosc, _ = rozlicz_prowizje(D("1.0"), D("140"), D("0.14"),
                                None, "SOL", "USDC")
    assert ilosc == D("1.0")
