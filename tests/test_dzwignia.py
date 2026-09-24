"""Limit dzwigni - brakujace zabezpieczenie strony krotkiej.

DLACZEGO TEN PLIK ISTNIEJE

Do 2026-09-24 `risk_rules.py` nie znal pojecia dzwigni ANI RAZU. Sprawdzone po
tekscie: jedyne dwa wystapienia slowa "dzwignia" w calym module to zdania
w komentarzach, a nie reguly - w tym jedno wprost zalozycielskie:

    "Limit lacznej ekspozycji przestaje wtedy cokolwiek ograniczac, bo na
     spocie BEZ DZWIGNI nie da sie przekroczyc 100% rachunku"

To zalozenie jest prawdziwe na spocie i falszywe na kontrakcie wieczystym,
gdzie ekspozycja 10x kapitalu jest ustawieniem, nie awaria. Zalozenie wpisane
w uzasadnienie limitu, a nie w jego warunek, nie broni sie samo.

DWIE RZECZY, KTORE Z TEGO WYNIKAJA I SA ROZNE

Pierwsza: strata na pozycji krotkiej jest NIEOGRANICZONA. Kupno moze stracic
najwyzej to, co kosztowalo; sprzedaz krotka traci tyle, ile urosnie cena, bez
gornego konca. Jedynym, co to ogranicza, jest wielkosc pozycji wzgledem
kapitalu - czyli dokladnie dzwignia.

Druga, wazniejsza dla tego systemu: kill-switch NIE JEST zleceniem stop-loss.
`risk_rules` mowi to wprost przy `min_equity` ("sprawdza sie raz na cykl
decyzyjny, wiec miedzy sprawdzeniami cena moze zejsc nizej"). Przy dzwigni 1x
luka miedzy sprawdzeniami kosztuje tyle, ile rynek zrobi w te minute. Przy 10x
kosztuje dziesiec razy tyle, a limit obsuniecia zostaje ten sam.

CZEGO TEN LIMIT NIE ZASTEPUJE: likwidacji. Gielda zamknie pozycje sama, gdy
depozyt przestanie ja pokrywac, i zrobi to miedzy naszymi cyklami. Limit
dzwigni ustawia odleglosc do tego zdarzenia - nie modeluje go.
"""
from decimal import Decimal

import pytest

from services.engine.risk_rules import (
    DEFAULT_LIMITS, RiskState, evaluate_limits,
)

KAPITAL = Decimal("100000")


def stan(ekspozycja: str = "0") -> RiskState:
    return RiskState(
        equity=KAPITAL, peak=KAPITAL, day_ago=KAPITAL,
        exposure=Decimal(ekspozycja), per_instrument={}, orders_last_hour=0,
    )


# Limity bez zadnego wiazacego progu POZA dzwignia - zeby werdykt nie mogl
# pochodzic od innej reguly. Ekspozycja i wielkosc pozycji celowo astronomiczne.
LIMITY_BEZ_DZWIGNI = {
    "max_daily_loss":        {"value": Decimal("99"), "unit": "pct", "action": "killswitch"},
    "max_drawdown":          {"value": Decimal("99"), "unit": "pct", "action": "killswitch"},
    "max_orders_per_hour":   {"value": Decimal("10000"), "unit": "count", "action": "block"},
    "max_position_notional": {"value": Decimal("10000"), "unit": "pct_equity", "action": "downsize"},
    "max_total_exposure":    {"value": Decimal("10000"), "unit": "pct_equity", "action": "block"},
}
LIMITY_Z_DZWIGNIA = LIMITY_BEZ_DZWIGNI | {
    "max_leverage": {"value": Decimal("2"), "unit": "x", "action": "block"},
}


# ------------------------------------------------------- limit w ogole istnieje
def test_limity_domyslne_MAJA_limit_dzwigni():
    """Symulacja walk-forward i produkcja musza miec te sama liste regul.
    Limit obecny wylacznie w bazie byłby limitem, ktorego backtest nie mierzy."""
    assert "max_leverage" in DEFAULT_LIMITS
    assert DEFAULT_LIMITS["max_leverage"]["action"] == "block"


