"""Egzekucja po audycie 2026-10-03: odpowiedź giełdy to wiedza, brak odczytu to niewiedza.

PIĘĆ WAD, KTÓRE TE TESTY PILNUJĄ (numeracja z raportu audytu):

  E1. Między odczytem arkusza a POST-em stały dwa GET-y `stan_zlecenia`
      z ponowieniami — sekundy, w których cena bid−1 tick przestaje być pasywna
      i PostOnly odrzuca zlecenie. Teraz: stan → arkusz → POST, a wiek odczytu
      liczony NASZYM zegarem monotonicznym ma granicę 5 s.
  E2. Każdy `retCode` ≠ 0 spoza dwóch zbiorów odmowy zapisywał się jako
      „odpowiedź nie dotarła" (`unknown`) — także limit zapytań, martwy klucz
      i błąd odczytu PRZED wysłaniem, gdy nic nie poszło na giełdę. Wiersz
      `unknown` przy tym nigdy nie był ponawiany.
  E3. Plan wygasał, gdy giełda chwilowo „nie znała" zlecenia, któremu sama
      nadała numer — a pozycja powstawała na giełdzie bez dziennika.
  E4. Kod „zlecenia nie ma" przy anulowaniu znał tylko kontrakty (110001).
  E5. Jeden plan z błędem odczytu przerywał rozstrzyganie wszystkich.

Ceny i strony NIE są tu przedmiotem testu — bid−1 tick zostaje bez zmian.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from decimal import Decimal as D

import httpx
import pytest

from services.engine import gielda_exec
from services.engine.gielda_exec import (
    BladPrzedWyslaniem, ponow_nieznane, rozstrzygnij_oczekujace,
    rozstrzygnij_plan, zloz_oczekujace, zloz_plan)
from services.engine.horizons import get as get_horizon
from services.engine.zlecenia import Filtry
from services.exchanges.base import (
    AuthError, BookLevel, ExchangeError, OdmowaGieldy, OrderBook, StanZlecenia,
    ZlozoneZlecenie)
from services.exchanges.bybit import BybitAdapter

REGULY = Filtry(krok_ilosci=D("0.001"), min_ilosc=D("0.001"),
                min_wartosc=D("5"), krok_ceny=D("0.01"))
# Te same reguly w postaci, w jakiej czyta je `filtry_z_bazy`.
REGULY_Z_BAZY = {"id": 3, "lot_size": D("0.001"), "tick_size": D("0.01"),
                 "min_notional": D("5"), "min_ilosc": "0.001"}


class Kursor:
    """Atrapa z odpowiedziami PO TREŚCI zapytania, nie po kolejności.

    `rozstrzygnij_oczekujace` czyta reguły instrumentów, potem plany, potem
    wiersz zlecenia — kolejka odpowiedzi rozjechałaby się przy pierwszym
    dodatkowym odczycie i test mierzyłby atrapę."""

    def __init__(self, odpowiedzi=None):
        self.sql: list[tuple[str, tuple | None]] = []
        self._odp = list(odpowiedzi or [])
        self._ostatnie = ""

    def execute(self, sql, params=None):
        jedno = " ".join(sql.split())
        assert jedno.count("%s") == (0 if params is None else len(params)), jedno
        self.sql.append((jedno, params))
        self._ostatnie = jedno

    def _znajdz(self):
        for fragment, wynik in self._odp:
            if fragment in self._ostatnie:
                return wynik
        return []

    def fetchall(self):
        w = self._znajdz()
        return list(w) if isinstance(w, list) else [w]

    def fetchone(self):
        w = self._znajdz()
        if isinstance(w, list):
            return w[0] if w else None
        return w

    def zapytania(self, fragment):
        return [(s, p) for s, p in self.sql if fragment in s]


def arkusz(bid="120.04", ask="120.05"):
    return OrderBook(event_time=dt.datetime.now(dt.timezone.utc),
                     bids=[BookLevel(D(bid), D("100"))],
                     asks=[BookLevel(D(ask), D("100"))])


def plan(pid=77, rodzaj="wyjscie", strona="sell"):
    trigger = {"rodzaj": rodzaj, "bar": "2026-10-03T08:00:00+00:00"}
    if rodzaj == "wyjscie":
        trigger["position_id"] = 32
    return dict(id=pid, decision_id=1000 + pid, instrument_id=3, side=strona,
                qty=D("1.5"), limit_price=D("120.00"), symbol="SOLUSDC",
                quote_ccy="USDC", base_ccy="SOL", atr_pct=None, trigger=trigger,
                valid_until=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))


class Gielda:
    """Atrapa adaptera, która zapisuje KOLEJNOŚĆ wywołań."""

    def __init__(self, *, stan=None, blad_stanu=None, blad_zlozenia=None):
        self.log: list[str] = []
        self.zlozone: list[dict] = []
        self._stan, self._blad_stanu = stan, blad_stanu
        self._blad_zlozenia = blad_zlozenia

    async def stan_zlecenia(self, id_wlasne, kategoria="spot"):
        self.log.append("stan_zlecenia")
        if self._blad_stanu is not None:
            raise self._blad_stanu
        return self._stan

    async def cena_odniesienia(self, symbol, kategoria=None):
        self.log.append("cena_odniesienia")
        return D("120.04")

    async def fetch_order_book(self, symbol, depth=50, *, ksiega_egzekucji=True):
        self.log.append("fetch_order_book")
        return arkusz()

    async def zloz_zlecenie(self, **kw):
        self.log.append("zloz_zlecenie")
        self.zlozone.append(kw)
        if self._blad_zlozenia is not None:
            raise self._blad_zlozenia
        return ZlozoneZlecenie(id_wlasne=kw["id_wlasne"], id_gieldy="G1", status="new")

    async def anuluj_zlecenie(self, **kw):
        return True


def zloz(g, p=None, cur=None):
    cur = cur or Kursor()
    ok, msg = asyncio.run(zloz_plan(cur, g, p or plan(), REGULY, engine="crypto",
                                    mode="demo", kategoria="spot"))
    return cur, ok, msg


def bybit(handler):
    k = BybitAdapter(api_key="k", api_secret="s")
    k._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return k


def post_z_kodem(kod, msg="komunikat gieldy"):
    """POST z danym `retCode`; odczyty stanu puste (zlecenia nie ma)."""
    metody = []

    def handler(req):
        metody.append(req.method)
        if req.method != "POST":
            return httpx.Response(200, json={"retCode": 0, "result": {"list": []}})
        return httpx.Response(200, json={"retCode": kod, "retMsg": msg})

    k = bybit(handler)
    with pytest.raises(ExchangeError) as e:
        asyncio.run(k.zloz_zlecenie(symbol="SOLUSDC", strona="sell", ilosc=D("1"),
                                    cena_limitu=D("120"), id_wlasne="tai-p77"))
    return e.value, metody


# ====================================================================== E1

def test_E1_arkusz_czytany_PO_stanie_zlecenia_i_TUZ_przed_wysylka():
    g = Gielda()
    _, ok, msg = zloz(g)
    assert ok, msg
    assert g.log.index("stan_zlecenia") < g.log.index("fetch_order_book"), (
        f"stan zlecenia czytany po arkuszu: {g.log}")
    assert g.log[g.log.index("fetch_order_book") + 1] == "zloz_zlecenie", (
        f"między arkuszem a POST-em stoi inne zapytanie: {g.log}")
    assert g.zlozone[0]["istnienie_sprawdzone"] is True, (
        "adapter dostał zgodę na drugie sprawdzenie stanu przed POST-em")


def test_E1_odczyt_starszy_niz_granica_NIE_jest_wysylany(monkeypatch):
    zegar = iter([100.0, 100.0 + gielda_exec.WIEK_ODCZYTU_PRZY_WYSYLCE_S + 0.5])
    monkeypatch.setattr(gielda_exec, "_zegar_wysylki", lambda: next(zegar))
    g = Gielda()
    cur, ok, msg = zloz(g)
    assert not ok
    assert "zloz_zlecenie" not in g.log, "wysłano cenę z odczytu po granicy wieku"
    assert not cur.zapytania("INSERT INTO exec.order"), (
        "wiersz zlecenia zablokowałby ponowienie w następnym cyklu")


def test_E1_wiek_odczytu_zapisany_przy_zleceniu():
    cur, ok, _ = zloz(Gielda())
    (_, params), = cur.zapytania("INSERT INTO exec.order")
    blok = json.loads(params[-1])["arkusz_wysylki"]
    assert 0 <= blok["wiek_przy_wysylce_s"] < gielda_exec.WIEK_ODCZYTU_PRZY_WYSYLCE_S


def test_E1_adapter_bybita_ze_sprawdzonym_stanem_wysyla_BEZ_odczytu():
    metody = []

    def handler(req):
        metody.append(req.method)
        if req.method == "POST":
            return httpx.Response(200, json={"retCode": 0, "result": {"orderId": "X"}})
        return httpx.Response(200, json={"retCode": 0, "result": {"list": []}})

    asyncio.run(bybit(handler).zloz_zlecenie(
        symbol="SOLUSDC", strona="buy", ilosc=D("1"), cena_limitu=D("120"),
        id_wlasne="tai-p1", istnienie_sprawdzone=True))
    assert metody == ["POST"]


def test_E1_bez_flagi_adapter_nadal_sprawdza_stan():
    """Kanarek: likwidacja i zlecenie zastępcze wołają adapter bez flagi —
    tam idempotencja musi zostać w adapterze."""
    metody = []

    def handler(req):
        metody.append(req.method)
        if req.method == "POST":
            return httpx.Response(200, json={"retCode": 0, "result": {"orderId": "X"}})
        return httpx.Response(200, json={"retCode": 0, "result": {"list": []}})

    asyncio.run(bybit(handler).zloz_zlecenie(
        symbol="SOLUSDC", strona="buy", ilosc=D("1"), cena_limitu=D("120"),
        id_wlasne="tai-p1"))
    assert metody[0] == "GET" and metody[-1] == "POST"


# ====================================================================== E2

@pytest.mark.parametrize("kod", [10006, 10002, 170999])
def test_E2_kod_spoza_zbiorow_na_POST_to_ODMOWA_z_kodem(kod):
    e, _ = post_z_kodem(kod)
    assert isinstance(e, OdmowaGieldy), f"{kod} wpadłby do `unknown`"
    assert e.rodzaj == "inne" and e.code == str(kod)
    assert str(kod) in str(e)


@pytest.mark.parametrize("kod", [10000, 10016, 170141])
def test_E2_stan_naprawde_nieznany_NIE_jest_odmowa(kod):
    """Kanarek w drugą stronę: 10000/10016 to błąd serwera (zlecenie mogło
    wejść), 170141 to duplikat naszego id (zlecenie JEST). Odmowa porzuciłaby
    żywe zlecenie."""
    e, _ = post_z_kodem(kod)
    assert not isinstance(e, OdmowaGieldy)


def test_E2_odmowa_z_POST_zapisuje_rejected_a_nie_unknown():
    g = Gielda(blad_zlozenia=OdmowaGieldy("limit (kod 10006)", code="10006",
                                          rodzaj="inne"))
    cur, ok, _ = zloz(g)
    assert not ok
    (sql, params), = cur.zapytania("INSERT INTO exec.order")
    assert "'rejected'" in sql and "'unknown'" not in sql
    raw = json.loads(params[-1])
    assert raw["etap"] != "odpowiedz_nie_dotarla"
    assert raw["kod_gieldy"] == "10006"


def test_E2_martwy_klucz_na_POST_idzie_W_GORE():
    g = Gielda(blad_zlozenia=AuthError("klucz odrzucony (kod 10003)", code="10003"))
    cur = Kursor()
    with pytest.raises(AuthError):
        zloz(g, cur=cur)
    assert not cur.zapytania("odpowiedz_nie_dotarla")
    assert not cur.zapytania("INSERT INTO exec.order")


def test_E2_martwy_klucz_z_adaptera_bybita_zostaje_AuthError():
    e, _ = post_z_kodem(10003)
    assert isinstance(e, AuthError) and e.wymaga_czlowieka


def test_E2_blad_odczytu_PRZED_wyslaniem_nie_jest_unknown():
    g = Gielda(blad_stanu=ExchangeError("limit zapytan", code="10006", retryable=True))
    cur = Kursor()
    with pytest.raises(BladPrzedWyslaniem) as e:
        zloz(g, cur=cur)
    assert "przed_wyslaniem" in str(e.value)
    assert "zloz_zlecenie" not in g.log, "wysłano bez odpowiedzi o stanie"
    assert not cur.zapytania("INSERT INTO exec.order"), (
        "wiersz zablokowałby ponowienie — a nic nie poszło na giełdę")


def test_E2_blad_odczytu_przed_wyslaniem_liczony_w_pulsie_i_nie_przerywa_wysylki():
    g = Gielda(blad_stanu=ExchangeError("siec", code="network"))
    cur = Kursor([("FROM ref.instrument WHERE engine", [REGULY_Z_BAZY]),
                  ("NOT EXISTS", [plan(pid=1), plan(pid=2)])])
    st = asyncio.run(zloz_oczekujace(cur, g, engine="crypto", mode="demo",
                                     kategoria="spot", h=get_horizon("4h"),
                                     verbose=False))
    assert st["przed_wyslaniem"] == 2 and st["zlozone"] == 0
    assert not cur.zapytania("odpowiedz_nie_dotarla")


def test_E2_tozsamosc_zlecenia_ze_stanu_idzie_W_GORE():
    g = Gielda(blad_stanu=ExchangeError("cudze orderLinkId", code="tozsamosc-zlecenia",
                                        wymaga_czlowieka=True))
    with pytest.raises(ExchangeError) as e:
        zloz(g)
    assert not isinstance(e.value, BladPrzedWyslaniem)
    assert e.value.code == "tozsamosc-zlecenia"


def _wiersz_unknown():
    return {"id": 501, "client_order_id": "tai-p77", "status": "unknown",
            "reject_reason": "brak odpowiedzi", "plan_id": 77, "symbol": "SOLUSDC"}


def test_E2_unknown_waznego_planu_ktorego_gielda_nie_zna_jest_PONAWIANY():
    cur = Kursor([("o.status = 'unknown'", [_wiersz_unknown()])])
    st = asyncio.run(ponow_nieznane(cur, Gielda(stan=None), engine="crypto",
                                    mode="demo", kategoria="spot", verbose=False))
    assert st["ponowione"] == 1
    (sql, params), = cur.zapytania("UPDATE exec.order")
    assert "planned_order_id=NULL" in sql, "plan musi wrócić do wysyłki jako bez wiersza"
    assert params[0] == "~501", "stary wiersz zwalnia identyfikator jawnym sufiksem"
    raw = json.loads(params[2])
    assert raw["client_order_id_wyslany"] == "tai-p77" and raw["planned_order_id"] == 77
    assert raw["etap"] == "zamkniete_bez_kontaktu_z_gielda"
    # Zapytanie bierze wyłącznie plany WAŻNE i wiersze bez kontaktu z giełdą.
    (wybor, _), = cur.zapytania("o.status = 'unknown'")
    assert "p.valid_until > now()" in wybor and "o.venue_order_id IS NULL" in wybor


def test_E2_unknown_ktore_gielda_zna_jest_uzupelniane_NIE_ponawiane():
    stan = StanZlecenia(id_wlasne="tai-p77", id_gieldy="G77", status="new",
                        ilosc=D("1.5"), wypelniono=D("0"),
                        srednia_cena=None, prowizja=D("0"))
    cur = Kursor([("o.status = 'unknown'", [_wiersz_unknown()])])
    st = asyncio.run(ponow_nieznane(cur, Gielda(stan=stan), engine="crypto",
                                    mode="demo", kategoria="spot", verbose=False))
    assert st == {"odnalezione": 1, "ponowione": 0, "unknown_bez_odczytu": 0}
    (sql, params), = cur.zapytania("UPDATE exec.order")
    assert "planned_order_id=NULL" not in sql
    assert params[:2] == ("new", "G77")


def test_E2_petla_zwalnia_unknown_PRZED_wysylka():
    from pathlib import Path
    zrodlo = (Path(__file__).resolve().parents[1] / "services/engine/loop.py").read_text()
    kod = "\n".join(w for w in zrodlo.splitlines() if not w.lstrip().startswith("#"))
    assert kod.index("await ponow_nieznane(") < kod.index("await zloz_oczekujace(")


# ====================================================================== E3

def _stan_none_z_wierszem(venue_order_id):
    cur = Kursor([("SELECT id, venue_order_id FROM exec.order",
                   {"id": 9, "venue_order_id": venue_order_id})])
    rodzaj, _ = asyncio.run(rozstrzygnij_plan(
        cur, Gielda(stan=None), plan(), REGULY, engine="crypto", mode="demo",
        kategoria="spot", teraz=dt.datetime.now(dt.timezone.utc), kind="spot_eu"))
    return rodzaj, cur


def test_E3_brak_odczytu_zlecenia_PRZYJETEGO_przez_gielde_to_nie_wiem():
    rodzaj, cur = _stan_none_z_wierszem("G-555")
    assert rodzaj == "nie_wiem"
    assert not cur.zapytania("UPDATE journal.planned_order"), "plan wygasł mimo venue_order_id"
    assert not cur.zapytania("UPDATE exec.order"), "wiersz zlecenia zmieniony bez wiedzy"


def test_E3_zlecenie_bez_kontaktu_z_gielda_nadal_wygasa():
    """Kanarek: bez `venue_order_id` „nie zna" znaczy „nie weszło"."""
    rodzaj, cur = _stan_none_z_wierszem(None)
    assert rodzaj == "brak"
    assert cur.zapytania("SET status='expired'")


def test_E3_trzeci_odczyt_openOnly_domyka_luke_historii():
    """Zlecenie wypełnione chwilę przed odczytem: nie ma go w otwartych,
    historia się spóźnia — jest w `openOnly=1`."""
    def handler(req):
        if "openOnly=1" in str(req.url):
            return httpx.Response(200, json={"retCode": 0, "result": {"list": [{
                "orderId": "G1", "orderLinkId": "tai-p77", "orderStatus": "Filled",
                "qty": "1", "cumExecQty": "1", "avgPrice": "120", "cumExecFee": "0"}]}})
        return httpx.Response(200, json={"retCode": 0, "result": {"list": []}})

    stan = asyncio.run(bybit(handler).stan_zlecenia("tai-p77"))
    assert stan is not None and stan.status == "filled"


# ====================================================================== E4

@pytest.mark.parametrize("kod", [110001, 170213])
def test_E4_anulowanie_nieistniejacego_zlecenia_spot_i_kontrakty(kod):
    def handler(req):
        return httpx.Response(200, json={"retCode": kod, "retMsg": "Order does not exist"})

    assert asyncio.run(bybit(handler).anuluj_zlecenie(
        symbol="SOLUSDC", id_wlasne="tai-p1")) is False


# ====================================================================== E5

class GieldaZBledemNaPierwszym(Gielda):
    def __init__(self, blad):
        super().__init__()
        self._blad = blad

    async def stan_zlecenia(self, id_wlasne, kategoria="spot"):
        if id_wlasne == "tai-p1":
            raise self._blad
        return StanZlecenia(id_wlasne=id_wlasne, id_gieldy="G2", status="cancelled",
                            ilosc=D("1.5"), wypelniono=D("0"),
                        srednia_cena=None, prowizja=D("0"))


def _rozstrzygnij(g):
    cur = Kursor([("FROM ref.instrument WHERE engine", [REGULY_Z_BAZY]),
                  ("p.status = 'pending' ORDER BY p.id", [plan(pid=1), plan(pid=2)]),
                  ("FROM exec.order WHERE planned_order_id", {"id": 7, "qty": D("1.5"),
                                                               "submitted_at": None})])
    st = asyncio.run(rozstrzygnij_oczekujace(
        cur, g, engine="crypto", mode="demo", h=get_horizon("4h"),
        now=dt.datetime.now(dt.timezone.utc), kategoria="spot", kind="spot_eu",
        verbose=False))
    return st, cur


def test_E5_blad_odczytu_jednego_planu_nie_zatrzymuje_drugiego():
    st, cur = _rozstrzygnij(GieldaZBledemNaPierwszym(ExchangeError("5xx", code="502")))
    assert st["blad_odczytu"] == 1
    assert st["niewypelnione"] == 1, "drugi plan nie został rozstrzygnięty"
    (_, params), = cur.zapytania("UPDATE journal.planned_order")
    assert params[-1] == 2, "rozstrzygnięto nie ten plan"


def test_E5_awaria_wymagajaca_czlowieka_nadal_idzie_w_gore():
    with pytest.raises(AuthError):
        _rozstrzygnij(GieldaZBledemNaPierwszym(AuthError("klucz", code="10003")))
