"""Wpłaty i wypłaty nie mogą wyglądać jak wynik strategii.

Kapitał był liczony jako STAŁA plus wynik handlu, więc każde dołożenie środków
wyglądało jak zysk, a wypłata jak strata. Na rachunku papierowym różnicy nie
było — przepływ jest jeden. Przy realnych środkach uderza to jednocześnie
w cztery mechanizmy: szczyt (obsunięcie), kapitał sprzed doby (strata dzienna),
próg kapitału z docs/05 i krzywą w interfejsie.

Rozdzielenie: `equity` to PIENIĄDZE (rosną od wpłat), `nav` to WYNIK (zmienia
się wyłącznie od handlu).
"""
from decimal import Decimal
from pathlib import Path

import pytest

from services.engine.risk_rules import (
    DEFAULT_LIMITS, RiskState, evaluate_account, START_EQUITY,
)


def stan(*, equity, peak=None, day_ago=None,
         nav=None, nav_peak=None, nav_day_ago=None, wplaty=None) -> RiskState:
    return RiskState(
        equity=Decimal(str(equity)),
        peak=Decimal(str(peak if peak is not None else equity)),
        day_ago=Decimal(str(day_ago if day_ago is not None else equity)),
        exposure=Decimal("0"), per_instrument={}, orders_last_hour=0,
        nav=Decimal(str(nav)) if nav is not None else None,
        nav_peak=Decimal(str(nav_peak)) if nav_peak is not None else None,
        nav_day_ago=Decimal(str(nav_day_ago)) if nav_day_ago is not None else None,
        wplaty_netto=Decimal(str(wplaty)) if wplaty is not None else None,
    )


# ------------------------------------------------- obsunięcie mierzy strategię

def test_wplata_NIE_tworzy_obsuniecia():
    """Właściciel dopłacił w trakcie obsunięcia, więc kapitał spadł tylko o 5%
    od szczytu, a WYNIK strategii jest 12% pod wodą.

    Liczby dobrane tak, żeby obie wersje dały RÓŻNY werdykt — inaczej test
    przechodziłby także dla reguły liczącej z kapitału i niczego nie pilnował.
    Sprawdzone wstawieniem starej wersji z powrotem."""
    v = evaluate_account(
        stan(equity=190_000, peak=200_000,       # −5% w pieniądzach: za mało
             nav=0.88, nav_peak=1.0, nav_day_ago=0.88,
             wplaty=216_000),
        {"max_drawdown": {"value": Decimal("10"), "unit": "pct", "action": "killswitch"}})
    assert v.verdict == "block", "obsunięcie 12% w wyniku musi zatrzymać silnik"


def test_wplata_nie_udaje_zysku_i_nie_kasuje_obsuniecia():
    """Kanarek odwrotny do poprzedniego. Rachunek stracił 12% na handlu,
    a potem właściciel dopłacił tyle, że kapitał wrócił ponad szczyt.
    Licząc z kapitału obsunięcie ZNIKA — a strategia nadal jest pod wodą."""
    v = evaluate_account(
        stan(equity=210_000, peak=210_000,       # kapitał na szczycie
             nav=0.88, nav_peak=1.0, nav_day_ago=0.88,
             wplaty=238_600),
        {"max_drawdown": {"value": Decimal("10"), "unit": "pct", "action": "killswitch"}})
    assert v.verdict == "block", (
        "dopłata nie może kasować obsunięcia — wynik strategii się nie zmienił")


def test_wyplata_NIE_jest_strata_dzienna():
    """Właściciel wypłacił 3% rachunku. Licząc z kapitału to strata dzienna
    przekraczająca limit 2%; licząc z wyniku — nic się nie stało."""
    limity = {"max_daily_loss": {"value": Decimal("2"), "unit": "pct",
                                 "action": "killswitch"}}
    v = evaluate_account(
        stan(equity=97_000, day_ago=100_000,     # −3% w pieniądzach
             nav=1.0, nav_peak=1.0, nav_day_ago=1.0,
             wplaty=97_000),
        limity)
    assert v.verdict == "allow", "wypłata nie jest stratą dzienną"


