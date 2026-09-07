"""Składanie zleceń: idempotencja i wypełnienia częściowe.

DLACZEGO TO JEST NAJOSTROŻNIEJ TESTOWANY KAWAŁEK

Ponawianie GET-a jest niegroźne — najwyżej pobierzemy te same świece dwa razy.
Ponawianie POST-a po przekroczeniu czasu jest DOKŁADNIE tym mechanizmem, który
tworzy drugą pozycję: odpowiedź mogła nie dotrzeć, mimo że zlecenie weszło.

Objaw podwójnej pozycji nie wygląda na awarię. Wygląda na strategię, która
weszła mocniej, niż zakładał risk kernel — i dopiero uzgodnienie z giełdą
albo obsunięcie dwa razy większe od zmierzonego to pokazuje.
"""
import asyncio
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from services.exchanges.base import ExchangeError
from services.exchanges.bybit import BybitAdapter, _status_bybita


def gielda(handler):
    k = BybitAdapter(api_key="k", api_secret="s")
    k._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return k


def pusto():
    return {"retCode": 0, "result": {"list": []}}


def zlecenie(status="New", qty="1", wypelniono="0", link="dec-42"):
    return {"retCode": 0, "result": {"list": [{
        "orderId": "gielda-1", "orderLinkId": link, "orderStatus": status,
        "qty": qty, "cumExecQty": wypelniono, "avgPrice": "64000",
        "cumExecFee": "0.5",
    }]}}


# --------------------------------------------------------------- idempotencja

def test_zlecenie_o_znanym_id_NIE_JEST_skladane_drugi_raz():
    """Sedno. Poprzednia próba skończyła się timeoutem, ale zlecenie weszło —
    ponowne wywołanie musi je ODNALEŹĆ, a nie złożyć kolejne."""
    zapytania = []

    def handler(req):
        zapytania.append((req.method, str(req.url).split("?")[0]))
        if "/order/realtime" in str(req.url):
            return httpx.Response(200, json=zlecenie())
        return httpx.Response(200, json=pusto())

    k = gielda(handler)
    wynik = asyncio.run(k.zloz_zlecenie(symbol="BTCUSDC", strona="buy",
                                        ilosc=Decimal("1"), cena_limitu=Decimal("64000"),
                                        id_wlasne="dec-42"))
    assert wynik.juz_istnialo is True
    assert wynik.id_gieldy == "gielda-1"
    assert not any(m == "POST" for m, _ in zapytania), (
        f"złożono zlecenie mimo że już istniało: {zapytania}")


def test_zlecenie_nieznane_gieldzie_JEST_skladane():
    """Kanarek do poprzedniego. Gdyby sprawdzenie zawsze mówiło 'jest',
    system nie złożyłby nigdy żadnego zlecenia — i wyglądałoby to jak
    strategia bez sygnałów."""
    metody = []

    def handler(req):
        metody.append(req.method)
        if req.method == "POST":
            return httpx.Response(200, json={"retCode": 0,
                                             "result": {"orderId": "nowe-1"}})
        return httpx.Response(200, json=pusto())

    k = gielda(handler)
    wynik = asyncio.run(k.zloz_zlecenie(symbol="BTCUSDC", strona="buy",
                                        ilosc=Decimal("1"), cena_limitu=Decimal("64000"),
                                        id_wlasne="dec-99"))
    assert wynik.juz_istnialo is False
    assert wynik.id_gieldy == "nowe-1"
    assert "POST" in metody


def test_stan_zlecenia_szuka_TAKZE_w_historii():
    """Zlecenie mogło się wypełnić i zniknąć z listy otwartych między naszym
    timeoutem a zapytaniem. Szukanie wyłącznie wśród otwartych dałoby wtedy
    'nie ma' — czyli zgodę na złożenie drugiego."""
    sciezki = []

    def handler(req):
        sciezki.append(str(req.url).split("?")[0])
        if "/order/history" in str(req.url):
            return httpx.Response(200, json=zlecenie(status="Filled", wypelniono="1"))
        return httpx.Response(200, json=pusto())

    k = gielda(handler)
    stan = asyncio.run(k.stan_zlecenia("dec-42"))
    assert stan is not None and stan.status == "filled"
    assert any("history" in s for s in sciezki), "nie sprawdzono historii"


