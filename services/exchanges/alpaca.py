"""Adapter Alpaca Markets - gielda tradycyjna (akcje USA), konto PAPER.

DLACZEGO ALPACA, A NIE INTERACTIVE BROKERS

README mowi o IBKR, research (docs/00-research.md, 2.3) rekomendowal Alpace
na start i wlasciciel to potwierdzil 2026-09-22. Roznica jest operacyjna,
nie strategiczna: IBKR wymaga stale dzialajacego IB Gateway (Java, sesja
logowania, okresowe 2FA) - w klastrze k3s to osobny proces do pilnowania,
ktory pada w nocy i nikt tego nie widzi. Alpaca to klucz API i REST/WS jak
Bybit. Adapter jest pisany pod ten sam protokol co `BybitAdapter`, wiec IBKR
da sie dolozyc pozniej jako trzecia implementacja, a nie przepisanie silnika.

CZYM ALPACA ROZNI SIE OD BYBITA I CO Z TEGO WYNIKA DLA SILNIKA

  * Prowizja jest ZEROWA. `StanZlecenia.prowizja` i `Wypelnienie.prowizja`
    sa zerem nie z lenistwa, tylko z cennika. Oplaty regulacyjne (SEC/FINRA
    TAF) przychodza jako OSOBNE pozycje rejestru (`activity_type = FEE`),
    groszowe i nie do przypisania do konkretnego wypelnienia - traktujemy je
    jak przeplyw kapitalu, nie jak koszt transakcji.
  * Rynek ma SESJE. Zlecenie `day` poza sesja czeka do otwarcia; `time_in_force`
    jest tu pierwszorzedna decyzja, a nie szczegolem. Zegar gieldy
    (`/v2/clock`) jest wystawiony wprost i silnik equity MUSI go pytac, zanim
    uzna, ze "gielda milczy".
  * Nie ma arkusza zlecen w REST - jest tylko najlepsza oferta (L1) z Alpaca
    Data. `fetch_order_book` oddaje wiec ksiege JEDNOPOZIOMOWA, uczciwie
    nazwana; `imbalance` z niej to nie jest ta sama cecha, co z 50 poziomow
    Bybita, i cechy equity nie moga jej tak traktowac.
  * Dane rynkowe w planie darmowym ida z feedu IEX (`feed=iex`) - to jedna
    gielda z kilkunastu, wiec wolumen jest ULAMKIEM calego rynku, a cena
    moze na chwile odjechac od NBBO. Feed SIP wymaga platnego planu. Nazwa
    feedu jest zapisywana w kazdym odczycie, zeby trener wiedzial, na czym
    sie uczyl.
  * Klucz jest przypisany do srodowiska: klucz paper NIE dziala na hoscie
    live i odwrotnie (401). Tryb wybiera host, nie flaga w zapytaniu.

WYLACZNIE REST. WebSocket dojdzie, gdy bedzie potrzebny do czegos, czego
odpytywanie co minute nie daje - na razie nie ma takiej rzeczy.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from services.exchanges.base import (
    AuthError, Balance, BookLevel, Candle, ConnectionCheck, ExchangeError,
    KontoZamrozone, OrderBook, StanZlecenia, Wypelnienie, ZlozoneZlecenie,
)

HOSTS = {
    "demo": "https://paper-api.alpaca.markets",
    "live": "https://api.alpaca.markets",
}
DATA_HOST = "https://data.alpaca.markets"

# Nazwy taktow po stronie Alpaca Data. Klucze to nasze `timeframe`
# z `market.candle` - te same, ktorych uzywa adapter Bybita.
TIMEFRAMES = {
    "1m": "1Min", "5m": "5Min", "15m": "15Min", "1h": "1Hour",
    "4h": "4Hour", "1d": "1Day",
}

# Statusy Alpaca -> statusy silnika ('new','partial','filled','cancelled',
# 'rejected'). Stany posrednie (`pending_new`, `accepted`, `pending_cancel`,
# `held`, ...) sa dla silnika "zywe i niewypelnione", czyli `new` - waznym
# rozroznieniem jest to, czy zlecenie moze sie jeszcze wypelnic.
_STATUSY = {
    "new": "new", "accepted": "new", "pending_new": "new", "held": "new",
    "accepted_for_bidding": "new", "calculated": "new", "stopped": "new",
    "suspended": "new", "pending_cancel": "new", "pending_replace": "new",
    "partially_filled": "partial",
    "filled": "filled",
    "canceled": "cancelled", "expired": "cancelled", "done_for_day": "cancelled",
    "replaced": "cancelled",
    "rejected": "rejected",
}


def _status_alpaki(status: str) -> str:
    return _STATUSY.get(status, f"nieznany:{status}")


def _dec(v) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal(0)


def _dec_albo_nic(v) -> Decimal | None:
    return Decimal(str(v)) if v not in (None, "") else None


def _czas(s: str) -> datetime:
    """RFC 3339 Alpaki -> datetime UTC.

    Dwie pulapki, obie zmierzone: nanosekundy ("...:00.123456789Z"), ktorych
    `fromisoformat` nie przyjmuje, oraz przesuniecie strefy zamiast "Z"
    ("2026-09-22T06:11:51.022556-04:00" z `/v2/clock`). Pierwsza wersja
    zbierala WSZYSTKIE cyfry po kropce - razem z "04:00" ze strefy - i oddawala
    czas naiwny, czyli o 4 h za wczesnie. Zegar sesji mowil wtedy "06:11 UTC"
    o 10:11 UTC, a silnik equity liczylby otwarcie gieldy z czterogodzinnym
    bledem.
    """
    s = s.replace("Z", "+00:00")
    if "." in s:
        glowa, reszta = s.split(".", 1)
        n = 0
        while n < len(reszta) and reszta[n].isdigit():
            n += 1
        ulamek, strefa = reszta[:n], reszta[n:]
        s = f"{glowa}.{ulamek[:6].ljust(6, '0')}{strefa}"
    t = datetime.fromisoformat(s)
    if t.tzinfo is None:
        raise ValueError(f"czas bez strefy od Alpaki: {s!r}")
    return t.astimezone(timezone.utc)


@dataclass(frozen=True)
class Zegar:
    """Stan sesji gieldy - odpowiedz na pytanie, czy zlecenie ma szanse wejsc TERAZ."""
    teraz: datetime
    otwarta: bool
    nastepne_otwarcie: datetime
    nastepne_zamkniecie: datetime


def _blad_z_odpowiedzi(r: httpx.Response) -> ExchangeError:
    """HTTP Alpaki -> wyjatek, ktory MOWI, CO ZROBIC (ta sama rodzina co Bybit)."""
    # `code` wyjatku to STATUS HTTP, nie kod wewnetrzny Alpaki (np. 40010001):
    # to po statusie wolajacy rozroznia "nie zna zlecenia" (404) od "duplikat"
    # (422). Kod Alpaki idzie do tresci, zeby nie zginal.
    kod = str(r.status_code)
    try:
        tresc = r.json()
        msg = tresc.get("message") or str(tresc)
        if tresc.get("code"):
            msg = f"{msg} [alpaca {tresc['code']}]"
    except ValueError:
        msg = r.text[:200]
    if r.status_code in (401,):
        return AuthError(f"Alpaca odrzucila klucz ({r.status_code}): {msg}", code=kod)
    if r.status_code == 403:
        # 403 to u Alpaki albo konto zablokowane/ograniczone, albo brak srodkow
        # na zlecenie ("insufficient buying power"). Drugie nie wymaga czlowieka
        # w panelu - wymaga mniejszego zlecenia - wiec rozrozniamy po tresci.
        if "buying power" in msg.lower() or "insufficient" in msg.lower():
            return ExchangeError(f"Alpaca: {msg}", code=kod, retryable=False)
        return KontoZamrozone(f"Alpaca: konto zablokowane lub ograniczone: {msg}", code=kod)
    if r.status_code == 429:
        return ExchangeError(f"Alpaca: limit zapytan ({msg})", code=kod, retryable=True)
    if r.status_code >= 500:
        return ExchangeError(f"Alpaca {r.status_code}: {msg}", code=kod, retryable=True)
    return ExchangeError(f"Alpaca {r.status_code}: {msg}", code=kod, retryable=False)


class AlpacaAdapter:
    venue_code = "alpaca"

    # 60 s, nie 15 jak u Bybita - ZMIERZONE 2026-09-22: zapytanie o swiece 1h
    # z zakresem 400 dni konczylo sie na 15 s pustym bledem transportu
    # (`httpx.ReadTimeout` bez tresci), a backfill historii to wlasnie takie
    # zapytania. Krotszy limit czasu zamienia dlugi odczyt w awarie, ktora
    # w logu nie mowi nawet, co ja spowodowalo.
    def __init__(self, api_key: str, api_secret: str, mode: str = "demo",
                 timeout: float = 60.0, host: str | None = None,
                 data_host: str | None = None, feed: str = "iex",
                 http: httpx.AsyncClient | None = None):
        if mode not in HOSTS:
            raise ValueError(f"nieznany tryb: {mode}")
        self._host = host or HOSTS[mode]
        self._data_host = data_host or DATA_HOST
        self._feed = feed
        self._naglowki = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret,
                          "Accept": "application/json"}
        self._client = http or httpx.AsyncClient(timeout=timeout)

    # ------------------------------------------------------------- transport
    async def _zapytaj(self, metoda: str, path: str, *, params: dict | None = None,
                       json: dict | None = None, host: str | None = None,
                       prob: int = 3) -> httpx.Response:
        """Jedno zapytanie z ponowieniem WYLACZNIE bledow przejsciowych.

        Ponawiamy 429 i 5xx z rosnacym odstepem. 4xx poza tymi dwoma nie
        ponawiamy nigdy: bledne zlecenie zlozone trzy razy nie staje sie
        poprawne, a duplikat `client_order_id` (422) jest sygnalem, nie awaria.
        """
        url = (host or self._host) + path
        ostatni: ExchangeError | None = None
        for proba in range(prob):
            try:
                r = await self._client.request(metoda, url, params=params, json=json,
                                               headers=self._naglowki)
            except httpx.HTTPError as e:
                # `str(e)` bywa PUSTE (ReadTimeout), wiec sama tresc nie mowi
                # nic - typ wyjatku mowi wszystko.
                ostatni = ExchangeError(
                    f"Alpaca: transport: {type(e).__name__}: {e or 'bez treści'}",
                    retryable=True)
            else:
                if r.status_code < 400:
                    return r
                blad = _blad_z_odpowiedzi(r)
                if not blad.retryable:
                    raise blad
                ostatni = blad
            await asyncio.sleep(0.5 * (2 ** proba))
        assert ostatni is not None
        raise ostatni

    async def _get(self, path: str, params: dict | None = None, *, host: str | None = None):
        r = await self._zapytaj("GET", path, params=params, host=host)
        return r.json() if r.content else None

    # ------------------------------------------------------------- odczyty
    async def zegar(self) -> Zegar:
        z = await self._get("/v2/clock")
        return Zegar(teraz=_czas(z["timestamp"]), otwarta=bool(z["is_open"]),
                     nastepne_otwarcie=_czas(z["next_open"]),
                     nastepne_zamkniecie=_czas(z["next_close"]))

    async def fetch_candles(self, symbol: str, timeframe: str = "1m", limit: int = 200,
                            start: datetime | None = None,
                            end: datetime | None = None) -> list[Candle]:
        """Swiece z Alpaca Data. Rosnaco po czasie, jak u Bybita.

        Alpaca stronicuje przez `next_page_token` i oddaje najwyzej 10 000
        swiec na strone. `limit` jest u nas gorna granica LACZNA - paginacja
        zatrzymuje sie, gdy ja osiagnie, albo gdy token sie skonczy.
        Bez `start` Alpaca liczy od poczatku dostepnej historii, wiec przy
        samym `limit` cofamy sie o rozsadne okno.
        """
        tf = TIMEFRAMES.get(timeframe)
        if not tf:
            raise ValueError(f"nieobslugiwany timeframe: {timeframe}")
        params: dict = {"symbols": symbol, "timeframe": tf, "feed": self._feed,
                        "adjustment": "raw", "limit": min(limit, 10_000), "sort": "asc"}
        if start is not None:
            params["start"] = start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if end is not None:
            params["end"] = end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        out: list[Candle] = []
        token = None
        while True:
            if token:
                params["page_token"] = token
            res = await self._get("/v2/stocks/bars", params, host=self._data_host)
            for row in (res.get("bars") or {}).get(symbol, []):
                out.append(Candle(
                    event_time=_czas(row["t"]), open=_dec(row["o"]), high=_dec(row["h"]),
                    low=_dec(row["l"]), close=_dec(row["c"]), volume=_dec(row["v"]),
                    trades=row.get("n")))
            token = res.get("next_page_token")
            if not token or len(out) >= limit:
                break
        return out[:limit]

    async def fetch_order_book(self, symbol: str, depth: int = 1) -> OrderBook:
        """Ksiega JEDNOPOZIOMOWA z najlepszej oferty. Alpaca nie wystawia
        glebokosci w REST; `depth` > 1 nie da wiecej i jest przyjmowany dla
        zgodnosci sygnatury."""
        res = await self._get("/v2/stocks/quotes/latest",
                              {"symbols": symbol, "feed": self._feed}, host=self._data_host)
        q = (res.get("quotes") or {}).get(symbol)
        if not q:
            raise ExchangeError(f"Alpaca: brak oferty dla {symbol}", retryable=True)
        return OrderBook(event_time=_czas(q["t"]),
                         bids=[BookLevel(price=_dec(q["bp"]), qty=_dec(q["bs"]))],
                         asks=[BookLevel(price=_dec(q["ap"]), qty=_dec(q["as"]))])

    async def cena_odniesienia(self, symbol: str, kategoria: str | None = None) -> Decimal | None:
        res = await self._get("/v2/stocks/trades/latest",
                              {"symbols": symbol, "feed": self._feed}, host=self._data_host)
        t = (res.get("trades") or {}).get(symbol)
        return _dec_albo_nic(t.get("p")) if t else None

    async def fetch_balances(self) -> list[Balance]:
        """Saldo gotowki w walucie konta. Akcje NIE sa saldami - sa pozycjami
        (`pozycje`), i uzgodnienie equity ma porownywac wlasnie je."""
        acc = await self._get("/v2/account")
        return [Balance(currency=acc.get("currency", "USD"), total=_dec(acc.get("cash")),
                        available=_dec(acc.get("buying_power")))]

    async def stan_konta(self) -> dict:
        """Kapital rachunku Z KSIAG GIELDY: equity, gotowka, wartosc pozycji.

        To jest zrodlo krzywej kapitalu silnika equity, dopoki (i takze gdy)
        silnik nie liczy jej z wlasnych wypelnien: paper Alpaki jest prawdziwym
        rachunkiem z prawdziwa wycena, a nie nasza symulacja.
        """
        acc = await self._get("/v2/account")
        long_mv = _dec(acc.get("long_market_value"))
        short_mv = _dec(acc.get("short_market_value"))
        return {"equity": _dec(acc.get("equity")), "cash": _dec(acc.get("cash")),
                "ekspozycja": abs(long_mv) + abs(short_mv),
                "waluta": acc.get("currency", "USD"), "status": acc.get("status")}

    async def pozycje_szczegoly(self) -> list[dict]:
        """Pozycje z wycena gieldy: ilosc ze znakiem, wartosc, niezrealizowany P&L."""
        out = []
        for p in await self._get("/v2/positions") or []:
            q = _dec(p["qty"])
            out.append({"symbol": p["symbol"], "qty": -q if p.get("side") == "short" else q,
                        "avg_price": _dec(p.get("avg_entry_price")),
                        "market_value": _dec(p.get("market_value")),
                        "unrealized_pl": _dec(p.get("unrealized_pl"))})
        return out

    async def pozycje(self) -> dict[str, Decimal]:
        """Ilosc akcji na koncie wg symbolu, ze znakiem (short ujemny)."""
        return {p["symbol"]: p["qty"] for p in await self.pozycje_szczegoly()}

    async def wypelnienia(self, *, od_ms: int | None = None,
                          symbol: str | None = None) -> list[Wypelnienie]:
        """Wykonania z rejestru aktywnosci konta, od najnowszych.

        Alpaca stronicuje po `page_token` = id ostatniej aktywnosci. Prowizja
        jest zerem z cennika (patrz naglowek modulu).
        """
        params: dict = {"activity_types": "FILL", "direction": "desc", "page_size": 100}
        if od_ms is not None:
            params["after"] = datetime.fromtimestamp(od_ms / 1000, tz=timezone.utc) \
                .isoformat().replace("+00:00", "Z")
        out: list[Wypelnienie] = []
        for _ in range(50):
            strona = await self._get("/v2/account/activities", params) or []
            for a in strona:
                if symbol and a.get("symbol") != symbol:
                    continue
                out.append(Wypelnienie(
                    id_gieldy=a["id"], id_wlasne=None, symbol=a["symbol"],
                    strona="Buy" if a.get("side") == "buy" else "Sell",
                    ilosc=_dec(a["qty"]), cena=_dec(a["price"]), prowizja=Decimal(0),
                    prowizja_w=None, czas=_czas(a["transaction_time"])))
            if len(strona) < params["page_size"]:
                return out
            params["page_token"] = strona[-1]["id"]
        raise ExchangeError("Alpaca: rejestr wypelnien nie konczy sie po 50 stronach - "
                            "urwana lista zanizylaby odtworzona pozycje", retryable=False)

    async def filtry_instrumentow(self, kategoria: str | None = None, *,
                                  host: str | None = None,
                                  symbole: list[str] | None = None) -> dict[str, dict]:
        """Czy aktywo jest w ogole do handlu i czy wolno ulamki. Akcje nie maja
        kroku ceny w sensie krypto (grosz) ani minimalnej wartosci zlecenia
        poza 1 USD dla ulamkow - oddajemy to w tym samym ksztalcie, co Bybit."""
        out: dict[str, dict] = {}
        for s in symbole or []:
            a = await self._get(f"/v2/assets/{s}")
            out[s] = {"tradable": bool(a.get("tradable")),
                      "fractionable": bool(a.get("fractionable")),
                      "shortable": bool(a.get("shortable")),
                      "krok_ilosci": Decimal("0.000000001") if a.get("fractionable") else Decimal(1),
                      "krok_ceny": Decimal("0.01"), "min_wartosc": Decimal(1)}
        return out

    async def check_connection(self) -> ConnectionCheck:
        t0 = time.monotonic()
        try:
            acc = await self._get("/v2/account")
        except ExchangeError as e:
            return ConnectionCheck(ok=False, message=str(e))
        ms = int((time.monotonic() - t0) * 1000)
        blokady = [k for k in ("trading_blocked", "account_blocked", "transfers_blocked")
                   if acc.get(k)]
        typ = "paper" if "paper" in self._host else "live"
        if blokady:
            return ConnectionCheck(ok=False, account_type=typ, latency_ms=ms,
                                   message=f"konto z blokada: {', '.join(blokady)}")
        return ConnectionCheck(
            ok=True, account_type=typ, latency_ms=ms,
            message=f"konto {acc.get('status')} ({typ}), kapital {acc.get('equity')} "
                    f"{acc.get('currency', 'USD')}",
            balances=[Balance(currency=acc.get("currency", "USD"), total=_dec(acc.get("cash")),
                              available=_dec(acc.get("buying_power")))],
            permissions=["trade"], can_withdraw=False)

    # ------------------------------------------------------------- zlecenia
    def _stan_z(self, o: dict) -> StanZlecenia:
        return StanZlecenia(
            id_wlasne=o.get("client_order_id") or "", id_gieldy=o.get("id"),
            status=_status_alpaki(o.get("status", "")), ilosc=_dec(o.get("qty")),
            wypelniono=_dec(o.get("filled_qty")),
            srednia_cena=_dec_albo_nic(o.get("filled_avg_price")),
            prowizja=Decimal(0), prowizja_w=None)

    async def stan_zlecenia(self, id_wlasne: str, kategoria: str = "spot") -> StanZlecenia | None:
        """Stan po NASZYM identyfikatorze. None, gdy gielda go nie zna."""
        try:
            o = await self._get("/v2/orders:by_client_order_id", {"client_order_id": id_wlasne})
        except ExchangeError as e:
            if e.code == "404":
                return None
            raise
        if not o:
            return None
        if o.get("client_order_id") != id_wlasne:
            raise ExchangeError(
                f"Alpaca oddala zlecenie {o.get('client_order_id')!r} zamiast {id_wlasne!r}",
                retryable=False)
        return self._stan_z(o)

    async def zloz_zlecenie(self, *, symbol: str, strona: str, ilosc: Decimal,
                            cena_limitu: Decimal, id_wlasne: str,
                            kategoria: str = "spot",
                            time_in_force: str = "day") -> ZlozoneZlecenie:
        """Zlecenie z limitem, IDEMPOTENTNIE po `client_order_id`.

        SPRAWDZ, POTEM SKLADAJ - jak u Bybita. Alpaca sama odrzuca duplikat
        `client_order_id` (422), wiec nawet wyscig dwoch procesow nie zlozy
        dwoch zlecen; my zamieniamy te odmowe na `juz_istnialo=True`.
        `time_in_force='day'` to swiadomy domysl: plan jest na jedna swiece,
        a zlecenie, ktore przezyloby noc, wypelniloby sie po cenie, ktorej
        zadna decyzja juz nie popiera.
        """
        istnieje = await self.stan_zlecenia(id_wlasne)
        if istnieje is not None:
            return ZlozoneZlecenie(id_wlasne=id_wlasne, id_gieldy=istnieje.id_gieldy,
                                   status=istnieje.status, juz_istnialo=True)
        body = {"symbol": symbol, "qty": str(ilosc),
                "side": "buy" if strona.lower() == "buy" else "sell",
                "type": "limit", "time_in_force": time_in_force,
                "limit_price": str(cena_limitu), "client_order_id": id_wlasne}
        try:
            r = await self._zapytaj("POST", "/v2/orders", json=body)
        except ExchangeError as e:
            if e.code == "422" and "client_order_id" in str(e):
                ponownie = await self.stan_zlecenia(id_wlasne)
                if ponownie is not None:
                    return ZlozoneZlecenie(id_wlasne=id_wlasne, id_gieldy=ponownie.id_gieldy,
                                           status=ponownie.status, juz_istnialo=True)
            raise
        o = r.json()
        return ZlozoneZlecenie(id_wlasne=id_wlasne, id_gieldy=o.get("id"),
                               status=_status_alpaki(o.get("status", "")))

    async def anuluj_zlecenie(self, *, symbol: str, id_wlasne: str,
                              kategoria: str = "spot") -> bool:
        """Anuluje po naszym identyfikatorze. False, gdy nie bylo czego."""
        stan = await self.stan_zlecenia(id_wlasne)
        if stan is None or stan.status in ("filled", "cancelled", "rejected"):
            return False
        try:
            await self._zapytaj("DELETE", f"/v2/orders/{stan.id_gieldy}")
        except ExchangeError as e:
            # 422 = zlecenie juz nie jest do anulowania (wypelnione w miedzyczasie).
            if e.code == "422":
                return False
            raise
        return True

    async def dryf_zegara(self) -> tuple[int, str | None]:
        """Roznica miedzy zegarem gieldy a naszym, w ms (ta sama semantyka co Bybit)."""
        z = await self.zegar()
        return int((z.teraz - datetime.now(timezone.utc)).total_seconds() * 1000), None

    async def close(self) -> None:
        await self._client.aclose()
