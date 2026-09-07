"""Testy regul limitow ryzyka.

Kazdy test sprawdza JEDNA regule i zawiera przypadek graniczny po obu stronach
progu. Test, ktory tylko potwierdza, ze przy zerowej ekspozycji wolno wejsc,
nie dowodzi niczego - przeszedlby takze wtedy, gdyby funkcja zawsze zwracala
"allow". Dlatego wszedzie jest para: tuz pod progiem przechodzi, na progu nie.
"""
from decimal import Decimal

import pytest

from services.engine.risk_rules import (
    DEFAULT_LIMITS, RiskState, evaluate_account, evaluate_limits,
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
    #
    # Prog kapitalu (95 000) jest CIASNIEJSZY niz limit obsuniecia i przy tych
    # wartosciach zadzialalby pierwszy, zaslaniajac to, co ten test mierzy.
    # Wylaczamy go tutaj, a jego pierwszenstwo sprawdza osobny test nizej -
    # inaczej "obsuniecie blokuje na 10%" przechodziloby z niewlasciwego powodu.
    bez_progu = {k: v for k, v in L.items() if k != "min_equity"}
    v = evaluate_limits(stan(equity=equity, day_ago=equity, peak=Decimal("100000")),
                        bez_progu, 1, Decimal("3000"), Decimal("0.1"))
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


# ============================================================================
# LIMITY ZWIAZANE Z KAPITALEM
#
# Trzy limity byly zapisane jako kwoty wyliczone dla rachunku 100 000 USD.
# Na rachunku 10 000 te same kwoty znaczylyby 50%, 150% i 15% - a limit lacznej
# ekspozycji, ktory trzyma cala konstrukcje, przestalby cokolwiek ograniczac,
# bo na spocie bez dzwigni nie da sie przekroczyc 100% rachunku.
#
# Nie objawiloby sie to awaria: system handlowalby dalej i raportowal zielone
# statusy, ryzykujac dziesieciokrotnie wiecej niz to, na czym mierzono wynik.
# ============================================================================

from services.engine.risk_rules import (            # noqa: E402
    position_budget, wartosc_limitu,
)


def konto(equity, peak=None, exposure="0", per_inst=None):
    return RiskState(equity=Decimal(str(equity)),
                     peak=Decimal(str(peak if peak is not None else equity)),
                     day_ago=Decimal(str(equity)),
                     exposure=Decimal(str(exposure)),
                     per_instrument=per_inst or {},
                     orders_last_hour=0)


def test_procent_kapitalu_skaluje_sie_z_rachunkiem():
    lim = {"value": Decimal("15"), "unit": "pct_equity"}
    assert wartosc_limitu(lim, Decimal("100000")) == Decimal("15000")
    assert wartosc_limitu(lim, Decimal("10000")) == Decimal("1500")


def test_kwota_NIE_skaluje_sie_z_rachunkiem():
    """Kanarek do poprzedniego: gdyby helper skalowal wszystko, limity, ktore
    maja byc kwotowe z natury (minimum gieldowe, prog kapitalu), zmienialyby
    sie razem z rachunkiem."""
    lim = {"value": Decimal("15000"), "unit": "quote_ccy"}
    assert wartosc_limitu(lim, Decimal("100000")) == Decimal("15000")
    assert wartosc_limitu(lim, Decimal("10000")) == Decimal("15000")


def test_brak_jednostki_znaczy_kwote():
    """Stare wiersze w bazie nie maja jednostki w slowniku podawanym do regul.
    Domysl musi byc taki, jaki byl - inaczej migracja zmienialaby zachowanie
    zanim ktokolwiek zmieni wartosc."""
    assert wartosc_limitu({"value": Decimal("5000")}, Decimal("10000")) == Decimal("5000")


def test_ten_sam_zapis_daje_TO_SAMO_ryzyko_na_obu_rachunkach():
    """Wlasciwa niezmienna wlasnosc: udzial kapitalu, a nie kwota."""
    duzy = position_budget(DEFAULT_LIMITS, 10, kapital=Decimal("100000"))
    maly = position_budget(DEFAULT_LIMITS, 10, kapital=Decimal("10000"))
    assert duzy / Decimal("100000") == maly / Decimal("10000")
    assert duzy == Decimal("1500"), "na 100 000 ma wyjsc tyle, ile bylo przed zmiana"
    assert maly == Decimal("1500") / 10


def test_limit_ekspozycji_na_malym_rachunku_FAKTYCZNIE_blokuje():
    """Sedno sprawy. Przy limicie kwotowym 15 000 na rachunku 10 000 zlecenie
    na polowe rachunku przechodzilo, bo 5 000 < 15 000."""
    polowa_rachunku = Decimal("5000")
    v = evaluate_limits(konto(10_000), DEFAULT_LIMITS, instrument_id=1,
                        notional=polowa_rachunku, qty=Decimal("1"))
    assert v.verdict != "allow", (
        f"zlecenie na 50% rachunku ma zostac zatrzymane, werdykt: {v.verdict}")


def test_prog_kapitalu_zatrzymuje_silnik():
    limity = DEFAULT_LIMITS | {
        "min_equity": {"value": Decimal("9500"), "unit": "quote_ccy",
                       "action": "killswitch"}}
    assert evaluate_account(konto(9_600), limity).verdict == "allow"
    v = evaluate_account(konto(9_500), limity)
    assert v.verdict == "block"
    assert v.trigger_killswitch, "prog kapitalu ma uruchamiac kill-switch, nie tylko blokowac"


def test_prog_kapitalu_NIE_jest_duplikatem_obsuniecia():
    """Prog liczony od szczytu bylby dokladnie limitem obsuniecia - czyli
    zabezpieczeniem, ktore wyglada na dodatkowe i nim nie jest.

    Rachunek urosl do 20 000 i wrocil do 9 400. Obsuniecie to 53%, wiec limit
    obsuniecia zadzialalby tak czy inaczej. Sprawdzamy sytuacje odwrotna:
    kapital ponizej progu przy MALYM obsunieciu."""
    limity = {"min_equity": {"value": Decimal("9500"), "unit": "quote_ccy",
                             "action": "killswitch"},
              "max_drawdown": {"value": Decimal("10"), "unit": "pct",
                               "action": "killswitch"}}
    # Szczyt 10 000, kapital 9 400 -> obsuniecie 6%, ponizej progu 10%.
    v = evaluate_account(konto(9_400, peak=10_000), limity)
    assert v.verdict == "block", "prog kapitalu ma zadzialac tam, gdzie obsuniecie jeszcze nie"
    assert "Kapitał" in (v.reason or "")


def test_prog_kapitalu_trzyma_zwiazek_z_kapitalem_startowym():
    """Wartosc progu jest w DEFAULT_LIMITS wpisana liczba, bo START_EQUITY
    definiuje sie nizej w pliku i odwolanie wywracaloby import. Zwiazek miedzy
    nimi musi wiec pilnowac test - inaczej zmiana kapitalu startowego zostawia
    prog na starej kwocie i nikt tego nie zauwaza."""
    from services.engine.risk_rules import START_EQUITY

    prog = DEFAULT_LIMITS["min_equity"]
    assert prog["unit"] == "quote_ccy"
    assert float(prog["value"]) == pytest.approx(START_EQUITY * 0.95), (
        f"prog {prog['value']} nie jest 5% ponizej kapitalu startowego {START_EQUITY}")


def test_prog_kapitalu_dziala_PRZED_limitem_obsuniecia():
    """Kolejnosc jest tresci a, nie szczegolem. Przy kapitale startowym 100 000
    prog 95 000 to strata 5%, a limit obsuniecia dopuszcza 10% - wiec o tym,
    kiedy system sie zatrzyma, decyduje prog. Gdyby bylo odwrotnie, prog byłby
    ozdoba: zadzialalby dopiero po tym, jak zadzialalo cos innego."""
    v = evaluate_account(konto(94_000, peak=100_000), DEFAULT_LIMITS)
    assert v.verdict == "block"
    assert "Kapitał" in (v.reason or ""), (
        f"pierwszy ma zadzialac prog kapitalu, a zadzialalo: {v.reason}")


def test_wydruk_limitu_ekspozycji_nie_zalezy_od_jednostki():
    """Kanarek na blad, ktory ten wydruk juz mial.

    Do 0028 limit byl kwota i wydruk dzielil go przez kapital startowy. Po
    zmianie jednostki ta sama linia pokazywala "15 z 100000, czyli 0%" - liczba
    prawdziwa, zdanie falszywe. Sprawdzamy oba zapisy tej samej rzeczy.
    """
    from services.engine.risk_rules import START_EQUITY

    kwotowo = {"value": Decimal("15000"), "unit": "quote_ccy"}
    procentowo = {"value": Decimal("15"), "unit": "pct_equity"}
    baza = Decimal(str(START_EQUITY))
    assert wartosc_limitu(kwotowo, baza) == wartosc_limitu(procentowo, baza)
    assert float(wartosc_limitu(procentowo, baza)) / START_EQUITY == pytest.approx(0.15)
