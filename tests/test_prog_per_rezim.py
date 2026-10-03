"""Próg wejścia liczony PER REŻIM — czyli percentyl mierzony na jednej skali.

TŁO, ZMIERZONE NA PRODUKCJI (crypto/demo, 2026-09-08..2026-09-27 — od wdrożenia
renormalizacji pokrycia wag; wynik odtworzony jako
`sum(contribution)/sum(abs(weight))` po `journal.decision_contribution`, bo
`journal.decision` nie ma kolumny z wynikiem):

    reżim       decyzji   p88 reżimu   % ponad progiem 0,2798
    range           646       0,1573                      7,0
    high_vol        306       0,4304                     34,0
    trend_up        175       0,3187                     18,9
    news_shock        3       0,0095                      0,0

`ENTER_PCTL = 0.88` znaczy „12% najsilniejszych sytuacji". Zamiar nie był
dotrzymany w ŻADNYM reżimie, bo wagi są brane per reżim, więc skala wyniku
zależy od reżimu, a jedna liczba nie wyraża jednego percentyla na kilku skalach.

CZEGO TE TESTY PILNUJĄ

  1. Miara REAGUJE na wejście: dwa rozkłady o różnej skali muszą dać dwa różne
     progi, a odsetek przepuszczonych sygnałów musi w OBU wyjść równy zamiarowi.
     Test porównuje to z arytmetyką sprzed poprawki na tych samych próbkach —
     inaczej „12%" byłoby liczbą, o której nie wiadomo, czy cokolwiek znaczy.
  2. Mała próba NIE dostaje progu wspólnego jako zastępnika, ale wyjście dostaje
     zawsze.
  3. Pętla decyzyjna porównuje wynik z progiem TEGO reżimu, nie ze zbiorczą
     liczbą modelu.
  4. Wersja modelu bez progów per reżim zachowuje DAWNE zachowanie — bo odmowa
     wejść we wszystkich reżimach zatrzymałaby handel po cichu.

Kanarki detektorów są na końcu pliku: bez nich cisza testów strukturalnych
znaczyłaby „parser nic nie zrozumiał", a nie „wpięte poprawnie".
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from services.engine.bramki import (
    EXIT_RATIO, MIN_PROB_PROGU, Bramki, bramki_z_modelu, bramki_z_prob,
)
from services.engine.horizons import get as get_horizon
from services.engine.portfolio import simulate_portfolio, udzial_powtornych_wejsc
from services.engine.signals import REGIMES, SignalOutput

KORZEN = Path(__file__).resolve().parents[1]
LOOP = KORZEN / "services/engine/loop.py"
TRAIN = KORZEN / "services/engine/train.py"
PCTL = 0.88
H = get_horizon("4h")
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _proby_dwoch_skal(n: int = 400) -> dict[str, list[float]]:
    """Dwa rezimy o skalach rozniacych sie szesciokrotnie - jak na produkcji.

    Rozklady sa ODTWARZALNE (bez losowania), zeby liczby w asercjach dawaly sie
    sprawdzic recznie, a nie zalezaly od ziarna.
    """
    return {
        "range": [0.20 * i / (n - 1) for i in range(n)],
        "high_vol": [1.20 * i / (n - 1) for i in range(n)],
    }


def _odsetek_ponad(proby, prog):
    return sum(1 for v in proby if v >= prog) / len(proby)


def test_prog_per_rezim_dotrzymuje_zamiaru_w_OBU_rezimach():
    """12% znaczy 12% w kazdym rezimie - i to jest cala tresc poprawki."""
    proby = _proby_dwoch_skal()
    b = bramki_z_prob(proby, PCTL)

    for rezim, lista in proby.items():
        prog = b.wejscie(rezim)
        assert prog is not None, f"{rezim} ma {len(lista)} probek i nie dostal progu"
        przepuszczone = _odsetek_ponad(lista, prog)
        assert abs(przepuszczone - (1 - PCTL)) < 0.01, (
            f"{rezim}: przepuszczone {przepuszczone:.3f}, zamiar {1 - PCTL:.2f}")

    # SKALA JEST ROZNA, wiec progi TEZ musza sie roznic - i to szescio-, nie
    # kilkuprocentowo. Bez tego warunku test przeszedlby takze na progu
    # wspolnym, gdyby tylko oba rozklady mialy podobny percentyl.
    assert b.wejscie("high_vol") > 5 * b.wejscie("range")


def test_prog_WSPOLNY_lamie_zamiar_na_tych_samych_probkach():
    """Kanarek arytmetyki sprzed poprawki, liczony na TYCH SAMYCH danych.

    Bez tego warunku test wyzej mowi tylko "nasz percentyl jest percentylem",
    a nie "poprzedni sposob liczenia dawal co innego".
    """
    proby = _proby_dwoch_skal()
    stary = bramki_z_prob(proby, PCTL, per_rezim=False)

    prog = stary.wejscie("range")
    assert prog == stary.wejscie("high_vol"), "wariant `wspolny` ma dac jeden prog"

    w_range = _odsetek_ponad(proby["range"], prog)
    w_high = _odsetek_ponad(proby["high_vol"], prog)
    # Konsolidacja nie przepuszcza NIC (prog lezy powyzej calego jej rozkladu),
    # a wysoka zmiennosc przepuszcza dwa razy wiecej, niz zamierzono.
    assert w_range == 0.0, f"w `range` powinno przejsc 0%, jest {w_range:.3f}"
    assert w_high >= 2 * (1 - PCTL), (
        f"w `high_vol` przechodzi {w_high:.3f}, czyli nie dwa razy wiecej "
        f"niz zamierzone {1 - PCTL:.2f}")


def test_mala_proba_ODMAWIA_WEJSC_i_nie_podstawia_progu_wspolnego():
    """Kierunek bledu jest wybrany: brak wejsc, nie wejscie po cudzym progu.

    Prog wspolny jest wlasnie ta liczba, o ktorej zmierzono, ze w `high_vol`
    przepuszcza 34% zamiast 12%. Podstawienie go rzadkiemu rezimowi znaczyloby
    najluzniejszy prog tam, gdzie dowodow jest najmniej.
    """
    proby = _proby_dwoch_skal()
    proby["trend_down"] = [0.9] * (MIN_PROB_PROGU - 1)
    b = bramki_z_prob(proby, PCTL)

    assert b.wejscie("trend_down") is None, (
        "rezim z probka ponizej minimum dostal prog wejscia")
    # I to jest ta wartosc, ktorej NIE WOLNO tu podstawic.
    wspolny = bramki_z_prob(proby, PCTL, per_rezim=False).wejscie("trend_down")
    assert wspolny is not None and b.wejscie("trend_down") != wspolny
    # Liczba probek zostaje ZAPISANA - inaczej "brak progu" nie da sie odroznic
    # od "rezimu w oknie nie bylo".
    assert b.prob["trend_down"] == MIN_PROB_PROGU - 1
    assert b.prob["news_shock"] == 0


def test_jedna_probka_wiecej_JUZ_daje_prog():
    """Granica ma przebiegac dokladnie tam, gdzie mowi `MIN_PROB_PROGU`.

    Bez tej pary test wyzej przechodzilby takze wtedy, gdyby prog nie powstawal
    NIGDY - czyli gdyby silnik przestal wchodzic w ogole.
    """
    proby = {"trend_down": [i / MIN_PROB_PROGU for i in range(MIN_PROB_PROGU)]}
    b = bramki_z_prob(proby, PCTL)
    assert b.wejscie("trend_down") is not None
    assert bramki_z_prob({"trend_down": proby["trend_down"][:-1]},
                         PCTL).wejscie("trend_down") is None


def test_WYJSC_wolno_zawsze_takze_w_rezimie_bez_progu_wejscia():
    """Pozycja otwarta w reżimie skalibrowanym moze dozyc nieskalibrowanego.

    Gdyby `wyjscie()` oddawalo None albo zero, taka pozycja przestalaby sie
    zamykac na oslabieniu sygnalu - a to jest strata bez gornego konca.
    """
    proby = _proby_dwoch_skal()
    proby["news_shock"] = [0.5] * 3
    b = bramki_z_prob(proby, PCTL)

    assert b.wejscie("news_shock") is None
    wyjscie = b.wyjscie("news_shock")
    assert wyjscie > 0.0
    # Prog zapasowy to percentyl ze ZLACZONEJ proby razy EXIT_RATIO - czyli
    # dokladnie to, czym byl dotad prog wyjscia. Sprawdzamy WARTOSC, nie to,
    # ze cokolwiek wrocilo.
    wspolny = bramki_z_prob(proby, PCTL, per_rezim=False).wejscie("range")
    assert abs(wyjscie - wspolny * EXIT_RATIO) < 1e-12
    # A w rezimie skalibrowanym wyjscie idzie z progu TEGO rezimu.
    assert abs(b.wyjscie("range") - b.wejscie("range") * EXIT_RATIO) < 1e-12
    assert b.wyjscie("high_vol") > b.wyjscie("range")


def test_stara_wersja_modelu_zachowuje_DAWNE_zachowanie():
    """Wersja bez progow per rezim nie moze zatrzymac handlu po cichu.

    Gdyby `bramki_z_modelu` traktowala starszy format jak "rezim bez probek",
    silnik przestalby wchodzic we WSZYSTKICH rezimach do najblizszego udanego
    treningu - a silnik bez wejsc wyglada tak samo jak spokojny rynek.
    """
    b = bramki_z_modelu({"enter_threshold": 0.2798, "exit_threshold": 0.1259})
    assert b.powod == "wersja_modelu_bez_progow_per_rezim"
    for rezim in REGIMES:
        assert b.wejscie(rezim) == 0.2798
        assert b.wyjscie(rezim) == 0.1259


def test_nowa_wersja_modelu_daje_ROZNE_progi_i_odmawia_brakujacym():
    """Wartosci, nie obecnosc kluczy: prog `range` ma byc INNY niz `high_vol`."""
    b = bramki_z_modelu({
        "enter_threshold": 0.2798,
        "enter_threshold_per_regime": {"range": 0.1573, "high_vol": 0.4304},
        "enter_threshold_windows": {"range": 38, "high_vol": 21},
    })
    assert b.wejscie("range") == 0.1573
    assert b.wejscie("high_vol") == 0.4304
    assert abs(b.wyjscie("range") - 0.1573 * EXIT_RATIO) < 1e-9
    # Rezim, ktorego trener nie skalibrowal, NIE dostaje progu zbiorczego.
    assert b.wejscie("trend_down") is None
    assert b.wejscie("news_shock") is None
    # ...ale wyjscie dostaje, i to zapisane w wersji modelu.
    assert b.wyjscie("news_shock") == round(0.2798 * EXIT_RATIO, 6)
    assert b.prob["range"] == 38
    # Opis do logu musi NAZWAC rezimy bez progu - inaczej brak wejsc jest cichy.
    assert "news_shock" in b.opis() and "trend_down" in b.opis()


def test_brak_modelu_to_zalozenia_poczatkowe_dla_wszystkich_rezimow():
    b = bramki_z_modelu(None)
    assert b.powod == "zalozenia_poczatkowe"
    assert {b.wejscie(r) for r in REGIMES} == {0.25}


# ============================ SYMULACJA: PROG STOSOWANY PER REZIM
#
# Bramki podajemy wprost, a rezim i sygnaly kladziemy w `cache`, zeby test
# mierzyl SAM WYBOR PROGU, a nie klasyfikacje rezimu ani liczenie sygnalow -
# te maja wlasne testy.

LIMITY_LUZNE = {
    "max_daily_loss":        {"value": Decimal("99"),     "action": "killswitch"},
    "max_drawdown":          {"value": Decimal("99"),     "action": "killswitch"},
    "max_orders_per_hour":   {"value": Decimal("1000"),   "action": "block"},
    "max_position_notional": {"value": Decimal("5000"),   "action": "downsize"},
    "max_total_exposure":    {"value": Decimal("999999"), "action": "block"},
    "max_leverage":          {"value": Decimal("9999"),   "action": "block"},
}


def _panel(n: int = 12) -> list[dict]:
    out = []
    for i in range(n):
        cena = 100.0 + i
        out.append({"t": T0 + timedelta(hours=4 * i), "f": {"atr_pct": 1.0},
                    "price": cena, "high": cena * 1.01, "low": cena * 0.99})
    return out


def _cache(rezim: str, wynik: float, n: int = 12) -> list[tuple]:
    return [(rezim, {"trend_follow": SignalOutput(wynik, 1.0, {})})] * n


def _przebieg(bramki: Bramki) -> dict[str, int]:
    """Ile transakcji powstalo na instrumencie w kazdym z dwoch rezimow."""
    panele = {"RANGEUSDT": _panel(), "HIGHUSDT": _panel()}
    cache = {"RANGEUSDT": _cache("range", 0.30),
             "HIGHUSDT": _cache("high_vol", 0.30)}
    wagi = {r: {"trend_follow": 1.0} for r in REGIMES}
    res = simulate_portfolio(panele, wagi, {"RANGEUSDT": 2.0, "HIGHUSDT": 2.0},
                             bramki, H, 0.05, LIMITY_LUZNE,
                             instrument_ids={"RANGEUSDT": 1, "HIGHUSDT": 2},
                             cache=cache)
    wynik = {"RANGEUSDT": 0, "HIGHUSDT": 0, "bez_progu": res.pominiete_bez_progu}
    for t in res.trades:
        wynik[t["symbol"]] += 1
    return wynik


def test_symulacja_wchodzi_TAM_gdzie_prog_rezimu_pozwala():
    """Ten sam wynik 0,30 w dwoch rezimach: wchodzi tam, gdzie prog jest nizszy."""
    luzny_range = Bramki({"range": (0.15, 0.0675), "high_vol": (0.50, 0.225)})
    wynik = _przebieg(luzny_range)
    assert wynik["RANGEUSDT"] >= 1, "prog 0,15 nie przepuscil wyniku 0,30"
    assert wynik["HIGHUSDT"] == 0, "prog 0,50 przepuscil wynik 0,30"

    # ODWROTNIE: ta sama symulacja, zamienione progi. Bez tej pary test
    # przechodzilby takze na kodzie, ktory bierze prog z pierwszego rezimu
    # w slowniku albo z minimum po rezimach.
    luzny_high = Bramki({"range": (0.50, 0.225), "high_vol": (0.15, 0.0675)})
    odwrotnie = _przebieg(luzny_high)
    assert odwrotnie["HIGHUSDT"] >= 1 and odwrotnie["RANGEUSDT"] == 0


def test_symulacja_NIE_WCHODZI_w_rezimie_bez_progu_i_liczy_to():
    """Brak kalibracji musi byc widoczny jako LICZBA, nie jako brak transakcji."""
    bez_high = Bramki({"range": (0.15, 0.0675)})
    wynik = _przebieg(bez_high)
    assert wynik["RANGEUSDT"] >= 1
    assert wynik["HIGHUSDT"] == 0
    assert wynik["bez_progu"] > 0, (
        "wejscia w rezimie bez progu odpadly, ale nikt ich nie policzyl")


# ====================== PETLA DECYZYJNA: Z CZYM POROWNUJE WYNIK
#
# `run_once` wola gielde, kernel i pomiar kapitalu naraz, wiec zamiast stawiac
# ja na atrapie czytamy DRZEWO SKLADNIOWE i pytamy o jedna rzecz: jaka wielkosc
# stoi po drugiej stronie porownania z |wynikiem|. Detektor ma wlasny kanarek
# nizej - inaczej jego cisza znaczylaby "parser nic nie zrozumial".


def _operandy_porownan_z_wynikiem(zrodlo: str) -> set[str]:
    """Z czym zrodlo porownuje `abs(score)`."""
    out = set()
    for w in ast.walk(ast.parse(zrodlo)):
        if not isinstance(w, ast.Compare):
            continue
        if not (isinstance(w.left, ast.Call) and isinstance(w.left.func, ast.Name)
                and w.left.func.id == "abs"
                and ast.unparse(w.left) == "abs(score)"):
            continue
        for cmp_ in w.comparators:
            out.add(ast.unparse(cmp_))
    return out


def _zrodla_progu_rezimu(zrodlo: str) -> set[str]:
    """Skad bierze sie `prog_wejscia_rezimu` / `prog_wyjscia_rezimu`."""
    out = set()
    for w in ast.walk(ast.parse(zrodlo)):
        if isinstance(w, ast.Assign) and any(
                isinstance(c, ast.Name) and c.id.startswith("prog_w")
                and c.id.endswith("_rezimu") for c in w.targets):
            out.add(ast.unparse(w.value))
    return out


def test_petla_porownuje_wynik_z_progiem_TEGO_rezimu():
    zrodlo = LOOP.read_text(encoding="utf-8")
    operandy = _operandy_porownan_z_wynikiem(zrodlo)

    assert "prog_wejscia_rezimu" in operandy, (
        f"petla nie porownuje |wyniku| z progiem rezimu, tylko z: {operandy}")
    assert "prog_wyjscia_rezimu" in operandy, (
        "prog wyjscia tez musi byc progiem rezimu - inaczej wejscie i wyjscie "
        "mierza sie na dwoch skalach")
    assert not {o for o in operandy if "model[" in o}, (
        f"petla nadal porownuje wynik ze zbiorcza liczba modelu: {operandy}")

    zrodla = _zrodla_progu_rezimu(zrodlo)
    assert "model['bramki'].wejscie(regime)" in zrodla, (
        f"prog wejscia nie pochodzi z bramek modelu per rezim: {zrodla}")
    assert "model['bramki'].wyjscie(regime)" in zrodla


def test_petla_ma_galaz_dla_rezimu_BEZ_progu():
    """Prog None nie moze wpasc do porownania - `abs(score) >= None` rzuca.

    Gorsze od wyjatku byloby jednak podstawienie liczby: rezim bez kalibracji
    handlowalby wtedy progiem, ktorego nikt nie zmierzyl.
    """
    zrodlo = LOOP.read_text(encoding="utf-8")
    warunki = [ast.unparse(w.test) for w in ast.walk(ast.parse(zrodlo))
               if isinstance(w, ast.If)]
    assert any("prog_wejscia_rezimu is None" in c for c in warunki), (
        "brak galezi rozstrzygajacej rezim bez skalibrowanego progu")
    # ...i musi ona stawiac powod, ktory baza dopuszcza (migracja 0067).
    assert "rezim_bez_progu" in zrodlo


# ================== SIODMA MIARA NA OKNO: ODSETEK POWTORNYCH WEJSC
#
# Mierzy hipoteze o `EXIT_RATIO`, nie prog wejscia: czy pozycja jest zamykana
# i natychmiast otwierana z powrotem. Ma NIE dac sie wziac za czestotliwosc
# handlu, i to jest osobno sprawdzone nizej.


def _transakcja(symbol: str, otwarta_h: int, zamknieta_h: int) -> dict:
    return {"symbol": symbol, "ret": 0.001, "conf": 0.5, "side": 1,
            "opened_at": T0 + timedelta(hours=otwarta_h),
            "closed_at": T0 + timedelta(hours=zamknieta_h)}


def test_powrot_ponizej_trzech_swiec_liczy_sie_powtornym_wejsciem():
    # Swieca 4h: wyjscie o 04:00, powrot o 08:00 - jedna swieca odstepu.
    t = [_transakcja("AAA", 0, 4), _transakcja("AAA", 8, 12)]
    assert udzial_powtornych_wejsc(t, H.bar_minutes) == 0.5


def test_powrot_po_trzech_swiecach_JUZ_sie_nie_liczy():
    """Granica ma przebiegac tam, gdzie mowi definicja miary.

    Bez tej pary poprzedni test przechodzilby takze na kodzie liczacym KAZDE
    kolejne wejscie w ten sam instrument - czyli mierzacym cos innego.
    """
    t = [_transakcja("AAA", 0, 4), _transakcja("AAA", 16, 20)]
    assert udzial_powtornych_wejsc(t, H.bar_minutes) == 0.0


def test_miara_NIE_JEST_czestotliwoscia_handlu():
    """Dwa okna o tej samej liczbie transakcji i skrajnie roznym odsetku."""
    gesto = [_transakcja("AAA", 0, 4), _transakcja("AAA", 8, 12),
             _transakcja("AAA", 16, 20), _transakcja("AAA", 24, 28)]
    # TEN SAM instrument i ta sama liczba transakcji, tylko rozsuniete w czasie:
    # odsetek musi spasc do zera. Wariant z czterema roznymi instrumentami nie
    # rozstrzygalby tego sam - kod liczacy KAZDE kolejne wejscie w ten sam
    # instrument dalby tam zero rowniez.
    rozsuniete = [_transakcja("AAA", 0, 4), _transakcja("AAA", 40, 44),
                  _transakcja("AAA", 80, 84), _transakcja("AAA", 120, 124)]
    rozne = [_transakcja("AAA", 0, 4), _transakcja("BBB", 8, 12),
             _transakcja("CCC", 16, 20), _transakcja("DDD", 24, 28)]
    assert len(gesto) == len(rozsuniete) == len(rozne)
    assert udzial_powtornych_wejsc(gesto, H.bar_minutes) == 0.75
    assert udzial_powtornych_wejsc(rozsuniete, H.bar_minutes) == 0.0
    assert udzial_powtornych_wejsc(rozne, H.bar_minutes) == 0.0


def test_odstep_liczy_sie_w_SWIECACH_a_nie_w_minutach():
    """Ta sama para transakcji, dwa horyzonty, dwie odpowiedzi."""
    t = [_transakcja("AAA", 0, 4), _transakcja("AAA", 8, 12)]
    godzinowy = get_horizon("1h")
    assert udzial_powtornych_wejsc(t, H.bar_minutes) == 0.5
    assert udzial_powtornych_wejsc(t, godzinowy.bar_minutes) == 0.0


def test_okno_bez_transakcji_oddaje_None_a_nie_zero():
    """Zero znaczy "zmierzono i wyszlo zero" - a tu nie ma czego mierzyc."""
    assert udzial_powtornych_wejsc([], H.bar_minutes) is None


def test_trener_zapisuje_te_miare_dla_KAZDEGO_okna():
    """Miara policzona tylko w `range` nie dalaby sie z niczym porownac."""
    zrodlo = TRAIN.read_text(encoding="utf-8")
    assert "udzial_powtornych_wejsc(oos_res.trades, h.bar_minutes)" in zrodlo, (
        "trener nie liczy odsetka powtornych wejsc z transakcji OOS")
    assert 'agg["udzial_powtornych_wejsc"] = powtorne' in zrodlo, (
        "miara nie trafia do `agg`, czyli nie zapisze sie w learn.wf_metric")
    # ...i nie wolno jej liczyc tylko dla wybranego rezimu.
    fragment = zrodlo.split("powtorne = udzial_powtornych_wejsc", 1)[0]
    assert "dominant" not in fragment.rsplit("agg = {", 1)[-1], (
        "miara policzona warunkowo wzgledem rezimu nie da sie porownac miedzy nimi")


def test_miara_powrotow_idzie_TAKZE_DO_WYDRUKU_a_nie_tylko_do_bazy():
    """`--sucho` NIE ZAPISUJE NIC, a wlasnie `--sucho` sluzy do porownan.

    Miara zapisana wylacznie do `learn.wf_metric` odpowiada na pytanie o
    EXIT_RATIO dopiero po nocnym przebiegu, i tylko temu, kto zna nazwe
    metryki. Zmierzone 2026-09-27: porownanie `--progi wspolny` kontra
    `--progi per-rezim` (oba `--sucho`) nie pokazalo tej liczby ani raz.
    Warunek rozstrzygajacy postawiono ZAWCZASU i wymaga wartosci PO OKNACH,
    razem z rezimem i liczba transakcji - czyli w tej samej linii wydruku.
    """
    zrodlo = TRAIN.read_text(encoding="utf-8")
    # od refaktoru T2 linie sklada `opis_okna`, a drukuje ja `train()`
    i = zrodlo.index('f"  okno {idx:>2d}')
    linia = zrodlo[i:zrodlo.index("\n", zrodlo.index("reżim=", i))]
    assert "powroty" in linia, (
        "wydruk okna nie podaje odsetka powrotow. Przy `--sucho` nie ma go "
        "wtedy NIGDZIE - ani w bazie, ani na ekranie")
    # Okno bez transakcji nie ma tej miary w `agg` (oddaje None), wiec wydruk
    # musi to znosic, a nie rzucac KeyError na pierwszym pustym oknie.
    fragment = zrodlo[zrodlo.index("powroty = (f", i - 3000):i]
    assert '"udzial_powtornych_wejsc" in agg' in fragment, (
        "wydruk siega po miare bez sprawdzenia, czy okno ja ma - okno bez "
        "transakcji wywroci caly przebieg na KeyError")


# ================================== KANARKI DETEKTOROW
#
# Testy strukturalne wyzej czytaja drzewo skladniowe. Gdyby detektor niczego nie
# rozumial, milczalby tak samo, jak przy kodzie poprawnym - wiec kazdy z nich
# musi jeszcze ZLAPAC kod sprzed poprawki.

PETLA_SPRZED_POPRAWKI = """
def run_once():
    if position:
        if not same_way or abs(score) < model["exit"]:
            action = "exit"
    elif abs(score) >= model["enter"] and confidence >= model["min_conf"]:
        action = "enter_long" if score > 0 else "enter_short"
    elif abs(score) < model["enter"]:
        action = "hold"
        powod_holdu = "slaby_wynik"
