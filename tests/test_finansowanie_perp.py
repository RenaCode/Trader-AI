"""Koszt finansowania perpetuala w symulacji portfelowej.

DLACZEGO TEN PLIK ISTNIEJE

`services/engine/koszty.py` mowi o tym wprost od 2026-09-04 i nikt tego nie
domknal: "Prawdziwy perpetual, placacy finansowanie, wypadlby jeszcze gorzej -
a tego kosztu symulacja NIE NALICZA (fees_paid zbiera wylacznie prowizje)".

To nie jest brak precyzji, tylko brak POZYCJI W RACHUNKU. Finansowanie placi
sie co osiem godzin od CALEGO notionalu, niezaleznie od tego, czy pozycja
zarabia. Strategia trzymajaca pozycje przez dobe placi je trzy razy. Przy
przewadze rzedu 0,12% na transakcje jest to skladnik tego samego rzedu co
prowizja - a prowizje ten projekt juz raz liczyl zle i to wystarczylo, zeby
wynik zmienil znak.

Kierunek bledu jest przy tym JEDNOSTRONNY dla strony dlugiej i to czyni go
grozniejszym niz sama skala: finansowanie na krypto jest historycznie w wiekszosci
dodatnie (dlugie placa krotkim), wiec symulacja pomijajaca je ZAWYZA wynik
strony dlugiej i ZANIZA wynik strony krotkiej. Czyli myli sie dokladnie w tym
porownaniu, dla ktorego caly wariant perp powstal.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services.engine.horizons import get as get_horizon
from services.engine.koszty import koszty
from services.engine.portfolio import Egzekucja, simulate_portfolio
from services.engine.signals import DEFAULT_WEIGHTS

H = get_horizon("4h")
# Start w polnoc UTC: chwile wyplaty finansowania (00/08/16 UTC) wypadaja wtedy
# na granicach swiec 4-godzinnych i test liczy je JAWNIE, zamiast zgadywac.
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

LIMITY_LUZNE = {
    "max_daily_loss":        {"value": Decimal("99"),     "action": "killswitch"},
    "max_drawdown":          {"value": Decimal("99"),     "action": "killswitch"},
    "max_orders_per_hour":   {"value": Decimal("1000"),   "action": "block"},
    "max_position_notional": {"value": Decimal("5000"),   "action": "downsize"},
    "max_total_exposure":    {"value": Decimal("999999"), "action": "block"},
    "max_leverage":          {"value": Decimal("100"),    "action": "block"},
}


def cechy(kierunek: str, stawka: float) -> dict:
    """Komplet cech dajacy silny sygnal w zadana strone, ze STAWKA FINANSOWANIA.

    `stawka` jest w PROCENTACH na osiem godzin - w tych jednostkach cecha
    siedzi w bazie (`services/ingest/features.py` zapisuje `last * 100`).
    """
    baza = {"atr_pct": 1.0, "rsi": 50.0, "range_pos": 0.5, "volume_z": 0.0,
            "news_sent": 0.0, "funding_rate": stawka, "funding_z": 0.0}
    if kierunek == "kup":
        return baza | {"ema_ratio": 1.01, "ret_short": 20.0, "ret_mid": 40.0,
                       "ret_long": 80.0}
    return baza | {"ema_ratio": 0.99, "ret_short": -20.0, "ret_mid": -40.0,
                   "ret_long": -80.0, "rel_strength": -0.9}


def panel_plaski(n: int, kierunek: str, stawka: float,
                 cena: float = 100.0) -> list[dict]:
    """Panel o STALEJ cenie. Caly wynik pochodzi wtedy z kosztow, nie z rynku.

    To jest cala konstrukcja tych testow: przy plaskiej cenie zwrot brutto
    wynosi zero, wiec roznica miedzy wariantami jest DOKLADNIE ta pozycja
    rachunku, ktorej szukamy. Panel z ruchem ceny mieszalby jedno z drugim
    i test przechodzilby przy dowolnej stawce.
    """
    return [{"t": T0 + timedelta(hours=4 * i), "f": cechy(kierunek, stawka),
             "price": cena, "high": cena * 1.03, "low": cena * 0.97}
            for i in range(n)]


def uruchom(panele, kind: str, **kw):
    gates = {s: (0.05, 0.02) for s in panele}
    return simulate_portfolio(panele, DEFAULT_WEIGHTS, {s: 2.0 for s in panele},
                              gates, H, 0.05, LIMITY_LUZNE, kind=kind,
                              egzekucja=Egzekucja(), **kw)


# --------------------------------------------------------------- jednostki
def test_stawka_finansowania_na_panelu_jest_W_PROCENTACH_nie_w_ulamku():
    """KANAREK NA POMYLKE STUKROTNA.

    `services/ingest/features.py` zapisuje `funding_rate` jako `last * 100`,
    gdzie `last` to surowa stawka z Bybita (ulamek, rzedu 1e-5..1e-4). Cecha
    na panelu jest wiec w PROCENTACH na osiem godzin.

    Gdyby symulacja wzieta ja za ulamek, doliczylaby koszt sto razy za duzy
    i kazdy wariant perp wyszedlby glęboko ujemny - "zmierzone", bez jednej
    awarii. Pomylka w druga strone jest rownie cicha i rownie kosztowna.

    Ten test wiaze jednostke z FUNKCJA, ktora ja stosuje, a nie z komentarzem.
    """
    from services.engine.koszty import stawka_finansowania_na_ulamek

    # 0,01% na 8 h - typowa stawka bazowa Bybita (surowo 0,0001).
    assert stawka_finansowania_na_ulamek(0.01) == pytest.approx(0.0001)
    assert stawka_finansowania_na_ulamek(-0.05) == pytest.approx(-0.0005)


def test_tylko_perpetual_placi_finansowanie():
    assert koszty("perp").finansowanie
    assert not koszty("spot").finansowanie
    assert not koszty("spot_eu").finansowanie


# ------------------------------------------------------------- naliczanie
def test_dluga_pozycja_na_perpetualu_PLACI_dodatnie_finansowanie():
    """Dodatnia stawka znaczy: dlugie placa krotkim. Przy plaskiej cenie
    jedynym zrodlem straty jest wiec prowizja i finansowanie."""
    bez = uruchom({"AAA": panel_plaski(40, "kup", 0.0)}, "perp")
    z_kosztem = uruchom({"AAA": panel_plaski(40, "kup", 0.05)}, "perp")

    assert z_kosztem.finansowanie_zaplacone > 0, \
        "długa pozycja przy dodatniej stawce musi zapłacić finansowanie"
    assert bez.finansowanie_zaplacone == 0
    assert len(z_kosztem.trades) == len(bez.trades), \
        "stawka finansowania nie ma prawa zmienić tego, KTÓRE pozycje powstają"


def test_krotka_pozycja_na_perpetualu_DOSTAJE_dodatnie_finansowanie():
    """Znak jest cala trescia tej pozycji rachunku.

    Gdyby finansowanie naliczalo sie obu stronom jednakowo, wariant perp
    wygladalby gorzej, niz jest - a to wlasnie jego przewage nad spotem
    mierzymy. Krotka noga na dodatnim fundingu INKASUJE.
    """
    res = uruchom({"AAA": panel_plaski(40, "sprzedaj", 0.05)}, "perp")

    assert res.trades, "panel musi otworzyć choć jedną krótką pozycję"
    assert all(t["ret"] < 1 for t in res.trades)
    assert res.finansowanie_zaplacone < 0, \
        "krótka pozycja przy dodatniej stawce finansowanie INKASUJE, nie płaci"


def test_finansowanie_siedzi_W_ZWROCIE_Z_TRANSAKCJI_a_nie_tylko_w_krzywej():
    """TEN SAM BLAD, KTORY POPELNIONO JUZ RAZ NA PROWIZJI.

    `portfolio.py` nosi przy polu `ret` komentarz o tym, ze prowizja odejmowana
    wylacznie od krzywej kapitalu zostawiala Sharpe'a liczonego PRZED kosztami -
    a to on decyduje o werdykcie okna. Finansowanie odjete tylko od krzywej
    powtorzyloby te wade co do joty.
    """
    bez = uruchom({"AAA": panel_plaski(40, "kup", 0.0)}, "perp")
    z_kosztem = uruchom({"AAA": panel_plaski(40, "kup", 0.05)}, "perp")

    assert len(bez.trades) == len(z_kosztem.trades)
    zwroty_bez = [t["ret"] for t in bez.trades]
    zwroty_z = [t["ret"] for t in z_kosztem.trades]
    assert sum(zwroty_z) < sum(zwroty_bez), \
        "koszt finansowania musi obniżyć ZWROT Z TRANSAKCJI, nie tylko kapitał"


def test_dluzsze_trzymanie_placi_wiecej_razy():
    """Finansowanie jest kosztem CZASU, nie kosztem transakcji.

    Prowizja zalezy od liczby zlecen, finansowanie od tego, ile wyplat
    przetrwala pozycja. Symulacja, ktora naliczylaby je raz na transakcje,
    myliaby dwie rozne rzeczy - i faworyzowala strategie trzymajace dlugo.
    """
    krotkie = uruchom({"AAA": panel_plaski(12, "kup", 0.05)}, "perp")
    dlugie = uruchom({"AAA": panel_plaski(60, "kup", 0.05)}, "perp")
    assert dlugie.finansowanie_zaplacone > krotkie.finansowanie_zaplacone * 3


def test_spot_nie_placi_finansowania_mimo_stawki_na_panelu():
    """Cecha `funding_rate` jest na panelu instrumentu SPOTOWEGO (bierze sie
    z perpetuala tej samej pary - to sygnal, nie koszt). Naliczenie jej
    spotowi byloby wzieciem cechy za pozycje rachunku."""
    res = uruchom({"AAA": panel_plaski(40, "kup", 0.05)}, "spot")
    assert res.finansowanie_zaplacone == 0


# ------------------------------------------------------------ fail-closed
def test_perp_BEZ_stawki_finansowania_na_panelu_PRZERYWA_zamiast_liczyc_zero():
    """BRAK DANEJ NIE JEST ZEREM.

    `compute_funding` nie zapisuje ANI JEDNEGO wiersza, gdy instrument nie ma
    `meta.funding_from` albo gdy w `market.funding_rate` nie ma historii. Panel
    bez tej cechy wyglada wtedy dokladnie tak samo jak panel ze stawka zerowa -
    i symulacja policzylaby wariant perp z zerowym kosztem finansowania,
    raportujac liczbe, ktora wyglada na zmierzona.

    To jest ten sam ksztalt co "milczace zero" przy uzgodnieniu sald: cisza
    mowiaca "sprawdzone i w porzadku" o czyms, czego sprawdzic sie nie da.
    """
    panel = panel_plaski(40, "kup", 0.0)
    for r in panel:
        del r["f"]["funding_rate"]

    with pytest.raises(ValueError, match="finansowan"):
        uruchom({"AAA": panel}, "perp")


def test_brak_stawki_na_panelu_SPOTOWYM_nie_przeszkadza():
    """Kanarek do poprzedniego: spot finansowania nie placi, wiec brak cechy
    nie ma prawa go zatrzymac."""
    panel = panel_plaski(40, "kup", 0.0)
    for r in panel:
        del r["f"]["funding_rate"]
    res = uruchom({"AAA": panel}, "spot")
    assert res.trades


# ------------------------------------------ rozdzielenie nog dlugiej i krotkiej
def test_transakcja_niesie_STRONE_zeby_dalo_sie_zmierzyc_krotka_noge():
    """BEZ TEGO POLA CALE ZADANIE NIE MA ODPOWIEDZI.

    Audyt zmierzyl, ze ze 180 sygnalow ponad progiem 135 (75%) to sygnaly
    KROTKIE, odrzucone, bo silnik handluje spotem. Pytanie "ile one byly warte"
    wymaga oddzielenia wyniku krotkiej nogi od dlugiej - a po samym zwrocie
    nie da sie tego zrobic: stratny long i stratny short wygladaja identycznie.
    """
    res = uruchom({"AAA": panel_plaski(40, "sprzedaj", 0.0)}, "perp")
    assert res.trades
    assert all(t["side"] == -1 for t in res.trades), \
        "panel z sygnałem sprzedaży musi dać transakcje po stronie krótkiej"

    dlugie = uruchom({"AAA": panel_plaski(40, "kup", 0.0)}, "perp")
    assert all(t["side"] == 1 for t in dlugie.trades)


def test_transakcja_niesie_WLASNE_finansowanie():
    """`ret` juz zawiera finansowanie, ale nie da sie z niego wyciagnac. Bez
    osobnego pola nie odpowiemy, czy krotka noga zarabia na ruchu ceny, czy na
    inkasowaniu stawki - a to dwie rozne strategie i dwa rozne ryzyka."""
    res = uruchom({"AAA": panel_plaski(40, "sprzedaj", 0.05)}, "perp")
    assert res.trades
    assert all(t["finansowanie"] <= 0 for t in res.trades), \
        "krótka noga przy dodatniej stawce inkasuje, więc finansowanie jest ujemne"
    assert sum(t["finansowanie"] for t in res.trades) == pytest.approx(
        res.finansowanie_zaplacone), \
        "suma po transakcjach musi zgadzać się z sumą globalną"


def test_domyslne_limity_NIE_BLOKUJA_wariantu_perp():
    """KANAREK NA LIMIT DZWIGNI USTAWIONY ZA CIASNO.

    `train.py --kind perp` chodzi na `DEFAULT_LIMITS`. Gdyby `max_leverage`
    zostal tam kiedys dobrany ponizej `max_total_exposure`, caly walk-forward
    wariantu perp dawalby ZERO transakcji - a to wyglada dokladnie tak samo jak
    strategia, ktora nie znajduje sygnalu. Pomiar, ktory nie moze wejsc
    w pozycje, nie jest pomiarem strategii.
    """
    from services.engine.risk_rules import DEFAULT_LIMITS

    res = simulate_portfolio(
        {"AAA": panel_plaski(40, "sprzedaj", 0.01)}, DEFAULT_WEIGHTS,
        {"AAA": 2.0}, {"AAA": (0.05, 0.02)}, H, 0.05, DEFAULT_LIMITS,
        kind="perp", egzekucja=Egzekucja())
    assert res.trades, (
        "domyślne limity muszą pozwolić wariantowi perp wejść w pozycję — "
        f"blocked={res.blocked}, pominięte shorty={res.pominiete_shorty}")


# --------------------------------------------------------- liczba wyplat
def test_wyplaty_licza_sie_co_osiem_godzin_o_00_08_16_UTC():
    """Chwile wyplaty sa STALE i wyznaczone przez gielde, nie przez nasza siatke.

    Bybit oglasza funding o 00:00, 08:00 i 16:00 UTC. Naliczanie "ulamka
    stawki na swiece" dalo by inny wynik dla tego samego okresu przy innym
    horyzoncie - a koszt nie zalezy od tego, jak czesto my patrzymy.
    """
    from services.engine.portfolio import wyplaty_finansowania

    # Doba to dokladnie trzy wyplaty.
    assert wyplaty_finansowania(T0, T0 + timedelta(hours=24)) == 3
    # Przedzial otwarty z lewej: wyplata w chwili wejscia juz nas nie dotyczy.
    assert wyplaty_finansowania(T0, T0 + timedelta(hours=8)) == 1
    assert wyplaty_finansowania(T0 + timedelta(hours=1),
                                T0 + timedelta(hours=7)) == 0
    assert wyplaty_finansowania(T0 + timedelta(hours=1),
                                T0 + timedelta(hours=9)) == 1
    # Okno zerowe nie placi nic.
    assert wyplaty_finansowania(T0, T0) == 0


# ----------------------------------------------- poslizg jako ZALOZENIE, nie stala
def test_poslizg_da_sie_NADPISAC_zeby_pokazac_wrazliwosc_wyniku():
    """POSLIZG MA DZIS ZERO MIERZALNYCH OBSERWACJI.

    Wszystkie piec wypelnien gieldowych na produkcji ma `exec.order.limit_price`
    rowne cenie wypelnienia co do grosza - nie dlatego, ze poslizgu nie bylo,
    tylko dlatego, ze do wdrozenia z 2026-09-23 17:58 kolumna "cena wyslana"
    byla nadpisywana cena wykonania. Obserwacji jest wiec ZERO, nie piec.

    Kazdy wynik policzony na jednej wartosci tego zalozenia wyglada jak pomiar
    i nie da sie go obronic. Ten parametr istnieje po to, zeby dalo sie pokazac
    ZAKRES - a ten test pilnuje, ze nadpisanie naprawde dziala, bo parametr,
    ktory jest ignorowany, jest gorszy od jego braku: sugeruje, ze wrazliwosc
    zostala sprawdzona.
    """
    wspolne = ({"AAA": panel_plaski(40, "kup", 0.0)}, DEFAULT_WEIGHTS,
               {"AAA": 2.0}, {"AAA": (0.05, 0.02)}, H, 0.05, LIMITY_LUZNE)
    bez = simulate_portfolio(*wspolne, kind="perp",
                             egzekucja=Egzekucja(limit=False), poslizg=0.0)
    duzy = simulate_portfolio(*wspolne, kind="perp",
                              egzekucja=Egzekucja(limit=False), poslizg=0.002)

    assert bez.poslizg == 0.0 and duzy.poslizg == 0.002, \
        "symulacja ma raportować ten poślizg, którym faktycznie liczyła"
    assert len(bez.trades) == len(duzy.trades), \
        "poślizg nie ma prawa zmienić tego, KTÓRE pozycje powstają"
    assert sum(t["ret"] for t in duzy.trades) < sum(t["ret"] for t in bez.trades), \
        "większy poślizg musi obniżyć zwrot z transakcji"


def test_domysl_poslizgu_zostaje_ZALOZENIEM_Z_KOSZTOW():
    """Kanarek: `poslizg=None` nie moze cicho wyzerowac poslizgu. Domysl ma
    dalej brac liczbe z `koszty.py`, inaczej dodanie tego parametru zmieniloby
    kazdy dotychczasowy pomiar."""
    from services.engine.koszty import POSLIZG_MAKER, POSLIZG_TAKER

    wspolne = ({"AAA": panel_plaski(40, "kup", 0.0)}, DEFAULT_WEIGHTS,
               {"AAA": 2.0}, {"AAA": (0.05, 0.02)}, H, 0.05, LIMITY_LUZNE)
    rynek = simulate_portfolio(*wspolne, kind="perp",
                               egzekucja=Egzekucja(limit=False))
    limit = simulate_portfolio(*wspolne, kind="perp",
                               egzekucja=Egzekucja(limit=True))
    assert rynek.poslizg == POSLIZG_TAKER
    assert limit.poslizg == POSLIZG_MAKER
