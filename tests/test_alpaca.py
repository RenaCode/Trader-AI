"""Adapter Alpaca na zapisanych ksztaltach odpowiedzi API.

Transport jest atrapa httpx - test nie wychodzi do sieci. Kazda sciezka
bledu ma probke, bo adapter, ktory dziala tylko na happy path, jest
sprawdzony dopiero noca na produkcji.
"""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal as D

import httpx
import pytest

from services.exchanges.alpaca import AlpacaAdapter, _czas
from services.exchanges.base import AuthError, ExchangeError, KontoZamrozone

KONTO = {"status": "ACTIVE", "currency": "USD", "cash": "100000", "buying_power": "200000",
         "equity": "100000", "trading_blocked": False, "account_blocked": False}
ZLECENIE = {"id": "904837e3-3b76-47ec-b432-046db621571b", "client_order_id": "dec-77",
            "status": "new", "qty": "10", "filled_qty": "0", "filled_avg_price": None,
            "symbol": "AAPL", "side": "buy", "type": "limit", "limit_price": "190.5"}


def _adapter(handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AlpacaAdapter("K", "S", mode="demo", http=http)


def _json(status, body):
    return httpx.Response(status, json=body)


def uruchom(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------ transport

def test_klucz_paper_idzie_na_host_paper_z_naglowkami():
    widziane = {}

    def handler(r: httpx.Request):
        widziane["url"] = str(r.url)
        widziane["key"] = r.headers.get("APCA-API-KEY-ID")
        return _json(200, KONTO)

    a = _adapter(handler)
    wynik = uruchom(a.check_connection())
    assert widziane["url"].startswith("https://paper-api.alpaca.markets/v2/account")
    assert widziane["key"] == "K"
    assert wynik.ok and wynik.account_type == "paper"
    assert wynik.balances[0].total == D("100000")


def test_401_to_AuthError_i_NIE_jest_ponawiane():
    liczba = {"n": 0}

    def handler(r):
        liczba["n"] += 1
        return _json(401, {"code": 40110000, "message": "access key verification failed"})

    with pytest.raises(AuthError):
        uruchom(_adapter(handler).fetch_balances())
    assert liczba["n"] == 1, "zly klucz ponowiony trzy razy nie staje sie dobry"


def test_403_konto_zablokowane_to_KontoZamrozone_a_brak_srodkow_NIE():
    def zablokowane(r):
        return _json(403, {"message": "account is not authorized to trade"})

    def brak_srodkow(r):
        return _json(403, {"code": 40310000, "message": "insufficient buying power"})

    with pytest.raises(KontoZamrozone):
        uruchom(_adapter(zablokowane).fetch_balances())
    with pytest.raises(ExchangeError) as e:
        uruchom(_adapter(brak_srodkow).fetch_balances())
    assert not isinstance(e.value, KontoZamrozone)
    assert not e.value.wymaga_czlowieka


def test_429_i_5xx_sa_ponawiane_a_potem_oddaja_wynik(monkeypatch):
    async def bez_snu(_):
        pass
    monkeypatch.setattr(asyncio, "sleep", bez_snu)
    odpowiedzi = [_json(429, {"message": "too many requests"}), _json(503, {}), _json(200, KONTO)]

    def handler(r):
        return odpowiedzi.pop(0)

    salda = uruchom(_adapter(handler).fetch_balances())
    assert salda[0].available == D("200000")


def test_kod_wyjatku_to_status_HTTP_a_kod_alpaki_idzie_do_tresci():
    def handler(r):
        return _json(422, {"code": 40010001, "message": "client_order_id must be unique"})

    with pytest.raises(ExchangeError) as e:
        uruchom(_adapter(handler).fetch_balances())
    assert e.value.code == "422"
    assert "40010001" in str(e.value)


# ------------------------------------------------------------ dane rynkowe

def test_swiece_ida_z_feedu_iex_rosnaco_i_stronicuja():
    strony = [
        {"bars": {"AAPL": [{"t": "2026-09-22T13:30:00Z", "o": 190, "h": 191, "l": 189.5,
                            "c": 190.5, "v": 1200, "n": 40}]},
         "next_page_token": "tok1"},
        {"bars": {"AAPL": [{"t": "2026-09-22T13:31:00Z", "o": 190.5, "h": 190.9, "l": 190.1,
                            "c": 190.2, "v": 800, "n": 22}]},
         "next_page_token": None},
    ]
    widziane = []

    def handler(r):
        widziane.append(dict(r.url.params))
        assert r.url.host == "data.alpaca.markets"
        return _json(200, strony.pop(0))

    swiece = uruchom(_adapter(handler).fetch_candles("AAPL", "1m", limit=10))
    assert [s.close for s in swiece] == [D("190.5"), D("190.2")]
    assert swiece[0].event_time < swiece[1].event_time
    assert swiece[1].trades == 22
    assert widziane[0]["feed"] == "iex" and widziane[0]["timeframe"] == "1Min"
    assert widziane[1]["page_token"] == "tok1"


def test_nieznany_timeframe_to_blad_a_nie_ciche_1Min():
    with pytest.raises(ValueError):
        uruchom(_adapter(lambda r: _json(200, {})).fetch_candles("AAPL", "3h"))


def test_ksiega_jest_JEDNOPOZIOMOWA_z_najlepszej_oferty():
    def handler(r):
        return _json(200, {"quotes": {"AAPL": {"t": "2026-09-22T13:30:00.123456789Z",
                                               "bp": 190.4, "bs": 3, "ap": 190.6, "as": 5}}})

    k = uruchom(_adapter(handler).fetch_order_book("AAPL"))
    assert len(k.bids) == 1 and len(k.asks) == 1
    assert k.spread == D("0.2")
    assert k.mid == D("190.5")


def test_czas_z_nanosekundami_jest_parsowany():
    t = _czas("2026-09-22T14:30:00.123456789Z")
    assert t.microsecond == 123456 and t.tzinfo is not None


def test_czas_z_przesunieciem_strefy_NIE_gubi_czterech_godzin():
    """Dokladna probka z `/v2/clock` 22.09.2026: pierwsza wersja parsera
    zbierala cyfry strefy do ulamka i oddawala 06:11 UTC zamiast 10:11 UTC."""
    t = _czas("2026-09-22T06:11:51.022556-04:00")
    assert t.hour == 10 and t.minute == 11 and t.microsecond == 22556
    assert _czas("2026-09-22T06:11:51-04:00").hour == 10
    with pytest.raises(ValueError):
        _czas("2026-09-22T06:11:51")


def test_zegar_sesji():
    def handler(r):
        return _json(200, {"timestamp": "2026-09-22T14:30:00Z", "is_open": True,
                           "next_open": "2026-09-23T13:30:00Z",
                           "next_close": "2026-09-22T20:00:00Z"})

    z = uruchom(_adapter(handler).zegar())
    assert z.otwarta and z.nastepne_zamkniecie > z.teraz


# ------------------------------------------------------------ zlecenia

def test_zloz_zlecenie_SPRAWDZA_potem_sklada_i_oddaje_id_gieldy():
    wywolania = []

    def handler(r):
        wywolania.append((r.method, r.url.path))
        if r.url.path == "/v2/orders:by_client_order_id":
            return _json(404, {"code": 40410000, "message": "order not found"})
        if r.method == "POST":
            body = json.loads(r.content)
            assert body == {"symbol": "AAPL", "qty": "10", "side": "buy", "type": "limit",
                            "time_in_force": "day", "limit_price": "190.5",
                            "client_order_id": "dec-77"}
            return _json(200, ZLECENIE)
        raise AssertionError(r.url)

    z = uruchom(_adapter(handler).zloz_zlecenie(symbol="AAPL", strona="Buy", ilosc=D("10"),
                                                cena_limitu=D("190.5"), id_wlasne="dec-77"))
    assert wywolania == [("GET", "/v2/orders:by_client_order_id"), ("POST", "/v2/orders")]
    assert z.id_gieldy == ZLECENIE["id"] and z.status == "new" and not z.juz_istnialo


def test_ponowienie_po_utraconej_odpowiedzi_NIE_sklada_drugiego_zlecenia():
    posty = {"n": 0}

    def handler(r):
        if r.url.path == "/v2/orders:by_client_order_id":
            return _json(200, dict(ZLECENIE, status="partially_filled", filled_qty="4",
                                   filled_avg_price="190.4"))
        posty["n"] += 1
        return _json(200, ZLECENIE)

    z = uruchom(_adapter(handler).zloz_zlecenie(symbol="AAPL", strona="Buy", ilosc=D("10"),
                                                cena_limitu=D("190.5"), id_wlasne="dec-77"))
    assert z.juz_istnialo and z.status == "partial"
    assert posty["n"] == 0


def test_wyscig_dwoch_procesow_duplikat_422_konczy_sie_juz_istnialo():
    """Pierwsze sprawdzenie nie widzi zlecenia (drugi proces zlozyl je chwile
    pozniej), POST dostaje 422 duplikat - wynik ma byc stan istniejacego."""
    sprawdzenia = {"n": 0}

    def handler(r):
        if r.url.path == "/v2/orders:by_client_order_id":
            sprawdzenia["n"] += 1
            if sprawdzenia["n"] == 1:
                return _json(404, {"message": "order not found"})
            return _json(200, ZLECENIE)
        return _json(422, {"code": 40010001, "message": "client_order_id must be unique"})

    z = uruchom(_adapter(handler).zloz_zlecenie(symbol="AAPL", strona="Buy", ilosc=D("10"),
                                                cena_limitu=D("190.5"), id_wlasne="dec-77"))
    assert z.juz_istnialo and z.id_gieldy == ZLECENIE["id"]


def test_stan_zlecenia_odrzuca_CUDZE_zlecenie():
    def handler(r):
        return _json(200, dict(ZLECENIE, client_order_id="dec-78"))

    with pytest.raises(ExchangeError):
        uruchom(_adapter(handler).stan_zlecenia("dec-77"))


def test_stan_zlecenia_mapuje_statusy_i_wypelnienie_czesciowe():
    def handler(r):
        return _json(200, dict(ZLECENIE, status="partially_filled", filled_qty="4",
                               filled_avg_price="190.4"))

    s = uruchom(_adapter(handler).stan_zlecenia("dec-77"))
    assert s.status == "partial" and s.czesciowe
    assert s.wypelniono == D("4") and s.srednia_cena == D("190.4")
    assert s.prowizja == 0, "Alpaca nie pobiera prowizji - zero z cennika, nie z braku pola"


def test_anuluj_zlecenie_False_gdy_juz_wypelnione_lub_nieznane():
    def wypelnione(r):
        return _json(200, dict(ZLECENIE, status="filled", filled_qty="10"))

    def nieznane(r):
        return _json(404, {"message": "order not found"})

    assert uruchom(_adapter(wypelnione).anuluj_zlecenie(symbol="AAPL", id_wlasne="dec-77")) is False
    assert uruchom(_adapter(nieznane).anuluj_zlecenie(symbol="AAPL", id_wlasne="dec-77")) is False


def test_anuluj_zlecenie_otwarte_woła_DELETE_po_id_gieldy():
    wywolania = []

    def handler(r):
        wywolania.append((r.method, r.url.path))
        if r.method == "DELETE":
            return httpx.Response(204)
        return _json(200, ZLECENIE)

    assert uruchom(_adapter(handler).anuluj_zlecenie(symbol="AAPL", id_wlasne="dec-77")) is True
    assert ("DELETE", f"/v2/orders/{ZLECENIE['id']}") in wywolania


# ------------------------------------------------------------ rejestr

def test_wypelnienia_stronicuja_po_id_i_maja_zerowa_prowizje():
    strona1 = [{"id": f"a{i}", "activity_type": "FILL", "symbol": "AAPL", "side": "buy",
                "qty": "1", "price": "190", "transaction_time": "2026-09-22T14:00:00Z"}
               for i in range(100)]
    strona2 = [{"id": "b0", "activity_type": "FILL", "symbol": "MSFT", "side": "sell",
                "qty": "2", "price": "410", "transaction_time": "2026-09-22T13:00:00Z"}]
    widziane = []

    def handler(r):
        widziane.append(dict(r.url.params))
        return _json(200, strona1 if "page_token" not in r.url.params else strona2)

    w = uruchom(_adapter(handler).wypelnienia(od_ms=1_700_000_000_000))
    assert len(w) == 101
    assert widziane[1]["page_token"] == "a99"
    assert w[-1].strona == "Sell" and w[-1].prowizja == 0
    assert "after" in widziane[0]


def test_pozycje_ze_znakiem_dla_shorta():
    def handler(r):
        return _json(200, [{"symbol": "AAPL", "qty": "10", "side": "long"},
                           {"symbol": "TSLA", "qty": "3", "side": "short"}])

    p = uruchom(_adapter(handler).pozycje())
    assert p == {"AAPL": D("10"), "TSLA": D("-3")}


def test_stan_konta_i_pozycje_z_wycena_gieldy():
    def handler(r):
        if r.url.path == "/v2/account":
            return _json(200, dict(KONTO, equity="100250.5", cash="60000",
                                   long_market_value="40250.5", short_market_value="0"))
        return _json(200, [{"symbol": "AAPL", "qty": "100", "side": "long",
                            "avg_entry_price": "400", "market_value": "40250.5",
                            "unrealized_pl": "250.5"}])

    a = _adapter(handler)
    st = uruchom(a.stan_konta())
    assert st["equity"] == D("100250.5") and st["ekspozycja"] == D("40250.5")
    poz = uruchom(a.pozycje_szczegoly())
    assert poz[0]["unrealized_pl"] == D("250.5") and poz[0]["qty"] == D("100")
