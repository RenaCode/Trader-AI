"""Brak limitów to NIE JEST brak ograniczeń — to brak oceny ryzyka.

Zapytanie ładujące limity filtruje po `mode`, a zasiane były wyłącznie dla
trybu `demo`. Przełączenie na `live` zwracało pusty słownik, a `evaluate_account`
przechodziło przez wszystkie `if lim:` i oddawało werdykt `allow`.

Znaczyło to, że pierwszy dzień z prawdziwymi pieniędzmi jest dokładnie tym
dniem, w którym cała architektura bezpiecznika jest wyłączona — bez żadnego
błędu, z bijącym heartbeatem i zielonym statusem. Dozorca buduje ten sam obiekt,
więc niezależny nadzorca milczałby tak samo.
"""
import pytest

from services.engine.risk import LIMITY_KONIECZNE, sprawdz_komplet


def test_pusty_zbior_limitow_jest_BRAKIEM_a_nie_wolna_reka():
    assert sorted(sprawdz_komplet({})) == sorted(LIMITY_KONIECZNE)


def test_sprawdzamy_KONKRETNE_nazwy_a_nie_czy_cokolwiek_jest():
    """Zasianie dla nowego trybu jednego limitu na liczbę zleceń wyglądałoby
    jak komplet, a zostawiałoby rachunek bez podłogi kapitału."""
    niepelne = {"max_orders_per_hour": {}, "max_total_exposure": {}}
    assert sprawdz_komplet(niepelne) == list(LIMITY_KONIECZNE)


def test_konieczne_sa_dokladnie_te_ktore_koncza_sie_killswitchem():
    """Limity `block` i `downsize` ograniczają pojedynczą decyzję; ich brak jest
    kosztowny. Te trzy stanowią PODŁOGĘ KAPITAŁU — bez niej nie ma czego bronić."""
    assert set(LIMITY_KONIECZNE) == {"max_daily_loss", "max_drawdown", "min_equity"}


def test_komplet_przechodzi():
    assert sprawdz_komplet({n: {} for n in LIMITY_KONIECZNE}) == []


def test_kernel_ODMAWIA_startu_bez_limitow():
    """To ma być wyjątek, a nie ostrzeżenie: proces, który wystartował bez
    limitów, wygląda identycznie jak proces, który je ma."""
    from services.engine.risk import BrakLimitow, RiskKernel

    class PustyKursor:
        def execute(self, *a, **k):
            return self

        def fetchall(self):
            return []

    class Polaczenie:
        def cursor(self, *a, **k):
            return PustyKursor()

    k = RiskKernel.__new__(RiskKernel)
    k.conn, k.engine, k.mode = Polaczenie(), "crypto", "live"
    with pytest.raises(BrakLimitow) as exc:
        k._load_limits()
    assert "BEZ OCENY" in str(exc.value)


def test_zywy_rachunek_NIE_zgaduje_kapitalu():
    """Kolejność zdarzeń jest naturalna: silnik startuje, właściciel przelewa
    10 000, nikt jeszcze nie zarejestrował wpłaty — i system przyjmuje 100 000.

    Limity są procentowe, więc pozycja „5% kapitału" staje się połową rachunku,
    a próg `min_equity` 95 000 nie zadziała nigdy, bo equity „wynosi" 100 000.
    """
    from pathlib import Path

    zrodlo = Path("services/engine/loop.py").read_text(encoding="utf-8")
    kod = "\n".join(w for w in zrodlo.splitlines()
                    if not w.lstrip().startswith("#"))
    fragment = kod.split("wplaty, units = przeplywy(")[1].split("equity =")[0]
    assert 'mode != "demo"' in fragment
    assert "BrakKapitalu" in fragment


