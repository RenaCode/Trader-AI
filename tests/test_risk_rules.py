"""Testy regul limitow ryzyka.

Kazdy test sprawdza JEDNA regule i zawiera przypadek graniczny po obu stronach
progu. Test, ktory tylko potwierdza, ze przy zerowej ekspozycji wolno wejsc,
nie dowodzi niczego - przeszedlby takze wtedy, gdyby funkcja zawsze zwracala
"allow". Dlatego wszedzie jest para: tuz pod progiem przechodzi, na progu nie.
"""
from decimal import Decimal

import pytest

from services.engine.risk_rules import (
    DEFAULT_LIMITS, RiskState, evaluate_limits,
)

L = DEFAULT_LIMITS


def stan(**kw) -> RiskState:
    baza = dict(equity=Decimal("100000"), peak=Decimal("100000"),
                day_ago=Decimal("100000"), exposure=Decimal("0"),
                per_instrument={}, orders_last_hour=0, killswitch=None)
    return RiskState(**(baza | kw))


def test_czysty_stan_przepuszcza():
    v = evaluate_limits(stan(), L, 1, Decimal("3000"), Decimal("0.1"))
    assert v.verdict == "allow"
    assert v.qty == Decimal("0.1")


def test_killswitch_blokuje_wszystko():
    v = evaluate_limits(stan(killswitch={"reason": "test", "triggered_at": None}),
                        L, 1, Decimal("1"), Decimal("0.001"))
    assert v.verdict == "block"
    assert "Kill-switch" in v.reason


def test_killswitch_ma_pierwszenstwo_przed_obcieciem():
    """Najwazniejszy test kolejnosci.

    Gdyby limit wielkosci pozycji sprawdzal sie przed kill-switchem, system
    zmniejszylby zlecenie i WYSLAL je mimo zatrzymanego silnika.
    """
    v = evaluate_limits(
        stan(killswitch={"reason": "obsuniecie", "triggered_at": None},
             per_instrument={1: Decimal("4900")}),
        L, 1, Decimal("3000"), Decimal("0.1"))
    assert v.verdict == "block", "kill-switch musi przebijac obciecie zlecenia"


@pytest.mark.parametrize("equity,oczekiwany", [
    (Decimal("98100"), "allow"),   # strata 1,90% - ponizej progu 2%
    (Decimal("98000"), "block"),   # strata 2,00% - na progu
])
def test_strata_dzienna(equity, oczekiwany):
    v = evaluate_limits(stan(equity=equity), L, 1, Decimal("3000"), Decimal("0.1"))
    assert v.verdict == oczekiwany


def test_strata_dzienna_uruchamia_killswitch():
    v = evaluate_limits(stan(equity=Decimal("97000")), L, 1, Decimal("3000"), Decimal("0.1"))
    assert v.trigger_killswitch is not None
    assert "Strata dzienna" in v.trigger_killswitch


@pytest.mark.parametrize("equity,oczekiwany", [
    (Decimal("90100"), "allow"),   # obsuniecie 9,90%
    (Decimal("90000"), "block"),   # obsuniecie 10,00% - na progu
])
def test_obsuniecie(equity, oczekiwany):
    # day_ago rowne equity, zeby limit straty dziennej nie zadzialal wczesniej.
    v = evaluate_limits(stan(equity=equity, day_ago=equity, peak=Decimal("100000")),
                        L, 1, Decimal("3000"), Decimal("0.1"))
    assert v.verdict == oczekiwany


@pytest.mark.parametrize("n,oczekiwany", [(19, "allow"), (20, "block")])
def test_czestotliwosc_zlecen(n, oczekiwany):
    v = evaluate_limits(stan(orders_last_hour=n), L, 1, Decimal("3000"), Decimal("0.1"))
    assert v.verdict == oczekiwany


@pytest.mark.parametrize("ekspozycja,oczekiwany", [
    (Decimal("12000"), "allow"),   # 12000 + 3000 = 15000, dokladnie na limicie
    (Decimal("12001"), "block"),   # 15001 - ponad
])
def test_laczna_ekspozycja(ekspozycja, oczekiwany):
    v = evaluate_limits(stan(exposure=ekspozycja), L, 1, Decimal("3000"), Decimal("0.1"))
    assert v.verdict == oczekiwany


def test_wielkosc_pozycji_obcina_a_nie_blokuje():
    v = evaluate_limits(stan(per_instrument={1: Decimal("3500")}),
                        L, 1, Decimal("3000"), Decimal("0.1"))
    assert v.verdict == "downsize"
    # Zostalo 1500 z 5000, wiec przechodzi polowa z 3000.
    assert v.qty == Decimal("0.050000")


def test_wielkosc_pozycji_blokuje_gdy_limit_juz_wypelniony():
    v = evaluate_limits(stan(per_instrument={1: Decimal("5000")}),
                        L, 1, Decimal("3000"), Decimal("0.1"))
    assert v.verdict == "block"


def test_limit_pozycji_dotyczy_wlasciwego_instrumentu():
    """Kanarek na pomylke identyfikatora.

    Pozycja na instrumencie 2 nie moze ograniczac zlecenia na instrumencie 1.
    Gdyby funkcja sumowala wszystko zamiast czytac po kluczu, ten test padnie.
    """
    v = evaluate_limits(stan(per_instrument={2: Decimal("5000")}, exposure=Decimal("5000")),
                        L, 1, Decimal("3000"), Decimal("0.1"))
    assert v.verdict == "allow"


def test_brak_limitu_w_konfiguracji_nie_wywraca_oceny():
    v = evaluate_limits(stan(exposure=Decimal("999999")), {}, 1,
                        Decimal("3000"), Decimal("0.1"))
    assert v.verdict == "allow"
