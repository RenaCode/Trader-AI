"""Cała runda siedzi w ZWROCIE Z TRANSAKCJI — w każdym modelu egzekucji.

DLACZEGO OSOBNY PLIK, SKORO JEDEN TAKI TEST JUŻ JEST

`test_portfolio.test_zwrot_z_transakcji_jest_po_prowizji_a_nie_przed` sprawdza
JEDEN wariant: `kind="spot"`, zlecenie rynkowe. Produkcja i trener chodzą na
`spot_eu` z `--limit` (charts/trader/values.yaml), czyli dokładnie na wariancie,
którego tamten test nie dotyka. Tamten ma też dwie słabości, które w tym miejscu
kosztują:

  * próg `ret <= -1.5 * taker` przepuszcza zwrot, w którym brakuje POŁOWY
    jednej nogi i całego poślizgu — a różnica między 0,200% a 0,580% rundy
    decyduje o znaku mediany Sharpe (renacode-infra: docs/projekty/trader/03-wynik-walk-forward.md);
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
from services.engine.koszty import (
    POSLIZG_MAKER, POSLIZG_TAKER, karta_rundy, koszty, runda,
)
from services.engine.portfolio import Egzekucja, simulate_portfolio
from services.engine.signals import DEFAULT_WEIGHTS

H = get_horizon("4h")

# RODZAJE INSTRUMENTU W PARAMETRYZACJI.
#
# `stock` dołączył 2026-09-25, razem z uruchomieniem toru equity. ZMIERZONE
# MUTACYJNIE tego dnia na zestawie SPRZED tej zmiany: wada wstawiona w prowizję
# wywracała 8 testów dla krypto i JEDEN dla equity, a wyłączenie
# `koszty("stock").krotka_pozycja` nie wywracało ANI JEDNEGO. Tor, który ma
# handlować, był więc niepokryty dokładnie tam, gdzie krypto jest pokryte
# ośmiokrotnie.
#
# `stock` WYMAGA WŁASNEJ ASERCJI, a nie tylko dopisania do listy: cennik Alpaki
# to zero prowizji, więc `runda("stock", True) == 0.0` i test porównujący zwrot
# z zerem przechodzi TAKŻE wtedy, gdy nic nie liczy. Patrz
# `_karta_stock_wymienia_braki` niżej — to ona jest tu jedynym pomiarem.
RODZAJE = ["spot", "spot_eu", "perp", "stock"]
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


def _karta_stock_wymienia_braki(kind: str, limit: bool) -> None:
    """Dla `stock` i `etf` ZERO w rachunku jest zerem PROWIZJI, nie kosztu.

    Bez tego sprawdzenia test rundy dla `stock` byłby zielony w obie strony:
    oczekiwana wartość wynosi dokładnie 0,0 (prowizja zero, przy limicie także
    poślizg zero), więc przeszedłby również dla symulacji, która nie nalicza
    NICZEGO — i dla takiej, która naliczyłaby nie to, co trzeba.

    Karta kosztu jest tu jedynym miejscem, w którym widać różnicę między
    „policzone i wyszło zero" a „nie policzone". Dwa składniki MUSZĄ być w niej
    bez wartości:
      * opłaty regulacyjne (SEC Section 31, FINRA TAF) — obciążają wyłącznie
        nogę sprzedaży, a TAF liczy się od LICZBY AKCJI,
      * spread — na equity nikt go nie zmierzył.
    Kosztu pożyczki papieru od 2026-10-18 w karcie NIE MA, bo nie ma nogi
    krótkiej (A-5) — patrz `test_krotka_pozycja_na_equity_WYLACZONA...`.
    """
    karta = karta_rundy(kind, limit)
    assert not karta.pelna, (
        f"karta kosztu dla {kind} ogłasza się PEŁNA, a `runda({kind!r}, {limit})` "
        f"wynosi {runda(kind, limit)} — zero z pełnej karty czyta się jako "
        f"„koszt zmierzony i wynosi zero”, co dla equity jest nieprawdą")
    braki = set(karta.nieznane)
    for oczekiwany in ("oplaty regulacyjne", "spread"):
        assert oczekiwany in braki, (
            f"karta {kind} nie wymienia braku „{oczekiwany}”; nieznane: "
            f"{sorted(braki)}. Składnik nieobecny w karcie wchodzi do sumy jako "
            f"ZERO, czyli im mniej rozumiemy koszt, tym tańszy wygląda")
    assert karta.policzone == pytest.approx(runda(kind, limit)), (
        "policzalna część karty rozjechała się z `runda()` — człowiek czytałby "
        "jeden koszt, a miara liczyłaby drugi")


@pytest.mark.parametrize("kind", RODZAJE)
@pytest.mark.parametrize("limit", [False, True])
def test_transakcja_bez_ruchu_ceny_kosztuje_dokladnie_runde(kind, limit):
    k = koszty(kind)
    fee = k.maker if limit else k.taker
    slip = POSLIZG_MAKER if limit else POSLIZG_TAKER

    # `sesja=None`: test mierzy RUNDĘ, nie kalendarz — panel co 4 h przez
    # całą dobę dałby na equity jedną decyzję dziennie (A-2, `sesja.py`).
    res = simulate_portfolio(
        {"AAA": panel_plaski()}, DEFAULT_WEIGHTS, {"AAA": 2.0},
        {"AAA": (0.05, 0.0225)}, H, 0.05, LIMITY_LUZNE,
        kind=kind, egzekucja=Egzekucja(limit=limit), sesja=None)

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

    # DRUGI POMIAR DLA EQUITY, bo pierwszy przy zerowej prowizji nie mierzy nic.
    if kind in ("stock", "etf"):
        assert oczekiwane == pytest.approx(0.0, abs=1e-9) or not limit, (
            "test przestał odtwarzać sytuację, w której zero jest zerem "
            "z cennika — sprawdź `CENNIK[\"stock\"]`")
        _karta_stock_wymienia_braki(kind, limit)


@pytest.mark.parametrize("kind", RODZAJE)
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


@pytest.mark.parametrize("kind", ["stock", "etf"])
def test_krotka_pozycja_na_equity_WYLACZONA_i_bez_kosztu_pozyczki(kind):
    """OD 2026-10-18 (A-5) NOGI KRÓTKIEJ NA EQUITY NIE MA — korekta pomiaru.

    Do tej daty test pilnował odwrotnej decyzji: flaga True i koszt pożyczki
    papieru w karcie jako składnik BEZ WARTOŚCI. Karta mówiła prawdę, ale
    wynik nie: zwrot shortów liczono bez kosztu pożyczki, a konto Alpaca paper
    też go nie nalicza — strona krótka nie była zmierzona ani w trenerze, ani
    na demo. Flaga decyduje o trzech rzeczach naraz i wszystkie czytają TO
    SAMO pole:
      1. symulacja pomija wejścia ze score < 0 (`pominiete_shorty`),
      2. pętla daje `hold` z powodem `short_niewykonalny` (`loop.py`),
      3. trener uczy się strony długiej (`train.strona_uczenia`, H-6).
    `etf` razem ze `stock`: trener liczy SPY w torze `--kind stock`, a pętla
    czyta rodzaj z `ref.instrument` (SPY = 'etf').
    """
    k = koszty(kind)
    assert k.krotka_pozycja is False, (
        f"`koszty({kind!r}).krotka_pozycja` jest True — symulacja otwiera "
        f"shorty bez kosztu pożyczki papieru, którego nikt nie zmierzył")
    assert k.finansowanie is False, (
        "akcje nie płacą finansowania perpetuala; podpięcie jednej wielkości pod "
        "drugą byłoby nazwaniem dwóch różnych rzeczy jedną nazwą")
    for limit in (False, True):
        nieznane = karta_rundy(kind, limit).nieznane
        assert "koszt pozyczki papieru" not in nieznane, (
            "karta wymienia koszt nogi, której symulacja nie otwiera")


def test_symulacja_stock_nie_otwiera_shortow():
    """Sygnał krótki na `stock` → zero transakcji krótkich, licznik pominięć."""
    def cechy_spadku():
        return _cechy(False) | {"ema_ratio": 0.99, "ret_short": -20.0,
                                "ret_mid": -40.0, "ret_long": -80.0}
    panel = [{"t": T0 + timedelta(hours=4 * i), "f": cechy_spadku(),
              "price": CENA, "high": CENA, "low": CENA} for i in range(30)]
    wyniki = {}
    for kind in ("perp", "stock"):
        res = simulate_portfolio(
            {"AAA": panel}, DEFAULT_WEIGHTS, {"AAA": 2.0},
            {"AAA": (0.05, 0.0225)}, H, 0.05, LIMITY_LUZNE,
            kind=kind, egzekucja=Egzekucja(limit=True), sesja=None)
        wyniki[kind] = res
    # Kanarek: na rynku z shortami ten sam panel daje noge krotka - inaczej
    # test nizej przechodzilby takze bez flagi.
    assert any(t["side"] < 0 for t in wyniki["perp"].trades)
    assert not any(t["side"] < 0 for t in wyniki["stock"].trades)
    assert wyniki["stock"].pominiete_shorty > 0


def test_perpetual_NIE_dostaje_kosztu_pozyczki_papieru():
    """Kanarek do poprzedniego: składnik doklejony wszystkim z pozycją krótką
    liczyłby perpetualowi ten sam koszt dwa razy.

    Kosztem trzymania nogi krótkiej na perpetualu JEST finansowanie i jest ono
    naliczane (`portfolio.simulate_portfolio` od 2026-09-24).
    """
    assert koszty("perp").krotka_pozycja is True, "test nie odtwarza sytuacji"
    nazwy = [s.nazwa for s in karta_rundy("perp", True).skladniki]
    assert "koszt pozyczki papieru" not in nazwy, (
        "perpetual dostał drugą pozycję o znaczeniu finansowania")
    assert "finansowanie" in nazwy


def test_spot_bez_pozycji_krotkiej_NIE_dostaje_tego_skladnika():
    """Druga strona tego samego warunku: składnik pojawia się i znika RAZEM
    z powodem swojego istnienia, a nie według listy nazw rodzajów."""
    assert koszty("spot").krotka_pozycja is False
    nazwy = [s.nazwa for s in karta_rundy("spot", True).skladniki]
    assert "koszt pozyczki papieru" not in nazwy, (
        "karta liczy koszt pożyczki papieru instrumentowi, którego nogi krótkiej "
        "symulacja nie otwiera")


def test_wariant_produkcyjny_jest_drozszy_niz_zaden_koszt():
    """Konfiguracja z wdrożenia (spot_eu + limit) musi kosztować dokładnie
    0,500% (od 2026-10-05: maker 0,250% na obu nogach; wcześniej 0,200%)."""
    assert runda("spot_eu", True) == pytest.approx(0.0050)
    # A ten sam podmiot zleceniem rynkowym — 0,580%: roznica to juz tylko poslizg takera.
    assert runda("spot_eu", False) == pytest.approx(0.0058)
