"""Podłoga kapitału zapisana procentowo NIE MOŻE po cichu stać się kwotą.

`prog_min_equity` schodził przy `wplaty_netto is None` na ostatnią linijkę
i zamieniał limit `{"value": 95, "unit": "pct_wplat"}` w próg 95 JEDNOSTEK
WALUTY zamiast 95% wpłat. Na rachunku 10 000 warunek `state.equity <= prog` nie
zostałby spełniony NIGDY — kill-switch chroniący pieniądze właściciela byłby
wyłączony, przy zielonym statusie i pełnym wierszu w `risk.limit_usage`
z threshold 95.

`wplaty_netto` jest `None` wszędzie tam, gdzie najświeższy punkt z niepustym
`nav` ma pustą kolumnę `wplaty_netto` — czyli w stanach po migracji lub
czyszczeniu, dla których 0042 świadomie zostawia NULL-e. Ta sama ścieżka
obowiązuje dozorcę przez `stan_kapitalu.stan_ryzyka`, więc niezależny nadzorca
milczałby tak samo.

Dziś jest to latentne (produkcja ma `min_equity` jako `quote_ccy` 95000,
migracja 0028), ale `pct_wplat` jest tym, co docstring tej funkcji zaleca dla
rachunku żywego. Pułapka czeka dokładnie na przejście na live.
"""
from decimal import Decimal as D

import pytest

from services.engine.risk_rules import (LimitNieobliczalny, RiskState,
                                        evaluate_account, pomiar_konta,
                                        prog_min_equity)

PROCENTOWY = {"id": 11, "name": "min_equity", "value": D("95"),
              "unit": "pct_wplat", "action": "killswitch"}
KWOTOWY = {"id": 11, "name": "min_equity", "value": D("9500"),
           "unit": "quote_ccy", "action": "killswitch"}


def _stan(wplaty):
    return RiskState(equity=D("10000"), peak=D("10000"), day_ago=D("10000"),
                     exposure=D("0"), per_instrument={}, orders_last_hour=0,
                     nav=D("1"), nav_peak=D("1"), nav_day_ago=D("1"),
                     wplaty_netto=wplaty)


def test_procent_bez_wplat_JEST_BLEDEM_a_nie_kwota():
    """95 jako kwota to próg, którego rachunek 10 000 nie dotknie nigdy."""
    with pytest.raises(LimitNieobliczalny):
        prog_min_equity(PROCENTOWY, _stan(None))


def test_procent_z_wplatami_liczy_sie_normalnie():
    assert prog_min_equity(PROCENTOWY, _stan(D("10000"))) == D("9500")


def test_limit_kwotowy_nie_potrzebuje_wplat():
    """Kanarek: poprawka ma dotyczyć wyłącznie jednostki procentowej."""
    assert prog_min_equity(KWOTOWY, _stan(None)) == D("9500")


def test_nieobliczalny_prog_WSTRZYMUJE_HANDEL():
    """Cichy powrót do jednostki kwotowej dawał `allow` przy wyłączonej
    podłodze. Jedyna bezpieczna odpowiedź to nie wchodzić."""
    w = evaluate_account(_stan(None), {"min_equity": PROCENTOWY})
    assert w.verdict == "block"
    assert "nieobliczalny" in w.reason
    assert [v["limit"] for v in w.violations] == ["min_equity"]


def test_nieobliczalny_prog_NIE_ZAPISUJE_pomiaru_ktorego_nie_bylo():
    """Wiersz w `risk.limit_usage` z threshold 95 przy progu, którego nie da
    się policzyć, jest GORSZY niż brak wiersza: wygląda na działający
    bezpiecznik. Pozostałe limity mierzymy dalej — „nadzór staje się
    niewidoczny w chwili, w której jest najbardziej potrzebny" to wada, którą
    `pomiar_konta` już raz naprawiało."""
    limity = {"min_equity": PROCENTOWY,
              "max_drawdown": {"id": 2, "name": "max_drawdown", "value": D("10"),
                               "unit": "pct", "action": "killswitch"}}
    usage, surowe = pomiar_konta(_stan(None), limity)

    assert "min_equity" not in [u["name"] for u in usage]
    assert "min_equity" not in surowe
    assert "min_equity_blad" in surowe
    assert "max_drawdown" in [u["name"] for u in usage], (
        "brak jednego limitu skasował pomiar pozostałych")


def test_naruszenie_progu_procentowego_nadal_dziala():
    """Kanarek w drugą stronę: poprawka nie może zepsuć limitu, który działa."""
    stan = RiskState(equity=D("9000"), peak=D("10000"), day_ago=D("10000"),
                     exposure=D("0"), per_instrument={}, orders_last_hour=0,
                     nav=D("0.9"), nav_peak=D("1"), nav_day_ago=D("1"),
                     wplaty_netto=D("10000"))
    w = evaluate_account(stan, {"min_equity": PROCENTOWY})
    assert w.verdict == "block"
    assert w.trigger_limit == "min_equity"