def test_POST_nie_jest_ponawiany_i_MOWI_co_zrobic():
    """Timeout przy składaniu nie może prowadzić do ponowienia w górę stosu.
    Komunikat musi kierować na SPRAWDZENIE, bo to jedyna bezpieczna reakcja."""
    def handler(req):
        if req.method == "POST":
            raise httpx.TimeoutException("brak odpowiedzi")
        return httpx.Response(200, json=pusto())

    k = gielda(handler)
    with pytest.raises(ExchangeError) as e:
        asyncio.run(k.zloz_zlecenie(symbol="BTCUSDC", strona="buy",
                                    ilosc=Decimal("1"), cena_limitu=Decimal("64000"),
                                    id_wlasne="dec-7"))
    assert e.value.retryable is False, "timeout przy POST oznaczony jako ponawialny"
    assert "sprawdz" in str(e.value).lower()


# --------------------------------------------------- wypełnienia częściowe

@pytest.mark.parametrize("ilosc,wypelniono,czesciowe", [
    ("1", "0", False),      # nic nie weszło
    ("1", "0.4", True),     # połowa
    ("1", "1", False),      # całość
])
def test_wypelnienie_czesciowe_jest_ROZPOZNAWANE(ilosc, wypelniono, czesciowe):
    """Symulacja zna dwa stany: wypełnione albo nie. Giełda zna trzeci."""
    def handler(req):
        return httpx.Response(200, json=zlecenie(status="PartiallyFilled",
                                                 qty=ilosc, wypelniono=wypelniono))

    stan = asyncio.run(gielda(handler).stan_zlecenia("dec-42"))
    assert stan.czesciowe is czesciowe, (
        f"{wypelniono} z {ilosc}: czesciowe={stan.czesciowe}, oczekiwano {czesciowe}")


def test_nieznany_status_zostaje_NIEZNANY():
    """Mapowanie słownikiem, nie łańcuchem if-ów: status, którego nie znamy,
    ma się rzucać w oczy, a nie wpaść do 'new' przez przypadek — bo 'new'
    znaczy 'czekamy', a to najgorszy domysł przy statusie nierozpoznanym."""
    assert _status_bybita("PartiallyFilled") == "partial"
    assert _status_bybita("Filled") == "filled"
    assert _status_bybita("CosCzegoNieZnamy").startswith("nieznany:")


def test_anulowanie_nieistniejacego_zlecenia_NIE_JEST_awaria():
    """Przy anulowaniu 'zlecenia nie ma' to sukces: cel osiągnięty."""
    def handler(req):
        return httpx.Response(200, json={"retCode": 110001, "retMsg": "order not exists"})

    assert asyncio.run(gielda(handler).anuluj_zlecenie(
        symbol="BTCUSDC", id_wlasne="dec-1")) is False


def test_identyfikator_wlasny_musi_byc_WYPROWADZONY_z_decyzji():
    """Idempotencja stoi na tym, że ponowienie użyje TEGO SAMEGO identyfikatora.
    Wyprowadzony z czasu albo losowy dawałby przy każdej próbie nowy — i cała
    ochrona znikałaby dokładnie wtedy, gdy jest potrzebna."""
    zrodlo = Path("services/exchanges/bybit.py").read_text(encoding="utf-8")
    fragment = zrodlo.split("async def zloz_zlecenie")[1].split("async def")[0]
    assert "uuid" not in fragment and "time.time()" not in fragment, (
        "identyfikator własny nie może powstawać w adapterze — musi przyjść z decyzji")
