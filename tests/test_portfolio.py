"""Testy symulacji portfelowej.

Sprawdzaja rzeczy, ktorych symulacja per instrument NIE MOGLA sprawdzic:
wspolna ekspozycje, wspolny kill-switch i to, ze zatrzymanie silnika naprawde
zatrzymuje wejscia na WSZYSTKICH instrumentach, nie tylko na tym, ktory stracil.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from services.engine.horizons import get as get_horizon
from services.engine.portfolio import metrics, simulate_portfolio
from services.engine.signals import DEFAULT_WEIGHTS

H = get_horizon("4h")
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def cechy_kupuj() -> dict:
    """Zestaw cech dajacy silny sygnal kupna w rezimie trend_up."""
    return {"atr_pct": 1.0, "ema_ratio": 1.01, "rsi": 50.0, "range_pos": 0.5,
            "ret_short": 20.0, "ret_mid": 40.0, "ret_long": 80.0,
            "volume_z": 0.0, "news_sent": 0.0, "funding_rate": 0.0, "funding_z": 0.0}


def cechy_neutralne() -> dict:
    return {"atr_pct": 1.0, "ema_ratio": 1.0, "rsi": 50.0, "range_pos": 0.5,
            "ret_short": 0.0, "ret_mid": 0.0, "ret_long": 0.0,
            "volume_z": 0.0, "news_sent": 0.0, "funding_rate": 0.0, "funding_z": 0.0}


def panel(n: int, cena_od: float, cena_do: float, okres: int = 5) -> list[dict]:
    """Panel n swiec z liniowa zmiana ceny i sygnalem MIGAJACYM.

    Sygnal na przemian silny i neutralny, zeby pozycje otwieraly sie i zamykaly
    wielokrotnie w ciagu okresu. Przy stalym sygnale kazdy instrument wchodzil
    raz na poczatku i trzymal do konca - a wtedy test "po kill-switchu nie ma
    nowych wejsc" przechodzi dlatego, ze nowych wejsc nie ma NIGDY, a nie
    dlatego, ze kill-switch zadzialal.
    """
    return [{"t": T0 + timedelta(hours=4 * i),
             "f": cechy_kupuj() if (i // okres) % 2 == 0 else cechy_neutralne(),
             "price": cena_od + (cena_do - cena_od) * i / max(1, n - 1)}
            for i in range(n)]


LIMITY_LUZNE = {
    "max_daily_loss":        {"value": Decimal("99"),     "action": "killswitch"},
    "max_drawdown":          {"value": Decimal("99"),     "action": "killswitch"},
    "max_orders_per_hour":   {"value": Decimal("1000"),   "action": "block"},
    "max_position_notional": {"value": Decimal("5000"),   "action": "downsize"},
    "max_total_exposure":    {"value": Decimal("999999"), "action": "block"},
}


def uruchom(panele, limity, gate=0.05):
    gates = {s: (gate, gate * 0.45) for s in panele}
    atr = {s: 2.0 for s in panele}          # atr_p80 wysoko => rezim NIE high_vol
    return simulate_portfolio(panele, DEFAULT_WEIGHTS, atr, gates, H, 0.05, limity)


def test_rosnaca_cena_daje_dodatni_wynik():
    res = uruchom({"AAA": panel(40, 100.0, 130.0)}, LIMITY_LUZNE)
    m = metrics(res, min_trades=1)
    assert m["n_trades"] >= 1, "przy stalym sygnale kupna musi powstac transakcja"
    assert m["total_return"] > 0, f"wzrost ceny o 30% ma dac zysk, jest {m['total_return']}%"


def test_spadajaca_cena_daje_ujemny_wynik():
    """Kanarek. Gdyby symulacja liczyla zwrot bez uwzglednienia kierunku,
    ten test przeszedlby razem z poprzednim."""
    res = uruchom({"AAA": panel(40, 130.0, 100.0)}, LIMITY_LUZNE)
    m = metrics(res, min_trades=1)
    assert m["total_return"] < 0


def test_limit_lacznej_ekspozycji_ogranicza_liczbe_pozycji():
    panele = {f"I{i}": panel(30, 100.0, 130.0) for i in range(8)}
    limity = LIMITY_LUZNE | {
        "max_total_exposure": {"value": Decimal("6000"), "action": "block"}}
    res = uruchom(panele, limity)
    assert res.blocked > 0, "przy limicie 6000 i zleceniach po ~3000 czesc musi zostac odrzucona"

    luzny = uruchom({f"I{i}": panel(30, 100.0, 130.0) for i in range(8)}, LIMITY_LUZNE)
    assert luzny.blocked == 0, "bez limitu nic nie powinno byc odrzucone"
    assert len(res.trades) < len(luzny.trades)


def test_limit_wielkosci_pozycji_obcina():
    limity = LIMITY_LUZNE | {
        "max_position_notional": {"value": Decimal("1000"), "action": "downsize"}}
    res = uruchom({"AAA": panel(30, 100.0, 130.0)}, limity)
    assert res.downsized > 0, "zlecenie ~3000 przy limicie 1000 musi zostac obciete"


def test_killswitch_zatrzymuje_wejscia_na_wszystkich_instrumentach():
    """Sedno przejscia na symulacje portfelowa.

    Strata doprowadza do obsuniecia ponad prog. Od tego momentu NIE MOZE
    powstac zadne nowe wejscie - takze na instrumencie, ktory nic nie stracil.
    """
    # Prog celowo niski: testujemy MECHANIZM, nie wielkosc straty. Przy dwoch
    # instrumentach i zleceniach po ~3000 realna strata w tym scenariuszu to
    # ulamek procenta kapitalu.
    limity = LIMITY_LUZNE | {
        "max_drawdown": {"value": Decimal("0.1"), "action": "killswitch"}}
    res = simulate_portfolio(
        {"SPADA": panel(60, 100.0, 40.0), "ROSNIE": panel(60, 100.0, 200.0)},
        DEFAULT_WEIGHTS, {"SPADA": 2.0, "ROSNIE": 2.0},
        {"SPADA": (0.05, 0.02), "ROSNIE": (0.05, 0.02)}, H, 0.05, limity)

    assert res.killswitch_at is not None, "obsuniecie ponad prog musi uruchomic kill-switch"
    assert "Obsunięcie" in res.killswitch_reason

    # Bez tego test przechodzilby takze wtedy, gdyby kill-switch zadzialal
    # natychmiast i zadna pozycja nigdy nie powstala.
    przed = [t for t in res.trades if t["opened_at"] < res.killswitch_at]
    assert przed, "przed kill-switchem musialy powstac jakies pozycje"

    po = [t for t in res.trades if t["opened_at"] > res.killswitch_at]
    assert not po, f"po kill-switchu nie wolno otworzyc pozycji, otwarto {len(po)}"


def test_bez_killswitcha_pozycje_powstaja_po_stracie():
    """Kanarek do poprzedniego testu.

    Ten sam scenariusz z wylaczonym limitem obsuniecia MUSI produkowac
    transakcje przez caly okres - inaczej poprzedni test przechodzilby
    z powodu braku sygnalu, a nie z powodu kill-switcha.
    """
    res = simulate_portfolio(
        {"SPADA": panel(60, 100.0, 40.0), "ROSNIE": panel(60, 100.0, 200.0)},
        DEFAULT_WEIGHTS, {"SPADA": 2.0, "ROSNIE": 2.0},
        {"SPADA": (0.05, 0.02), "ROSNIE": (0.05, 0.02)}, H, 0.05, LIMITY_LUZNE)
    assert res.killswitch_at is None
    polowa = T0 + timedelta(hours=4 * 30)
    assert any(t["opened_at"] > polowa for t in res.trades), \
        "bez kill-switcha pozycje musza powstawac takze w drugiej polowie okresu"


def test_obsuniecie_liczone_z_krzywej_kapitalu():
    """Obsuniecie ma pochodzic z wyceny biezacej, nie z wyniku zrealizowanego."""
    res = uruchom({"AAA": panel(40, 100.0, 60.0)}, LIMITY_LUZNE)
    m = metrics(res, min_trades=1)
    assert m["max_dd"] < 0, "spadek ceny o 40% musi zostawic slad w obsunieciu"