def test_dzwignia_ponad_limit_blokuje_zlecenie():
    # Ekspozycja 150 000 + zlecenie 60 000 = 210 000 przy kapitale 100 000,
    # czyli 2,1x przy limicie 2x.
    v = evaluate_limits(stan("150000"), LIMITY_Z_DZWIGNIA, 1,
                        Decimal("60000"), Decimal("1"), kind="perp")
    assert v.verdict == "block"
    assert "dźwigni" in (v.reason or "").lower() or "dzwigni" in (v.reason or "").lower()
    assert any(w["limit"] == "max_leverage" for w in v.violations)


def test_dzwignia_w_limicie_przepuszcza():
    v = evaluate_limits(stan("150000"), LIMITY_Z_DZWIGNIA, 1,
                        Decimal("40000"), Decimal("1"), kind="perp")
    assert v.verdict == "allow", v.reason


def test_zuzycie_dzwigni_jest_MIERZONE_a_nie_tylko_egzekwowane():
    """Ten projekt ma juz historie limitu, ktory dzialal i nie zostawial sladu
    (`min_equity` bez ani jednego pomiaru w calej historii). Narastanie dzwigni
    musi byc widoczne PRZED przekroczeniem, nie dopiero w chwili blokady."""
    v = evaluate_limits(stan("50000"), LIMITY_Z_DZWIGNIA, 1,
                        Decimal("10000"), Decimal("1"), kind="perp")
    pomiar = [u for u in v.usage if u["name"] == "max_leverage"]
    assert pomiar, "limit dźwigni musi zostawiać pomiar w `usage`"
    assert pomiar[0]["used"] == pytest.approx(0.6), \
        "zużycie to KROTNOŚĆ kapitału (60 000 / 100 000), nie kwota"
    assert pomiar[0]["threshold"] == pytest.approx(2.0)


# ------------------------------------------------------------- fail-closed
def test_instrument_z_krotka_strona_BEZ_limitu_dzwigni_jest_BLOKOWANY():
    """SEDNO TEGO PLIKU.

    Brak limitu nie moze znaczyc "dzwignia dowolna". Strata na shorcie nie ma
    gornego konca, wiec rachunek bez limitu dzwigni nie ma na nia ZADNEJ
    granicy - ani w regule, ani w arytmetyce instrumentu.

    Ta sama zasada, ktora `position_budget` stosuje do `max_total_exposure`
    ("brak limitu lacznej ekspozycji nie znaczy budzet dowolny") i ktora
    `prog_min_equity` stosuje do nieobliczalnego progu kapitalu: limit,
    ktorego nie ma, musi ZATRZYMAC, a nie zamilknac.
    """
    v = evaluate_limits(stan("0"), LIMITY_BEZ_DZWIGNI, 1,
                        Decimal("1000"), Decimal("1"), kind="perp")
    assert v.verdict == "block"
    assert "dźwigni" in (v.reason or "")
    assert any(w["limit"] == "max_leverage" for w in v.violations)


def test_spot_BEZ_limitu_dzwigni_handluje_dalej():
    """KANAREK: gdyby blokada dzialala bezwarunkowo, powyzszy test przechodzilby
    z powodu, ktory nie ma nic wspolnego z dzwignia - a produkcja stanelaby.

    Na spocie bez pozyczki marzowej dzwigni nie ma z czego wziac: nie da sie
    zaangazowac wiecej, niz jest na rachunku. Limit jest tam zbedny i jego brak
    nie jest luka.
    """
    v = evaluate_limits(stan("0"), LIMITY_BEZ_DZWIGNI, 1,
                        Decimal("1000"), Decimal("1"), kind="spot")
    assert v.verdict == "allow", v.reason


def test_NIEZNANY_rodzaj_instrumentu_tez_jest_bezpieczny():
    """`kind=None` znaczy "wolajacy nie powiedzial". Domysl musi wtedy zachowac
    sie jak spot, bo tym dzis jest cale uniwersum - ale nie wolno, zeby
    przemilczenie rodzaju bylo SPOSOBEM na ominiecie limitu dzwigni. Pilnuje
    tego `RiskKernel`, ktory rodzaj czyta z `ref.instrument` sam."""
    v = evaluate_limits(stan("0"), LIMITY_BEZ_DZWIGNI, 1,
                        Decimal("1000"), Decimal("1"))
    assert v.verdict == "allow"


