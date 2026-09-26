"""`Decimal(0)` nieodróżnialne od braku pomiaru — cztery kopie jednego wzorca.

CO BYŁO NIE TAK

`services/engine/stan_kapitalu.py` w czterech miejscach składało stan ryzyka
tak:

    szczyt     = _dec(...) or equity      # :97
    doba       = _dec(...) or equity      # :99
    nav_szczyt = _dec(...) or nav         # :109
    nav_doba   = _dec(...) or nav         # :114

`Decimal(0)` jest falsy, więc ZMIERZONE zero było podmieniane wartością
bieżącą. To nie jest ostrożne — to jest nieprawda o stanie, który akurat znamy
dokładnie.

KIERUNEK AWARII JEST OTWARTY, NIE ZACHOWAWCZY

Gdyby `doba` wyniosło 0 i zostało podmienione na `equity`, strata dzienna
wyliczyłaby się jako `(equity − equity) / equity = 0,00%` i reguła
`max_daily_loss` NIE ZAPALIŁABY SIĘ w dobie, w której rachunek został
wyzerowany. Tak samo `nav_doba`. Reguła nie milczy z powodu progu — milczy,
bo dostała liczbę, której nikt nie zmierzył, i ta liczba wygląda jak pomiar.

CO SIĘ ZMIENIA PO POPRAWCE, KONKRETNIE

Nie „limit zapala się" — limit przestaje UDAWAĆ, że jest mierzony.
`pomiar_konta` ma warunek `state.wynik_doba > 0`, który mówi o BRAKU DANYCH:
przy odniesieniu równym zero nie ma czego dzielić, więc `max_daily_loss` nie
trafia do `usage`. Limit bez ani jednego pomiaru jest dla czujki
(`dozor.ocen_cisze_pomiaru`) alarmem — a limit zmierzony na 0,00% jest zielonym
wierszem. Przed poprawką dostawaliśmy zielony wiersz; po poprawce brak pomiaru
jest widoczny jako brak pomiaru.

DLACZEGO TO WARTO BYŁO ZROBIĆ, MIMO ŻE OSIĄGALNOŚĆ JEST BLISKA ZERA

`equity = 0` to wyzerowany rachunek. Ale ten sam projekt JUŻ RAZ usunął tę
konstrukcję świadomie i z uzasadnieniem — `RiskState.exposure`: „zero jest
poprawną zmierzoną wartością, więc nie da się go odróżnić od braku pomiaru".
Zostawienie czterech kopii wzorca, który reguła projektu nazwała błędem,
zaprasza do skopiowania go dalej — a następny raz może być w miejscu, w którym
zero jest zwyczajne.
"""
from decimal import Decimal

import pytest

from services.engine.risk_rules import evaluate_account
from services.engine.stan_kapitalu import stan_ryzyka, zloz_stan

LIMITY = {
    "max_daily_loss": {"id": 1, "value": Decimal("2"), "unit": "pct",
                       "action": "killswitch"},
    "max_drawdown": {"id": 2, "value": Decimal("10"), "unit": "pct",
                     "action": "killswitch"},
}


def wiersze(*, equity="90000", szczyt="100000", doba="100000", pierwszy=None,
            nav=None, nav_szczyt=None, nav_doba=None, bez_nav=0,
            wplaty="100000") -> dict:
    """Wiersze `stan_kapitalu.PLAN`. `None` znaczy BRAK WIERSZA, nie zero."""
    def _w(klucz, wartosc):
        return None if wartosc is None else {klucz: wartosc}

    return {
        "equity_biez": _w("equity", equity),
        "equity_szczyt": _w("peak", szczyt),
        "equity_doba": _w("equity", doba),
        "equity_pierwszy": _w("equity", pierwszy),
        "nav_biez": ({"nav": nav, "wplaty_netto": wplaty}
                     if nav is not None else None),
        "nav_agregat": {"szczyt": nav_szczyt, "bez_nav": bez_nav},
        "nav_doba": _w("nav", nav_doba),
        "nav_pierwszy": None,
    }


# ===================================== cztery miejsca, cztery osobne kanarki

def test_zmierzony_szczyt_ZERO_nie_zostaje_podmieniony_kapitalem():
    """`stan_kapitalu.py:97`."""
    st = stan_ryzyka(wiersze(equity="5000", szczyt="0"))
    assert st.peak == Decimal("0"), (
        f"szczyt zmierzony jako 0 wyszedł {st.peak} — `or equity` podmienił "
        f"pomiar wartością bieżącą")


