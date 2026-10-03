"""Worker rynku: jeden wadliwy symbol nie wylacza zbierania dla wszystkich (audyt G2).

DO 2026-10-03 kazde nieudane pobranie konczylo cykl porazka. Jeden instrument
z trwalym bledem gieldy dawal piec nieudanych cykli i koniec procesu,
a CrashLoopBackOff zostawial dziury w swiecach 1m i `book_snapshot` dla
WSZYSTKICH instrumentow. Restart niczego nie naprawial - ten sam symbol padal
znowu.

Trzy rzeczy, ktore te testy pilnuja:
  1. symbol z bledem trwalym NIE konczy procesu, a pozostale zbieraja dalej,
  2. taki symbol NIE ZNIKA w ciszy - po `POD_RZAD_MAX` porazkach trafia do
     `detail` pulsu jako `bez_danych`,
  3. awaria CALEJ gieldy (wszystkie symbole) nadal konczy proces po pieciu.
Do tego polaczenie z baza otwierane co cykl, a nie raz na proces.
"""
from __future__ import annotations

import asyncio
import sys


from services.exchanges.base import ExchangeError
from services.ingest import market_worker
from tests.test_worker_rynku_lista_i_licznik import (Gielda, Koniec, Polaczenie,
                                                    instrument)


class GieldaZWadliwym(Gielda):
    def __init__(self, wadliwe: set[str]):
        super().__init__()
        self.wadliwe = wadliwe

    async def fetch_candles(self, symbol, timeframe, limit=200):
        if symbol in self.wadliwe:
            self.pytane.append(symbol)
            raise ExchangeError(f"170xxx: {symbol} zawieszony")
        return await super().fetch_candles(symbol, timeframe, limit)


def _uruchom(monkeypatch, gielda, instrumenty, cykli):
    polaczenia: list[Polaczenie] = []

    def connect(url, **kw):
        c = Polaczenie(instrumenty)
        polaczenia.append(c)
        return c

    licznik = {"n": 0}

    async def natychmiast(_s):
        licznik["n"] += 1
        if licznik["n"] >= cykli:
            raise Koniec
    monkeypatch.setattr(asyncio, "sleep", natychmiast)
    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(market_worker.psycopg, "connect", connect)
    monkeypatch.setattr(market_worker, "adapter_danych",
                        lambda conn, engine, **kw: gielda)
    monkeypatch.setattr(sys, "argv", ["market_worker.py", "--interval", "60"])
    try:
        wynik = asyncio.run(market_worker.main())
    except Koniec:
        wynik = None
    return wynik, polaczenia


def _pulsy(polaczenia):
    out = []
    for c in polaczenia:
        for sql, p in c.zapytania:
            if "risk.worker_heartbeat" in sql:
                out.append(("udany", p[-1].obj) if sql.upper().startswith("INSERT")
                           else ("awaria", p))
    return out


def test_jeden_wadliwy_symbol_NIE_konczy_procesu(monkeypatch):
    g = GieldaZWadliwym({"XRPUSDC"})
    inst = [instrument(1, "BTCUSDC"), instrument(2, "XRPUSDC"), instrument(3, "ETHUSDC")]
    cykli = 3 * market_worker.POD_RZAD_MAX
    wynik, polaczenia = _uruchom(monkeypatch, g, inst, cykli)

    assert wynik is None, (
        "proces zakonczyl sie przez jeden symbol - CrashLoop zostawilby dziury "
        "w swiecach WSZYSTKICH instrumentow")
    assert g.pytane.count("BTCUSDC") == cykli, "zdrowe symbole przestaly byc zbierane"
    assert {r for r, _ in _pulsy(polaczenia)} == {"udany"}


def test_symbol_milczacy_uporczywie_trafia_do_pulsu(monkeypatch):
    g = GieldaZWadliwym({"XRPUSDC"})
    inst = [instrument(1, "BTCUSDC"), instrument(2, "XRPUSDC")]
    _, polaczenia = _uruchom(monkeypatch, g, inst, market_worker.POD_RZAD_MAX + 1)
    detale = [d for r, d in _pulsy(polaczenia) if r == "udany"]

    assert detale[0]["bez_danych"] == [], "pojedyncza porazka to jeszcze nie stan trwaly"
    assert detale[0]["bledy"] == 1
    assert detale[-1]["bez_danych"] == ["XRPUSDC"], (
        "symbol bez danych od pieciu cykli nie doszedl do bazy - cykl uznany "
        "za udany twierdzilby, ze zebrano wszystko")


def test_symbol_ktory_ozyl_znika_z_listy():
    licz: dict[str, int] = {}
    inst = [instrument(1, "A"), instrument(2, "B")]
    for _ in range(market_worker.POD_RZAD_MAX):
        market_worker.ocen_cykl({"bledy": 1, "nieudane": ["B"]}, inst, licz)
    assert market_worker.ocen_cykl({"bledy": 1, "nieudane": ["B"]}, inst, licz)[1] == ["B"]
    assert market_worker.ocen_cykl({"bledy": 0, "nieudane": []}, inst, licz) == (None, [])
    assert licz == {}


def test_awaria_CALEJ_gieldy_nadal_konczy_proces(monkeypatch):
    """Zabezpieczenie zostaje: martwa siec to nie symbol."""
    g = GieldaZWadliwym({"BTCUSDC", "ETHUSDC"})
    inst = [instrument(1, "BTCUSDC"), instrument(2, "ETHUSDC")]
    wynik, polaczenia = _uruchom(monkeypatch, g, inst, 100)
    assert wynik == 1
    assert [r for r, _ in _pulsy(polaczenia)] == ["awaria"] * market_worker.POD_RZAD_MAX


def test_polaczenie_z_baza_otwierane_CO_CYKL(monkeypatch):
    """Jedno polaczenie na proces po restarcie bazy zrywa sie raz i juz sie
    nie naprawia - kazdy kolejny cykl padalby na martwym gniezdzie."""
    _, polaczenia = _uruchom(monkeypatch, Gielda(), [instrument(1, "BTCUSDC")], 4)
    # pierwsze polaczenie buduje adapter, kolejne - po jednym na cykl
    assert len(polaczenia) == 1 + 4


def test_baza_nieosiagalna_to_awaria_cyklu_a_nie_wyjatek(monkeypatch):
    stan = {"n": 0}

    def connect(url, **kw):
        stan["n"] += 1
        if stan["n"] > 1:
            raise OSError("connection refused")
        return Polaczenie([instrument(1, "BTCUSDC")])

    async def natychmiast(_s):
        pass
    monkeypatch.setattr(asyncio, "sleep", natychmiast)
    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(market_worker.psycopg, "connect", connect)
    monkeypatch.setattr(market_worker, "adapter_danych",
                        lambda conn, engine, **kw: Gielda())
    monkeypatch.setattr(sys, "argv", ["market_worker.py", "--interval", "60"])
    assert asyncio.run(market_worker.main()) == 1
    assert stan["n"] == 1 + market_worker.POD_RZAD_MAX
