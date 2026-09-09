"""Szczyt liczony z OKROJONEJ historii zaniża obsunięcie — jednostronnie.

CO SIĘ STAŁO

Migracja 0031 przestawiła obsunięcie i stratę dzienną z kapitału na wartość
jednostki (`nav`), bo wpłata podnosiła szczyt i wyglądała jak zysk. Dodała
jednak KOLUMNĘ, a nie DANE: punkty krzywej sprzed migracji zostały z `nav`
równym NULL. Bezpiecznik czytał `max(nav)`, czyli szczyt liczony od dnia
migracji, a nie od początku rachunku.

Dla agregatu punkt bez `nav` nie jest punktem NIŻSZYM — jest punktem
NIEISTNIEJĄCYM. Szczyt sprzed migracji nie zostaje więc uwzględniony jako
gorszy; on po prostu przepada.

DLACZEGO TO JEST GROŹNE, A NIE TYLKO NIEDOKŁADNE

Kierunek błędu jest JEDNOSTRONNY. Szczyt z krótszego okna jest zawsze mniejszy
albo równy prawdziwemu, więc obsunięcie wychodzi zawsze MNIEJSZE — nigdy
większe. Próg 10% przestaje znaczyć 10%, i to zawsze na korzyść dalszego
handlu. Gdyby `nav` zaczęło się zapisywać w dołku (nowy tryb, purge_demo.py,
migracja przerwana w połowie), rachunek 9% pod wodą dostałby do dyspozycji
kolejne 10% — razem 19% przy progu 10%, bez jednego błędu w dzienniku.

WŁASNOŚĆ, KTÓREJ PILNUJĄ TE TESTY

Brak pełnej historii nie może zaniżać obsunięcia. Nie wiedząc, gdzie był
szczyt, mierzymy z `equity`, którego szczyt liczy się z całej krzywej —
zachowawczo, a nie optymistycznie.
"""
from decimal import Decimal

import pytest

from services.engine.risk import RiskKernel
from services.engine.risk_rules import RiskState, evaluate_account

# ------------------------------------------------------- liczby z PRODUKCJI
#
# exec.equity_point, engine=crypto, mode=demo, odczyt 2026-09-09.
# 3190 z 3714 punktów bez `nav`; pierwszy punkt krzywej 2026-08-27 09:07,
# pierwszy punkt z `nav` 2026-09-07 11:49 — czyli 86% krzywej niewidoczne
# dla miary, która decyduje o kill-switchu.
PROD_SZCZYT_EQUITY = Decimal("100105.995041609656")   # 2026-09-07 06:16
PROD_EQUITY = Decimal("100063.092070322300")
PROD_SZCZYT_NAV = Decimal("1.000731879327")           # dopiero od 11:49
PROD_NAV = Decimal("1.000630920703")

# Obsunięcie, które bezpiecznik MIERZYŁ, i to, które BYŁO.
PROD_DD_ZANIZONE = 0.0100885   # (nav_peak − nav) / nav_peak
PROD_DD_PRAWDZIWE = 0.0428575  # (peak − equity) / peak

LIMIT_DD = {"max_drawdown": {"id": 2, "value": Decimal("10"), "unit": "pct",
                             "action": "killswitch"}}
LIMIT_STRATA = {"max_daily_loss": {"id": 1, "value": Decimal("2"), "unit": "pct",
                                   "action": "killswitch"}}


def stan(*, equity, peak=None, day_ago=None, nav=None, nav_peak=None,
         nav_day_ago=None, wplaty=None, nav_od_poczatku=True) -> RiskState:
    d = lambda x: Decimal(str(x)) if x is not None else None   # noqa: E731
    return RiskState(
        equity=d(equity),
        peak=d(peak if peak is not None else equity),
        day_ago=d(day_ago if day_ago is not None else equity),
        exposure=Decimal("0"), per_instrument={}, orders_last_hour=0,
        nav=d(nav), nav_peak=d(nav_peak), nav_day_ago=d(nav_day_ago),
        wplaty_netto=d(wplaty), nav_od_poczatku=nav_od_poczatku)


def uzycie(werdykt, nazwa: str) -> float:
    """Zmierzona wartość limitu — to, co bezpiecznik porównuje z progiem."""
    for u in werdykt.usage:
        if u["name"] == nazwa:
            return u["used"]
    raise AssertionError(f"limit {nazwa} nie został w ogóle zmierzony")


# --------------------------------------------------- odtworzenie produkcji

