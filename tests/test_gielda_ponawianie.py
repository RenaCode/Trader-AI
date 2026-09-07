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


# ------------------------------------------------------------- dryf zegara
#
# Podpis zapytania prywatnego zawiera znacznik czasu, a recv_window to pięć
# sekund. Zegar rozjechany o więcej powoduje odrzucanie WSZYSTKICH zapytań
# prywatnych — i objawia się jako błąd uwierzytelnienia, czyli kieruje szukanie
# na klucz zamiast na czas.

import time as _time

from services.exchanges.bybit import RECV_WINDOW


def gielda_z_czasem(przesuniecie_ms: int):
    """Giełda, której zegar jest przesunięty o zadaną wartość."""
    def handler(req):
        # timeSecond siedzi w `result`, tak jak w prawdziwym API - pierwsza
        # wersja tego mocka kladla go na wierzchu i test padal na wlasnym
        # bledzie, nie na kodzie.
        return httpx.Response(200, json={
            "retCode": 0,
            "result": {"timeSecond": str(int(_time.time()) + przesuniecie_ms // 1000)},
        })
    k = BybitAdapter(api_key="k", api_secret="s")
    k._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return k


def test_zegar_zgodny_nie_ostrzega():
    dryf, ostrzezenie = asyncio.run(gielda_z_czasem(0).dryf_zegara())
    assert ostrzezenie is None, f"zgodny zegar nie ma o czym mówić, a mówi: {ostrzezenie}"
    assert abs(dryf) < 2000


def test_zegar_ponad_oknem_mowi_ZE_TO_NIE_KLUCZ():
    """Sedno tego sprawdzenia. Bez niego objaw — odrzucane zapytania prywatne —
    wygląda dokładnie jak zły klucz, więc szukanie idzie w złą stronę."""
    dryf, ostrzezenie = asyncio.run(gielda_z_czasem(20_000).dryf_zegara())
    assert ostrzezenie is not None
    assert "NIE" in ostrzezenie and "kluczem" in ostrzezenie, (
        f"komunikat musi wykluczyć klucz, a brzmi: {ostrzezenie}")
    assert dryf > int(RECV_WINDOW)


def test_zegar_blisko_granicy_ostrzega_ZANIM_zacznie_boleć():
    """Połowa okna to jeszcze nie awaria, ale zapas topnieje. Ostrzeżenie
    dopiero po przekroczeniu przychodziłoby razem z awarią, a nie przed nią."""
    # 4 s, nie 3: czas gieldy przychodzi w PELNYCH SEKUNDACH, wiec obciecie
    # daje do 1 s pozornego dryfu. Przy 3 s test bylby chwiejny w zaleznosci
    # od tego, w ktorym ulamku sekundy sie uruchomi.
    _, ostrzezenie = asyncio.run(gielda_z_czasem(4_000).dryf_zegara())
    assert ostrzezenie is not None and "zapas topnieje" in ostrzezenie


def test_sprawdzenie_czasu_idzie_endpointem_PUBLICZNYM():
    """Cała wartość tego sprawdzenia. Gdyby wymagało podpisu, nie
    odpowiedziałoby dokładnie w sytuacji, dla której istnieje — bo podpis
    z rozjechanym zegarem jest właśnie tym, co przestaje działać."""
    zrodlo = Path("services/exchanges/bybit.py").read_text(encoding="utf-8")
    fragment = zrodlo.split("async def dryf_zegara")[1].split("async def")[0]
    assert 'private=False' in fragment, "sprawdzenie czasu wymaga podpisu"
