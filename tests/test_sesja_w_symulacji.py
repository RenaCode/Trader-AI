"""Symulacja nie sklada zlecen, ktorych petla nie zlozy, bo gielda jest zamknieta (A-2).

SKAD. Przebieg trenera equity z 2026-10-18: 890 z 4061 zlecen symulacji
"bez swiecy 1h". Decyzja z zamkniecia ostatniej swiecy sesji (19-20 UTC latem)
zapada o 16:00 czasu Nowego Jorku i wychodzi o 16:20 - petla jej nie podejmie
(BRAMKA 1 w `loop.py`, zegar `/v2/clock`), a symulacja liczyla ja jako
niewypelniona i trzymala pozycje przez luke otwarcia. Do tego swiece przed-
i posesyjne Alpaki trafiaja do panelu, a symulacja decydowala takze na nich.

CO PILNUJE TEN PLIK:
  * kalendarz `sesja.sesja_regularna_nyse` / `otwarcie_nyse` - granice sesji
    w czasie letnim i zimowym, weekend, czas bez strefy;
  * `portfolio.okno_wykonania` - te same reguly co petla: decyzja w sesji,
    wysylka w sesji; swieca z decyzja 15:00 NY (wysylka 15:20) ma okno
    wypelnienia do 16:00 zamiast odrzucenia; swieca przedsesyjna (decyzja
    w pierwszym cyklu po otwarciu) - wyjscie tak, wejscie nie
    (`decyzja_przeterminowana`);
  * symulacja na `stock`: decyzje tylko z tych swiec, przy danych bez swiec
    posesyjnych zadne zlecenie nie wypada "bez swiecy 1h", a `poza_sesja`
    liczy ZLECENIA (raz na noc), nie swiece;
  * krypto (`spot`, `spot_eu`, `perp`) idzie DOKLADNIE ta sama sciezka co bez
    kalendarza.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services.engine.horizons import get as get_horizon
from services.engine.portfolio import (
    Egzekucja, Wykonanie, indeks_swiec, okno_wykonania, simulate_portfolio,
)
from services.engine.sesja import (
    NYSE_REGULARNA, otwarcie_nyse, sesja_historyczna, sesja_regularna_nyse,
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


def test_otwarcie_dnia_w_utc():
    assert otwarcie_nyse(datetime(2026, 7, 15, 15, 0, tzinfo=UTC)) == \
        datetime(2026, 7, 15, 13, 30, tzinfo=UTC)
    # 03:00Z to jeszcze poprzedni dzien w Nowym Jorku
    assert otwarcie_nyse(datetime(2026, 7, 15, 3, 0, tzinfo=UTC)) == \
        datetime(2026, 7, 14, 13, 30, tzinfo=UTC)
    assert otwarcie_nyse(datetime(2026, 1, 14, 23, 0, tzinfo=UTC)) == \
        datetime(2026, 1, 14, 14, 30, tzinfo=UTC)


@pytest.mark.parametrize("kind,z_sesja", [("stock", True), ("etf", True),
                                          ("spot", False), ("spot_eu", False),
                                          ("perp", False)])
def test_regula_z_rodzaju_instrumentu(kind, z_sesja):
    assert (sesja_historyczna(kind) is not None) is z_sesja


EGZ = Egzekucja(limit=True, opoznienie_min=20, pesymistyczna=True)
LATO = datetime(2026, 7, 15, tzinfo=UTC)      # sroda, EDT: sesja 13:30-20:00Z
ZIMA = datetime(2026, 1, 14, tzinfo=UTC)      # sroda, EST: sesja 14:30-21:00Z


def _okno(dzien, godzina, wejscie, egz=EGZ, sesja=NYSE_REGULARNA):
    return okno_wykonania(dzien + timedelta(hours=godzina), H1, egz, sesja,
                          wejscie=wejscie)


@pytest.mark.parametrize("wejscie", [True, False])
def test_okno_w_srodku_sesji_jak_dotad(wejscie):
    assert _okno(LATO, 17, wejscie) == Wykonanie(LATO + timedelta(hours=18, minutes=20))


@pytest.mark.parametrize("dzien,godzina", [(LATO, 18), (ZIMA, 19)])
@pytest.mark.parametrize("wejscie", [True, False])
def test_decyzja_godzine_przed_zamknieciem_ma_okno_do_zamkniecia(dzien, godzina, wejscie):
    """Decyzja 15:00 NY, wysylka 15:20: zlecenie `day` zyje do 16:00. Do
    poprawki z recenzji #39 ta swieca byla odrzucana w calosci (5 decyzji
    dziennie w symulacji zamiast 7 w produkcji)."""
    w = _okno(dzien, godzina, wejscie)
    assert w == Wykonanie(dzien + timedelta(hours=godzina + 1, minutes=20),
                          do_zamkniecia=True)


@pytest.mark.parametrize("dzien,godzina", [(LATO, 19), (ZIMA, 20)])
@pytest.mark.parametrize("wejscie", [True, False])
def test_ostatnia_swieca_sesji_bez_decyzji(dzien, godzina, wejscie):
    assert _okno(dzien, godzina, wejscie) is None


@pytest.mark.parametrize("dzien,godzina", [(LATO, 12), (ZIMA, 13)])
def test_swieca_przedsesyjna_wyjscie_tak_wejscie_nie(dzien, godzina):
    """Petla decyduje o niej w pierwszym cyklu po otwarciu: wejscie odpada
    (`decyzja_przeterminowana`), wyjscie idzie - wysylka otwarcie + 20 min."""
    assert _okno(dzien, godzina, wejscie=True) is None
    otwarcie = otwarcie_nyse(dzien + timedelta(hours=godzina))
    assert _okno(dzien, godzina, wejscie=False) == Wykonanie(
        otwarcie + timedelta(minutes=20))


@pytest.mark.parametrize("dzien,godzina", [(LATO, 11), (ZIMA, 12), (LATO, 3),
                                           (LATO + timedelta(days=3), 12)])  # sobota
def test_wczesniejsze_swiece_i_weekend_bez_decyzji(dzien, godzina):
    for wejscie in (True, False):
        assert _okno(dzien, godzina, wejscie) is None


def test_rynek_ciagly_zawsze_koniec_swiecy_plus_opoznienie():
    assert _okno(LATO + timedelta(days=3), 2, True, sesja=None) == Wykonanie(
        LATO + timedelta(days=3, hours=3, minutes=20))


def test_bez_modelu_pesymistycznego_nie_ma_okna_do_zamkniecia():
    w = _okno(LATO, 18, True, egz=Egzekucja(limit=True, opoznienie_min=20))
    assert w == Wykonanie(LATO + timedelta(hours=19, minutes=20))


# ------------------------------------------------------------- symulacja

def _cechy(kup: bool) -> dict:
    wspolne = {"atr_pct": 1.0, "rsi": 50.0, "range_pos": 0.5, "volume_z": 0.0,
               "news_sent": 0.0, "funding_rate": 0.0, "funding_z": 0.0}
    if kup:
        return wspolne | {"ema_ratio": 1.01, "ret_short": 20.0,
                          "ret_mid": 40.0, "ret_long": 80.0}
    return wspolne | {"ema_ratio": 1.0, "ret_short": 0.0,
                      "ret_mid": 0.0, "ret_long": 0.0}


def _panel_i_swiece(start: datetime, godzin: int, okres: int = 3,
                    tylko_sesja: bool = False):
    """Panel z wierszem O KAZDEJ GODZINIE (Alpaka oddaje swiece przed-
    i posesyjne), sygnal migajacy co `okres` swiec, szeroki zakres high-low -
    kazde zlecenie z limitem by weszlo, wiec jedyne, co moze je zatrzymac, to
    sesja. `tylko_sesja` - swiece 1h do wypelnien tylko z godzin sesji, jak
    przy rzadkim obrocie po godzinach: brak swiecy 20:00Z to dokladnie
    przypadek "bez swiecy 1h" z przebiegu 18.10."""
    panel, swiece = [], []
    for i in range(godzin):
        t = start + timedelta(hours=i)
        cena = 100.0 + (i % 5) * 0.1
        panel.append({"t": t, "f": _cechy((i // okres) % 2 == 0),
                      "price": cena, "high": cena * 1.01, "low": cena * 0.99})
        if not tylko_sesja or 13 <= t.hour < 20:
            swiece.append({"t": t, "open": cena, "high": cena * 1.01,
                           "low": cena * 0.99, "close": cena})
    return panel, {"AAA": indeks_swiec(swiece)}


# Poniedzialek 2026-07-13 00:00 UTC, piec pelnych dob - piec sesji letnich.
START = datetime(2026, 7, 13, tzinfo=UTC)
GODZINY_WEJSC = set(range(13, 19))     # decyzje 10:00-15:00 NY (EDT)
GODZINY_WYJSC = set(range(12, 19))     # + swieca przedsesyjna 12:00Z


def _symuluj(kind: str, panel, swiece, **kw):
    return simulate_portfolio({"AAA": panel}, DEFAULT_WEIGHTS, {"AAA": 2.0},
                              {"AAA": (0.05, 0.0225)}, H1, 0.05, LIMITY,
                              kind=kind, egzekucja=EGZ, swiece_wykonania=swiece,
                              **kw)


def _decyzje(res, panel):
    poprzednia = {panel[i + 1]["t"]: panel[i]["t"] for i in range(len(panel) - 1)}
    wejscia = [tr["opened_at"] for tr in res.trades]
    wyjscia = [poprzednia[tr["closed_at"]] for tr in res.trades
               if not tr.get("koniec_okna")]
    return wejscia, wyjscia


@pytest.mark.parametrize("okres", [2, 3, 5])
def test_stock_decyduje_tylko_jak_petla(okres):
    panel, swiece = _panel_i_swiece(START, 24 * 5, okres=okres, tylko_sesja=True)
    res = _symuluj("stock", panel, swiece)
    wejscia, wyjscia = _decyzje(res, panel)
    assert wejscia and wyjscia, "panel nie dal transakcji - test niczego nie mierzy"
    assert {t.hour for t in wejscia} <= GODZINY_WEJSC, sorted({t.hour for t in wejscia})
    assert {t.hour for t in wyjscia} <= GODZINY_WYJSC, sorted({t.hour for t in wyjscia})
    # Bez swiec posesyjnych ani jedno zlecenie nie wypada z braku swiecy 1h:
    # decyzja 15:00 NY wypelnia sie w godzinie wysylki, 16:00 NY nie powstaje.
    assert res.brak_swiec_wykonania == 0


def test_stock_wchodzi_z_decyzji_15_NY_i_wychodzi_z_przedsesyjnej():
    """Oba przypadki z recenzji #39 na jednym panelu: przy okresie 3 sygnal
    zmienia sie tak, ze wypadaja i wejscie z 18:00Z, i wyjscie z 12:00Z."""
    znalezione_wejscie = znalezione_wyjscie = False
    for okres in (2, 3, 4, 5, 6, 7):
        panel, swiece = _panel_i_swiece(START, 24 * 5, okres=okres, tylko_sesja=True)
        wejscia, wyjscia = _decyzje(_symuluj("stock", panel, swiece), panel)
        znalezione_wejscie |= any(t.hour == 18 for t in wejscia)
        znalezione_wyjscie |= any(t.hour == 12 for t in wyjscia)
    assert znalezione_wejscie, "zadne wejscie z decyzji 15:00 NY"
    assert znalezione_wyjscie, "zadne wyjscie z decyzji na swiecy przedsesyjnej"


def test_poza_sesja_liczy_zlecenia_a_nie_swiece():
    """Pozycja z sygnalem wyjscia przez cala noc to JEDNO niezlozone wyjscie;
    sygnal wejscia przez caly ranek przed sesja to JEDNO niezlozone wejscie.

    Poniedzialek: sygnal kupna 08:00-18:00Z (wejscia przed sesja czekaja,
    wejscie z 13:00Z), od 19:00Z sygnal neutralny, czyli wyjscie: 19:00Z
    poza sesja, potem noc, wyjscie z przedsesyjnej 12:00Z we wtorek.
    """
    start = START + timedelta(hours=8)
    panel = []
    for i in range(32):
        t = start + timedelta(hours=i)
        panel.append({"t": t, "f": _cechy(t.day == START.day and t.hour <= 18),
                      "price": 100.0, "high": 101.0, "low": 99.0})
    swiece = {"AAA": indeks_swiec([{"t": r["t"], "open": 100.0, "high": 101.0,
                                    "low": 99.0, "close": 100.0} for r in panel])}
    res = _symuluj("stock", panel, swiece)
    wejscia, wyjscia = _decyzje(res, panel)
    assert [t.hour for t in wejscia] == [13]
    assert [(t.day, t.hour) for t in wyjscia] == [(START.day + 1, 12)]
    assert res.poza_sesja_wejscia == 1
    assert res.poza_sesja_wyjscia == 1
    assert res.poza_sesja == 2


def test_kanarek_bez_kalendarza_stock_decyduje_takze_po_godzinach():
    """Bez reguly sesji ten sam panel daje decyzje poza godzinami petli -
    inaczej testy wyzej przechodzilyby takze bez poprawki."""
    panel, swiece = _panel_i_swiece(START, 24 * 5)
    res = _symuluj("stock", panel, swiece, sesja=None)
    wejscia, _ = _decyzje(res, panel)
    assert {t.hour for t in wejscia} - GODZINY_WEJSC
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
    z_kalendarzem = _symuluj(kind, panel, swiece, sesja=NYSE_REGULARNA)
    assert z_kalendarzem.trades != domyslnie.trades