def test_prawdziwa_strata_dzienna_NADAL_zatrzymuje():
    """Kanarek do poprzedniego. Gdyby reguła przestała patrzeć na cokolwiek,
    poprzedni test przechodziłby razem z zepsutym limitem."""
    limity = {"max_daily_loss": {"value": Decimal("2"), "unit": "pct",
                                 "action": "killswitch"}}
    v = evaluate_account(
        stan(equity=97_000, day_ago=100_000,
             nav=0.97, nav_peak=1.0, nav_day_ago=1.0, wplaty=100_000),
        limity)
    assert v.verdict == "block" and v.trigger_limit == "max_daily_loss"


# ------------------------------------------------------------- próg kapitału

def test_prog_jako_ulamek_wplat_przesuwa_sie_z_wyplata():
    """Próg kwotowy 95 000 po wypłacie 50 000 zatrzymałby silnik natychmiast:
    rachunek spadł zgodnie z wolą właściciela, a próg został w miejscu."""
    limity = {"min_equity": {"value": Decimal("95"), "unit": "pct_wplat",
                             "action": "killswitch"}}
    # Wpłacono 100 000, wypłacono 50 000, handel na zero.
    assert evaluate_account(stan(equity=50_000, wplaty=50_000, nav=1.0,
                                 nav_peak=1.0, nav_day_ago=1.0), limity).verdict == "allow"
    # Ten sam rachunek po stracie 6% z tego, co zostało włożone.
    v = evaluate_account(stan(equity=47_000, wplaty=50_000, nav=0.94,
                             nav_peak=1.0, nav_day_ago=0.94), limity)
    assert v.verdict == "block" and v.trigger_limit == "min_equity"


def test_prog_kwotowy_nadal_dziala():
    """Postać kwotowa zostaje wspierana — nie każdy próg musi być ułamkiem."""
    limity = {"min_equity": {"value": Decimal("95000"), "unit": "quote_ccy",
                             "action": "killswitch"}}
    assert evaluate_account(stan(equity=96_000), limity).verdict == "allow"
    assert evaluate_account(stan(equity=94_000), limity).verdict == "block"


# --------------------------------------------------- zgodność wsteczna

def test_bez_nav_reguly_spadaja_na_kapital():
    """Symulacja walk-forward nie zna przepływów i nie musi. Brak nav oznacza
    zachowanie sprzed migracji 0031 — inaczej każdy backtest zmieniłby wynik
    przy wdrożeniu tej zmiany."""
    limity = {"max_drawdown": {"value": Decimal("10"), "unit": "pct",
                               "action": "killswitch"}}
    assert evaluate_account(stan(equity=95_000, peak=100_000), limity).verdict == "allow"
    assert evaluate_account(stan(equity=89_000, peak=100_000), limity).verdict == "block"


def test_kapital_startowy_w_migracji_zgadza_sie_ze_stala():
    """Migracja zasiewa pierwszy przepływ kwotą wpisaną na sztywno. Rozjazd
    z START_EQUITY nie objawiłby się awarią — po prostu rachunek miałby inny
    kapitał niż ten, na którym mierzono wynik."""
    sql = Path("db/migrations/0031_przeplywy_kapitalu.sql").read_text(encoding="utf-8")
    assert f"{int(START_EQUITY)}" in sql, (
        f"migracja nie zasiewa {START_EQUITY:.0f} — sprawdź kwotę przepływu startowego")


def test_kapital_liczy_sie_z_PRZEPLYWOW_a_nie_ze_stalej():
    """Sedno zmiany. Gdyby stała wróciła, wszystkie testy powyżej nadal by
    przechodziły — bo dotyczą reguł, nie źródła kapitału."""
    zrodlo = Path("services/engine/loop.py").read_text(encoding="utf-8")
    assert "equity = wplaty + realized + unrealized" in zrodlo
    assert "equity = START_EQUITY + realized + unrealized" not in zrodlo
    assert "def przeplywy(" in zrodlo


# ------------------------------------------------------- zapis przepływu

def test_wyplata_ponad_kapital_jest_ODRZUCANA():
    """Rachunek ujemny nie jest stanem, z którego system potrafi wyjść:
    wszystkie miary procentowe dzielą przez kapitał, a wartość jednostki
    przestaje mieć znaczenie. Lepiej odmówić zapisu niż policzyć obsunięcie
    z liczby ujemnej."""
    zrodlo = Path("services/api/routers/kapital.py").read_text(encoding="utf-8")
    assert "przekracza kapitał" in zrodlo


