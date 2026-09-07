"""Wspolny interfejs adapterow gield.

Silnik decyzyjny nie wie, z jaka gielda rozmawia - zna wylacznie ten protokol.
Dzieki temu zmiana gieldy albo dolozenie drugiej to nowa implementacja, a nie
przepisywanie logiki.

Wszystkie kwoty ida jako Decimal, nigdy float: float("0.1") + float("0.2") to
0.30000000000000004, a przy wielkosciach zlecen taki blad kumuluje sie w realna
roznice i potrafi wywolic odrzucenie zlecenia przez gielde.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol


class ExchangeError(Exception):
    """Blad po stronie gieldy albo transportu."""

    def __init__(self, message: str, *, code: str | None = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        # Rozroznienie jest istotne: bledu walidacji zlecenia nie ma sensu ponawiac,
        # a chwilowego 5xx albo rate limitu - owszem.
        self.retryable = retryable


class AuthError(ExchangeError):
    """Klucz odrzucony, wygasly albo bez wymaganych uprawnien."""


@dataclass(frozen=True)
class Balance:
    currency: str
    total: Decimal
    available: Decimal


@dataclass(frozen=True)
class Candle:
    event_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trades: int | None = None


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    qty: Decimal


@dataclass(frozen=True)
class OrderBook:
    event_time: datetime
    bids: list[BookLevel]
    asks: list[BookLevel]

    @property
    def mid(self) -> Decimal:
        return (self.bids[0].price + self.asks[0].price) / 2

    @property
    def spread(self) -> Decimal:
        return self.asks[0].price - self.bids[0].price

    @property
    def imbalance(self) -> float:
        """(bid - ask) / (bid + ask) na calej dostepnej glebokosci, zakres -1..1."""
        b = sum(l.qty for l in self.bids)
        a = sum(l.qty for l in self.asks)
        total = b + a
        return float((b - a) / total) if total else 0.0


@dataclass(frozen=True)
class ConnectionCheck:
    """Wynik testu poswiadczen - to widzi uzytkownik po kliknieciu w UI."""

    ok: bool
    message: str
    account_type: str | None = None
    balances: list[Balance] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    can_withdraw: bool | None = None
    latency_ms: int | None = None


class ExchangeAdapter(Protocol):
    """Minimalny zestaw operacji, ktorego potrzebuje silnik.

    Skladanie zlecen dochodzi w fazie 4 - na razie wylacznie odczyt, zeby dalo
    sie zbierac dane i zweryfikowac poswiadczenia bez ryzyka wyslania czegokolwiek.
    """

    venue_code: str

    async def check_connection(self) -> ConnectionCheck: ...

    async def fetch_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]: ...

    async def fetch_order_book(self, symbol: str, depth: int) -> OrderBook: ...

    async def fetch_balances(self) -> list[Balance]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class ZlozoneZlecenie:
    """Odpowiedź giełdy na złożenie zlecenia.

    `id_wlasne` jest w tej strukturze WAŻNIEJSZE niż `id_gieldy`: to my je
    nadajemy i to po nim odnajdujemy zlecenie, gdy odpowiedź nie dotarła.
    Identyfikator giełdy poznajemy dopiero z odpowiedzi, więc w sytuacji, dla
    której idempotencja istnieje, jeszcze go nie mamy.
    """
    id_wlasne: str
    id_gieldy: str | None
    status: str                    # 'new','partial','filled','cancelled','rejected'
    juz_istnialo: bool = False     # zlecenie odnalezione, nie zlozone od nowa


@dataclass(frozen=True)
class StanZlecenia:
    """Stan zlecenia po stronie giełdy — źródło prawdy przy uzgadnianiu."""
    id_wlasne: str
    id_gieldy: str | None
    status: str
    ilosc: Decimal
    wypelniono: Decimal            # ILE JUŻ WESZŁO — wypełnienie częściowe
    srednia_cena: Decimal | None
    prowizja: Decimal

    @property
    def czesciowe(self) -> bool:
        return Decimal(0) < self.wypelniono < self.ilosc

@dataclass(frozen=True)
class Wypelnienie:
    """Pojedyncze wykonanie zapisane przez giełdę.

    To jest zapis GIEŁDY o tym, co się faktycznie stało — niezależny od naszego
    dziennika. Na tym polega jego wartość przy uzgadnianiu: dwa zapisy tego
    samego zdarzenia, prowadzone przez dwie strony.

    `prowizja_w` jest osobnym polem, bo na spocie prowizja bywa pobierana
    W KUPIONYM AKTYWIE. Pominięcie tego zaniża odtworzoną pozycję dokładnie
    o wartość prowizji i wygląda potem jak rozjazd.
    """
    id_gieldy: str
    id_wlasne: str | None
    symbol: str
    strona: str                    # 'Buy' / 'Sell'
    ilosc: Decimal
    cena: Decimal
    prowizja: Decimal
    prowizja_w: str | None
    czas: datetime
