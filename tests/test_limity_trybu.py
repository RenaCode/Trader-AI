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
