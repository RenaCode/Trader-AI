"""Kill-switch: cały łańcuch, ogniwo po ogniwie.

DLACZEGO OSOBNY PLIK

Kill-switch to jedyny mechanizm w tym systemie, którego awaria nie objawia się
niczym. Silnik po jego zadziałaniu wygląda dokładnie tak samo jak silnik, który
nie ma sygnału do wejścia: zero decyzji `enter`, żadnego błędu w logu, heartbeat
bije dalej.

Łańcuch ma pięć ogniw i każde jest tu sprawdzane osobno:

    1. limit przekroczony  -> werdykt żąda kill-switcha
    2. żądanie             -> wiersz w risk.killswitch_event
    3. aktywny             -> wejścia wstrzymane
    4. aktywny             -> WYJŚCIA NADAL DZIAŁAJĄ
    5. człowiek            -> zwolnienie, z powodem i śladem

Ogniwo 5 nie istniało do 2026-09-07. Kolumny `released_at`, `released_by`
i `release_note` były w schemacie od migracji 0007, ale router ryzyka miał
wyłącznie GET i nikt nie pisał do tej tabeli. Kill-switch był drzwiami
jednokierunkowymi — a powiadomienie o jego zadziałaniu odsyłało do zakładki
w interfejsie, która pokazywała komunikat „odblokowanie wymaga ręcznego
potwierdzenia" i nie dawała czym potwierdzić.
"""
from decimal import Decimal
from pathlib import Path

import pytest

from services.engine.risk_rules import DEFAULT_LIMITS, RiskState, evaluate_account


def konto(equity, peak, day_ago=None, killswitch=None) -> RiskState:
    return RiskState(equity=Decimal(str(equity)), peak=Decimal(str(peak)),
                     day_ago=Decimal(str(day_ago if day_ago is not None else equity)),
                     exposure=Decimal("0"), per_instrument={},
                     orders_last_hour=0, killswitch=killswitch)


# ------------------------------------------------- ogniwo 1: limit -> żądanie

@pytest.mark.parametrize("nazwa,stan", [
    ("obsunięcie", konto(85_000, 100_000)),
    ("strata dzienna", konto(97_000, 100_000, day_ago=100_000)),
    ("próg kapitału", konto(94_000, 96_000)),
])
def test_kazdy_limit_krytyczny_zada_killswitcha(nazwa, stan):
    """Trzy limity mają akcję `killswitch`. Limit, który blokuje, ale nie żąda
    zatrzymania, zostawia system w stanie 'nie wchodzi i nie wiadomo dlaczego'."""
    v = evaluate_account(stan, DEFAULT_LIMITS)
    assert v.verdict == "block", f"{nazwa}: miało zablokować"
    assert v.trigger_killswitch, f"{nazwa}: zablokowało, ale nie zażądało kill-switcha"


def test_zdrowe_konto_NIE_zada_killswitcha():
    """Kanarek do poprzedniego. Gdyby żądanie szło zawsze, testy wyżej
    przechodziłyby, a system zatrzymywałby się na pierwszym cyklu."""
    v = evaluate_account(konto(100_000, 100_000), DEFAULT_LIMITS)
    assert v.verdict == "allow" and not v.trigger_killswitch


# --------------------------------------- ogniwo 2 i 3: aktywny -> blokada wejść

def test_aktywny_killswitch_blokuje_zanim_cokolwiek_zostanie_policzone():
    """Kill-switch ma pierwszeństwo przed każdym innym limitem. Inaczej system
    mógłby zmniejszyć zlecenie i wysłać je mimo zatrzymania."""
    v = evaluate_account(konto(100_000, 100_000, killswitch={"reason": "test"}),
                         DEFAULT_LIMITS)
    assert v.verdict == "block"
    assert "Kill-switch" in (v.reason or "")


def test_aktywny_killswitch_NIE_zada_kolejnego():
    """Bez tego każdy cykl dokładałby wiersz do risk.killswitch_event, a dozór
    zgłaszałby to samo zdarzenie w kółko — alert, który przychodzi zawsze,
    przestaje cokolwiek znaczyć."""
    v = evaluate_account(konto(50_000, 100_000, killswitch={"reason": "test"}),
                         DEFAULT_LIMITS)
    assert not v.trigger_killswitch, "aktywny kill-switch nie może żądać kolejnego"


# ---------------------------------------------- ogniwo 4: wyjścia nadal działają