def test_zmierzona_doba_ZERO_nie_zostaje_podmieniona_kapitalem():
    """`stan_kapitalu.py:99` — miejsce, w którym kierunek awarii jest otwarty."""
    st = stan_ryzyka(wiersze(equity="5000", doba="0"))
    assert st.day_ago == Decimal("0"), (
        f"odniesienie sprzed doby zmierzone jako 0 wyszło {st.day_ago}; "
        f"strata dzienna wyliczy się wtedy jako 0,00% i reguła nie zapali się, "
        f"gdy powinna")


def test_zmierzony_szczyt_jednostki_ZERO_nie_zostaje_podmieniony():
    """`stan_kapitalu.py:109`."""
    st = stan_ryzyka(wiersze(nav="0.9", nav_szczyt="0", nav_doba="0.95"))
    assert st.nav_peak == Decimal("0")


def test_zmierzona_doba_jednostki_ZERO_nie_zostaje_podmieniona():
    """`stan_kapitalu.py:114`."""
    st = stan_ryzyka(wiersze(nav="0.9", nav_szczyt="1.0", nav_doba="0"))
    assert st.nav_day_ago == Decimal("0")


# ============================================== kanarki odwrotne: BRAK zostaje

@pytest.mark.parametrize("brak, pole, oczekiwane", [
    ("szczyt", "peak", Decimal("5000")),
    ("doba", "day_ago", Decimal("5000")),
])
def test_KANAREK_prawdziwy_BRAK_wiersza_nadal_spada_na_kapital(brak, pole,
                                                               oczekiwane):
    """Poprawka nie może zamienić fallbacku w `None`.

    Bez tego kanarka testy wyżej przechodziłyby także dla wersji, która
    fallback usuwa — a wtedy `evaluate_account` dostawałby `None` tam, gdzie
    oczekuje `Decimal`, i bezpiecznik wywracałby się na świeżym rachunku.
    Brak punktu sprzed doby znaczy „cała krzywa jest młodsza niż doba",
    a odniesieniem jest wtedy jej POCZĄTEK albo wartość bieżąca — nie pustka.
    """
    st = stan_ryzyka(wiersze(equity="5000", **{brak: None}))
    assert getattr(st, pole) == oczekiwane


def test_KANAREK_brak_szczytu_jednostki_nadal_spada_na_nav():
    st = stan_ryzyka(wiersze(nav="0.9", nav_szczyt=None, nav_doba="0.95"))
    assert st.nav_peak == Decimal("0.9")


def test_KANAREK_brak_doby_jednostki_nadal_spada_na_nav():
    st = stan_ryzyka(wiersze(nav="0.9", nav_szczyt="1.0", nav_doba=None))
    assert st.nav_day_ago == Decimal("0.9")


# ===================================== skutek dla reguły: brak MA BYĆ WIDOCZNY

def test_zerowa_doba_przestaje_udawac_strate_dzienna_0_procent():
    """Po co ta poprawka jest, w jednym pomiarze.

    Przed nią `doba` podmienione na `equity` dawało `max_daily_loss` zmierzone
    na 0,00% — zielony wiersz w `risk.limit_usage`. Po niej odniesienie jest
    zerem, więc limitu NIE DA SIĘ policzyć i nie trafia do pomiaru wcale.
    Limit bez ani jednego pomiaru jest dla czujki alarmem
    (`dozor.ocen_cisze_pomiaru`); limit zmierzony na 0,00% nie jest.
    """
    st = stan_ryzyka(wiersze(equity="5000", doba="0", szczyt="5000"))
    v = evaluate_account(st, LIMITY)
    zmierzone = {u["name"] for u in v.usage}
    assert "max_daily_loss" not in zmierzone, (
        "strata dzienna została zmierzona przy zerowym odniesieniu — "
        "czyli podzielono liczbę przez samą siebie i nazwano to pomiarem")


def test_zloz_stan_oddaje_zmierzone_zero_takze_wznowieniu():
    """Drugi czytelnik krzywej dostaje tę samą liczbę.

    `wznowienie.stan_konta` liczy z `zloz_stan`, czy przyczyna kill-switcha
    ustała. Podmienione `doba` dawało tam stratę dzienną 0,00%, czyli
    „przyczyna ustała" — i zdejmowało hamulec na podstawie wartości, której
    nikt nie zmierzył. To ta sama rodzina błędu, którą opisuje nagłówek
    `stan_kapitalu`: rozjazd jednostronny, zawsze w stronę „zwolnij za wcześnie".
    """
    s = zloz_stan(wiersze(equity="5000", doba="0", szczyt="5000"))
    assert s["doba"] == 0.0
    assert s["wynik"] == 5000.0
