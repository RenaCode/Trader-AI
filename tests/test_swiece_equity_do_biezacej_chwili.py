"""Świece 1m equity sięgają bieżącej chwili, a puls widzi, gdy nie sięgają (audyt 3 T1).

DO 2026-10-03 `AlpacaAdapter.fetch_candles` bez `start` wysyłał `sort=asc`
i `limit=200`. Alpaca bierze wtedy `start` = początek dnia i oddaje PIERWSZE 200
świec, więc `market-equity` zapisywał 13:30–16:50 UTC i nic więcej. Każdy z 21
symboli miał dokładnie 200 świec dziennie, a puls meldował `bledy 0`.

Atrapą jest tu transport HTTP — ale atrapa ZACHOWUJE SIĘ JAK ALPACA: honoruje
`start`, `sort`, `limit` i `page_token`, a bez `start` liczy od początku dnia.
Atrapa, która oddaje zawsze to samo niezależnie od parametrów, przepuściłaby
stary kod.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from services.exchanges import alpaca
from services.exchanges.alpaca import AlpacaAdapter
from services.exchanges.base import Candle
from services.ingest import market_worker
from tests.test_worker_rynku_lista_i_licznik import (Gielda, Koniec, Polaczenie,
                                                    instrument)

OTWARCIE = datetime(2026, 10, 2, 13, 30, tzinfo=timezone.utc)
# 300 minut sesji: 13:30 .. 18:29 - więcej niż `limit=200` workera.
SWIECE_DNIA = [OTWARCIE + timedelta(minutes=i) for i in range(300)]


def _czas(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _alpaca(widziane: list[dict], swiece=SWIECE_DNIA):
    """Atrapa `/v2/stocks/bars` z semantyką dostawcy."""
    def handler(r: httpx.Request):
        p = dict(r.url.params)
        widziane.append(p)
        start = _czas(p["start"]) if "start" in p else OTWARCIE.replace(hour=0, minute=0)
        wybrane = [t for t in swiece if t >= start]
        if p.get("sort", "asc") == "desc":
            wybrane.reverse()
        od = int(p.get("page_token", 0))
        limit = int(p["limit"])
        strona = wybrane[od:od + limit]
        token = str(od + limit) if od + limit < len(wybrane) else None
        return httpx.Response(200, json={
            "bars": {"AAPL": [{"t": t.isoformat().replace("+00:00", "Z"), "o": 1,
                               "h": 1, "l": 1, "c": 1, "v": 1, "n": 1} for t in strona]},
            "next_page_token": token})
    return AlpacaAdapter("K", "S", mode="demo",
                         http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_bez_start_przychodza_NAJNOWSZE_swiece_a_nie_pierwsze_200_dnia():
    widziane: list[dict] = []
    swiece = asyncio.run(_alpaca(widziane).fetch_candles("AAPL", "1m", limit=200))

    assert swiece[-1].event_time == SWIECE_DNIA[-1], (
        f"ostatnia świeca {swiece[-1].event_time:%H:%M}, a sesja trwa do "
        f"{SWIECE_DNIA[-1]:%H:%M} - adapter oddał początek dnia zamiast końca")
    assert len(swiece) == 200
    assert [s.event_time for s in swiece] == SWIECE_DNIA[-200:], "kontrakt: rosnąco"
    assert "start" in widziane[0], "bez `start` Alpaca liczy od początku dnia"


def test_najnowsze_siegaja_przez_weekend():
    """Poniedziałkowy cykl przed otwarciem widzi ogon piątku, nie pustkę."""
    widziane: list[dict] = []
    asyncio.run(_alpaca(widziane).fetch_candles("AAPL", "1m", limit=200))
    start = _czas(widziane[0]["start"])
    assert datetime.now(timezone.utc) - start >= timedelta(days=3)


def test_z_start_zostaje_rosnaco_od_start_jak_dla_backfillu():
    widziane: list[dict] = []
    od = SWIECE_DNIA[10]
    swiece = asyncio.run(_alpaca(widziane).fetch_candles("AAPL", "1m", limit=5, start=od))
    assert [s.event_time for s in swiece] == SWIECE_DNIA[10:15]
    assert widziane[0]["sort"] == "asc"


# ------------------------------------------------------------ limit zapytań

def _adapter_z_odpowiedziami(odpowiedzi):
    def handler(r):
        return odpowiedzi.pop(0)
    return AlpacaAdapter("K", "S", mode="demo",
                         http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _sen(monkeypatch) -> list[float]:
    spane: list[float] = []

    async def spij(s):
        spane.append(s)
    monkeypatch.setattr(asyncio, "sleep", spij)
    return spane


KONTO = {"currency": "USD", "cash": "1", "buying_power": "1"}


def test_429_czeka_do_odnowienia_okna_a_nie_pol_sekundy(monkeypatch):
    """Limit jest MINUTOWY. Stare 0,5-1-2 s zużywało trzy próby w 3,5 s."""
    spane = _sen(monkeypatch)
    import time
    monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
    a = _adapter_z_odpowiedziami([
        httpx.Response(429, json={"message": "too many requests"},
                       headers={"X-RateLimit-Reset": "1000042"}),
        httpx.Response(200, json=KONTO)])
    asyncio.run(a.fetch_balances())
    assert spane == [pytest.approx(42.0)]


def test_429_bez_naglowka_czeka_pelne_okno(monkeypatch):
    spane = _sen(monkeypatch)
    a = _adapter_z_odpowiedziami([
        httpx.Response(429, json={"message": "too many requests"}),
        httpx.Response(200, json=KONTO)])
    asyncio.run(a.fetch_balances())
    assert spane == [alpaca.CZEKANIE_LIMITU_MAX_S]


def test_konczacy_sie_limit_wspolnego_klucza_wstrzymuje_PRZED_429(monkeypatch):
    """Inne procesy na tym samym kluczu nie widzą naszego licznika - widzą
    tylko to, co zostało u Alpaki. Zużycie reszty wywraca ICH zapytanie."""
    spane = _sen(monkeypatch)
    import time
    monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
    a = _adapter_z_odpowiedziami([httpx.Response(
        200, json=KONTO,
        headers={"X-RateLimit-Remaining": "3", "X-RateLimit-Reset": "1000020"})])
    asyncio.run(a.fetch_balances())
    assert spane == [pytest.approx(20.0)]


def test_zapas_limitu_nie_spowalnia_normalnej_pracy(monkeypatch):
    spane = _sen(monkeypatch)
    a = _adapter_z_odpowiedziami([httpx.Response(
        200, json=KONTO,
        headers={"X-RateLimit-Remaining": "150", "X-RateLimit-Reset": "9999999999"})])
    asyncio.run(a.fetch_balances())
    assert spane == []


def test_zegar_alpaki_z_przyszlosci_nie_zawiesza_workera(monkeypatch):
    spane = _sen(monkeypatch)
    a = _adapter_z_odpowiedziami([
        httpx.Response(429, json={}, headers={"X-RateLimit-Reset": "9999999999"}),
        httpx.Response(200, json=KONTO)])
    asyncio.run(a.fetch_balances())
    assert spane == [alpaca.CZEKANIE_LIMITU_MAX_S]


# ------------------------------------------------------------ puls i świeżość

def test_prog_workera_to_prog_silnika():
    from services.engine import loop
    assert market_worker.SWIECA_STARA_PO_MIN == loop.CENA_STARA_PO_MIN


T = datetime(2026, 10, 5, 18, 0, tzinfo=timezone.utc)


def test_nieswieze_liczy_od_pozniejszej_z_dwoch_chwil():
    sesja = T - timedelta(hours=4)
    ostatnie = {"AAPL": T - timedelta(minutes=2),
                "LLY": T - timedelta(minutes=16),
                "NOWY": None}
    assert market_worker.nieswieze(ostatnie, T, sesja) == ["LLY", "NOWY"]
    # Tuż po otwarciu wczorajsza świeca NIE jest alarmem.
    assert market_worker.nieswieze(ostatnie, T, T - timedelta(minutes=5)) == []


def test_awaria_cyklu_dopiero_przy_polowie_uniwersum():
    inst = [instrument(i, f"S{i}") for i in range(4)]
    assert market_worker.awaria_swiezosci(["S0"], inst) is None
    assert "2 z 4" in market_worker.awaria_swiezosci(["S0", "S1"], inst)


class ZegarDT(datetime):
    """`datetime.now` przesuwany o 6 minut na każde wywołanie."""
    krok = 0

    @classmethod
    def now(cls, tz=None):
        cls.krok += 1
        return T + timedelta(minutes=6 * cls.krok)


class GieldaZSesja(Gielda):
    """Alpaca po starej poprawce: oddaje wciąż te same świece z początku dnia."""

    def __init__(self, otwarta: bool):
        super().__init__()
        self.otwarta = otwarta

    async def fetch_candles(self, symbol, timeframe, limit=200):
        return [Candle(event_time=T - timedelta(minutes=30), open=1, high=1, low=1,
                       close=1, volume=1, trades=1)]

    async def zegar(self):
        class Z:
            otwarta = self.otwarta
        return Z()


def _uruchom(monkeypatch, gielda):
    polaczenia: list[Polaczenie] = []
    inst = [instrument(1, "AAPL"), instrument(2, "LLY")]

    def connect(url, **kw):
        c = Polaczenie(inst)
        polaczenia.append(c)
        return c

    licznik = {"n": 0}

    async def sen(_s):
        licznik["n"] += 1
        if licznik["n"] >= 20:
            raise Koniec
    ZegarDT.krok = 0
    monkeypatch.setattr(market_worker, "datetime", ZegarDT)
    monkeypatch.setattr(asyncio, "sleep", sen)
    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(market_worker.psycopg, "connect", connect)
    monkeypatch.setattr(market_worker, "adapter_danych", lambda conn, engine, **kw: gielda)
    monkeypatch.setattr(sys, "argv", ["market_worker.py", "--interval", "60",
                                      "--engine", "equity"])
    try:
        wynik = asyncio.run(market_worker.main())
    except Koniec:
        wynik = None
    zapisy = [(sql, p) for c in polaczenia for sql, p in c.zapytania
              if "risk.worker_heartbeat" in sql]
    return wynik, zapisy


def test_stare_swiece_w_sesji_to_awaria_pulsu_a_nie_bledy_0(monkeypatch):
    wynik, zapisy = _uruchom(monkeypatch, GieldaZSesja(otwarta=True))
    awarie = [p for sql, p in zapisy if sql.upper().startswith("UPDATE")]
    assert awarie, "świece sprzed kwadransa w otwartej sesji, a puls bije dalej"
    assert any("starsze niż 15 min" in str(p) for p in awarie)
    assert wynik == 1, "pięć cykli bez świeżych świec kończy proces jak każda awaria"


def test_zamkniety_rynek_nie_jest_awaria(monkeypatch):
    wynik, zapisy = _uruchom(monkeypatch, GieldaZSesja(otwarta=False))
    assert wynik is None
    assert all(sql.upper().startswith("INSERT") for sql, _ in zapisy)
    assert zapisy[-1][1][-1].obj["nieswieze"] == []