def test_produkcja_obsuniecie_bylo_zanizone_4x():
    """DOKŁADNIE liczby z produkcji 2026-09-09, nie „coś się liczy".

    Stan konta jest jeden; różni się tylko to, czy historia `nav` sięga
    początku rachunku. Test przechodzi tylko wtedy, gdy oba pomiary są
    ODRÓŻNIALNE i gdy niepełna historia daje ten WIĘKSZY.
    """
    pelna = stan(equity=PROD_EQUITY, peak=PROD_SZCZYT_EQUITY,
                 day_ago=PROD_EQUITY, nav=PROD_NAV, nav_peak=PROD_SZCZYT_NAV,
                 nav_day_ago=PROD_NAV, nav_od_poczatku=True)
    okrojona = stan(equity=PROD_EQUITY, peak=PROD_SZCZYT_EQUITY,
                    day_ago=PROD_EQUITY, nav=PROD_NAV, nav_peak=PROD_SZCZYT_NAV,
                    nav_day_ago=PROD_NAV, nav_od_poczatku=False)

    zanizone = uzycie(evaluate_account(pelna, LIMIT_DD), "max_drawdown")
    poprawione = uzycie(evaluate_account(okrojona, LIMIT_DD), "max_drawdown")

    assert zanizone == pytest.approx(PROD_DD_ZANIZONE, abs=1e-6), (
        "to jest liczba, którą kernel pokazywał na produkcji — jeśli się "
        "zmieniła, zmieniła się definicja obsunięcia, nie tylko dane")
    assert poprawione == pytest.approx(PROD_DD_PRAWDZIWE, abs=1e-6), (
        "obsunięcie od szczytu CAŁEJ krzywej kapitału")
    assert poprawione / zanizone == pytest.approx(4.248, abs=0.01), (
        "zaniżenie było ponad czterokrotne — gdyby ten iloraz spadł do 1, "
        "test przestałby cokolwiek odróżniać")


@pytest.mark.parametrize("szczyt_ogona, zanizone", [
    ("0.91", 0.00),    # historia `nav` zaczyna się DOKŁADNIE w dołku
    ("0.93", 2.15),
    ("0.95", 4.21),
    ("1.00", 9.00),    # historia sięga szczytu — nie ma czego naprawiać
])
def test_niepelna_historia_NIGDY_nie_zaniza_obsuniecia(szczyt_ogona, zanizone):
    """Własność, a nie pojedynczy przypadek: im później zaczyna się `nav`, tym
    bardziej zaniżone było obsunięcie — a po poprawce jest zawsze te same 9%.

    Rachunek zszedł ze 100 000 na 91 000, więc obsunięcie wynosi 9% niezależnie
    od tego, od kiedy zapisywana jest wartość jednostki. Wiersz „1.00" jest
    kanarkiem odwrotnym: gdy okno obejmuje szczyt, obie liczby MUSZĄ być równe,
    inaczej poprawka zmieniałaby pomiar tam, gdzie nie było błędu.
    """
    peak, equity = Decimal("100000"), Decimal("91000")
    nav = equity / peak                                  # nav ∝ equity na demo
    nav_peak = Decimal(szczyt_ogona)
    wspolne = dict(equity=equity, peak=peak, nav=nav, nav_peak=nav_peak,
                   nav_day_ago=nav)

    okno = uzycie(evaluate_account(
        stan(**wspolne, nav_od_poczatku=True), LIMIT_DD), "max_drawdown")
    pelne = uzycie(evaluate_account(
        stan(**wspolne, nav_od_poczatku=False), LIMIT_DD), "max_drawdown")

    assert okno == pytest.approx(zanizone, abs=0.01), (
        "to jest liczba, którą bezpiecznik widział przy szczycie z okrojonej "
        "historii — jeśli się zmieniła, test przestał opisywać ten błąd")
    assert pelne == pytest.approx(9.0, abs=0.01), (
        f"szczyt ogona {szczyt_ogona}: obsunięcie po poprawce ({pelne:.2f}%) "
        f"musi wynosić 9% niezależnie od początku historii `nav`")
    assert pelne >= okno - 1e-9, "poprawka nie może działać w drugą stronę"


# ------------------------------------------------------------------ kanarki