def test_limit_dzwigni_dziala_takze_na_spocie_gdy_jest_ustawiony():
    """Rodzaj instrumentu decyduje o tym, czy limit jest KONIECZNY - nie o tym,
    czy obowiazuje. Ustawiony limit obowiazuje wszedzie."""
    v = evaluate_limits(stan("150000"), LIMITY_Z_DZWIGNIA, 1,
                        Decimal("60000"), Decimal("1"), kind="spot")
    assert v.verdict == "block"


# ----------------------------------------------------------- KOLEJNOSC REGUL
def test_obciecie_wielkosci_pozycji_NIE_POLYKA_blokady_dzwigni():
    """WADA ZNALEZIONA PRZY PISANIU TEGO PLIKU, W DOKLADNIE TYM KSZTALCIE,
    PRZED KTORYM OSTRZEGA DOCSTRING `risk_rules`.

    Modul mowi wprost: "najpierw to, co zatrzymuje CALY silnik, potem to, co
    dotyczy pojedynczego zlecenia. Inaczej system moglby zmniejszyc zlecenie
    i wyslac je mimo aktywnego kill-switcha".

    `max_position_notional` konczy sie werdyktem `downsize` i ZWRACA. Limit
    dzwigni postawiony za nim byl wiec nieosiagalny dla kazdego zlecenia, ktore
    wczesniej zostalo obciete - a obcinane sa te NAJWIEKSZE, czyli dokladnie te,
    przy ktorych dzwignia ma znaczenie. Blokada wygladalaby na dzialajaca
    (przechodzi wlasne testy na malych zleceniach) i nie zapalilaby sie nigdy
    tam, gdzie jest potrzebna.

    Regula blokujaca musi isc PRZED regula obcinajaca. To nie jest preferencja
    porzadku - `downsize` jest zgoda na handel.
    """
    limity = LIMITY_Z_DZWIGNIA | {
        # Obciecie zadziala: zlecenie 60 000 przy limicie pozycji 50 000.
        "max_position_notional": {"value": Decimal("50"), "unit": "pct_equity",
                                  "action": "downsize"},
    }
    v = evaluate_limits(stan("150000"), limity, 1,
                        Decimal("60000"), Decimal("1"), kind="perp")
    assert v.verdict == "block", (
        f"dźwignia 2,1x przy limicie 2x musi ZABLOKOWAĆ, a dostaliśmy "
        f"'{v.verdict}': {v.reason}")
    assert any(w["limit"] == "max_leverage" for w in v.violations)


def test_brak_limitu_dzwigni_tez_nie_daje_sie_polknac_obcieciu():
    """Ta sama kolejnosc dla drugiej galezi: fail-closed nie moze zniknac
    dlatego, ze zlecenie bylo za duze."""
    limity = LIMITY_BEZ_DZWIGNI | {
        "max_position_notional": {"value": Decimal("1"), "unit": "pct_equity",
                                  "action": "downsize"},
    }
    v = evaluate_limits(stan("0"), limity, 1,
                        Decimal("60000"), Decimal("1"), kind="perp")
    assert v.verdict == "block"
    assert any(w["limit"] == "max_leverage" for w in v.violations)


# -------------------------------------------------- dzwignia a kapital zerowy
def test_kapital_zerowy_nie_przepuszcza_zlecenia_przez_dzielenie():
    """Przy kapitale zerowym krotnosc nie istnieje. Odpowiedzia nie moze byc
    ani przepuszczenie (dzwignia nieskonczona), ani cichy wyjatek."""
    s = RiskState(equity=Decimal("0"), peak=KAPITAL, day_ago=KAPITAL,
                  exposure=Decimal("0"), per_instrument={}, orders_last_hour=0)
    v = evaluate_limits(s, LIMITY_Z_DZWIGNIA, 1, Decimal("1000"), Decimal("1"),
                        kind="perp")
    assert v.verdict == "block"
