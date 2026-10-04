"""Strategia "momentum + filtr trendu" na demo (decyzja "A", 2026-10-04, E-1).

Cztery rzeczy, na ktorych stoi sens tego przebiegu jako POMIARU egzekucji:

  * parametry sa TE, ktore zmierzyl H-3 - i zmienic sie nie moga bez wpisu
    w rejestrze hipotez (test przypina liczby, a nie odwoluje sie do stalej);
  * sklad liczony na zywo jest tym samym skladem, ktory liczyl harness H-3
    na tych samych swiecach - inaczej porownanie z modelem pesymistycznym
    dotyczyloby innej strategii;
  * budzet to 15% kapitalu (5% na pozycje) i nie rosnie razem z limitem konta;
  * petla podejmuje decyzje regula, nie modelem - i zapisuje, ktora regula.
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from services.engine import loop
from services.engine import r4_momentum as r4
from services.engine import strategia_momentum as sm
from services.engine.horizons import get as get_horizon
from services.engine.risk_rules import RiskVerdict, wielkosc_pozycji
from tests import test_run_once as _petla
from tests.test_run_once import CENA, INSTRUMENTY, POZYCJA, cykl, plany

# Fikstura swiata `test_run_once` (atrapa bazy, zegar, risk kernel) - ta sama,
# na ktorej chodza testy ensemble, zeby obie strategie przechodzily te sama petle.
swiat = _petla.swiat

H = get_horizon("4h")
UTC = timezone.utc


# ------------------------------------------------------------ parametry

def test_parametry_ZAMROZONE_na_wartosciach_H3():
    """Liczby wpisane wprost, nie `== r4.PARAMETRY_R4`: zmiana w r4_momentum.py
    ma wywrocic ten test, bo to bylaby inna regula niz zmierzona w H-3."""
    assert dict(sm.PARAMETRY) == {"okno_momentum_dni": 30, "pominiete_dni": 1,
                                  "top": 3, "ema_filtra_dni": 50,
                                  "opoznienie_min": 20}
    assert sm.UDZIAL_LACZNY == D("0.15")
    assert sm.UDZIAL_POZYCJI == D("0.05")
    with pytest.raises(TypeError):
        sm.PARAMETRY["top"] = 5        # widok tylko do odczytu


def test_nazwa_strategii_to_wartosc_z_CHECK_migracji():
    from tests.atrapa_bazy import MIGRACJE
    tresc = "\n".join(p.read_text(encoding="utf-8") for p in MIGRACJE)
    assert f"'{sm.NAZWA}'" in tresc
    assert loop.STRATEGIE == {"model": "ensemble", "momentum": sm.NAZWA}


def test_migracja_0091_ma_lock_timeout_i_transakcje():
    from tests.atrapa_bazy import KORZEN
    tresc = (KORZEN / "db/migrations/0091_strategia_momentum.sql").read_text()
    assert "SET LOCAL lock_timeout" in tresc
    assert tresc.strip().startswith("--") and "BEGIN;" in tresc
    assert tresc.rstrip().endswith("COMMIT;")


# ------------------------------------------------------------ budzet 15%

@pytest.mark.parametrize("kapital", ["10000", "100063.09", "99999.99", "12345.67",
                                     "1000000", "0.07"])
def test_trzy_wejscia_NIGDY_nie_przekraczaja_15_procent(kapital):
    k = D(kapital)
    b = sm.budzet_pozycji(k)
    assert b == (k * D("0.05")).quantize(D("0.01"), rounding="ROUND_DOWN")
    assert 3 * b <= k * D("0.15")
    assert sm.miesci_sie(k, 2 * b, b)


def test_budzet_pozycji_przy_10000_to_500():
    assert sm.budzet_pozycji(D("10000")) == D("500.00")


def test_wlasny_sufit_niezalezny_od_limitu_konta():
    assert sm.miesci_sie(D("10000"), D("1000"), D("500"))          # rowno 15%
    assert not sm.miesci_sie(D("10000"), D("1000.01"), D("500"))
    # Pozycje, ktore urosly, zajmuja sufit wartoscia rynkowa.
    assert not sm.miesci_sie(D("10000"), D("1100"), D("500"))


# ------------------------------------------------------------ kalendarz

def test_wejscie_tylko_na_swiecy_konczacej_dobe():
    assert sm.swieca_decyzyjna(datetime(2026, 10, 3, 20, tzinfo=UTC), H)
    for godz in (0, 4, 8, 12, 16):
        assert not sm.swieca_decyzyjna(datetime(2026, 10, 4, godz, tzinfo=UTC), H)


def test_cel_na_kazdej_swiecy_dnia_z_tej_samej_polnocy():
    polnoc = datetime(2026, 10, 4, tzinfo=UTC)
    assert sm.chwila_decyzji(datetime(2026, 10, 3, 20, tzinfo=UTC), H) == polnoc
    for godz in (0, 4, 8, 12, 16):
        assert sm.chwila_decyzji(datetime(2026, 10, 4, godz, tzinfo=UTC), H) == polnoc


# ------------------------------------------------------------ regula

def _cel(filtr=True, sklad=("ADAUSDC",), sygnaly=None):
    return sm.Cel(datetime(2026, 10, 4, tzinfo=UTC), True, filtr,
                  sklad if filtr else (), sygnaly or {"ADAUSDC": 0.2, "XRPUSDC": 0.1},
                  "BTCUSDC", 60000.0, 58000.0)


def test_akcja_wszystkie_galezie():
    cel = _cel()
    dlugi = {"side": "long"}
    assert sm.akcja(cel, None, "ADAUSDC", True) == ("enter_long", None)
    assert sm.akcja(cel, None, "ADAUSDC", False) == ("hold", "poza_decyzja_dobowa")
    assert sm.akcja(cel, None, "XRPUSDC", True) == ("hold", "poza_skladem")
    assert sm.akcja(cel, dlugi, "ADAUSDC", False) == ("hold", "trzyma_pozycje")
    # Wyjscie na KAZDEJ swiecy, nie tylko o polnocy.
    assert sm.akcja(cel, dlugi, "XRPUSDC", False) == ("exit", None)
    assert sm.akcja(cel, {"side": "short"}, "ADAUSDC", True) == ("exit", None)
    gotowka = _cel(filtr=False)
    assert sm.akcja(gotowka, None, "ADAUSDC", True) == ("hold", "filtr_trendu")
    assert sm.akcja(gotowka, dlugi, "ADAUSDC", True) == ("exit", None)


def _losowy_rynek(symbole, dni=160, ziarno=7):
    """Swiece 4h z blądzeniem losowym - rozne rankingi z dnia na dzien."""
    rng = random.Random(ziarno)
    t0 = datetime(2025, 3, 1, tzinfo=UTC)
    panele = {}
    for s in symbole:
        cena, dryf, rows = 100.0, rng.uniform(-0.002, 0.003), []
        for i in range(dni * 6):
            cena *= 1 + dryf + rng.gauss(0, 0.01)
            rows.append({"t": t0 + timedelta(hours=4 * i), "price": cena})
        panele[s] = rows
    return panele


def test_sklad_na_zywo_ROWNY_skladowi_harnessu_H3():
    """Ten sam rynek, te same chwile: `wyznacz_cel` (petla) i
    `r4_momentum.decyzje_okna` (pomiar H-3) maja dac identyczny filtr,
    sygnaly i top 3. Petla dostaje WYLACZNIE swiece domkniete do chwili
    decyzji - jak z bazy - wiec rownosc jest tez testem braku przyszlosci."""
    symbole = ["ADAUSDC", "BTCUSDC", "DOGEUSDC", "ETHUSDC", "SOLUSDC", "XRPUSDC"]
    panele = _losowy_rynek(symbole)
    koniec = panele["BTCUSDC"][-1]["t"] + timedelta(hours=4)
    od, do = koniec - timedelta(days=60), koniec
    indeksy = {s: {r["t"]: r for r in rows} for s, rows in panele.items()}
    filtr = r4.Filtr(panele["BTCUSDC"], 240, r4.PARAMETRY_R4["ema_filtra_dni"])
    decyzje = r4.decyzje_okna(panele, indeksy, None, symbole, od, do, filtr, 240)
    assert len(decyzje) > 50
    filtry = set()
    for d in decyzje:
        przeszlosc = {s: [r for r in rows if r["t"] + timedelta(hours=4) <= d.t]
                      for s, rows in panele.items()}
        cel = sm.wyznacz_cel(przeszlosc, d.t, 240)
        assert cel.filtr == d.filtr, d.t
        assert cel.sygnaly == pytest.approx(d.sygnaly)
        assert cel.sklad == (tuple(r4.ranking(d.sygnaly, 3)) if d.filtr else ())
        filtry.add(d.filtr)
    assert filtry == {True, False}, "rynek testowy nie sprawdza obu stanow filtra"


def test_bez_BTC_albo_z_krotka_historia_GOTOWKA():
    panele = _losowy_rynek(["ADAUSDC", "XRPUSDC"], dni=60)
    t = panele["ADAUSDC"][-1]["t"] + timedelta(hours=4)
    cel = sm.wyznacz_cel(panele, t)
    assert not cel.filtr and cel.sklad == () and "brak BTC" in cel.powod
    krotka = _losowy_rynek(["BTCUSDC", "ADAUSDC"], dni=40)
    t = krotka["BTCUSDC"][-1]["t"] + timedelta(hours=4)
    cel = sm.wyznacz_cel(krotka, t)
    assert not cel.filtr and "za krótka historia" in cel.powod


class KursorSwiec:
    """Odpowiada na `SQL_ZAMKNIECIA` swiecami z `panele` (z `n` 1h na swiece)."""

    def __init__(self, panele, ids, n_ostatniej=4):
        self.panele, self.ids, self.n_ostatniej = panele, ids, n_ostatniej
        self.wynik = []

    def execute(self, sql, p):
        assert sql == sm.SQL_ZAMKNIECIA and p["tf"] == "1h"
        sym = self.ids[p["iid"]]
        rows = [r for r in self.panele[sym] if p["od"] <= r["t"] < p["do"]]
        self.wynik = [{"t": r["t"], "price": r["price"],
                       "n": self.n_ostatniej if r is rows[-1] else 4} for r in rows]

    def fetchall(self):
        return self.wynik


def test_cel_z_bazy_czeka_na_domknieta_swiece_BTC():
    symbole = ["ADAUSDC", "BTCUSDC", "XRPUSDC"]
    panele = _losowy_rynek(symbole, dni=420)
    ids = dict(enumerate(symbole, start=1))
    instr = [{"id": i, "symbol": s} for i, s in ids.items()]
    polnoc = panele["BTCUSDC"][-1]["t"] + timedelta(hours=4)
    assert polnoc.hour == 0
    bar = polnoc - timedelta(hours=4)
    niedomknieta = sm.cel_z_bazy(KursorSwiec(panele, ids, n_ostatniej=3), instr, bar, H)
    assert not niedomknieta.kompletny and "3/4" in niedomknieta.powod
    po_oknie = sm.cel_z_bazy(KursorSwiec(panele, ids, n_ostatniej=3), instr, bar, H,
                             czekaj=False)
    assert po_oknie.kompletny and "niepełnych danych" in po_oknie.powod
    cel = sm.cel_z_bazy(KursorSwiec(panele, ids), instr, bar, H)
    assert cel.kompletny and cel.chwila == polnoc
    # Pelna historia (420 dni) wobec 400 dni z bazy: ten sam sklad i filtr,
    # EMA rozni sie o ulamek 1e-6 - to jest rachunek z HISTORIA_FILTRA_DNI.
    pelny = sm.wyznacz_cel({s: [r for r in rows if r["t"] < polnoc]
                            for s, rows in panele.items()}, polnoc)
    assert (cel.filtr, cel.sklad, cel.sygnaly) == (pelny.filtr, pelny.sklad,
                                                   pelny.sygnaly)
    assert cel.btc_ema == pytest.approx(pelny.btc_ema, rel=1e-6)
    assert cel.btc_ema != pelny.btc_ema, "test nie sprawdza obcięcia historii"


# ------------------------------------------------------------ petla

# 00:05 UTC: swieca 20:00-24:00 (konczaca dobe) zamknela sie 5 minut temu.
POLNOC = datetime(2026, 10, 4, 0, 5, tzinfo=UTC)
RANO = datetime(2026, 10, 4, 4, 5, tzinfo=UTC)


@pytest.fixture
def momentum(swiat, monkeypatch):
    """Swiat `test_run_once` + cel strategii i ekspozycja z risk kernela."""
    swiat.cel = sm.Cel(datetime(2026, 10, 4, tzinfo=UTC), True, True,
                       ("ADAUSDC",), {"ADAUSDC": 0.21, "XRPUSDC": -0.04},
                       "BTCUSDC", 61000.0, 59000.0)
    swiat.ekspozycja = D(0)
    swiat.teraz = POLNOC
    swiat.cele = []

    def cel_z_bazy(cur, instrumenty, bar_start, h, czekaj=True):
        swiat.cele.append(bar_start)
        swiat.czekaj = czekaj
        return swiat.cel

    def bez_modelu(*a, **kw):
        raise AssertionError("strategia momentum przeczytała model")

    monkeypatch.setattr(sm, "cel_z_bazy", cel_z_bazy)
    monkeypatch.setattr(loop, "active_model", bez_modelu)
    monkeypatch.setattr(loop, "latest_features", bez_modelu)
    monkeypatch.setattr(loop, "evaluate", bez_modelu)
    monkeypatch.setattr(loop.RiskKernel, "ekspozycja",
                        lambda self: swiat.ekspozycja, raising=False)
    return swiat


def test_na_polnoc_wejscie_w_sklad_po_5_procent_kapitalu(momentum):
    stats, cur = cykl(momentum, strategia="momentum")

    d = {x["iid"]: x for x in momentum.decyzje}
    assert d[1]["action"] == "enter_long" and d[1]["powod_holdu"] is None
    assert d[2]["action"] == "hold" and d[2]["powod_holdu"] == "poza_skladem"
    assert {x["strategia"] for x in momentum.decyzje} == {"momentum_filtr_trendu"}
    (plan,) = plany(cur)
    _, qty = wielkosc_pozycji(D("500.00"), 1.0, CENA)    # 5% z 10 000
    assert plan[1] == 1 and plan[2] == "buy" and plan[3] == qty == D("200")
    assert stats["strategia"] == "momentum" and stats["wejscia"] == 1
    assert momentum.cele == [datetime(2026, 10, 3, 20, tzinfo=UTC)], \
        "cel liczony raz na cykl"
    assert momentum.czekaj is True, "w oknie wejścia czekamy na domkniętą świecę BTC"


def test_poza_polnoca_bez_wejsc_ale_WYJSCIE_idzie(momentum):
    momentum.teraz = RANO
    momentum.pozycje = {2: POZYCJA}
    stats, cur = cykl(momentum, strategia="momentum")

    assert momentum.czekaj is False, "poza oknem wejścia czekanie więziłoby wyjścia"
    d = {x["iid"]: x for x in momentum.decyzje}
    assert d[1]["action"] == "hold" and d[1]["powod_holdu"] == "poza_decyzja_dobowa"
    assert d[2]["action"] == "exit"
    (plan,) = plany(cur)
    assert plan[1] == 2 and plan[2] == "sell" and '"rodzaj": "wyjscie"' in plan[5]
    assert stats["wejscia"] == 0 and stats["wyjscia"] == 1


def test_wlasny_sufit_15_procent_blokuje_wejscie(momentum, monkeypatch):
    """Ekspozycja 1 200 przy kapitale 10 000: 1 200 + 500 > 1 500. Odmowa
    PRZED risk kernelem - limit konta moglby kiedys byc luzniejszy."""
    momentum.ekspozycja = D("1200")
    sprawdzone = []
    monkeypatch.setattr(loop.RiskKernel, "check",
                        lambda self, *a, **kw: sprawdzone.append(a)
                        or RiskVerdict("allow", qty=a[2]))
    stats, cur = cykl(momentum, strategia="momentum")

    d = {x["iid"]: x for x in momentum.decyzje}
    assert d[1]["action"] == "enter_long" and d[1]["verdict"] == "block"
    assert plany(cur) == [] and sprawdzone == []
    assert stats["zablokowane"] == 1 and stats["wejscia"] == 0


def test_filtr_wylaczony_wszystko_wychodzi_nic_nie_wchodzi(momentum):
    momentum.cel = sm.Cel(datetime(2026, 10, 4, tzinfo=UTC), True, False, (),
                          {"ADAUSDC": 0.21, "XRPUSDC": -0.04}, "BTCUSDC",
                          57000.0, 59000.0)
    momentum.pozycje = {1: POZYCJA}
    stats, cur = cykl(momentum, strategia="momentum")

    d = {x["iid"]: x for x in momentum.decyzje}
    assert d[1]["action"] == "exit"
    assert d[2]["action"] == "hold" and d[2]["powod_holdu"] == "filtr_trendu"
    (plan,) = plany(cur)
    assert plan[2] == "sell"


def test_niedomknieta_swieca_BTC_ODKLADA_decyzje(momentum):
    momentum.cel = sm.Cel(datetime(2026, 10, 4, tzinfo=UTC), False,
                          powod="świeca niedomknięta")
    stats, cur = cykl(momentum, strategia="momentum")

    assert momentum.decyzje == [] and plany(cur) == []
    assert stats["odlozone_momentum"] == 2 and stats["rozwazone"] == 0


def test_killswitch_wstrzymuje_wejscie_strategii(momentum):
    momentum.killswitch = {"id": 5, "reason": "obsunięcie"}
    stats, cur = cykl(momentum, strategia="momentum")

    d = {x["iid"]: x for x in momentum.decyzje}
    assert d[1]["action"] == "hold" and d[1]["powod_holdu"] == "wstrzymanie_ryzykiem"
    assert plany(cur) == [] and stats["wstrzymanie_killswitch_id"] == 5


def test_strategia_model_zapisuje_ensemble(swiat):
    cykl(swiat)
    assert {x["strategia"] for x in swiat.decyzje} == {"ensemble"}


def test_nieznana_strategia_i_zly_horyzont_to_blad(swiat):
    with pytest.raises(ValueError, match="nieznana strategia"):
        cykl(swiat, strategia="b2")
    from tests.test_run_once import Kursor, Polaczenie
    conn = Polaczenie(Kursor([dict(i) for i in INSTRUMENTY]))
    with pytest.raises(ValueError, match="świecy 4h"):
        asyncio.run(loop.run_once(conn, "crypto", "demo", h=get_horizon("1h"),
                                  verbose=False, strategia="momentum"))


def test_momentum_ODMAWIA_startu_poza_demo(monkeypatch):
    monkeypatch.setattr("sys.argv", ["loop.py", "--strategia", "momentum",
                                     "--mode", "live"])
    monkeypatch.setenv("DATABASE_URL", "postgresql://nie-wolno-sie-polaczyc")
    with pytest.raises(SystemExit, match="tylko z --mode demo"):
        asyncio.run(loop.main())


def test_chart_wlacza_momentum_TYLKO_w_silniku_crypto_demo():
    import yaml

    from tests.atrapa_bazy import KORZEN
    workers = yaml.safe_load((KORZEN / "charts/trader/values.yaml").read_text())["workers"]
    z_momentum = {n for n, w in workers.items()
                  if "--strategia momentum" in " ".join(w.get("command", []))}
    assert z_momentum == {"engine"}
    komenda = " ".join(workers["engine"]["command"])
    assert "--mode demo" in komenda and "--mode live" not in komenda
    assert "--likwiduj-po-killswitchu" not in komenda