def test_KANAREK_historia_nav_zaczeta_w_dolku_nie_ukrywa_przekroczenia():
    """Scenariusz, dla którego cała ta poprawka istnieje.

    Rachunek zszedł z 100 000 na 89 000 — obsunięcie 11% przy progu 10%.
    `nav` zaczęło się zapisywać dopiero W DOŁKU (nowy tryb / purge_demo /
    migracja przerwana), więc `max(nav)` widzi szczyt 0,895 zamiast 1,0
    i mierzy 0,56%. Silnik handluje dalej, mając do dyspozycji kolejne 10%.
    """
    dolek = dict(equity=89_000, peak=100_000, nav=0.89, nav_peak=0.895,
                 nav_day_ago=0.89)

    stara = evaluate_account(stan(**dolek, nav_od_poczatku=True), LIMIT_DD)
    assert stara.verdict == "allow" and uzycie(stara, "max_drawdown") < 1.0, (
        "kanarek stracił sens: szczyt z dołka MUSI dawać obsunięcie poniżej "
        "progu, inaczej test nie odróżnia poprawki od jej braku")

    nowa = evaluate_account(stan(**dolek, nav_od_poczatku=False), LIMIT_DD)
    assert nowa.verdict == "block", (
        "obsunięcie 11% przy progu 10% musi zatrzymać silnik, nawet gdy "
        "historia jednostki zaczyna się w dołku")
    assert nowa.trigger_killswitch, "to jest limit krytyczny, nie ostrzeżenie"
    assert uzycie(nowa, "max_drawdown") == pytest.approx(11.0, abs=1e-9)


def test_KANAREK_historia_nav_mlodsza_niz_doba_nie_ukrywa_straty_dziennej():
    """Ta sama dziura w limicie straty dziennej, nie tylko w obsunięciu.

    `nav` zapisuje się od dwóch godzin, więc „nav sprzed doby" to nav sprzed
    dwóch godzin. Rachunek stracił 5% w ciągu doby przy progu 2%, a mierzone
    jest 0,4% — bo odniesienie jest młodsze niż okno pomiaru.
    """
    swieza = dict(equity=95_000, day_ago=100_000, nav=0.95, nav_peak=0.954,
                  nav_day_ago=0.954)

    stara = evaluate_account(stan(**swieza, nav_od_poczatku=True), LIMIT_STRATA)
    assert stara.verdict == "allow", (
        "kanarek stracił sens: odniesienie sprzed dwóch godzin MUSI dawać "
        "stratę poniżej progu")

    nowa = evaluate_account(stan(**swieza, nav_od_poczatku=False), LIMIT_STRATA)
    assert nowa.verdict == "block", (
        "strata dzienna 5% przy progu 2% musi zatrzymać silnik")
    assert uzycie(nowa, "max_daily_loss") == pytest.approx(5.0, abs=1e-9)


def test_KANAREK_nav_bez_szczytu_nie_miesza_jednostek():
    """Trzy widoki decydują RAZEM, bo `nav` ≈ 1, a `equity` ≈ 100 000.

    Gdyby każdy spadał na kapitał osobno, stan z `nav` bez `nav_peak` dałby
    obsunięcie (100000 − 1) / 100000 = 99,999% — kill-switch z arytmetyki,
    nie z rynku. Test pilnuje, że pomiar zostaje w jednych jednostkach.
    """
    v = evaluate_account(
        stan(equity=100_000, peak=100_000, nav=1.0, nav_peak=None,
             nav_day_ago=None), LIMIT_DD)
    assert v.verdict == "allow", (
        "rachunek na szczycie nie ma obsunięcia — werdykt 'block' znaczy, że "
        "porównano wartość jednostki ze szczytem w dolarach")
    assert uzycie(v, "max_drawdown") == pytest.approx(0.0, abs=1e-9)


def test_KANAREK_pelna_historia_nadal_mierzy_JEDNOSTKA():
    """Poprawka nie może cofnąć migracji 0031.

    Wpłata w trakcie obsunięcia: kapitał spadł o 5%, a wynik strategii jest
    12% pod wodą. Przy PEŁNEJ historii `nav` musi nadal wygrywać z kapitałem,
    inaczej „naprawa" polegałaby na wyłączeniu jednostki uczestnictwa.
    """
    v = evaluate_account(
        stan(equity=190_000, peak=200_000, nav=0.88, nav_peak=1.0,
             nav_day_ago=0.88, wplaty=216_000, nav_od_poczatku=True),
        LIMIT_DD)
    assert v.verdict == "block", "obsunięcie 12% w wyniku musi zatrzymać silnik"
    assert uzycie(v, "max_drawdown") == pytest.approx(12.0, abs=1e-9)


def test_KANAREK_symulacja_bez_nav_dziala_jak_dotad():
    """Symulacja walk-forward nie zna przepływów i nie podaje `nav` wcale.

    Domyślne `nav_od_poczatku=True` nie może jej niczego zmienić — inaczej
    backtest mierzyłby inny system niż ten, który handluje.
    """
    v = evaluate_account(stan(equity=88_000, peak=100_000), LIMIT_DD)
    assert v.verdict == "block"
    assert uzycie(v, "max_drawdown") == pytest.approx(12.0, abs=1e-9)


