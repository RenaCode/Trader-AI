"""Adapter Bybit V5.

Podpis wg dokumentacji V5: HMAC-SHA256 z ciagu
    timestamp + api_key + recv_window + (queryString | jsonBody)
zapisany malymi literami szesnastkowo, w naglowku X-BAPI-SIGN.

Rozroznienie hostow jest tu istotne i latwe do pomylenia:
  - konto DEMO      -> https://api-demo.bybit.com   (klucz z konta PRODUKCYJNEGO
                       wygenerowany w trybie Demo Trading)
  - testnet         -> https://api-testnet.bybit.com  (osobne konto, izolowany
                       orderbook - inne ceny niz rynek)
  - produkcja       -> https://api.bybit.com

Dane publiczne (swiece, orderbook) bierzemy ZAWSZE z hosta produkcyjnego, bo
host demo ich nie duplikuje - i o to chodzi: demo ma chodzic po prawdziwych
cenach, inaczej szesciomiesieczna faza testowa nic nie znaczy.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from .base import (
    AuthError, Balance, BookLevel, Candle, ConnectionCheck, ExchangeError, OrderBook,
)

PUBLIC_HOST = "https://api.bybit.com"
HOSTS = {
    "demo": "https://api-demo.bybit.com",
    "live": "https://api.bybit.com",
}

# Nasze nazwy timeframe'ow -> parametr `interval` Bybit.
INTERVAL = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "2h": "120", "4h": "240", "1d": "D", "1w": "W",
}

RECV_WINDOW = "5000"


class BybitAdapter:
    venue_code = "bybit"

    def __init__(self, api_key: str, api_secret: str, mode: str = "demo",
                 category: str = "spot", timeout: float = 15.0):
        if mode not in HOSTS:
            raise ValueError(f"nieznany tryb: {mode}")
        self._key = api_key
        self._secret = api_secret.encode()
        self._host = HOSTS[mode]
        self._category = category
        self._client = httpx.AsyncClient(timeout=timeout)

    # ------------------------------------------------------------- transport
    def _sign(self, ts: str, payload: str) -> str:
        return hmac.new(self._secret, f"{ts}{self._key}{RECV_WINDOW}{payload}".encode(),
                        hashlib.sha256).hexdigest()

    async def _get(self, path: str, params: dict | None = None, *, private: bool = True,
                   host: str | None = None) -> dict:
        params = params or {}
        # Kolejnosc parametrow MUSI byc identyczna w podpisie i w zapytaniu,
        # inaczej Bybit odrzuci podpis. httpx zachowuje kolejnosc dict-a.
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{host or self._host}{path}" + (f"?{query}" if query else "")

        headers = {}
        if private:
            ts = str(int(time.time() * 1000))
            headers = {
                "X-BAPI-API-KEY": self._key,
                "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": RECV_WINDOW,
                "X-BAPI-SIGN": self._sign(ts, query),
            }

        try:
            r = await self._client.get(url, headers=headers)
        except httpx.TimeoutException as e:
            raise ExchangeError(f"przekroczono czas oczekiwania: {e}", retryable=True) from e
        except httpx.HTTPError as e:
            raise ExchangeError(f"blad polaczenia: {e}", retryable=True) from e

        if r.status_code == 429:
            raise ExchangeError("przekroczono limit zapytan", code="429", retryable=True)
        if r.status_code >= 500:
            raise ExchangeError(f"blad gieldy ({r.status_code})", code=str(r.status_code),
                                retryable=True)

        try:
            data = r.json()
        except ValueError as e:
            raise ExchangeError(f"odpowiedz nie jest JSON-em ({r.status_code})") from e

        ret = data.get("retCode")
        if ret != 0:
            msg = data.get("retMsg", "nieznany blad")
            # 10003/10004/10005 to rodzina bledow uwierzytelniania - warto je
            # rozroznic, bo w UI znacza "popraw klucz", a nie "sprobuj ponownie".
            if ret in (10003, 10004, 10005, 10006, 33004):
                raise AuthError(f"{msg} (kod {ret})", code=str(ret))
            raise ExchangeError(f"{msg} (kod {ret})", code=str(ret))
        return data.get("result") or {}

    # ------------------------------------------------------------- publiczne
    async def fetch_candles(self, symbol: str, timeframe: str = "1m", limit: int = 200,
                            start: datetime | None = None,
                            end: datetime | None = None) -> list[Candle]:
        """`start`/`end` sluza do backfillu historii.

        Bybit zwraca maksymalnie 1000 swiec na zapytanie i liczy je OD `start`
        w gore, wiec paginacja polega na przesuwaniu `start` na czas ostatniej
        otrzymanej swiecy + jeden takt.
        """
        interval = INTERVAL.get(timeframe)
        if not interval:
            raise ValueError(f"nieobslugiwany timeframe: {timeframe}")

        params = {"category": self._category, "symbol": symbol, "interval": interval,
                  "limit": min(limit, 1000)}
        if start is not None:
            params["start"] = int(start.timestamp() * 1000)
        if end is not None:
            params["end"] = int(end.timestamp() * 1000)

        res = await self._get("/v5/market/kline", params, private=False, host=PUBLIC_HOST)
        out = []
        # Bybit zwraca od najnowszej; my trzymamy rosnaco po czasie.
        for row in reversed(res.get("list", [])):
            out.append(Candle(
                event_time=datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc),
                open=Decimal(row[1]), high=Decimal(row[2]), low=Decimal(row[3]),
                close=Decimal(row[4]), volume=Decimal(row[5]),
            ))
        return out

    async def fetch_funding(self, symbol: str, start: datetime, end: datetime,
                            limit: int = 200) -> list[tuple[datetime, float]]:
        """Historia stawek finansowania perpetuala.

        Funding istnieje wylacznie dla kontraktow wieczystych, wiec zapytanie
        idzie zawsze do kategorii `linear` - niezaleznie od tego, czy adapter
        obsluguje spot. Zwraca [(czas_ogloszenia, stawka)] rosnaco po czasie.
        """
        res = await self._get(
            "/v5/market/funding/history",
            {"category": "linear", "symbol": symbol,
             "startTime": int(start.timestamp() * 1000),
             "endTime": int(end.timestamp() * 1000),
             "limit": min(limit, 200)},
            private=False, host=PUBLIC_HOST,
        )
        out = []
        for row in res.get("list", []):
            out.append((
                datetime.fromtimestamp(int(row["fundingRateTimestamp"]) / 1000, tz=timezone.utc),
                float(row["fundingRate"]),
            ))
        out.sort(key=lambda x: x[0])
        return out

    async def fetch_order_book(self, symbol: str, depth: int = 50) -> OrderBook:
        res = await self._get(
            "/v5/market/orderbook",
            {"category": self._category, "symbol": symbol, "limit": min(depth, 200)},
            private=False, host=PUBLIC_HOST,
        )
        bids = [BookLevel(Decimal(p), Decimal(q)) for p, q in res.get("b", [])]
        asks = [BookLevel(Decimal(p), Decimal(q)) for p, q in res.get("a", [])]
        if not bids or not asks:
            raise ExchangeError(f"pusty orderbook dla {symbol}", retryable=True)
        return OrderBook(
            event_time=datetime.fromtimestamp(int(res.get("ts", 0)) / 1000, tz=timezone.utc),
            bids=bids, asks=asks,
        )

    # ------------------------------------------------------------- prywatne
    async def fetch_balances(self) -> list[Balance]:
        res = await self._get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        out = []
        for acc in res.get("list", []):
            for coin in acc.get("coin", []):
                total = Decimal(coin.get("walletBalance") or "0")
                if total == 0:
                    continue
                out.append(Balance(
                    currency=coin["coin"], total=total,
                    available=Decimal(coin.get("availableToWithdraw") or coin.get("walletBalance") or "0"),
                ))
        return out

    async def check_connection(self) -> ConnectionCheck:
        """Sprawdza poswiadczenia i raportuje uprawnienia.

        Swiadomie uzywa endpointu informacyjnego o kluczu, nie skladania zlecen -
        test polaczenia nie moze miec skutkow ubocznych.
        """
        started = time.perf_counter()
        try:
            info = await self._get("/v5/user/query-api")
        except AuthError as e:
            return ConnectionCheck(ok=False, message=f"Klucz odrzucony: {e}")
        except ExchangeError as e:
            return ConnectionCheck(ok=False, message=f"Nie udalo sie polaczyc: {e}")

        perms_raw = info.get("permissions") or {}
        perms = sorted({p for group in perms_raw.values() if isinstance(group, list) for p in group})
        can_withdraw = "Withdraw" in perms

        balances: list[Balance] = []
        note = ""
        try:
            balances = await self.fetch_balances()
        except ExchangeError as e:
            # Klucz dziala, ale nie ma prawa odczytu salda - to wazna informacja
            # dla uzytkownika, a nie powod, zeby uznac polaczenie za nieudane.
            note = f" Odczyt salda niedostepny: {e}."

        latency = int((time.perf_counter() - started) * 1000)
        return ConnectionCheck(
            ok=True,
            message=("Polaczono." + note +
                     (" UWAGA: klucz ma prawo WYPLATY - to niepotrzebne ryzyko."
                      if can_withdraw else "")),
            account_type=info.get("type"),
            balances=balances,
            permissions=perms,
            can_withdraw=can_withdraw,
            latency_ms=latency,
        )

    async def close(self) -> None:
        await self._client.aclose()
