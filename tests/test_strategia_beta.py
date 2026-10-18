"""Regula "beta z filtrem trendu" (SPY, SMA 10 mies.) na demo equity - decyzja D-3.

Czego pilnuja te testy:

  * parametry sa ZAMROZONE - liczby wpisane wprost, zmiana to nowa decyzja;
  * zamkniecie miesieczne to zamkniecie OSTATNIEJ SESJI miesiaca (czas NY),
    z ostatniej swiecy 1h sesji regularnej - swiece przed- i posesyjne,
    weekendy i swieta nie przesuwaja go;
  * filtr X > SMA, a brak kompletu 10 miesiecy (albo dziura na koncu miesiaca)
    to gotowka z powodem;
  * BRAK ZAGLADANIA W PRZYSZLOSC: nic z miesiaca decyzji nie zmienia celu;
  * wejscie tylko na pierwszej swiecy sesji, w calych akcjach, 95% kapitalu;
    wyjscie na kazdej swiecy; pozostalosci spoza reguly wychodza;
  * petla nie czyta modelu, bramka `--wymagaj-modelu` jej nie blokuje,
    a poza demo regula odmawia startu.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal as D

import pytest

from services.engine import loop
from services.engine import strategia_beta as sb
from services.engine.horizons import get as get_horizon
from services.engine.sesja import STREFA_US, StanSesji
from tests import test_run_once as _petla
from tests.test_run_once import KAPITAL, POZYCJA, cykl, plany

swiat = _petla.swiat

H1 = get_horizon("1h")
UTC = timezone.utc
SESJA = StanSesji(otwarta=True, zrodlo="zegar giełdy", venue="alpaca")


# ------------------------------------------------------------ parametry

def test_parametry_ZAMROZONE():
    """Liczby wpisane wprost: zmiana w module ma wywrocic ten test."""
    assert dict(sb.PARAMETRY) == {"symbol": "SPY", "okno_sma_miesiecy": 10,
                                  "udzial_kapitalu": "0.95"}
    assert sb.SYMBOL == "SPY" and sb.OKNO_SMA == 10 and sb.UDZIAL == D("0.95")
    assert sb.TIMEFRAME_ZRODLA == "1h"
    with pytest.raises(TypeError):
        sb.PARAMETRY["okno_sma_miesiecy"] = 12


def test_nazwa_strategii_w_STRATEGIE_i_w_CHECK_migracji():
    from tests.atrapa_bazy import MIGRACJE
    tresc = "\n".join(p.read_text(encoding="utf-8") for p in MIGRACJE)
    assert f"'{sb.NAZWA}'" in tresc and sb.NAZWA == "beta_filtr_trendu"
    assert loop.STRATEGIE["beta"] == sb.NAZWA
    assert loop.REGULY["beta"] is sb


def test_migracja_0100_zmienia_limity_TYLKO_equity_demo_i_nie_rusza_tradeable():
    from tests.atrapa_bazy import KORZEN
    tresc = (KORZEN / "db/migrations/0100_strategia_beta.sql").read_text()
    assert "SET LOCAL lock_timeout" in tresc
    assert tresc.strip().startswith("--") and "BEGIN;" in tresc
    assert tresc.rstrip().endswith("COMMIT;")
    sql = "\n".join(linia for linia in tresc.splitlines()
                    if not linia.lstrip().startswith("--"))
    zmiany = [z for z in sql.split(";") if "UPDATE risk.limit_def" in z]
    assert len(zmiany) == 4
    for z in zmiany:
        assert "engine = 'equity'" in z and "mode = 'demo'" in z, z
    nazwy = " ".join(zmiany)
    for limit in ("max_total_exposure", "max_position_notional", "max_daily_loss",
                  "max_drawdown", "min_equity"):
        assert f"'{limit}'" in nazwy
    assert "tradeable" not in sql, "zmiana tradeable ruszylaby odcisk uniwersum"
    for powod in sb.POWODY_HOLDU:
        assert f"'{powod}'" in sql


# ------------------------------------------------------------ kalendarz

def test_pierwsza_swieca_sesji_lato_i_zima():
    # Lato (EDT): otwarcie 13:30 UTC -> swieca 13:00-14:00.
    assert sb.swieca_decyzyjna(datetime(2026, 10, 16, 13, tzinfo=UTC), H1)
    for g in (12, 14, 15, 19):
        assert not sb.swieca_decyzyjna(datetime(2026, 10, 16, g, tzinfo=UTC), H1)
    # Zima (EST): otwarcie 14:30 UTC -> swieca 14:00-15:00.
    assert sb.swieca_decyzyjna(datetime(2026, 12, 4, 14, tzinfo=UTC), H1)
    assert not sb.swieca_decyzyjna(datetime(2026, 12, 4, 13, tzinfo=UTC), H1)
    # Sobota.
    assert not sb.swieca_decyzyjna(datetime(2026, 10, 17, 13, tzinfo=UTC), H1)


def test_miesiac_decyzji_w_czasie_NY():
    # 2026-11-01 02:00 UTC to jeszcze 31 pazdziernika w Nowym Jorku.
    assert sb.miesiac_decyzji(datetime(2026, 11, 1, 2, tzinfo=UTC)) == date(2026, 10, 1)
    assert sb.miesiac_decyzji(datetime(2026, 11, 2, 14, tzinfo=UTC)) == date(2026, 11, 1)
    assert sb.poczatek_miesiaca_utc(date(2026, 11, 1)) == datetime(2026, 11, 1, 4, tzinfo=UTC)
    assert sb.poprzednie_miesiace(date(2026, 2, 1), 3) == [
        date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1)]


# ------------------------------------------------------------ dane syntetyczne

# Cena ostatniej swiecy sesji (15:00-16:00 NY) danego dnia; swiece przed-
# i posesyjne dostaja ceny absurdalne - wyciek do zamkniecia od razu widac.
ABSURD = 1e6


def _dni_sesji(od: date, do: date, swieta=()) -> list[date]:
    dni, d = [], od
    while d <= do:
        if d.weekday() < 5 and d not in swieta:
            dni.append(d)
        d += timedelta(days=1)
    return dni


def _swiece(dni, cena_dnia) -> list[dict]:
    """Swiece 1h od 4:00 do 19:00 NY (Alpaca oddaje rozszerzone godziny)."""
    rows = []
    for d in dni:
        for godz in range(4, 20):
            t = datetime.combine(d, time(godz), tzinfo=STREFA_US).astimezone(UTC)
            if 9 <= godz <= 14:
                c = cena_dnia(d) * 0.999          # wczesniejsze swiece sesji
            elif godz == 15:
                c = cena_dnia(d)                  # ostatnia swieca sesji
            else:
                c = ABSURD                        # przed- i posesyjne
            rows.append({"t": t, "close": c})
    return rows


def test_zamkniecie_dzienne_to_ostatnia_swieca_SESJI_nie_posesyjna():
    d = date(2026, 10, 16)
    dzienne = sb.zamkniecia_dzienne(_swiece([d], lambda _: 500.0))
    assert dzienne == [(d, 500.0)]


def test_zamkniecie_miesieczne_przez_WEEKEND_i_SWIETO():
    """Maj 2021: 31.05 to Memorial Day (poniedzialek), 29-30 weekend -
    ostatnia sesja to piatek 28.05. Styczen 2021: 31.01 niedziela -> 29.01."""
    swieta = {date(2021, 5, 31)}
    dni = _dni_sesji(date(2021, 1, 1), date(2021, 6, 30), swieta)
    miesieczne = sb.zamkniecia_miesieczne(
        sb.zamkniecia_dzienne(_swiece(dni, lambda d: d.toordinal() / 1000)))
    assert miesieczne[date(2021, 5, 1)] == (date(2021, 5, 28),
                                            date(2021, 5, 28).toordinal() / 1000)
    assert miesieczne[date(2021, 1, 1)][0] == date(2021, 1, 29)
    assert miesieczne[date(2021, 6, 1)][0] == date(2021, 6, 30)


def _rynek(miesiac: date, ceny_miesiecy: list[float], swieta=()) -> list[dict]:
    """Swiece za `len(ceny_miesiecy)` miesiecy przed `miesiac`: w kazdym dniu
    cena = cena miesiaca, ostatnia sesja miesiaca = dokladnie ta cena."""
    miesiace = sb.poprzednie_miesiace(miesiac, len(ceny_miesiecy))
    cena = dict(zip(miesiace, ceny_miesiecy))
    dni = _dni_sesji(miesiace[0], miesiac - timedelta(days=1), swieta)
    return _swiece(dni, lambda d: cena[d.replace(day=1)])


LISTOPAD = date(2026, 11, 1)


def test_filtr_X_powyzej_SMA_w_rynku_inaczej_gotowka():
    rosnacy = _rynek(LISTOPAD, [100 + i for i in range(10)])
    cel = sb.wyznacz_cel(rosnacy, LISTOPAD)
    assert cel.w_rynku and cel.zamkniecie == 109 and cel.sma == pytest.approx(104.5)
    assert [m for m, _ in cel.zamkniecia][-1] == date(2026, 10, 30)
    assert len(cel.zamkniecia) == 10 and cel.powod == ""

    spadek = _rynek(LISTOPAD, [110 - i for i in range(10)])
    cel = sb.wyznacz_cel(spadek, LISTOPAD)
    assert not cel.w_rynku and cel.sma is not None

    # Rownosc to NIE "powyzej" - gotowka.
    plaski = _rynek(LISTOPAD, [100.0] * 10)
    assert not sb.wyznacz_cel(plaski, LISTOPAD).w_rynku


def test_tylko_10_OSTATNICH_miesiecy():
    """Miesiac M-11 z cena absurdalna nie wchodzi do SMA."""
    rynek = _rynek(LISTOPAD, [1e9] + [100 + i for i in range(10)])
    cel = sb.wyznacz_cel(rynek, LISTOPAD)
    assert cel.w_rynku and cel.sma == pytest.approx(104.5)


def test_za_malo_historii_to_GOTOWKA_z_powodem():
    rynek = _rynek(LISTOPAD, [100 + i for i in range(9)])
    cel = sb.wyznacz_cel(rynek, LISTOPAD)
    assert not cel.w_rynku and cel.sma is None and cel.zamkniecia == ()
    assert "za mało historii" in cel.powod and "2026-01" in cel.powod
    assert sb.akcja(cel, None, "SPY", True) == ("hold", "historia_filtra_niepelna")
    assert sb.wyznacz_cel([], LISTOPAD).powod.startswith("za mało historii")


def test_DZIURA_na_koncu_miesiaca_to_brak_miesiaca_a_nie_zla_cena():
    """Dane wrzesnia koncza sie 15.09: "zamkniecie wrzesnia" byloby cena sprzed
    dwoch tygodni. Taki miesiac jest brakujacy -> gotowka."""
    rynek = [s for s in _rynek(LISTOPAD, [100 + i for i in range(10)])
             if not (datetime(2026, 9, 16, tzinfo=UTC) <= s["t"] < datetime(2026, 10, 1, tzinfo=UTC))]
    cel = sb.wyznacz_cel(rynek, LISTOPAD)
    assert not cel.w_rynku and "2026-09" in cel.powod


def test_BRAK_ZAGLADANIA_W_PRZYSZLOSC():
    """Swiece z miesiaca decyzji (i pozniej) - choćby absurdalne - nie zmieniaja
    celu. Takze swieca z 1 listopada 00:00 NY, czyli dokladnie na granicy."""
    przeszlosc = _rynek(LISTOPAD, [110 - i for i in range(10)])
    bez = sb.wyznacz_cel(przeszlosc, LISTOPAD)
    granica = sb.poczatek_miesiaca_utc(LISTOPAD)
    przyszlosc = [{"t": granica + timedelta(hours=k), "close": ABSURD}
                  for k in range(0, 24 * 20)]
    z = sb.wyznacz_cel(przeszlosc + przyszlosc, LISTOPAD)
    assert z == bez and not z.w_rynku
    # Swieca 23:00-24:00 NY ostatniego dnia miesiaca konczy sie NA granicy -
    # posesyjna, wiec i tak bez znaczenia, ale nie moze zmienic celu.
    ostatnia = [{"t": granica - timedelta(hours=1), "close": ABSURD}]
    assert sb.wyznacz_cel(przeszlosc + ostatnia, LISTOPAD) == bez


class KursorSwiec:
    """Odpowiada na `SQL_SWIECE` i zapisuje, o jakie okno pytano."""

    def __init__(self, rows):
        self.rows, self.wynik, self.pytania = rows, [], []

    def execute(self, sql, p):
        assert sql == sb.SQL_SWIECE and p["tf"] == "1h" and p["iid"] == 30
        self.pytania.append(p)
        self.wynik = [r for r in self.rows if p["od"] <= r["t"] < p["do"]]

    def fetchall(self):
        return self.wynik


def test_cel_z_bazy_pyta_TYLKO_o_dane_sprzed_miesiaca_decyzji():
    rows = _rynek(LISTOPAD, [100 + i for i in range(12)])
    rows += [{"t": sb.poczatek_miesiaca_utc(LISTOPAD) + timedelta(hours=k), "close": 1.0}
             for k in range(24 * 10)]
    kur = KursorSwiec(rows)
    spy = [{"id": 30, "symbol": "SPY"}]
    cel = sb.cel_z_bazy(kur, spy, datetime(2026, 11, 2, 14, tzinfo=UTC), H1)
    (p,) = kur.pytania
    assert p["do"] == sb.poczatek_miesiaca_utc(LISTOPAD)
    assert p["od"] == p["do"] - timedelta(days=sb.HISTORIA_DNI)
    assert cel.w_rynku and cel.miesiac == LISTOPAD and cel.kompletny
    # Ten sam cel na kazdej swiecy miesiaca.
    assert sb.cel_z_bazy(kur, spy, datetime(2026, 11, 27, 19, tzinfo=UTC), H1) == cel


def test_cel_z_bazy_bez_SPY_to_gotowka_z_powodem():
    cel = sb.cel_z_bazy(KursorSwiec([]), [{"id": 1, "symbol": "AAPL"}],
                        datetime(2026, 11, 2, 14, tzinfo=UTC), H1)
    assert not cel.w_rynku and "brak SPY" in cel.powod


def test_instrumenty_reguly_po_symbolu_NIE_po_tradeable():
    assert "tradeable" not in sb.SQL_INSTRUMENTY
    assert "exec.position" in sb.SQL_INSTRUMENTY and "closed_at IS NULL" in sb.SQL_INSTRUMENTY


# ------------------------------------------------------------ regula

W_RYNKU = sb.Cel(LISTOPAD, True, ((date(2026, 10, 30), 520.0),), 520.0, 500.0)
GOTOWKA = sb.Cel(LISTOPAD, False, ((date(2026, 10, 30), 480.0),), 480.0, 500.0)


def test_akcja_wszystkie_galezie():
    dlugi, krotki = {"side": "long"}, {"side": "short"}
    assert sb.akcja(W_RYNKU, None, "SPY", True) == ("enter_long", None)
    assert sb.akcja(W_RYNKU, None, "SPY", False) == ("hold", "czeka_na_sesje")
    assert sb.akcja(W_RYNKU, dlugi, "SPY", False) == ("hold", "trzyma_pozycje")
    assert sb.akcja(W_RYNKU, dlugi, "SPY", True) == ("hold", "trzyma_pozycje")
    assert sb.akcja(W_RYNKU, krotki, "SPY", True) == ("exit", None)
    # Pozostalosc po ensemble wychodzi zawsze, niezaleznie od filtra.
    assert sb.akcja(W_RYNKU, dlugi, "AAPL", False) == ("exit", None)
    assert sb.akcja(W_RYNKU, None, "AAPL", True) == ("hold", "poza_instrumentem_reguly")
    # Cel gotowka: wyjscie na KAZDEJ swiecy, nie tylko na pierwszej sesji.
    assert sb.akcja(GOTOWKA, dlugi, "SPY", False) == ("exit", None)
    assert sb.akcja(GOTOWKA, None, "SPY", True) == ("hold", "filtr_trendu_spy")


def test_snapshot_i_uzasadnienie_niosa_liczby_decyzji():
    s = W_RYNKU.snapshot("SPY")
    assert s["beta_w_rynku"] == 1.0 and s["beta_spy_sma_10m"] == 500.0
    assert s["beta_odchylenie"] == pytest.approx(0.04)
    assert all(isinstance(v, float) for v in s.values())
    txt = sb.uzasadnienie(W_RYNKU, "SPY")
    assert "520.00 > SMA 10 mies. 500.00" in txt and "nie hipoteza przewagi" in txt
    assert "poza regułą" in sb.uzasadnienie(GOTOWKA, "AAPL")


# ------------------------------------------------------------ budzet

@pytest.mark.parametrize("kapital,ekspozycja,cena,qty,notional", [
    ("100000", "0", "512.37", "185", "94788.45"),
    ("100000", "10000", "512.37", "165", "84541.05"),
    ("10000", "0", "512.37", "18", "9222.66"),
    ("400", "0", "512.37", "0", "0"),          # nie kupi jednej akcji
    ("100000", "95000", "512.37", "0", "0"),   # sufit wypelniony
    ("100000", "96000", "512.37", "0", "0"),
])
def test_wielkosc_wejscia_CALE_akcje_w_dol_do_95_procent(kapital, ekspozycja, cena,
                                                        qty, notional):
    n, q = sb.wielkosc_wejscia(D(kapital), D(ekspozycja), D(cena))
    assert (q, n) == (D(qty), D(notional))
    assert q == q.to_integral_value()
    assert n == 0 or sb.miesci_sie(D(kapital), D(ekspozycja), n)


def test_wielkosc_wejscia_WLASNOSC_nigdy_ponad_95_procent():
    import random
    los = random.Random(16)
    for _ in range(2000):
        k = D(str(round(los.uniform(100, 5_000_000), 2)))
        e = (k * D(str(los.uniform(0, 1)))).quantize(D("0.01"))
        c = D(str(round(los.uniform(1, 2000), 2)))
        n, q = sb.wielkosc_wejscia(k, e, c)
        assert q >= 0 and q == q.to_integral_value()
        assert n == 0 or e + n <= k * D("0.95"), (k, e, c, n)
        # W dol: jeszcze jedna akcja przebilaby sufit.
        assert e + (q + 1) * c > k * D("0.95") or e >= k * D("0.95"), (k, e, c, q)


# ------------------------------------------------------------ petla

SPY = {"id": 30, "symbol": "SPY", "kind": "etf", "quote_ccy": "USD", "lot_size": D("1")}
AAPL = {"id": 31, "symbol": "AAPL", "kind": "stock", "quote_ccy": "USD", "lot_size": D("1")}
CENA_SPY = D("512.37")
# Piatek 16.10.2026, 14:05 UTC: swieca 13:00-14:00 (z otwarciem 13:30) zamknieta.
PIERWSZA = datetime(2026, 10, 16, 14, 5, tzinfo=UTC)
POZNIEJ = datetime(2026, 10, 16, 16, 5, tzinfo=UTC)


@pytest.fixture
def beta(swiat, monkeypatch):
    swiat.cel = W_RYNKU
    swiat.instrumenty = [SPY]
    swiat.ekspozycja = D(0)
    swiat.teraz = PIERWSZA
    swiat.cele = []
    swiat.wiersze = []

    def cel_z_bazy(cur, instrumenty, bar_start, h, czekaj=True):
        swiat.cele.append(bar_start)
        return swiat.cel

    def bez_modelu(*a, **kw):
        raise AssertionError("reguła beta przeczytała model")

    zapis = loop.record_decision

    def decyzja(cur, engine, mode, iid, now, action, qty, *reszta, bar_start,
                strategia=None):
        swiat.wiersze.append({"model_version_id": reszta[9], "rationale": reszta[5],
                              "features": reszta[3]})
        return zapis(cur, engine, mode, iid, now, action, qty, *reszta,
                     bar_start=bar_start, strategia=strategia)

    monkeypatch.setattr(sb, "cel_z_bazy", cel_z_bazy)
    monkeypatch.setattr(sb, "instrumenty_reguly",
                        lambda cur, engine, mode: list(swiat.instrumenty))
    monkeypatch.setattr(loop, "record_decision", decyzja)
    monkeypatch.setattr(loop, "active_model", bez_modelu)
    monkeypatch.setattr(loop, "latest_features", bez_modelu)
    monkeypatch.setattr(loop, "evaluate", bez_modelu)
    monkeypatch.setattr(loop, "horizon_feature_ids", bez_modelu)
    monkeypatch.setattr(loop, "zakres_swiecy",
                        lambda cur, iid, h, bar_start: {"close": CENA_SPY})
    monkeypatch.setattr(loop.RiskKernel, "ekspozycja",
                        lambda self: swiat.ekspozycja, raising=False)
    return swiat


def cykl_beta(swiat, **kw):
    kw.setdefault("sesja", SESJA)
    return cykl(swiat, instrumenty=[], engine="equity", h=H1, strategia="beta", **kw)


def test_pierwsza_swieca_sesji_WEJSCIE_95_procent_w_calych_akcjach(beta):
    stats, cur = cykl_beta(beta)

    (d,) = beta.decyzje
    assert d["action"] == "enter_long" and d["strategia"] == "beta_filtr_trendu"
    (plan,) = plany(cur)
    qty = (KAPITAL * D("0.95") / CENA_SPY).to_integral_value(rounding="ROUND_DOWN")
    assert plan[1] == 30 and plan[2] == "buy" and plan[3] == qty == D("18")
    assert stats["strategia"] == "beta" and stats["wejscia"] == 1
    assert stats["model_status"] is None and stats["model_version_id"] is None
    (w,) = beta.wiersze
    assert w["model_version_id"] is None
    assert w["features"]["beta_w_rynku"] == 1.0
    assert "Zamiar wejścia: 18 akcji" in w["rationale"]
    assert beta.cele == [datetime(2026, 10, 16, 13, tzinfo=UTC)], "cel raz na cykl"


def test_poza_pierwsza_swieca_CZEKA_a_nie_wchodzi(beta):
    beta.teraz = POZNIEJ
    stats, cur = cykl_beta(beta)

    (d,) = beta.decyzje
    assert d["action"] == "hold" and d["powod_holdu"] == "czeka_na_sesje"
    assert plany(cur) == [] and stats["wejscia"] == 0


def test_cel_gotowka_WYJSCIE_na_kazdej_swiecy_i_pozostalosc_ensemble_wychodzi(beta):
    beta.cel = GOTOWKA
    beta.teraz = POZNIEJ
    beta.instrumenty = [AAPL, SPY]
    beta.pozycje = {30: POZYCJA, 31: POZYCJA}
    stats, cur = cykl_beta(beta)

    d = {x["iid"]: x for x in beta.decyzje}
    assert d[30]["action"] == "exit" and d[31]["action"] == "exit"
    assert {p[2] for p in plany(cur)} == {"sell"} and stats["wyjscia"] == 2


def test_w_rynku_pozycja_TRZYMANA_bez_dorownywania(beta):
    beta.pozycje = {30: POZYCJA}
    stats, cur = cykl_beta(beta)

    (d,) = beta.decyzje
    assert d["action"] == "hold" and d["powod_holdu"] == "trzyma_pozycje"
    assert plany(cur) == []


def test_wlasny_sufit_95_procent_blokuje_wejscie_przed_kernelem(beta, monkeypatch):
    beta.ekspozycja = KAPITAL * D("0.95")
    sprawdzone = []
    monkeypatch.setattr(loop.RiskKernel, "check",
                        lambda self, *a, **kw: sprawdzone.append(a))
    stats, cur = cykl_beta(beta)

    (d,) = beta.decyzje
    assert d["action"] == "enter_long" and d["verdict"] == "block"
    assert plany(cur) == [] and sprawdzone == [] and stats["zablokowane"] == 1


def test_killswitch_wstrzymuje_wejscie_reguly(beta):
    beta.killswitch = {"id": 9, "reason": "obsunięcie"}
    stats, cur = cykl_beta(beta)

    (d,) = beta.decyzje
    assert d["action"] == "hold" and d["powod_holdu"] == "wstrzymanie_ryzykiem"
    assert plany(cur) == []


def test_BRAMKA_wymagaj_modelu_NIE_blokuje_reguly(beta):
    """Bez kandydata `--wymagaj-modelu` konczy cykl bez decyzji - regule beta
    nie wolno (D-3). `active_model` podmienione na wyjatek: gdyby bramka
    zapytala o model, test by sie wywrocil."""
    stats, cur = cykl_beta(beta, wymagaj_modelu=True)

    assert not stats.get("bez_modelu") and stats["wejscia"] == 1


def test_poza_sesja_bez_decyzji(beta):
    stats, cur = cykl_beta(beta, sesja=StanSesji(False, "zegar giełdy"))
    assert stats["poza_sesja"] and beta.decyzje == []


@pytest.mark.parametrize("kw,komunikat", [
    ({"strategia": "beta", "model_status": "candidate"}, "nie czyta modelu"),
    ({"strategia": "beta", "h": get_horizon("4h")}, "świecy 1h"),
])
def test_run_once_ODMAWIA(beta, kw, komunikat):
    from tests.test_run_once import Kursor, Polaczenie
    conn = Polaczenie(Kursor([]))
    kw.setdefault("h", H1)
    with pytest.raises(ValueError, match=komunikat):
        asyncio.run(loop.run_once(conn, "equity", "demo", verbose=False, **kw))


def test_run_once_ODMAWIA_poza_demo(beta):
    from tests.test_run_once import Kursor, Polaczenie
    conn = Polaczenie(Kursor([]))
    with pytest.raises(ValueError, match="tylko na demo"):
        asyncio.run(loop.run_once(conn, "equity", "live", h=H1, verbose=False,
                                  strategia="beta"))


@pytest.mark.parametrize("argv,komunikat", [
    (["--strategia", "beta", "--mode", "live", "--kind", "stock"], "tylko z --mode demo"),
    (["--strategia", "beta", "--model-status", "candidate"], "wykluczają się"),
    (["--strategia", "beta", "--wymagaj-modelu"], "wykluczają się"),
])
def test_main_ODMAWIA_startu(monkeypatch, argv, komunikat):
    monkeypatch.setattr("sys.argv", ["loop.py", "--engine", "equity", "--bar", "1h", *argv])
    monkeypatch.setenv("DATABASE_URL", "postgresql://nie-wolno-sie-polaczyc")
    with pytest.raises(SystemExit, match=komunikat):
        asyncio.run(loop.main())


# ------------------------------------------------------------ chart

def test_chart_equity_gra_regula_beta_a_trener_bez_zmian():
    import yaml

    from tests.atrapa_bazy import KORZEN
    v = yaml.safe_load((KORZEN / "charts/trader/values.yaml").read_text())
    workers = v["workers"]
    k = " ".join(workers["engine-equity"]["command"])
    # Kontrola przed startem i petla - ta sama regula.
    assert k.count("--strategia beta") == 2
    assert "--mode demo" in k and "--mode live" not in k
    assert "--wymagaj-modelu" not in k and "--model-status" not in k
    z_beta = {n for n, w in workers.items()
              if "--strategia beta" in " ".join(w.get("command", []))}
    assert z_beta == {"engine-equity"}
    # Trener equity zostaje (H-13 liczy sie z trenera, nie z demo).
    assert v["trainerCron"]["equity"]["horyzonty"] == ["1h"]
