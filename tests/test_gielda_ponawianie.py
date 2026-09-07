"""Ponawianie u giełdy: flaga istniała, wykonania nie było.

`ExchangeError` niósł flagę `retryable` od początku, ustawianą poprawnie dla
przekroczeń czasu, błędów połączenia, HTTP 429 i 5xx. Czytali ją dokładnie
dwaj odbiorcy — backfill i funding — i tylko po to, żeby przerwać własną pętlę.
Nigdzie nie było odczekania ani ponowienia.

Flaga opisywała więc intencję, której nikt nie wykonywał. Ten sam kształt co
brak ponawiania u dostawcy analizy wiadomości, gdzie objawiało się to jako
„błędów 7" i cisza.
"""
import asyncio
from pathlib import Path

import httpx
import pytest

from services.exchanges.base import ExchangeError
from services.exchanges.bybit import PROBY, BybitAdapter


def bez_czekania(monkeypatch):
    async def natychmiast(_s):
        return None
    monkeypatch.setattr(asyncio, "sleep", natychmiast)


def gielda(odpowiedzi: list[tuple[int, dict]]):
    """Klient z podstawionym transportem. Zwraca (klient, licznik zapytań)."""
    licznik = {"n": 0}

    def handler(req):
        i = min(licznik["n"], len(odpowiedzi) - 1)
        licznik["n"] += 1
        status, tresc = odpowiedzi[i]
        return httpx.Response(status, json=tresc)

    k = BybitAdapter(api_key="k", api_secret="s")
    k._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return k, licznik


OK = (200, {"retCode": 0, "result": {"list": []}})
LIMIT = (429, {"retCode": 10006, "retMsg": "too many visits"})
AWARIA = (503, {"retMsg": "service unavailable"})
ZLY_KLUCZ = (401, {"retCode": 10003, "retMsg": "API key is invalid"})


@pytest.mark.parametrize("przejsciowy", [LIMIT, AWARIA])
def test_stan_przejsciowy_jest_PONAWIANY(monkeypatch, przejsciowy):
    """429 i 5xx to stany przejściowe. Pierwsze niepowodzenie nie może kończyć
    zapytania — dane rynkowe wrócą za sekundę."""
    bez_czekania(monkeypatch)
    k, licznik = gielda([przejsciowy, przejsciowy, OK])
    asyncio.run(k._get("/v5/market/kline", {"symbol": "BTCUSDC"}, private=False))
    assert licznik["n"] == 3, f"powinny być trzy próby, było {licznik['n']}"


def test_bledny_klucz_NIE_jest_ponawiany(monkeypatch):
    """Kanarek do poprzedniego. Zły klucz nie jest stanem przejściowym —
    ponawianie go tylko opóźnia komunikat o prawdziwej przyczynie i zużywa
    limit zapytań."""
    bez_czekania(monkeypatch)
    k, licznik = gielda([ZLY_KLUCZ])
    with pytest.raises(ExchangeError):
        asyncio.run(k._get("/v5/account/info", private=True))
    assert licznik["n"] == 1, f"401 ma iść bez ponawiania, prób było {licznik['n']}"


def test_wyczerpane_proby_oddaja_ORYGINALNY_blad(monkeypatch):
    """Po ostatniej próbie leci ten sam wyjątek, który przyszedł z giełdy —
    a nie własny komunikat o wyczerpaniu prób. Inaczej odbiorca traci kod
    błędu, na którym opiera decyzję."""
    bez_czekania(monkeypatch)
    k, licznik = gielda([LIMIT])
    with pytest.raises(ExchangeError) as e:
        asyncio.run(k._get("/v5/market/kline", private=False))
    assert e.value.code == "429"
    assert licznik["n"] == PROBY


def test_podpis_jest_ODNAWIANY_przy_kazdej_probie(monkeypatch):
    """Podpis zawiera znacznik czasu, a recv_window u Bybita to pięć sekund.
    Ponowienie ze starym podpisem po odczekaniu zostałoby odrzucone jako błąd
    uwierzytelnienia — czyli skierowałoby szukanie na klucz zamiast na sieć."""
    bez_czekania(monkeypatch)
    podpisy = []

    def handler(req):
        podpisy.append(req.headers.get("X-BAPI-TIMESTAMP"))
        return httpx.Response(429 if len(podpisy) < 3 else 200,
                              json={"retCode": 0, "result": {}})

    k = BybitAdapter(api_key="k", api_secret="s")
    k._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    asyncio.run(k._get("/v5/account/info", private=True))
    assert len(podpisy) == 3
    assert all(p is not None for p in podpisy), "brak znacznika czasu w podpisie"


def test_flaga_retryable_ma_WYKONAWCE():
    """Sedno zmiany. Flaga bez pętli, która ją czyta, jest deklaracją intencji."""
    zrodlo = Path("services/exchanges/bybit.py").read_text(encoding="utf-8")
    assert "if not e.retryable or proba == PROBY:" in zrodlo
    assert "await asyncio.sleep(" in zrodlo, "brak odczekania między próbami"