"""


def test_KANAREK_detektor_lapie_petle_sprzed_poprawki():
    operandy = _operandy_porownan_z_wynikiem(PETLA_SPRZED_POPRAWKI)
    assert operandy == {"model['exit']", "model['enter']"}, (
        f"detektor nie rozpoznal porownan sprzed poprawki: {operandy}")
    assert "prog_wejscia_rezimu" not in operandy
    assert not _zrodla_progu_rezimu(PETLA_SPRZED_POPRAWKI), (
        "detektor melduje progi per rezim w kodzie, w ktorym ich nie ma")
    warunki = [ast.unparse(w.test) for w in ast.walk(ast.parse(PETLA_SPRZED_POPRAWKI))
               if isinstance(w, ast.If)]
    assert not any("prog_wejscia_rezimu is None" in c for c in warunki)


def test_KANAREK_prog_wspolny_NIE_dotrzymuje_zamiaru_na_probie_produkcyjnej():
    """Odsetki zmierzone na produkcji, odtworzone z p88 reżimów.

    Test sprawdza to, co jest sedno zadania: przy JEDNYM progu dla wszystkich
    reżimów rozkłady o różnych skalach dają różne odsetki przepuszczonych
    sygnałów - i żaden z nich nie jest zamierzonym 12%.
    """
    # Rozklady jednostajne o p88 rownym zmierzonemu na produkcji.
    proby = {
        "range": [0.1573 / 0.88 * i / 645 for i in range(646)],
        "high_vol": [0.4304 / 0.88 * i / 305 for i in range(306)],
        "trend_up": [0.3187 / 0.88 * i / 174 for i in range(175)],
    }
    wspolny = bramki_z_prob(proby, PCTL, per_rezim=False).wejscie("range")
    odsetki = {r: _odsetek_ponad(l, wspolny) for r, l in proby.items()}

    # Rozrzut miedzy rezimami, nie odleglosc od 12% w kazdym z nich: przy
    # jednostajnym przyblizeniu rozkladu `trend_up` wypada blisko zamiaru
    # przypadkiem, a rzecz dotyczy tego, ze JEDNA liczba nie moze opisac
    # czterech skal naraz.
    assert max(odsetki.values()) - min(odsetki.values()) > 0.2, (
        f"prog wspolny dawalby podobne odsetki we wszystkich rezimach: {odsetki}")
    assert odsetki["range"] < 0.12 < odsetki["high_vol"], (
        f"kierunek rozjazdu inny niz zmierzony na produkcji: {odsetki}")

    # ...a per rezim kazdy dostaje swoje 12%.
    per = bramki_z_prob(proby, PCTL)
    for rezim, lista in proby.items():
        assert abs(_odsetek_ponad(lista, per.wejscie(rezim)) - 0.12) < 0.01


# ========================= TRENER: PROBY GRUPOWANE PO REZIMIE


def test_proby_wynikow_grupuja_po_rezimie_a_nie_po_instrumencie():
    """Populacja percentyla to (okno, rezim) - wszystkie instrumenty razem.

    Skale wyniku rozsuwaja WAGI, a wagi sa per rezim i te same dla kazdego
    instrumentu, wiec to rezim jest wymiarem, wzdluz ktorego percentyl przestaje
    znaczyc to samo. Przy okazji znosi to rozjazd trening-produkcja: do
    2026-09-27 trener symulowal okno progami per instrument, a produkcja
    dostawala MEDIANE z nich.
    """
    from services.engine.train import proby_wynikow

    panele = {"AAA": _panel(6), "BBB": _panel(6)}
    cache = {"AAA": _cache("range", 0.10, 6), "BBB": _cache("high_vol", 0.90, 6)}
    wagi = {r: {"trend_follow": 1.0} for r in REGIMES}

    proby = proby_wynikow(panele, wagi, {"AAA": 2.0, "BBB": 2.0}, H, cache)

    assert set(proby) == {"range", "high_vol"}
    assert proby["range"] == [0.10] * 6
    assert proby["high_vol"] == [0.90] * 6


def test_proby_wynikow_odsiewaja_niska_pewnosc_tak_jak_produkcja():
    """Percentyl liczony na szerszej populacji niz ta, ktora go potem mija,
    opisywalby inny rozklad niz ten, do ktorego prog jest przykladany."""
    from services.engine.train import MIN_CONF, proby_wynikow

    panele = {"AAA": _panel(4)}
    slaba = [("range", {"trend_follow": SignalOutput(0.5, MIN_CONF / 2, {})})] * 4
    proby = proby_wynikow(panele, {r: {"trend_follow": 1.0} for r in REGIMES},
                          {"AAA": 2.0}, H, {"AAA": slaba})
    assert proby == {}


# ================================== PROGI NA WAGACH, KTORYMI SIE HANDLUJE (T4)
#
# Audyt Fable F-2.1: przy `--wybor ic` progi liczyly sie na `bazowe`, a handel
# szedl wagami IC - po 28.09 udzial decyzji ponad progiem `range` 0,0% (n=72)
# wobec zamiaru 12%. Teraz `train.bramki_na_wagach` liczy prog na `best_w`.

def _okno_treningowe_syntetyczne():
    import sys
    sys.path.insert(0, str(KORZEN / "tests"))
    from panel_syntetyczny import plik_syntetyczny

    from services.engine import train as tr
    from services.engine.portfolio import precompute_panels

    plik = plik_syntetyczny()
    od = plik["od"]
    do = od + timedelta(days=H.train_days)
    panele = {s: [r for r in p if od <= r["t"] < do] for s, p in plik["panele"].items()}
    progi_atr = {s: tr.atr_threshold(p) for s, p in panele.items()}
    cache = precompute_panels(panele, progi_atr, H)
    martwe = tr.martwe_sygnaly(cache)
    return tr, panele, progi_atr, cache, martwe


def test_prog_na_wagach_IC_przepuszcza_12_procent_na_oknie_treningowym():
    """Kryterium T4: udzial ponad progiem na oknie treningowym w [10%; 14%]
    w kazdym rezimie z >= 50 probkami - na wagach, ktorymi sie handluje."""
    tr, panele, progi_atr, cache, martwe = _okno_treningowe_syntetyczne()
    wagi = tr.ic_weights(panele, cache, martwe, H.ic_horizon_bars)
    bramki = tr.bramki_okna(panele, wagi, progi_atr, H, cache)
    proby = tr.proby_wynikow(panele, wagi, progi_atr, H, cache)
    sprawdzone = 0
    for rezim, lista in proby.items():
        if len(lista) < MIN_PROB_PROGU:
            continue
        prog = bramki.wejscie(rezim)
        udzial = sum(1 for x in lista if x >= prog) / len(lista)
        assert 0.10 <= udzial <= 0.14, (rezim, udzial, len(lista))
        sprawdzone += 1
    assert sprawdzone >= 2


def test_trener_liczy_progi_ic_na_best_w_a_argmax_na_bazowych():
    """Sklad: dla `ic` progi powstaja PO wyborze wag (`bramki_na_wagach`),
    dla `argmax` przed nim, na zalozeniach - jeden prog dla wszystkich
    kandydatow okna."""
    import inspect

    from services.engine import train as tr
    zrodlo = inspect.getsource(tr.walk_forward)
    assert 'przygotuj_okno(kt, okno, z_bramkami=(wybor != "ic"))' in zrodlo
    assert "gates=bramki_na_wagach(kt, dane, best_w)" in zrodlo
    assert "dane.train_slices, wagi" in inspect.getsource(tr.bramki_na_wagach)