def test_zasiew_kapitalu_dotyczy_TYLKO_demo():
    """Na żywym rachunku wpisanie 100 000 jako punktu wyjścia jest fikcją,
    od której liczone są potem wszystkie limity procentowe i całe obsunięcie."""
    from services.engine.loop import seed_equity

    class Kursor:
        def __init__(self):
            self.sql = []

        def execute(self, sql, params=None):
            self.sql.append(sql)

        def fetchone(self):
            return None

    c = Kursor()
    seed_equity(c, "crypto", "live")
    assert c.sql == []

    c = Kursor()
    seed_equity(c, "crypto", "demo")
    assert any("INSERT INTO exec.equity_point" in s for s in c.sql)


def test_wynik_wyjscia_czesciowego_WCHODZI_do_kapitalu():
    """Wyjście częściowe księguje `realized_pnl` na wierszu, który POZOSTAJE
    otwarty: zmniejsza się `qty`, a `closed_at` jest nadal puste.

    Warunek `closed_at IS NOT NULL` pomijał ten wynik całkowicie, a `unrealized`
    liczy się już z pomniejszonej ilości — strata na sprzedanej części znikała
    z krzywej kapitału. Kierunek błędu jest najgorszy z możliwych: obsunięcie
    było zaniżone, więc kill-switch nie strzelał w sytuacji, dla której istnieje.
    """
    from pathlib import Path

    zrodlo = Path("services/engine/loop.py").read_text(encoding="utf-8")
    kod = "\n".join(w for w in zrodlo.splitlines()
                    if not w.lstrip().startswith("#"))
    zapytanie = kod.split("AS realized FROM exec.position")[1].split(")")[0]
    assert "closed_at" not in zapytanie, (
        "wynik zrealizowany liczony tylko z pozycji zamkniętych pomija "
        "wyjścia częściowe i zaniża obsunięcie")


def test_silnik_rozstrzyga_KAZDA_swiece_a_nie_co_druga():
    """`decided_at` to czas zegarowy, a świeca, której decyzja dotyczy, zamyka
    się o `bar_start + długość`. Decyzje dla świecy B zapadają więc w oknie
    [B + długość, B + 2·długość).

    Porównywanie z samym `bar_start` sprawiało, że decyzje dla świecy
    POPRZEDNIEJ liczyły się jako decyzje dla bieżącej — silnik rozstrzygał
    CO DRUGĄ świecę i handlował z połową częstotliwości, na której kalibrowano
    walk-forward.

    Zmierzone na produkcji: decyzje o 00:00, 08:00 i 16:00; świece 04:00, 12:00
    i 20:00 pomijane. W logu wyglądało to na zdrowe odsiewanie duplikatów.
    """
    from datetime import datetime, timedelta, timezone

    from services.engine.horizons import get
    from services.engine.loop import current_bar, decided_for_bar, okno_decyzji

    h = get("4h")
    teraz = datetime(2026, 9, 7, 20, 12, tzinfo=timezone.utc)
    bar = current_bar(teraz, h)
    assert bar == datetime(2026, 9, 7, 16, 0, tzinfo=timezone.utc)

    # Decyzje dla swiecy 16:00 zapadaja dopiero PO jej zamknieciu, o 20:00.
    assert okno_decyzji(bar, h) == datetime(2026, 9, 7, 20, 0, tzinfo=timezone.utc)

    class Kursor:
        def __init__(self, ostatnia):
            self.ostatnia, self.prog = ostatnia, None

        def execute(self, sql, params):
            self.prog = params[-1]

        def fetchone(self):
            return (1,) if self.ostatnia >= self.prog else None

    # Decyzja dla swiecy POPRZEDNIEJ (12:00), podjeta o 16:05, NIE moze
    # blokowac rozstrzygniecia swiecy 16:00.
    poprzednia = Kursor(bar + timedelta(minutes=5))
    assert decided_for_bar(poprzednia, "crypto", "demo", 1, bar, h) is False

    # Decyzja dla TEJ swiecy, podjeta o 20:02, blokuje kolejna w tym samym oknie.
    biezaca = Kursor(okno_decyzji(bar, h) + timedelta(minutes=2))
    assert decided_for_bar(biezaca, "crypto", "demo", 1, bar, h) is True