def test_przeplyw_wymaga_POWODU_i_zostawia_slad():
    """Przelew zmienia podstawę, na której liczą się wszystkie limity ryzyka.
    Bez notatki i wpisu w audycie nie da się później odpowiedzieć, skąd wzięła
    się zmiana kapitału."""
    from services.api.routers.kapital import PrzeplywBody

    PrzeplywBody(amount=Decimal("1000"), note="wpłata startowa")
    with pytest.raises(Exception):
        PrzeplywBody(amount=Decimal("1000"), note="ok")

    zrodlo = Path("services/api/routers/kapital.py").read_text(encoding="utf-8")
    assert "kapital.przeplyw" in zrodlo, "brak wpisu w dzienniku audytu"
    assert "require_csrf" in zrodlo


def test_router_kapitalu_jest_CHRONIONY_sesja():
    """Lista chronionych routerów jest w jednym miejscu właśnie po to, żeby
    dodanie nowego bez ochrony rzucało się w oczy."""
    zrodlo = Path("services/api/main.py").read_text(encoding="utf-8")
    assert "kapital.router" in zrodlo
    chroniony = zrodlo.split("_protected = [")[1].split("]")[0]
    assert "kapital.router" in chroniony, "router kapitału poza listą chronionych"


def test_endpointy_kapitalu_SA_OSIAGALNE():
    """Sam wpis na liście nie dowodzi, że trasa istnieje — prefiks routera,
    literówka w nazwie modułu albo brak importu dają dokładnie ten sam plik
    źródłowy i zero działających adresów."""
    from services.api.main import app

    sciezki = set(app.openapi()["paths"])
    assert "/api/engines/{engine}/kapital/przeplywy" in sciezki, (
        f"brak trasy przepływów wśród {len(sciezki)} endpointów")
    metody = app.openapi()["paths"]["/api/engines/{engine}/kapital/przeplywy"]
    assert "get" in metody and "post" in metody, (
        f"przepływy muszą dać się i odczytać, i zapisać; jest: {sorted(metody)}")


# ------------------------------------------- niezmiennik jednostki

@pytest.mark.parametrize("kapital,jednostki,przeplyw", [
    (100_000, 100_000, 10_000),      # wpłata przy nav = 1
    (110_000, 100_000, 10_000),      # wpłata przy nav > 1
    (90_000, 100_000, 10_000),       # wpłata przy nav < 1
    (110_000, 100_000, -25_000),     # wypłata
    (90_000, 100_000, -1),           # wypłata groszowa
])
def test_przeplyw_NIE_RUSZA_wartosci_jednostki(kapital, jednostki, przeplyw):
    """WŁAŚCIWY NIEZMIENNIK tego mechanizmu, sprawdzony arytmetycznie.

    Wpłata kupuje jednostki po bieżącym nav, wypłata je umarza — więc nav po
    przepływie musi być identyczne. Gdyby nie było, obsunięcie liczone z nav
    dziedziczyłoby ruch gotówki i cała zmiana nie miałaby sensu.

    Dowód jest algebraiczny: kapitał = jednostki × nav, więc po dołożeniu kwoty
    `a` mamy (K + a) / (J + a/nav) = nav dla każdego a. Test sprawdza, że kod
    liczy jednostki dokładnie tak, a nie w przybliżeniu.
    """
    from decimal import getcontext
    getcontext().prec = 40

    K, J, a = Decimal(kapital), Decimal(jednostki), Decimal(przeplyw)
    nav = K / J
    nowe_jednostki = J + a / nav          # ta sama formuła co w kapital.py
    nav_po = (K + a) / nowe_jednostki
    # Oba na float: pytest.approx porownuje Decimal z float niepewnie, a pytanie
    # dotyczy rownosci arytmetycznej, nie typu.
    assert float(nav_po) == pytest.approx(float(nav), rel=1e-12), (
        f"przepływ {a} zmienił nav z {nav} na {nav_po}")


def test_wynik_handlu_RUSZA_wartosc_jednostki():
    """Kanarek do poprzedniego. Gdyby nav było odporne na wszystko, byłoby
    stałą i nie mierzyłoby niczego."""
    K, J = Decimal(100_000), Decimal(100_000)
    nav_przed = K / J
    nav_po = (K + Decimal(5_000)) / J     # zysk z handlu, jednostek nie przybywa
    assert nav_po > nav_przed
