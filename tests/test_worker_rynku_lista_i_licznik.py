"""Worker rynku: lista instrumentów odświeżana w trakcie pracy.

LISTA CZYTANA RAZ NA PROCES

`market_worker.main` czytał `ref.instrument` PRZED pętlą `while True`, więc zmiana
w bazie działała dopiero po restarcie poda — a pod procesu tła nie restartuje się
sam. Instrument wyłączony był odpytywany dalej, a instrument DOPISANY nie zbierał
danych, dopóki ktoś nie zauważył, że ich nie ma. Drugi kierunek jest gorszy, bo
nie widać go w żadnym logu: brak wiersza wygląda jak rynek bez zmian.

CO TU JEST ATRAPĄ, A CO MIERZONE. Atrapą jest baza i giełda; mierzona jest
`market_worker.main` — prawdziwa, nieprzepisana.
Nic nie łączy się z siecią ani z produkcyjną bazą.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from services.exchanges.base import BookLevel, Candle, OrderBook
from services.ingest import market_worker

T_OFERTY = datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)


class Koniec(Exception):
    """Sygnał zatrzymania pętli workera — rzucany z atrapy `asyncio.sleep`."""


# ------------------------------------------------------------ atrapa giełdy

class Gielda:
    """Adapter danych: świeca z przeszłości i ZAWSZE TEN SAM znacznik oferty.

    Stały `event_time` oferty nie jest tu uproszczeniem — jest odtworzeniem
    zmierzonego zachowania. Giełda poza sesją oddaje ostatnią znaną ofertę, więc
    każdy kolejny cykl trafia na `ON CONFLICT` z tym samym kluczem.
    """

    venue_code = "atrapa"

    def __init__(self):
        self.pytane: list[str] = []

    async def fetch_candles(self, symbol, timeframe, limit=200):
        self.pytane.append(symbol)
        return [Candle(event_time=datetime(2026, 9, 1, tzinfo=timezone.utc),
                       open=Decimal(1), high=Decimal(2), low=Decimal(1),
                       close=Decimal(2), volume=Decimal(10), trades=3)]

    async def fetch_order_book(self, symbol, depth=25, *, ksiega_egzekucji=True):
        return OrderBook(event_time=T_OFERTY,
                         bids=[BookLevel(price=Decimal(1), qty=Decimal(1))],
                         asks=[BookLevel(price=Decimal(2), qty=Decimal(1))])

    async def close(self):
        pass


# ------------------------------------------------------------- atrapa bazy

class Kursor:
    """Kursor oddający listę instrumentów i raportujący zadany `rowcount`.

    `rowcount_arkusza` odtwarza `ON CONFLICT DO NOTHING`: zero znaczy „wiersz
    już był, nic nie wstawiono".
    """

    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0
        self._odp: list[dict] = []

    def execute(self, sql, params=None):
        self.conn.zapytania.append((" ".join(sql.split()), params))
        if "ref.instrument" in sql and sql.lstrip().upper().startswith("SELECT"):
            self.conn.odczyty_listy += 1
            self._odp = list(self.conn.instrumenty)
            self.rowcount = len(self._odp)
        elif "market.book_snapshot" in sql:
            self.rowcount = self.conn.rowcount_arkusza
        else:
            self.rowcount = 1
        return self

    def executemany(self, sql, params=None):
        lista = list(params or [])
        self.conn.zapytania.append((" ".join(sql.split()), lista))
        self.rowcount = len(lista)
        return self

    def fetchall(self):
        return self._odp

    def fetchone(self):
        return self._odp[0] if self._odp else None


class Polaczenie:
    def __init__(self, instrumenty, rowcount_arkusza: int = 1):
        self.instrumenty = list(instrumenty)
        self.rowcount_arkusza = rowcount_arkusza
        self.zapytania: list[tuple] = []
        self.odczyty_listy = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        # Puls idzie prosto z połączenia (`puls.bij`).
        self.zapytania.append((" ".join(sql.split()), params))
        return Kursor(self)

    def cursor(self, *a, **kw):
        return Kursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


def instrument(i: int, symbol: str, *, tradeable: bool = True) -> dict:
    return {"id": i, "symbol": symbol, "tradeable": tradeable}


def _pulsy(conn) -> list[dict]:
    """`detail` każdego udanego pulsu — psycopg pakuje słownik w `Jsonb`."""
    return [p[-1].obj for sql, p in conn.zapytania
            if "risk.worker_heartbeat" in sql and sql.upper().startswith("INSERT")]


def _uruchom(monkeypatch, conn, gielda, cykli: int, *reszta):
    """Pętla workera zatrzymana po `cykli` cyklach, przez atrapę `asyncio.sleep`.

    Zatrzymanie idzie z uśpienia, a nie z `--once`: mierzona jest właśnie ta
    ścieżka, która przy `--once` nie zachodzi — DRUGI obieg pętli.
    """
    licznik = {"n": 0}

    async def natychmiast(_s):
        licznik["n"] += 1
        if licznik["n"] >= cykli:
            raise Koniec
    monkeypatch.setattr(asyncio, "sleep", natychmiast)
    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(market_worker.psycopg, "connect", lambda url, **kw: conn)
    monkeypatch.setattr(market_worker, "adapter_danych",
                        lambda conn, engine, **kw: gielda)
    monkeypatch.setattr(sys, "argv",
                        ["market_worker.py", "--interval", "60", *reszta])
    with pytest.raises(Koniec):
        asyncio.run(market_worker.main())


# =========================================== 1. LISTA ODŚWIEŻANA W TRAKCIE

def test_instrument_dopisany_do_bazy_wchodzi_BEZ_restartu_poda(monkeypatch):
    """Sedno poprawki 11e.

    `ODSWIEZANIE_LISTY_S = 0` wymusza odczyt w każdym obiegu — mierzymy TO, czy
    pętla w ogóle czyta listę ponownie, a nie to, jak długo czeka (interwał ma
    własny test niżej).
    """
    monkeypatch.setattr(market_worker, "ODSWIEZANIE_LISTY_S", 0)
    conn = Polaczenie([instrument(1, "BTCUSDC")])
    g = Gielda()

    # Podmiana listy po pierwszym cyklu: pierwsze uśpienie = koniec cyklu 1.
    licznik = {"n": 0}

    async def natychmiast(_s):
        licznik["n"] += 1
        if licznik["n"] == 1:
            conn.instrumenty = [instrument(1, "BTCUSDC"), instrument(2, "ETHUSDC")]
        if licznik["n"] >= 2:
            raise Koniec
    monkeypatch.setattr(asyncio, "sleep", natychmiast)
    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(market_worker.psycopg, "connect", lambda url, **kw: conn)
    monkeypatch.setattr(market_worker, "adapter_danych",
                        lambda conn, engine, **kw: g)
    monkeypatch.setattr(sys, "argv", ["market_worker.py", "--interval", "60"])
    with pytest.raises(Koniec):
        asyncio.run(market_worker.main())

    assert "ETHUSDC" in g.pytane, (
        "instrument dopisany do bazy nie został odpytany ani razu — lista jest "
        "czytana raz na proces, więc działa dopiero po restarcie poda")
    assert g.pytane.count("BTCUSDC") >= 2, "test nie wykonał dwóch cykli"


def test_instrument_wylaczony_w_bazie_przestaje_byc_odpytywany(monkeypatch):
    """Druga strona tej samej wady — ta, którą audyt opisał jako pierwszą."""
    monkeypatch.setattr(market_worker, "ODSWIEZANIE_LISTY_S", 0)
    conn = Polaczenie([instrument(1, "BTCUSDC"), instrument(2, "ETHUSDC")])
    g = Gielda()
    licznik = {"n": 0}

    async def natychmiast(_s):
        licznik["n"] += 1
        if licznik["n"] == 1:
            conn.instrumenty = [instrument(1, "BTCUSDC")]
        if licznik["n"] >= 2:
            raise Koniec
    monkeypatch.setattr(asyncio, "sleep", natychmiast)
    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(market_worker.psycopg, "connect", lambda url, **kw: conn)
    monkeypatch.setattr(market_worker, "adapter_danych",
                        lambda conn, engine, **kw: g)
    monkeypatch.setattr(sys, "argv", ["market_worker.py", "--interval", "60"])
    with pytest.raises(Koniec):
        asyncio.run(market_worker.main())

    assert g.pytane.count("ETHUSDC") == 1, (
        f"wyłączony instrument odpytany {g.pytane.count('ETHUSDC')} razy — "
        f"spodziewany raz, w cyklu przed wyłączeniem")


def test_lista_NIE_jest_czytana_w_kazdym_cyklu(monkeypatch):
    """Kanarek odwrotny. Odczyt co cykl to 1440 zapytań na dobę o odpowiedź,
    która zmienia się w skali godzin — i byłby drugą wadą w miejscu pierwszej.

    Przy domyślnym `ODSWIEZANIE_LISTY_S` dwa szybkie cykle mieszczą się w oknie,
    więc lista ma być przeczytana DOKŁADNIE RAZ (przed pętlą).
    """
    assert market_worker.ODSWIEZANIE_LISTY_S > 60, (
        "próg odświeżania nie jest większy od interwału cyklu — test nie "
        "mierzyłby wtedy niczego")
    conn = Polaczenie([instrument(1, "BTCUSDC")])
    _uruchom(monkeypatch, conn, Gielda(), cykli=3)
    assert conn.odczyty_listy == 1, (
        f"lista instrumentów czytana {conn.odczyty_listy} razy na trzy cykle")


def test_awaria_odczytu_listy_LICZY_SIE_jako_awaria_cyklu(monkeypatch):
    """Odczyt musi stać w tym samym `try`, co cykl.

    Inaczej proces chodziłby na starej liście i MILCZAŁ o tym, że nie umie jej
    odczytać — czyli dokładnie ta klasa awarii, którą ten worker tropi: puls
    bijący, dane nie te.
    """
    monkeypatch.setattr(market_worker, "ODSWIEZANIE_LISTY_S", 0)
    conn = Polaczenie([instrument(1, "BTCUSDC")])
    g = Gielda()
    licznik = {"n": 0}
    pierwotny = Kursor.execute

    def wybuchowy(self, sql, params=None):
        if ("ref.instrument" in sql and sql.lstrip().upper().startswith("SELECT")
                and licznik["n"] >= 1):
            raise RuntimeError("baza nie odpowiada")
        return pierwotny(self, sql, params)

    async def natychmiast(_s):
        licznik["n"] += 1
        if licznik["n"] >= 2:
            raise Koniec
    monkeypatch.setattr(Kursor, "execute", wybuchowy)
    monkeypatch.setattr(asyncio, "sleep", natychmiast)
    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(market_worker.psycopg, "connect", lambda url, **kw: conn)
    monkeypatch.setattr(market_worker, "adapter_danych",
                        lambda conn, engine, **kw: g)
    monkeypatch.setattr(sys, "argv", ["market_worker.py", "--interval", "60"])
    with pytest.raises(Koniec):
        asyncio.run(market_worker.main())

    awarie = [p for sql, p in conn.zapytania
              if "risk.worker_heartbeat" in sql and not sql.upper().startswith("INSERT")]
    assert awarie, (
        "wywrócony odczyt listy nie zgłosił awarii cyklu — proces chodziłby "
        "dalej na starej liście i twierdził, że wszystko zebrał")