def test_petla_wstrzymuje_WYLACZNIE_wejscia():
    """Najgroźniejsza możliwa pomyłka w tym miejscu.

    Kill-switch zadziałał, bo kapitał spada. Jeśli zablokuje też wyjścia,
    system zostaje z otwartymi pozycjami w rynku, który właśnie ucieka
    przeciwko niemu — czyli robi dokładnie odwrotność tego, po co istnieje.
    """
    zrodlo = Path("services/engine/loop.py").read_text(encoding="utf-8")
    assert 'if action.startswith("enter") and wejscia_wstrzymane:' in zrodlo, (
        "warunek wstrzymania musi dotyczyć wyłącznie wejść; jeśli zmienił "
        "kształt, sprawdź, czy nadal przepuszcza wyjścia")
    assert "wejscia_wstrzymane" in zrodlo


# ------------------------------------------------- ogniwo 5: zwolnienie ręczne

def test_zwolnienie_wymaga_POWODU_a_nie_tylko_kliknięcia():
    """Kill-switch nie wygasa sam, bo automatyczne wznowienie po obsunięciu to
    najprostsza droga do powtórzenia tej samej straty. Zwolnienie jednym
    kliknięciem jest tym samym wznowieniem — tyle że z krokiem, który daje
    złudzenie namysłu."""
    from services.api.routers.risk import ZwolnienieBody

    ZwolnienieBody(note="dane świec poprawione w sha-abc1234")
    for zly in ("", "ok", "bo tak"):
        with pytest.raises(Exception):
            ZwolnienieBody(note=zly)


def test_endpoint_zwolnienia_ISTNIEJE_i_pisze_do_wlasciwych_kolumn():
    """Ogniwo, którego brakowało. Kolumny były od 0007, ścieżki zapisu nie."""
    zrodlo = Path("services/api/routers/risk.py").read_text(encoding="utf-8")
    assert "@router.post(" in zrodlo, "router ryzyka miał wyłącznie GET"
    assert "UPDATE risk.killswitch_event" in zrodlo
    for kolumna in ("released_at", "released_by", "release_note"):
        assert kolumna in zrodlo, f"zwolnienie nie zapisuje {kolumna}"


def test_zwolnienie_jest_chronione_i_zostawia_slad():
    """Zwolnienie kill-switcha pozwala systemowi znowu ryzykować pieniądze.
    Bez CSRF, bez sesji i bez wpisu w audycie byłoby to jedyne takie miejsce
    w aplikacji."""
    zrodlo = Path("services/api/routers/risk.py").read_text(encoding="utf-8")
    assert "require_csrf" in zrodlo
    assert "current_user" in zrodlo
    assert "risk.killswitch_released" in zrodlo, "brak wpisu w dzienniku audytu"


def test_powtorzone_zwolnienie_jest_bledem_a_nie_cisza():
    """409, nie 200. Powtórzone zwolnienie znaczy, że ktoś widzi inny stan niż
    baza; ciche potwierdzenie zostawiłoby go w tym przekonaniu."""
    zrodlo = Path("services/api/routers/risk.py").read_text(encoding="utf-8")
    # Szukamy WYWOLANIA, nie liczby: "409" wystepuje tez w komentarzu obok,
    # wiec sam ciag przechodzil takze po usunieciu warunku. Sprawdzone
    # wstawieniem bledu - test milczal.
    assert "HTTPException(409" in zrodlo, "powtórzone zwolnienie nie zwraca 409"


def test_interfejs_ma_czym_zwolnic():
    """Powiadomienie mówi 'Zwolnienie: zakładka Ryzyko w interfejsie'.
    Zakładka pokazywała komunikat o ręcznym potwierdzeniu i nie miała
    kontrolki — instrukcja odsyłała do rzeczy, której nie było."""
    zrodlo = Path("web/src/components/Risk.jsx").read_text(encoding="utf-8")
    assert "/risk/killswitch/" in zrodlo and "release" in zrodlo, (
        "zakładka Ryzyko nie wywołuje zwolnienia")
    assert "Powód zwolnienia" in zrodlo, "brak pola na powód"


def test_nowy_limit_ma_nazwe_w_interfejsie():
    """Limit bez tłumaczenia wyświetla się surową nazwą kolumny i wygląda jak
    usterka — a to jedyne miejsce, w którym widać, gdzie stoi podłoga."""
    zrodlo = Path("web/src/components/Risk.jsx").read_text(encoding="utf-8")
    assert "min_equity:" in zrodlo, "próg kapitału bez nazwy w interfejsie"