# ----------------------------------------------- kernel czyta lukę z bazy
#
# Atrapa krzywej jest WSPÓLNA z tests/test_swiezy_rachunek.py, a nie skopiowana.
# Zna warunki, o które tu chodzi (`nav IS NOT NULL`, `max(nav)`, `24 hours`,
# kierunek sortowania, wiek pomiaru) i jest utrzymywana razem z kernelem. Druga
# kopia rozjechałaby się przy pierwszym nowym zapytaniu — i rozjazd byłby
# niewidoczny, bo atrapa, która nie zna zapytania, oddaje pustkę.
from tests.test_swiezy_rachunek import kernel as kernel_krzywej  # noqa: E402


def kernel(punkty) -> RiskKernel:
    k, _ = kernel_krzywej(punkty, limity=LIMIT_DD)
    return k


def _p(wiek_h, equity, nav):
    """Punkt krzywej, w którym `nav` MOŻE być nieznane (NULL sprzed 0031)."""
    return {"wiek_h": wiek_h, "equity": Decimal(str(equity)),
            "nav": Decimal(str(nav)) if nav is not None else None,
            "wplaty": Decimal("100000")}


def test_KANAREK_kernel_wykrywa_luke_w_krzywej_nav():
    """Ścieżka bazodanowa: dziura w `nav` ma dojść do reguł jako fakt.

    Krzywa z produkcji w miniaturze — szczyt kapitału leży w części BEZ `nav`.
    Gdyby `_wynik()` nie liczyło punktów bez `nav`, kernel oddałby werdykt
    z okrojonego okna i nic by się nie wywróciło.
    """
    z_luka = [_p(100, 100_000, None), _p(80, 111_000, None),   # szczyt bez nav
              _p(30, 100_500, 1.005), _p(0, 99_900, 0.999)]
    k = kernel(z_luka)
    *_, od_poczatku = k._wynik()
    assert od_poczatku is False, "dwa punkty bez `nav` to nie jest pełna historia"

    v = k.check_account()
    assert v.verdict == "block", (
        "obsunięcie ze 111 000 na 99 900 to 10,0% przy progu 10% — szczyt "
        "leży w części krzywej bez `nav`")
    assert uzycie(v, "max_drawdown") == pytest.approx(10.0, abs=0.01)


def test_KANAREK_kernel_bez_luki_ufa_szczytowi_jednostki():
    """Kanarek odwrotny: komplet `nav` MUSI zostać użyty.

    Bez tego poprzedni test przechodziłby także dla wersji, która na `nav`
    nie patrzy nigdy — czyli dla cofnięcia migracji 0031.
    """
    pelna = [_p(100, 216_000, 1.0), _p(30, 200_000, 0.92), _p(0, 190_000, 0.88)]
    k = kernel(pelna)
    *_, od_poczatku = k._wynik()
    assert od_poczatku is True

    v = k.check_account()
    assert uzycie(v, "max_drawdown") == pytest.approx(12.0, abs=1e-6), (
        "przy pełnej historii mierzy się jednostką (1,0 → 0,88 = 12%), "
        "a nie kapitałem (216 000 → 190 000 = 12,04%)")


# -------------------------------------------------------------- migracja 0042

MIGRACJA = "db/migrations/0042_nav_wstecz.sql"


def test_migracja_uzupelnia_nav_TYLKO_gdy_da_sie_go_odtworzyc():
    """Backfill wymaga znajomości `units` w każdym punkcie.

    `units` jest stałe wyłącznie wtedy, gdy żaden przepływ nie wydarzył się
    w trakcie krzywej. Bez tego warunku migracja wpisywałaby `equity/units`
    z DZISIEJSZĄ liczbą jednostek w punkty sprzed wpłaty — czyli zgadywałaby,
    zostawiając krzywą wyglądającą na kompletną i mierzącą co innego.
    """
    from pathlib import Path
    sql = Path(MIGRACJA).read_text(encoding="utf-8")
    assert "ostatni_przeplyw <= k.pierwszy_punkt" in sql, (
        "brak warunku odtwarzalności — migracja zgadywałaby `units` dla "
        "rachunku z przepływem w trakcie krzywej")
    assert "ep.nav IS NULL" in sql, (
        "backfill musi dotykać wyłącznie punktów bez `nav`; nadpisanie "
        "istniejących wartości zamieniłoby migrację w przeliczenie historii")
    assert "DISABLE TRIGGER" not in sql, (
        "exec.equity_point nie ma wyzwalacza niezmienności (ma go journal."
        "decision z 0005) — wyłączanie czegoś, czego nie ma, ukrywałoby "
        "moment, w którym ktoś taki wyzwalacz doda")
