"""Testy symulacji portfelowej.

Sprawdzaja rzeczy, ktorych symulacja per instrument NIE MOGLA sprawdzic:
wspolna ekspozycje, wspolny kill-switch i to, ze zatrzymanie silnika naprawde
zatrzymuje wejscia na WSZYSTKICH instrumentach, nie tylko na tym, ktory stracil.
"""
from datetime import datetime, timedelta, timezone

import pytest
from decimal import Decimal

from services.engine.horizons import get as get_horizon
from services.engine.portfolio import kup_i_trzymaj, metrics, simulate_portfolio
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
    out = []
    for i in range(n):
        cena = cena_od + (cena_do - cena_od) * i / max(1, n - 1)
        out.append({
            "t": T0 + timedelta(hours=4 * i),
            "f": cechy_kupuj() if (i // okres) % 2 == 0 else cechy_neutralne(),
            "price": cena,
            # Zakres swiecy potrzebny do modelu wypelnien zlecen z limitem.
            "high": cena * 1.004,
            "low": cena * 0.996,
        })
    return out


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


def test_ekspozycja_nigdy_nie_przekracza_limitu():
    """Wlasciwa niezmienna wlasnosc portfela.

    Mocniejsza niz "czesc zlecen zostaje odrzucona": ta ostatnia zalezy od tego,
    jak duze sa zlecenia, a ekspozycja ponad limit jest bledem zawsze.
    """
    LIMIT = Decimal("6000")
    panele = {f"I{i}": panel(30, 100.0, 130.0) for i in range(8)}
    limity = LIMITY_LUZNE | {"max_total_exposure": {"value": LIMIT, "action": "block"}}
    res = uruchom(panele, limity)
    assert res.max_exposure <= float(LIMIT) + 1e-6, \
        f"ekspozycja {res.max_exposure:.0f} przekroczyla limit {LIMIT}"
    assert res.trades, "przy budzecie z limitu transakcje maja powstawac, nie znikac"


def test_budzet_na_pozycje_skaluje_sie_liczba_instrumentow():
    """Ten sam limit rozlozony na wiecej instrumentow = mniejsze pojedyncze zlecenie.

    Bez tego 12 instrumentow po 3000 chcialoby 36000 przy limicie 15000
    i mieciloby sie tylko piec pozycji - portfel skoncentrowany zamiast
    rozproszonego, przy tej samej ekspozycji lacznej.
    """
    limity = LIMITY_LUZNE | {
        "max_total_exposure": {"value": Decimal("12000"), "action": "block"}}
    maly = uruchom({f"I{i}": panel(20, 100.0, 130.0) for i in range(3)}, limity)
    duzy = uruchom({f"I{i}": panel(20, 100.0, 130.0) for i in range(12)}, limity)
    assert maly.base_notional > duzy.base_notional
    assert abs(maly.base_notional - 12000 / 3) < 1e-6
    assert abs(duzy.base_notional - 12000 / 12) < 1e-6


def cechy_o_sile(x: float) -> dict:
    """Cechy dajace sygnal momentum o zadanej sile x w (0,1).

    Zwroty skalowane oczekiwanym ruchem (atr * sqrt(liczba swiec)), zeby kazda
    z trzech skal wnosila dokladnie x. Dzieki temu sila sygnalu jest sterowalna
    i porownywalna miedzy instrumentami.
    """
    atr = 1.0
    return {"atr_pct": atr, "ema_ratio": 1.01, "rsi": 50.0, "range_pos": 0.5,
            "ret_short": x * atr * (H.ret_short ** 0.5),
            "ret_mid": x * atr * (H.ret_mid ** 0.5),
            "ret_long": x * atr * (H.ret_long ** 0.5),
            "volume_z": 0.0, "news_sent": 0.0, "funding_rate": 0.0, "funding_z": 0.0}


def test_przy_niedoborze_kapital_dostaje_najmocniejszy_sygnal():
    """Przy niedoborze decyduje sila sygnalu, nie nazwa instrumentu.

    Wazkie zastrzezenie: po wyprowadzeniu budzetu z limitu ekspozycji
    (limit/n na pozycje) laczna ekspozycja z definicji nie przekracza limitu,
    wiec to NIE ON jest tu waskim gardlem. Jest nim limit liczby zlecen na
    godzine - i wlasnie na nim widac, czy kolejnosc rozpatrywania ma znaczenie.

    Instrumenty nazwane tak, ze najmocniejszy jest OSTATNI alfabetycznie.
    Poprzednia wersja iterowala po sorted(present) i wybieralaby AAA.
    """
    sily = {"AAA": 0.15, "BBB": 0.25, "CCC": 0.40, "ZZZ": 0.95}
    panele = {
        sym: [{"t": T0 + timedelta(hours=4 * i), "f": cechy_o_sile(x),
               "price": 100.0 + i} for i in range(12)]
        for sym, x in sily.items()
    }
    limity = LIMITY_LUZNE | {"max_orders_per_hour": {"value": Decimal("1"), "action": "block"}}
    res = simulate_portfolio(panele, DEFAULT_WEIGHTS,
                             {s: 2.0 for s in panele},
                             {s: (0.02, 0.01) for s in panele},
                             H, 0.05, limity)
    assert res.trades, "co najmniej jedna pozycja musi powstac"
    pierwsza = min(res.trades, key=lambda t: t["opened_at"])
    assert pierwsza["symbol"] == "ZZZ", \
        f"kapital ma dostac najmocniejszy sygnal, dostal {pierwsza['symbol']}"
    assert res.blocked > 0, "przy limicie 1 zlecenia na godzine reszta musi zostac odrzucona"


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


def test_pamiec_podreczna_daje_identyczny_wynik():
    """Optymalizacja nie moze zmieniac wyniku.

    Bez tego testu przyspieszenie treningu bylo zmiana zachowania udajaca
    zmiane wydajnosci - a to najgorszy rodzaj optymalizacji.
    """
    from services.engine.portfolio import precompute_panels

    # Cechy MUSZA sie roznic miedzy swiecami, inaczej test nie wykryje blednego
    # indeksowania pamieci podrecznej - przy identycznych swiecach kazda pozycja
    # w cache jest wymienna z kazda inna.
    import math
    panele = {}
    for i in range(4):
        rows = []
        for j in range(40):
            faza = math.sin((j + i * 3) / 4.0)
            rows.append({
                "t": T0 + timedelta(hours=4 * j),
                "f": cechy_kupuj() | {"rsi": 50 + 40 * faza,
                                      "range_pos": 0.5 + 0.45 * faza,
                                      "rel_strength": faza,
                                      "ret_short": 20.0 * faza},
                "price": 100.0 + 20 * faza,
            })
        panele[f"I{i}"] = rows
    atr = {s: 2.0 for s in panele}
    gates = {s: (0.05, 0.02) for s in panele}

    bez = simulate_portfolio(panele, DEFAULT_WEIGHTS, atr, gates, H, 0.05, LIMITY_LUZNE)
    cache = precompute_panels(panele, atr, H)
    z = simulate_portfolio(panele, DEFAULT_WEIGHTS, atr, gates, H, 0.05, LIMITY_LUZNE,
                           cache=cache)

    assert len(bez.trades) == len(z.trades)
    for a, b in zip(bez.trades, z.trades):
        assert a["ret"] == b["ret"]
        assert a["symbol"] == b["symbol"]
    assert bez.max_exposure == z.max_exposure
    assert [e for _, e in bez.equity_curve] == [e for _, e in z.equity_curve]


def test_pamiec_podreczna_odpowiada_swiecom_po_indeksie():
    """Sprawdza SAMA pamiec podreczna, nie zgodnosc dwoch sciezek.

    Poprzedni test porownywal wywolanie z jawna pamiecia z wywolaniem bez niej -
    ale to drugie buduje ta sama pamiec wewnetrznie, wiec oba byly jednakowo
    bledne, gdy blad siedzial w samym budowaniu. Wykryl to dopiero kanarek
    z odwrocona kolejnoscia, ktory przeszedl mimo wstrzyknietego bledu.

    Tutaj punktem odniesienia jest evaluate() liczone wprost z cech danej swiecy.
    """
    import math

    from services.engine.portfolio import precompute_panels
    from services.engine.signals import combine, evaluate

    rows = []
    for j in range(30):
        faza = math.sin(j / 3.0)
        rows.append({
            "t": T0 + timedelta(hours=4 * j),
            "f": cechy_kupuj() | {"rsi": 50 + 40 * faza, "rel_strength": faza,
                                  "range_pos": 0.5 + 0.45 * faza},
            "price": 100.0 + j,
        })
    cache = precompute_panels({"AAA": rows}, {"AAA": 2.0}, H)["AAA"]

    assert len(cache) == len(rows)
    for i, r in enumerate(rows):
        rezim, sygnaly = cache[i]
        z_cache = combine(sygnaly, rezim, None)
        wprost = evaluate(r["f"], rezim, None, H)
        assert z_cache[0] == pytest.approx(wprost[0], abs=1e-12), \
            f"świeca {i}: pamięć podręczna nie odpowiada cechom tej świecy"
        assert z_cache[1] == pytest.approx(wprost[1], abs=1e-12)


# ------------------------------------------------------ egzekucja z limitem
def _swieca(t, cena, lo_pct, hi_pct, cechy):
    return {"t": t, "f": cechy, "price": cena,
            "low": cena * (1 - lo_pct), "high": cena * (1 + hi_pct)}


def test_limit_wypelnia_sie_gdy_cena_przyszla_do_nas():
    from services.engine.portfolio import Egzekucja

    # Cena rosnie, ale kazda swieca schodzi 1% ponizej poprzedniego zamkniecia -
    # zlecenie kupna po cenie zamkniecia zawsze sie wypelnia.
    rows = [_swieca(T0 + timedelta(hours=4 * i), 100.0 + i,
                    lo_pct=0.02, hi_pct=0.02, cechy=cechy_kupuj())
            for i in range(30)]
    res = simulate_portfolio({"AAA": rows}, DEFAULT_WEIGHTS, {"AAA": 2.0},
                             {"AAA": (0.05, 0.02)}, H, 0.05, LIMITY_LUZNE,
                             kind="perp", egzekucja=Egzekucja(limit=True))
    assert res.trades, "przy szerokim zakresie świec limit musi się wypełniać"
    assert res.niewypelnione_wejscia == 0


def test_limit_NIE_wypelnia_sie_gdy_cena_ucieka():
    """SEDNO uczciwego modelu.

    Swieca, ktora otwiera sie luka w gore i nigdy nie wraca, nie wypelni
    zlecenia kupna zlozonego nizej. Gdyby model tego nie sprawdzal, backtest
    dostawalby najlepsza cene przy kazdym ruchu, ktory uciekl - czyli
    dokladnie tam, gdzie w rzeczywistosci zostajemy poza rynkiem.
    """
    from services.engine.portfolio import Egzekucja

    rows = []
    for i in range(30):
        cena = 100.0 * (1.03 ** i)          # gwaltowny wzrost
        rows.append({"t": T0 + timedelta(hours=4 * i), "f": cechy_kupuj(),
                     "price": cena,
                     "low": cena * 0.9999,   # swieca nigdy nie schodzi nizej
                     "high": cena * 1.02})
    res = simulate_portfolio({"AAA": rows}, DEFAULT_WEIGHTS, {"AAA": 2.0},
                             {"AAA": (0.05, 0.02)}, H, 0.05, LIMITY_LUZNE,
                             kind="perp", egzekucja=Egzekucja(limit=True))
    assert res.niewypelnione_wejscia > 0, \
        "przy uciekającej cenie zlecenie z limitem musi zostać niewypełnione"


def test_egzekucja_rynkowa_wypelnia_zawsze():
    """Kanarek do poprzedniego testu: ten sam uciekajacy rynek, ale zlecenie
    rynkowe - wypelnienie ma byc pewne."""
    from services.engine.portfolio import Egzekucja

    rows = []
    for i in range(30):
        cena = 100.0 * (1.03 ** i)
        rows.append({"t": T0 + timedelta(hours=4 * i), "f": cechy_kupuj(),
                     "price": cena, "low": cena * 0.9999, "high": cena * 1.02})
    res = simulate_portfolio({"AAA": rows}, DEFAULT_WEIGHTS, {"AAA": 2.0},
                             {"AAA": (0.05, 0.02)}, H, 0.05, LIMITY_LUZNE,
                             kind="perp", egzekucja=Egzekucja(limit=False))
    assert res.niewypelnione_wejscia == 0
    assert res.trades


def test_limit_jest_tanszy_niz_rynkowe_przy_tych_samych_wypelnieniach():
    """Przy szerokich swiecach oba tryby wypelniaja tyle samo, wiec roznica
    w wyniku pochodzi WYLACZNIE z prowizji i poslizgu."""
    from services.engine.portfolio import Egzekucja, metrics

    rows = [_swieca(T0 + timedelta(hours=4 * i), 100.0 + i * 0.5,
                    lo_pct=0.03, hi_pct=0.03, cechy=cechy_kupuj())
            for i in range(40)]
    wspolne = ({"AAA": rows}, DEFAULT_WEIGHTS, {"AAA": 2.0},
               {"AAA": (0.05, 0.02)}, H, 0.05, LIMITY_LUZNE)
    rynek = simulate_portfolio(*wspolne, kind="perp", egzekucja=Egzekucja(limit=False))
    limit = simulate_portfolio(*wspolne, kind="perp", egzekucja=Egzekucja(limit=True))
    assert len(rynek.trades) == len(limit.trades)
    assert metrics(limit, 1)["total_return"] > metrics(rynek, 1)["total_return"]


def test_koszty_zaleza_od_rodzaju_instrumentu():
    """Spot placi 0,1% od strony, perpetual 0,055% taker - piecio- i dwukrotna
    roznica. Wczesniej w symulacji siedziala jedna stala dla obu."""
    from services.engine.koszty import runda

    assert runda("spot") > runda("perp")
    assert runda("perp", maker=True) < runda("perp") / 3
    # Na spocie zlecenie z limitem nie daje rabatu prowizyjnego - tylko brak poślizgu.
    from services.engine.koszty import koszty
    assert koszty("spot").maker == koszty("spot").taker


def test_spot_nie_pozwala_na_krotka_pozycje():
    """Fakt rynkowy, ktory musi byc w modelu: 8 z 12 pozycji w dzienniku
    produkcyjnym to byly krotkie sprzedaze na instrumentach spotowych."""
    from services.engine.koszty import koszty

    assert not koszty("spot").krotka_pozycja
    assert koszty("perp").krotka_pozycja


def test_limit_placi_prowizje_maker_a_rynkowe_taker():
    """KANAREK NA PODMIANE STAWKI.

    Poprzednia wersja tego testu porownywala tylko wynik limitu z rynkowym -
    a ten jest lepszy juz przez sam brak poslizgu, niezaleznie od prowizji.
    Test przechodzil wiec takze wtedy, gdy limit placil stawke taker.
    """
    from services.engine.koszty import koszty
    from services.engine.portfolio import Egzekucja

    rows = [_swieca(T0 + timedelta(hours=4 * i), 100.0 + i * 0.5,
                    lo_pct=0.03, hi_pct=0.03, cechy=cechy_kupuj())
            for i in range(20)]
    wspolne = ({"AAA": rows}, DEFAULT_WEIGHTS, {"AAA": 2.0},
               {"AAA": (0.05, 0.02)}, H, 0.05, LIMITY_LUZNE)

    limit = simulate_portfolio(*wspolne, kind="perp", egzekucja=Egzekucja(limit=True))
    rynek = simulate_portfolio(*wspolne, kind="perp", egzekucja=Egzekucja(limit=False))

    assert limit.prowizja == koszty("perp").maker
    assert rynek.prowizja == koszty("perp").taker
    assert limit.poslizg == 0.0
    assert rynek.poslizg > 0.0


def test_stawki_spotowe_sa_wyzsze_niz_perpetuala():
    from services.engine.portfolio import Egzekucja

    rows = [_swieca(T0 + timedelta(hours=4 * i), 100.0 + i * 0.5,
                    lo_pct=0.03, hi_pct=0.03, cechy=cechy_kupuj())
            for i in range(20)]
    wspolne = ({"AAA": rows}, DEFAULT_WEIGHTS, {"AAA": 2.0},
               {"AAA": (0.05, 0.02)}, H, 0.05, LIMITY_LUZNE)
    spot = simulate_portfolio(*wspolne, kind="spot", egzekucja=Egzekucja(limit=False))
    perp = simulate_portfolio(*wspolne, kind="perp", egzekucja=Egzekucja(limit=False))
    assert spot.prowizja > perp.prowizja


def test_na_spocie_nie_otwieramy_krotkich_pozycji():
    """Krotkiej pozycji na spocie nie da sie zajac bez pozyczki marzowej.

    W dzienniku produkcyjnym 8 z 12 pozycji to byly shorty na instrumentach
    spotowych - dwie trzecie transakcji strategii nie bylo wykonalnych.
    """
    from services.engine.portfolio import Egzekucja

    # Cechy dajace sygnal SPRZEDAZY: wszystko skrajnie w dol.
    sprzedaj = cechy_kupuj() | {"ret_short": -20.0, "ret_mid": -40.0,
                                "ret_long": -80.0, "ema_ratio": 0.99,
                                "rel_strength": -0.9}
    rows = [_swieca(T0 + timedelta(hours=4 * i), 100.0 - i * 0.5,
                    lo_pct=0.03, hi_pct=0.03, cechy=sprzedaj)
            for i in range(30)]
    wspolne = ({"AAA": rows}, DEFAULT_WEIGHTS, {"AAA": 2.0},
               {"AAA": (0.05, 0.02)}, H, 0.05, LIMITY_LUZNE)

    spot = simulate_portfolio(*wspolne, kind="spot", egzekucja=Egzekucja())
    perp = simulate_portfolio(*wspolne, kind="perp", egzekucja=Egzekucja())

    assert spot.pominiete_shorty > 0, "na spocie krótkie pozycje muszą być pomijane"
    assert perp.pominiete_shorty == 0, "na perpetualu krótkie pozycje są dozwolone"
    assert len(perp.trades) > len(spot.trades)


# ------------------------------------------------------ punkt odniesienia
def test_kup_i_trzymaj_na_rosnacym_rynku_jest_dodatni():
    from services.engine.portfolio import kup_i_trzymaj

    rows = [_swieca(T0 + timedelta(hours=4 * i), 100.0 * (1.01 ** i),
                    0.01, 0.01, cechy_kupuj()) for i in range(50)]
    assert kup_i_trzymaj({"AAA": rows}, "perp") > 0


def test_kup_i_trzymaj_na_spadajacym_jest_ujemny():
    from services.engine.portfolio import kup_i_trzymaj

    rows = [_swieca(T0 + timedelta(hours=4 * i), 100.0 * (0.99 ** i),
                    0.01, 0.01, cechy_kupuj()) for i in range(50)]
    assert kup_i_trzymaj({"AAA": rows}, "perp") < 0


def test_kup_i_trzymaj_odejmuje_koszty():
    """Kupienie i trzymanie tez kosztuje - jedno wejscie i jedno wyjscie.

    Bez tego porownanie bylo by nieuczciwe na korzysc odniesienia.
    """
    from services.engine.portfolio import kup_i_trzymaj

    plaski = [_swieca(T0 + timedelta(hours=4 * i), 100.0, 0.0, 0.0, cechy_kupuj())
              for i in range(20)]
    assert kup_i_trzymaj({"AAA": plaski}, "perp") < 0, \
        "na płaskim rynku kup-i-trzymaj musi wyjść na minus przez koszty"


def test_kup_i_trzymaj_drozszy_na_spocie_niz_na_perpetualu():
    from services.engine.portfolio import kup_i_trzymaj

    plaski = [_swieca(T0 + timedelta(hours=4 * i), 100.0, 0.0, 0.0, cechy_kupuj())
              for i in range(20)]
    assert kup_i_trzymaj({"AAA": plaski}, "spot") < kup_i_trzymaj({"AAA": plaski}, "perp")


def test_kup_i_trzymaj_usrednia_po_instrumentach():
    """Rowna waga: jeden instrument mocno rosnacy nie moze przeslonic reszty."""
    from services.engine.portfolio import kup_i_trzymaj

    rosnie = [_swieca(T0 + timedelta(hours=4 * i), 100.0 * (1.02 ** i),
                      0.01, 0.01, cechy_kupuj()) for i in range(30)]
    spada = [_swieca(T0 + timedelta(hours=4 * i), 100.0 * (0.98 ** i),
                     0.01, 0.01, cechy_kupuj()) for i in range(30)]
    tylko_rosnie = kup_i_trzymaj({"A": rosnie}, "perp")
    oba = kup_i_trzymaj({"A": rosnie, "B": spada}, "perp")
    assert oba < tylko_rosnie


# --- prowizja MUSI siedziec w zwrocie z transakcji, nie tylko w kapitale -----
def test_wyzsza_prowizja_obniza_sharpe():
    """Kanarek na blad, ktory przeszedl przez caly zestaw testow.

    Sharpe liczy sie ze zwrotow z transakcji i to ON decyduje, czy okno jest
    dodatnie - czyli o odsetku dodatnich okien i o statusie modelu. Wczesniejsza
    wersja odejmowala prowizje WYLACZNIE od krzywej kapitalu, wiec dwa przebiegi
    rozniace sie stawka taker dwuipolkrotnie (spot 0,100% wobec spot_eu 0,250%)
    dawaly identyczny Sharpe i identyczny odsetek dodatnich okien. Roznil sie
    tylko wykres kapitalu, ktorego nikt nie uzywa do werdyktu.
    """
    panele = {"AAA": panel(60, 100.0, 130.0)}
    gates = {s: (0.05, 0.05 * 0.45) for s in panele}
    atr = {s: 2.0 for s in panele}

    def przebieg(kind):
        res = simulate_portfolio(panele, DEFAULT_WEIGHTS, atr, gates, H, 0.05,
                                 LIMITY_LUZNE, kind=kind)
        return metrics(res, min_trades=1)

    tanio, drogo = przebieg("spot"), przebieg("spot_eu")
    assert tanio["n_trades"] == drogo["n_trades"] > 0, "porownujemy te same transakcje"
    assert drogo["sharpe"] < tanio["sharpe"], (
        f"wyzsza prowizja musi obnizyc Sharpe, jest {drogo['sharpe']} wobec {tanio['sharpe']}")
    assert drogo["avg_r"] < tanio["avg_r"]


def test_zwrot_z_transakcji_jest_po_prowizji_a_nie_przed():
    """Przy zerowym ruchu ceny transakcja musi byc STRATNA o dwie nogi prowizji."""
    from services.engine.koszty import koszty

    panele = {"AAA": panel(60, 100.0, 100.0)}
    gates = {s: (0.0, 0.0) for s in panele}
    atr = {s: 2.0 for s in panele}
    res = simulate_portfolio(panele, DEFAULT_WEIGHTS, atr, gates, H, 0.0,
                             LIMITY_LUZNE, kind="spot")
    if not res.trades:
        pytest.skip("brak transakcji na plaskiej cenie - nie ma czego mierzyc")
    k = koszty("spot")
    for t in res.trades:
        assert t["ret"] < 0, "transakcja bez ruchu ceny nie moze byc zyskowna"
        assert t["ret"] <= -1.5 * k.taker, (
            f"zwrot {t['ret']} nie zawiera obu nog prowizji ({k.taker} kazda)")


# --------------------------------------------------------------------------
# POROWNANIE Z KUP-I-TRZYMAJ MUSI ISC PRZY TEJ SAMEJ EKSPOZYCJI
#
# Odkryte 2026-09-07 na przebiegu nocnym. Strategia byla lepsza od kup-i-trzymaj
# w 17/39 okien horyzontu 4h - i byly to DOKLADNIE te okna, w ktorych rynek
# spadl (17/17), oraz zadne z tych, w ktorych rosl (0/22). Na horyzoncie 1h to
# samo: 48/48 i 0/40. Sto dwadziescia siedem okien, zero wyjatkow.
#
# Powod jest strukturalny, nie statystyczny: strategia nie moze zaangazowac
# wiecej niz `max_total_exposure`, czyli 15% kapitalu, a kup-i-trzymaj angazuje
# 100%. Przy takiej roznicy wynik porownania jest funkcja ZNAKU RYNKU i nie
# zawiera zadnej informacji o strategii.
# --------------------------------------------------------------------------

def test_srednia_ekspozycja_miesci_sie_miedzy_zerem_a_szczytem():
    panele = {f"I{i}": panel(40, 100.0, 130.0) for i in range(5)}
    limity = LIMITY_LUZNE | {
        "max_total_exposure": {"value": Decimal("15000"), "action": "block"}}
    res = uruchom(panele, limity)
    assert res.trades, "test bez transakcji nie mierzy ekspozycji"
    assert 0.0 < res.avg_exposure <= res.max_exposure + 1e-6
    assert res.avg_exposure <= 15000.0 + 1e-6


def test_srednia_ekspozycja_zeruje_sie_gdy_nie_ma_pozycji():
    panele = {"AAA": [dict(r, f=cechy_neutralne()) for r in panel(40, 100.0, 130.0)]}
    res = uruchom(panele, LIMITY_LUZNE, gate=0.9)
    assert not res.trades, "przy neutralnym sygnale i wysokiej bramce nie ma wejsc"
    assert res.avg_exposure == 0.0


def test_srednia_ekspozycja_liczy_sie_po_WSZYSTKICH_swiecach():
    """Kanarek na jedyny sposob, w jaki ta srednia moze byc zla po cichu.

    Gdyby liczyla sie wylacznie po swiecach Z POZYCJA, portfel wchodzacy raz na
    sto swiec wygladalby na w pelni zaangazowany - a wtedy odniesienie
    dopasowane ekspozycja byloby zawyzone dokladnie tam, gdzie strategia
    handluje najrzadziej.

    Test dokleja do panelu spokojny ogon, w ktorym nie ma zadnych pozycji.
    Kapital pracowal tyle samo co wczesniej, ale przez dwa razy dluzszy czas,
    wiec SREDNIA MUSI SPASC. Srednia liczona tylko po swiecach z pozycja
    nie drgnelaby.
    """
    baza = panel(40, 100.0, 130.0)
    ogon = [{"t": baza[-1]["t"] + timedelta(hours=4 * (i + 1)),
             "f": cechy_neutralne(), "price": 130.0,
             "high": 130.0 * 1.004, "low": 130.0 * 0.996}
            for i in range(40)]

    krotka = uruchom({"AAA": list(baza)}, LIMITY_LUZNE)
    dluga = uruchom({"AAA": baza + ogon}, LIMITY_LUZNE)

    assert krotka.avg_exposure > 0.0
    assert dluga.max_exposure == pytest.approx(krotka.max_exposure, rel=1e-9), \
        "ogon nie ma otwierac nowych pozycji - inaczej test mierzy co innego"
    assert dluga.avg_exposure < krotka.avg_exposure * 0.7, (
        f"srednia nie spadla po dolozeniu bezczynnego ogona: "
        f"{krotka.avg_exposure:.0f} -> {dluga.avg_exposure:.0f}")


def test_werdykt_bez_dopasowania_ekspozycji_zalezy_od_ZNAKU_RYNKU():
    """Ten sam kod strategii, dwa rynki: werdykt odwraca sie sam z siebie.

    Gdyby porownanie mierzylo umiejetnosc, nie musialoby wychodzic
    ujemnie na kazdym rosnacym rynku i dodatnio na kazdym spadajacym.
    """
    limity = LIMITY_LUZNE | {
        "max_total_exposure": {"value": Decimal("15000"), "action": "block"}}

    rosnacy = {f"I{i}": panel(40, 100.0, 130.0) for i in range(5)}
    spadajacy = {f"I{i}": panel(40, 130.0, 100.0) for i in range(5)}

    m_ros = metrics(uruchom(rosnacy, limity), min_trades=1)
    m_spa = metrics(uruchom(spadajacy, limity), min_trades=1)
    bh_ros = kup_i_trzymaj(rosnacy)
    bh_spa = kup_i_trzymaj(spadajacy)

    assert bh_ros > 0 > bh_spa, "panele maja isc w przeciwne strony"
    # Werdykt sprzed poprawki: przegrana na rosnacym, wygrana na spadajacym -
    # niezaleznie od tego, co strategia zrobila.
    assert m_ros["total_return"] < bh_ros
    assert m_spa["total_return"] > bh_spa


def test_odniesienie_dopasowane_ekspozycja_jest_porownywalne_co_do_rzedu():
    """Po dopasowaniu obie liczby opisuja ten sam kapital.

    Wlasciwa miara nie jest "strategia wygrywa", tylko "porownanie w ogole
    ma sens": odleglosc od odniesienia spada o rzad wielkosci.
    """
    limity = LIMITY_LUZNE | {
        "max_total_exposure": {"value": Decimal("15000"), "action": "block"}}
    rosnacy = {f"I{i}": panel(40, 100.0, 130.0) for i in range(5)}
    res = uruchom(rosnacy, limity)
    m = metrics(res, min_trades=1)

    udzial = m["avg_exposure"]
    assert 0.0 < udzial < 0.15, \
        f"ekspozycja {udzial:.2%} - przy limicie 15% wiecej byc nie moze"

    bh = kup_i_trzymaj(rosnacy)
    surowe = abs(m["total_return"] - bh)
    dopasowane = abs(m["total_return"] - bh * udzial)
    assert dopasowane < surowe / 3, (
        f"dopasowanie ekspozycja nie zblizylo liczb: surowo {surowe:.2f} pkt, "
        f"po dopasowaniu {dopasowane:.2f} pkt")


# ---------------------------------------------- rozklad ekspozycji na czynniki
#
# Srednia 1,28% przy limicie 15% (pomiar 2026-09-07) nie mowi, DLACZEGO kapital
# nie pracuje. Portfel poza rynkiem przez 90% czasu i portfel trzymajacy bez
# przerwy jedna mala pozycje daja te sama srednia, a wymagaja czegos zupelnie
# innego. Rozbicie ma sens tylko wtedy, gdy mnozy sie z powrotem.

def test_rozklad_ekspozycji_mnozy_sie_z_powrotem_do_sredniej():
    """TOZSAMOSC, nie przyblizenie.

    Gdyby ktorykolwiek licznik liczyl sie po innym zbiorze swiec niz pozostale,
    ten test by padl - a same liczby nadal wygladalyby sensownie i nikt by tego
    nie zauwazyl.
    """
    panele = {f"I{i}": panel(40, 100.0, 130.0) for i in range(5)}
    limity = LIMITY_LUZNE | {
        "max_total_exposure": {"value": Decimal("15000"), "action": "block"}}
    res = uruchom(panele, limity)
    assert res.trades, "test bez transakcji nie mierzy rozkladu"

    assert res.srednio_pozycji * res.sredni_notional == pytest.approx(
        res.avg_exposure, rel=1e-9), (
        f"{res.srednio_pozycji} x {res.sredni_notional} != {res.avg_exposure}")
    assert res.udzial_swiec_z_pozycja * res.pozycji_gdy_w_rynku == pytest.approx(
        res.srednio_pozycji, rel=1e-9)


def test_rozklad_odroznia_rzadkie_wejscia_od_malych_pozycji():
    """Kanarek na to, po co ten rozklad w ogole powstal.

    Dwa portfele o zblizonej sredniej ekspozycji, ale z zupelnie roznych
    powodow: jeden siedzi w rynku rzadko, drugi ciagle, za to malymi pozycjami.
    Sama srednia ich nie rozroznia.
    """
    ciagly = {f"I{i}": panel(40, 100.0, 130.0, okres=100) for i in range(4)}
    rzadki = {f"I{i}": panel(40, 100.0, 130.0, okres=2) for i in range(4)}
    limity = LIMITY_LUZNE | {
        "max_total_exposure": {"value": Decimal("15000"), "action": "block"}}

    c = uruchom(ciagly, limity)
    r = uruchom(rzadki, limity)
    assert c.trades and r.trades

    assert c.udzial_swiec_z_pozycja > r.udzial_swiec_z_pozycja, (
        f"sygnal ciagly ma siedziec w rynku dluzej: "
        f"{c.udzial_swiec_z_pozycja:.2f} vs {r.udzial_swiec_z_pozycja:.2f}")


def test_rozklad_zeruje_sie_bez_pozycji():
    """Dzielenie przez zero w diagnostyce jest gorsze niz brak diagnostyki:
    wywraca przebieg, ktory poza tym mial sie dobrze."""
    panele = {"AAA": [dict(r, f=cechy_neutralne()) for r in panel(40, 100.0, 130.0)]}
    res = uruchom(panele, LIMITY_LUZNE, gate=0.9)
    assert not res.trades
    assert res.srednio_pozycji == 0.0
    assert res.sredni_notional == 0.0
    assert res.pozycji_gdy_w_rynku == 0.0
    assert res.udzial_swiec_z_pozycja == 0.0


def test_TRZY_czynniki_z_wydruku_mnoza_sie_do_sredniej():
    """Kanarek na blad, ktory ten wydruk mial juz raz.

    Pierwsza wersja mnozyla `w_rynku` przez `srednio_pozycji` - a ta druga jest
    JUZ usredniona po swiecach pustych, wiec czynnik "ile czasu w rynku" wchodzil
    do iloczynu dwa razy. Zmierzone 1,29% wychodzilo z wydruku jako 0,56%,
    a linia czytala sie jak wyjasnienie.

    Do iloczynu wchodzi `pozycji_w_rynku` - liczba pozycji na swiece, w ktorej
    cokolwiek jest otwarte.
    """
    panele = {f"I{i}": panel(60, 100.0, 130.0) for i in range(6)}
    limity = LIMITY_LUZNE | {
        "max_total_exposure": {"value": Decimal("15000"), "action": "block"}}
    m = metrics(uruchom(panele, limity), min_trades=1)
    assert m["n_trades"], "test bez transakcji nie mierzy rozkladu"

    # Tolerancja z zaokraglenia w `metrics` (szesc miejsc), nie z niepewnosci
    # rachunku - w PortfolioResult tozsamosc jest dokladna i pilnuje jej
    # osobny test. Blad podwojnego liczenia myli sie o 2,5 raza, wiec ta
    # tolerancja go nie przepusci.
    iloczyn = m["w_rynku"] * m["pozycji_w_rynku"] * m["notional_na_pozycje"]
    assert iloczyn == pytest.approx(m["avg_exposure"], rel=1e-4), (
        f"{m['w_rynku']:.4f} x {m['pozycji_w_rynku']:.4f} x "
        f"{m['notional_na_pozycje']:.6f} = {iloczyn:.6f}, "
        f"a średnia to {m['avg_exposure']:.6f}")

    # I wprost: uzycie `srednio_pozycji` w tym miejscu MA byc bledem, dopoki
    # portfel bywa poza rynkiem. Bez tego warunku test przechodzilby takze dla
    # zlej wersji na panelu, ktory nigdy z rynku nie wychodzi.
    assert m["w_rynku"] < 1.0, "panel testowy ma miec swiece bez pozycji"
    zle = m["w_rynku"] * m["srednio_pozycji"] * m["notional_na_pozycje"]
    assert zle < m["avg_exposure"] * 0.99, (
        "zla wersja iloczynu ma dawac WYRAZNIE mniej - inaczej ten test "
        "nie odroznia jej od dobrej")


def test_wydruk_rozkladu_mnozy_sie_do_ZMIERZONEJ_sredniej():
    """Test na SKLEJENIE miar, nie na same miary.

    Poprzedni test pilnuje tozsamosci wewnatrz `metrics`. Ten pilnuje, ze
    trener sklada z tych miar wlasciwy iloczyn - a to osobna rzecz: wstawienie
    bledu podwojnego liczenia z powrotem do `train.py` przechodzilo caly zestaw
    testow symulacji, bo pomylka nie siedziala w symulacji.
    """
    from services.engine.train import opis_rozkladu, probka_rozkladu

    panele = {f"I{i}": panel(60, 100.0, 130.0, okres=3) for i in range(6)}
    limity = LIMITY_LUZNE | {
        "max_total_exposure": {"value": Decimal("15000"), "action": "block"}}
    m = metrics(uruchom(panele, limity), min_trades=1)
    assert m["n_trades"] and 0 < m["w_rynku"] < 1.0

    linie, iloczyn = opis_rozkladu([probka_rozkladu(m, instrumentow=6)])
    assert iloczyn == pytest.approx(m["avg_exposure"], rel=1e-4), (
        f"iloczyn z wydruku {iloczyn:.6f} != zmierzona średnia {m['avg_exposure']:.6f}")
    assert linie and "w rynku" in linie[0]


def test_wydruk_rozkladu_bez_probek_nie_wywraca_przebiegu():
    from services.engine.train import opis_rozkladu

    linie, iloczyn = opis_rozkladu([])
    assert linie == [] and iloczyn == 0.0
