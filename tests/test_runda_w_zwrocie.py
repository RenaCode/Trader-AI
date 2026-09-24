"""Cała runda siedzi w ZWROCIE Z TRANSAKCJI — w każdym modelu egzekucji.

DLACZEGO OSOBNY PLIK, SKORO JEDEN TAKI TEST JUŻ JEST

`test_portfolio.test_zwrot_z_transakcji_jest_po_prowizji_a_nie_przed` sprawdza
JEDEN wariant: `kind="spot"`, zlecenie rynkowe. Produkcja i trener chodzą na
`spot_eu` z `--limit` (charts/trader/values.yaml), czyli dokładnie na wariancie,
którego tamten test nie dotyka. Tamten ma też dwie słabości, które w tym miejscu
kosztują:

  * próg `ret <= -1.5 * taker` przepuszcza zwrot, w którym brakuje POŁOWY
    jednej nogi i całego poślizgu — a różnica między 0,200% a 0,580% rundy
    decyduje o znaku mediany Sharpe (docs/03);
  * `pytest.skip` przy braku transakcji: dzień, w którym bramka przestanie
    generować wejścia na płaskiej cenie, zamienia ten kanarek w wydruk „skipped".

Tu wartość oczekiwana jest policzona DOKŁADNIE, we wszystkich czterech
kombinacjach, a brak transakcji jest błędem testu, nie jego pominięciem.

WZÓR. Przy cenie stałej P wejście idzie po P(1+s·k), wyjście po P(1−s·k),
gdzie k to strona pozycji, a prowizja bierze się od obu notionali:

    ret = k·(wyjście/wejście − 1) − fee·(1 + wyjście/wejście)
        = −2s/(1+s) − fee·(2 − 2s/(1+s))

czyli w granicy małych s: −2·(fee + poślizg). Człon drugiego rzędu jest tu
policzony jawnie, żeby test nie potrzebował tolerancji szerszej niż sam mierzony
efekt.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services.engine.horizons import get as get_horizon
from services.engine.koszty import POSLIZG_MAKER, POSLIZG_TAKER, koszty, runda
from services.engine.portfolio import Egzekucja, simulate_portfolio
from services.engine.signals import DEFAULT_WEIGHTS

H = get_horizon("4h")
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
CENA = 100.0

LIMITY_LUZNE = {
    "max_daily_loss":        {"value": Decimal("99"),     "action": "killswitch"},
    "max_drawdown":          {"value": Decimal("99"),     "action": "killswitch"},
    "max_orders_per_hour":   {"value": Decimal("1000"),   "action": "block"},
    "max_position_notional": {"value": Decimal("5000"),   "action": "downsize"},
    "max_total_exposure":    {"value": Decimal("999999"), "action": "block"},
    # Wariant `perp` w parametryzacji nizej nie wejdzie w zadna pozycje bez
    # limitu dzwigni: od 2026-09-24 `evaluate_limits` odmawia handlu
    # instrumentem pozwalajacym na pozycje krotka, gdy tego limitu nie ma
    # (`risk_rules`, punkt 6 — strata na shorcie nie ma gornego konca).
    # Wartosc astronomiczna jak reszta zestawu: tu nie mierzymy dzwigni, tylko
    # runde kosztowa, i zaden limit nie ma prawa wiazac.
    "max_leverage":          {"value": Decimal("9999"),   "action": "block"},
}


def _cechy(kup: bool) -> dict:
    wspolne = {"atr_pct": 1.0, "rsi": 50.0, "range_pos": 0.5, "volume_z": 0.0,
               "news_sent": 0.0, "funding_rate": 0.0, "funding_z": 0.0}
    if kup:
        return wspolne | {"ema_ratio": 1.01, "ret_short": 20.0,
                          "ret_mid": 40.0, "ret_long": 80.0}
    return wspolne | {"ema_ratio": 1.0, "ret_short": 0.0,
                      "ret_mid": 0.0, "ret_long": 0.0}


def panel_plaski(n: int = 30, okres: int = 5) -> list[dict]:
    """Cena STAŁA, sygnał migający — wejście i wyjście bez żadnego ruchu rynku.

    Zakres świecy też jest zerowy (high = low = close), więc zlecenie z limitem
    po cenie zamknięcia wypełnia się zawsze: wariant limitowy mierzy wtedy sam
    koszt, a nie model wypełnień.
    """
    return [{"t": T0 + timedelta(hours=4 * i),
             "f": _cechy((i // okres) % 2 == 0),
             "price": CENA, "high": CENA, "low": CENA}
            for i in range(n)]


def oczekiwany_zwrot(fee: float, slip: float) -> float:
    """Dokładny zwrot rundy przy zerowym ruchu ceny — bez przybliżeń."""
    iloraz = (1 - slip) / (1 + slip)          # wyjście/wejście dla pozycji długiej
    return (iloraz - 1) - fee * (1 + iloraz)


@pytest.mark.parametrize("kind", ["spot", "spot_eu", "perp"])
@pytest.mark.parametrize("limit", [False, True])
def test_transakcja_bez_ruchu_ceny_kosztuje_dokladnie_runde(kind, limit):
    k = koszty(kind)
    fee = k.maker if limit else k.taker
    slip = POSLIZG_MAKER if limit else POSLIZG_TAKER

    res = simulate_portfolio(
        {"AAA": panel_plaski()}, DEFAULT_WEIGHTS, {"AAA": 2.0},
        {"AAA": (0.05, 0.0225)}, H, 0.05, LIMITY_LUZNE,
        kind=kind, egzekucja=Egzekucja(limit=limit))

    assert res.trades, (
        "płaska cena nie dała ani jednej transakcji — kanarek przestał mierzyć "
        "cokolwiek zamiast zgłosić, że nie ma czego mierzyć")
    assert res.prowizja == pytest.approx(fee), "symulacja zastosowała inną stawkę, niż deklaruje"
    assert res.poslizg == pytest.approx(slip)

    oczekiwane = oczekiwany_zwrot(fee, slip)
    for t in res.trades:
        assert t["ret"] == pytest.approx(oczekiwane, abs=1e-9), (
            f"{kind}/{'limit' if limit else 'rynek'}: zwrot {t['ret']:.8f} zamiast "
            f"{oczekiwane:.8f} — w zwrocie z transakcji brakuje części rundy")


@pytest.mark.parametrize("kind", ["spot", "spot_eu", "perp"])
@pytest.mark.parametrize("limit", [False, True])
def test_zwrot_zgadza_sie_z_deklarowana_runda(kind, limit):
    """`runda()` jest liczbą wypisywaną przy starcie pętli (`loop.py --kind`).

    Gdyby rozjechała się ze zwrotem, człowiek czytałby jeden koszt, a miara
    liczyłaby drugi — i żadna strona nie dałaby znać.
    """
    deklarowana = runda(kind, limit)
    faktyczna = -oczekiwany_zwrot(koszty(kind).maker if limit else koszty(kind).taker,
                                  POSLIZG_MAKER if limit else POSLIZG_TAKER)
    # Różnica to wyłącznie człon drugiego rzędu 2s(s+fee)/(1+s) — poniżej 1% rundy.
    assert faktyczna == pytest.approx(deklarowana, rel=0.01)
    assert faktyczna <= deklarowana, "zwrot nie może być TAŃSZY niż deklarowana runda"


def test_wariant_produkcyjny_jest_drozszy_niz_zaden_koszt():
    """Konfiguracja z wdrożenia (spot_eu + limit) musi kosztować dokładnie 0,200%."""
    assert runda("spot_eu", True) == pytest.approx(0.0020)
    # A ten sam podmiot zleceniem rynkowym — 0,580%, czyli prawie trzykrotnie.
    assert runda("spot_eu", False) == pytest.approx(0.0058)
