"""Symulacja nie sklada zlecen, ktorych petla nie zlozy, bo gielda jest zamknieta (A-2).

SKAD. Przebieg trenera equity z 2026-10-18: 890 z 4061 zlecen symulacji
"bez swiecy 1h". Decyzja z zamkniecia ostatniej swiecy sesji (19-20 UTC latem)
zapada o 16:00 czasu Nowego Jorku i wychodzi o 16:20 - petla jej nie podejmie
(BRAMKA 1 w `loop.py`, zegar `/v2/clock`), a symulacja liczyla ja jako
niewypelniona i trzymala pozycje przez luke otwarcia. Do tego swiece przed-
i posesyjne Alpaki trafiaja do panelu, a symulacja decydowala takze na nich.

CO PILNUJE TEN PLIK:
  * kalendarz `sesja.sesja_regularna_nyse` - granice sesji w czasie letnim
    i zimowym, weekend, czas bez strefy;
  * na `stock` zadna transakcja nie ma decyzji (wejscia ani wyjscia) poza
    sesja, a `poza_sesja` liczy pominiete zlecenia - przy panelu, w ktorym
    swiece 1h istnieja o KAZDEJ godzinie, wiec stary kod mial na czym
    "wypelnic" zlecenie posesyjne;
  * krypto (`spot`, `perp`) idzie DOKLADNIE ta sama sciezka co bez kalendarza.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services.engine.horizons import get as get_horizon
from services.engine.portfolio import (
    KROK_WYKONANIA, Egzekucja, _poczatek_godziny_wysylki, indeks_swiec,
    simulate_portfolio,
)
from services.engine.sesja import (
    sesja_historyczna, sesja_regularna_nyse,
)
from services.engine.signals import DEFAULT_WEIGHTS

H1 = get_horizon("1h")
UTC = timezone.utc

LIMITY = {
    "max_daily_loss":        {"value": Decimal("99"),     "action": "killswitch"},
    "max_drawdown":          {"value": Decimal("99"),     "action": "killswitch"},
    "max_orders_per_hour":   {"value": Decimal("1000"),   "action": "block"},
    "max_position_notional": {"value": Decimal("5000"),   "action": "downsize"},
    "max_total_exposure":    {"value": Decimal("999999"), "action": "block"},
    "max_leverage":          {"value": Decimal("9999"),   "action": "block"},
}


# ------------------------------------------------------------- kalendarz

@pytest.mark.parametrize("chwila,otwarta", [
    # czas letni (EDT, UTC-4): 13:30-20:00 UTC
    (datetime(2026, 7, 15, 13, 29, tzinfo=UTC), False),
    (datetime(2026, 7, 15, 13, 30, tzinfo=UTC), True),
    (datetime(2026, 7, 15, 19, 59, tzinfo=UTC), True),
    (datetime(2026, 7, 15, 20, 0, tzinfo=UTC), False),   # zegar Alpaki: zamknieta
    (datetime(2026, 7, 15, 20, 20, tzinfo=UTC), False),  # wysylka "widma" z 18.10
    # czas zimowy (EST, UTC-5): 14:30-21:00 UTC
    (datetime(2026, 1, 14, 14, 29, tzinfo=UTC), False),
    (datetime(2026, 1, 14, 14, 30, tzinfo=UTC), True),
    (datetime(2026, 1, 14, 20, 30, tzinfo=UTC), True),
    (datetime(2026, 1, 14, 21, 0, tzinfo=UTC), False),
    # weekend
    (datetime(2026, 7, 18, 15, 0, tzinfo=UTC), False),
    (datetime(2026, 7, 19, 15, 0, tzinfo=UTC), False),
])
def test_sesja_regularna_nyse(chwila, otwarta):
    assert sesja_regularna_nyse(chwila) is otwarta


def test_czas_bez_strefy_nie_jest_zgadywany():
    with pytest.raises(ValueError):
        sesja_regularna_nyse(datetime(2026, 7, 15, 15, 0))


@pytest.mark.parametrize("kind,z_sesja", [("stock", True), ("etf", True),
                                          ("spot", False), ("spot_eu", False),
                                          ("perp", False)])
def test_regula_z_rodzaju_instrumentu(kind, z_sesja):
    assert (sesja_historyczna(kind) is not None) is z_sesja


# ------------------------------------------------------------- symulacja

def _cechy(kup: bool) -> dict:
    wspolne = {"atr_pct": 1.0, "rsi": 50.0, "range_pos": 0.5, "volume_z": 0.0,
               "news_sent": 0.0, "funding_rate": 0.0, "funding_z": 0.0}
    if kup:
        return wspolne | {"ema_ratio": 1.01, "ret_short": 20.0,
                          "ret_mid": 40.0, "ret_long": 80.0}
    return wspolne | {"ema_ratio": 1.0, "ret_short": 0.0,
                      "ret_mid": 0.0, "ret_long": 0.0}


def _panel_i_swiece(start: datetime, godzin: int, okres: int = 3):
    """Swieca 1h O KAZDEJ GODZINIE (jak posesyjne i przedsesyjne swiece
    Alpaki), sygnal migajacy co `okres` swiec, szeroki zakres high-low - kazde
    zlecenie z limitem by weszlo, wiec jedyne, co moze je zatrzymac, to sesja."""
    panel, swiece = [], []
    for i in range(godzin):
        t = start + timedelta(hours=i)
        cena = 100.0 + (i % 5) * 0.1
        panel.append({"t": t, "f": _cechy((i // okres) % 2 == 0),
                      "price": cena, "high": cena * 1.01, "low": cena * 0.99})
        swiece.append({"t": t, "open": cena, "high": cena * 1.01,
                       "low": cena * 0.99, "close": cena})
    return panel, {"AAA": indeks_swiec(swiece)}


EGZ = Egzekucja(limit=True, opoznienie_min=20, pesymistyczna=True)
# Poniedzialek 2026-07-13 00:00 UTC, piec pelnych dob - piec sesji letnich.
START = datetime(2026, 7, 13, tzinfo=UTC)


def _symuluj(kind: str, panel, swiece, **kw):
    return simulate_portfolio({"AAA": panel}, DEFAULT_WEIGHTS, {"AAA": 2.0},
                              {"AAA": (0.05, 0.0225)}, H1, 0.05, LIMITY,
                              kind=kind, egzekucja=EGZ, swiece_wykonania=swiece,
                              **kw)


def _okno_w_sesji(t_swiecy: datetime) -> bool:
    """Niezalezny zapis reguly: decyzja, wysylka i godzina wypelnienia."""
    t_dec = t_swiecy + timedelta(minutes=H1.bar_minutes)
    t_wys = t_dec + timedelta(minutes=EGZ.opoznienie_min)
    od = _poczatek_godziny_wysylki(t_wys) + KROK_WYKONANIA
    return all(sesja_regularna_nyse(x) for x in
               (t_dec, t_wys, od, od + KROK_WYKONANIA - timedelta(minutes=1)))


def test_stock_nie_decyduje_poza_sesja():
    panel, swiece = _panel_i_swiece(START, 24 * 5)
    res = _symuluj("stock", panel, swiece)
    poprzednia = {panel[i + 1]["t"]: panel[i]["t"] for i in range(len(panel) - 1)}

    assert res.trades, "panel nie dal transakcji - test niczego nie mierzy"
    for tr in res.trades:
        assert _okno_w_sesji(tr["opened_at"]), (
            f"wejscie z decyzji na swiecy {tr['opened_at']:%a %H:%M} UTC - "
            f"petla nie zlozylaby tego zlecenia")
        if not tr.get("koniec_okna"):
            decyzja_wyjscia = poprzednia[tr["closed_at"]]
            assert _okno_w_sesji(decyzja_wyjscia), (
                f"wyjscie z decyzji na swiecy {decyzja_wyjscia:%a %H:%M} UTC")
    assert res.poza_sesja > 0
    # Swiece 1h sa o kazdej godzinie, wiec ani jedno zlecenie nie wypada
    # z braku swiecy - "widma" nie przenosza sie do tego licznika.
    assert res.brak_swiec_wykonania == 0


def test_kanarek_bez_kalendarza_stock_decyduje_takze_po_godzinach():
    """Bez reguly sesji ten sam panel daje transakcje z decyzji poza sesja -
    inaczej test wyzej przechodzilby takze bez poprawki."""
    panel, swiece = _panel_i_swiece(START, 24 * 5)
    res = _symuluj("stock", panel, swiece, sesja=None)
    assert any(not _okno_w_sesji(tr["opened_at"]) for tr in res.trades)
    assert res.poza_sesja == 0


@pytest.mark.parametrize("kind", ["spot", "spot_eu", "perp"])
def test_krypto_bez_zmian(kind):
    """Rynek ciagly: domyslna regula = brak reguly, wynik identyczny co do
    transakcji, krzywej kapitalu i licznikow - na panelu przez noce i weekend."""
    panel, swiece = _panel_i_swiece(START, 24 * 7)
    domyslnie = _symuluj(kind, panel, swiece)
    bez = _symuluj(kind, panel, swiece, sesja=None)
    assert domyslnie.trades == bez.trades
    assert domyslnie.equity_curve == bez.equity_curve
    assert domyslnie.poza_sesja == 0
    assert (domyslnie.wypelnione_wejscia, domyslnie.niewypelnione_wejscia,
            domyslnie.wypelnione_wyjscia, domyslnie.niewypelnione_wyjscia) == (
        bez.wypelnione_wejscia, bez.niewypelnione_wejscia,
        bez.wypelnione_wyjscia, bez.niewypelnione_wyjscia)
    # Kanarek: kalendarz NYSE na tym samym panelu zmienia wynik - porownanie
    # wyzej nie jest wiec rowne z definicji.
    z_kalendarzem = _symuluj(kind, panel, swiece, sesja=sesja_regularna_nyse)
    assert z_kalendarzem.trades != domyslnie.trades
