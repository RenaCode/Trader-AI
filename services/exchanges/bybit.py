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

PODMIOT UNIJNY TO OSOBNA GIELDA, NIE INNA DOMENA TEJ SAMEJ

Po MiCA uzytkownicy z EOG maja konta na bybit.eu, z wlasnymi hostami
(https://api.bybit.eu, https://api-demo.bybit.eu). Klucz z bybit.eu dostaje na
hoscie globalnym odpowiedz 10003 "API key is invalid" - i to jest poprawne
zachowanie, nie blad klucza.

Roznica siega ksiegi zlecen i listy instrumentow (zmierzone 2026-09-04):

    bybit.com   541 par spot, 399 par *USDT, 859 kontraktow wieczystych
    bybit.eu    140 par spot,   4 pary *USDT,   0 kontraktow wieczystych

Plynnosc unijna siedzi na parach USDC. Konsekwencja dla projektu jest twarda:
strategia dwustronna wymaga kontraktow wieczystych (patrz engine/koszty.py),
a tych podmiot unijny nie oferuje i nie bedzie.

Dane publiczne (swiece, orderbook) bierzemy ZAWSZE z hosta produkcyjnego, bo
host demo ich nie duplikuje - i o to chodzi: demo ma chodzic po prawdziwych
cenach, inaczej szesciomiesieczna faza testowa nic nie znaczy.

OGRANICZENIE DEMO, ktore wymusza projekt bazy: gielda trzyma zlecenia z konta
demo TYLKO 7 DNI. Przy fazie testowej liczonej w miesiacach historia zlecen
i wypelnien musi byc kompletna PO NASZEJ STRONIE (schemat `exec`) - odtworzenie
jej z gieldy po fakcie jest niemozliwe. Limit zapytan na demo jest domyslny
i nie da sie go podniesc.
"""
from __future__ import annotations

# Ponawianie stanow przejsciowych. Trzy proby z odstepem 1 s, 2 s - lacznie
# najwyzej trzy sekundy zwloki. Dluzej nie ma sensu przy danych rynkowych:
# swieca sprzed pieciu sekund opisuje juz inny rynek, a proces i tak wroci
# po nia w nastepnym cyklu.
PROBY = 3
ODSTEP_BAZA_S = 1.0
ODSTEP_MAX_S = 8.0

import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from .base import (
    AuthError, Balance, BookLevel, Candle, ConnectionCheck, ExchangeError, OrderBook,
    StanZlecenia, Wypelnienie, ZlozoneZlecenie,
)

# Dane publiczne bierzemy z podmiotu GLOBALNEGO niezaleznie od tego, gdzie stoi
# konto - to on ma ksiege, na ktorej model byl trenowany.
PUBLIC_HOST = "https://api.bybit.com"

# Hosty PRYWATNE (podpisane zapytania). Domyslne wartosci sluza wylacznie
# adapterom czytajacym dane publiczne; wlasciwy host przychodzi z ref.venue,
# zeby nie trzymac tej samej prawdy w dwoch miejscach.
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

# Mapowanie statusow Bybita na nasze. Slownik, a nie lancuch if-ow: status
# nieznany ma zostac nieznany, a nie wpasc do "new" przez przypadek.
_STATUSY = {
    "New": "new", "Created": "new", "Untriggered": "new",
    "PartiallyFilled": "partial",
    "Filled": "filled",
    "Cancelled": "cancelled", "PartiallyFilledCanceled": "cancelled",
    "Deactivated": "cancelled",
    "Rejected": "rejected",
}


def _status_bybita(status: str) -> str:
    return _STATUSY.get(status, f"nieznany:{status}")



def _strona_bybita(strona: str) -> str:
    """'buy'/'sell' -> 'Buy'/'Sell'. Nieznana wartosc KONCZY SIE BLEDEM.

    Domyslanie sie strony jest tu najgorszym mozliwym zachowaniem: pomylka nie
    objawia sie awaria, tylko zleceniem wykonanym w PRZECIWNA strone niz decyzja
    - a to wyglada w dzienniku jak strategia, ktora zrobila cos dziwnego.
    """
    s = strona.strip().lower()
    if s not in ("buy", "sell"):
        raise ValueError(f"nieznana strona zlecenia: {strona!r}")
    return "Buy" if s == "buy" else "Sell"


def _dec_albo_nic(v) -> Decimal | None:
    """Pusty string i brak pola znacza to samo: gielda tego nie podaje.

    Zamiana ich na Decimal("0") bylaby gorsza niz None, bo zero jako krok
    ilosci przechodzi przez kazde sprawdzenie i wywraca sie dopiero przy
    dzieleniu - daleko od miejsca, w ktorym powstalo.
    """
    if v in (None, "", "0"):
        return None
    try:
        return Decimal(str(v))
    except Exception:                                        # noqa: BLE001
        return None


class BybitAdapter:
    venue_code = "bybit"

    def __init__(self, api_key: str, api_secret: str, mode: str = "demo",
                 category: str = "spot", timeout: float = 15.0,
                 host: str | None = None):
        if mode not in HOSTS:
            raise ValueError(f"nieznany tryb: {mode}")
        self._key = api_key
        self._secret = api_secret.encode()
        # Host z ref.venue ma pierwszenstwo. Tabela wie, ktorego PODMIOTU jest
        # konto - a to nie wynika z samego trybu demo/live, odkad Bybit rozdzielil
        # sie na globalny i unijny (patrz komentarz na gorze modulu).
        self._host = host or HOSTS[mode]
        self._category = category
        self._client = httpx.AsyncClient(timeout=timeout)

    # ------------------------------------------------------------- transport
    def _sign(self, ts: str, payload: str) -> str:
        return hmac.new(self._secret, f"{ts}{self._key}{RECV_WINDOW}{payload}".encode(),
                        hashlib.sha256).hexdigest()

    async def _get(self, path: str, params: dict | None = None, *, private: bool = True,
                   host: str | None = None) -> dict:
        """Zapytanie z PONAWIANIEM stanow przejsciowych.

        DLACZEGO TO TU JEST

        `ExchangeError` niosl flage `retryable` od poczatku, ustawiana poprawnie
        dla przekroczen czasu, bledow polaczenia, HTTP 429 i 5xx. Czytali ja
        DOKLADNIE DWAJ odbiorcy - backfill i funding - i tylko po to, zeby
        przerwac wlasna petle. Nigdzie nie bylo odczekania ani ponowienia.

        Flaga opisywala wiec intencje, ktorej nikt nie wykonywal. Ten sam ksztalt
        co brak ponawiania u dostawcy analizy wiadomosci, gdzie objawialo sie to
        jako "bledow 7" i cisza.

        PODPIS JEST ODNAWIANY PRZY KAZDEJ PROBIE. Zawiera znacznik czasu, a
        `recv_window` u Bybita to piec sekund - ponowienie ze starym podpisem
        po odczekaniu kilku sekund zostaloby odrzucone jako blad
        uwierzytelnienia, czyli skierowaloby szukanie na klucz zamiast na siec.
        """
        ostatni: ExchangeError | None = None
        for proba in range(1, PROBY + 1):
            try:
                return await self._get_raz(path, params, private=private, host=host)
            except ExchangeError as e:
                if not e.retryable or proba == PROBY:
                    raise
                ostatni = e
                await asyncio.sleep(min(ODSTEP_MAX_S, ODSTEP_BAZA_S * 2 ** (proba - 1)))
        raise ostatni  # nieosiagalne, ale czytelniejsze niz milczaca sciezka

    async def _get_raz(self, path: str, params: dict | None = None, *, private: bool = True,
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

        if r.status_code == 404:
            # BRAK TRASY, nie blad klucza. Podmiot unijny nie wystawia czesci
            # endpointow konta na hoscie demo, a odpowiedz 404 bywa HTML-em,
            # wiec bez tego warunku konczylaby sie jako "odpowiedz nie jest
            # JSON-em" - komunikat nie do odroznienia od awarii.
            raise ExchangeError(f"endpoint niedostepny na tym hoscie: {path}",
                                code="404")
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

    # Ile stron wypelnien wolno pobrac w jednym uzgodnieniu. Limit istnieje po
    # to, zeby bledny kursor nie zapetlil sie w nieskonczonosc - a nie po to,
    # zeby uciac wynik. Przekroczenie KONCZY SIE BLEDEM, bo urwana lista
    # wypelnien zaniza odtworzona pozycje i wyglada potem jak rozjazd
    # "pozycja-widmo": alarm o pozycji, ktora naprawde istnieje.
    STRON_MAX = 50

    async def wypelnienia(self, *, od_ms: int | None = None,
                          symbol: str | None = None) -> list[Wypelnienie]:
        """Wszystkie wykonania z rejestru gieldy, od najnowszych.

        POBIERA WSZYSTKIE STRONY. Bybit zwraca `nextPageCursor`, dopoki jest co
        oddawac; zatrzymanie sie na pierwszej stronie dalo by obraz kilkudziesieciu
        ostatnich transakcji podany jako calosc historii.
        """
        params: dict = {"category": self._category, "limit": "100"}
        if symbol:
            params["symbol"] = symbol
        if od_ms is not None:
            params["startTime"] = str(od_ms)

        out: list[Wypelnienie] = []
        kursor: str | None = None
        for strona in range(self.STRON_MAX + 1):
            if strona == self.STRON_MAX:
                raise ExchangeError(
                    f"wypelnienia: przekroczono {self.STRON_MAX} stron - lista jest "
                    f"niepelna, a niepelna lista zaniza odtworzona pozycje")
            p = dict(params)
            if kursor:
                p["cursor"] = kursor
            res = await self._get("/v5/execution/list", p)
            for w in res.get("list", []):
                out.append(Wypelnienie(
                    id_gieldy=w.get("execId", ""),
                    id_wlasne=w.get("orderLinkId") or None,
                    symbol=w.get("symbol", ""),
                    strona=w.get("side", ""),
                    ilosc=Decimal(w.get("execQty") or "0"),
                    cena=Decimal(w.get("execPrice") or "0"),
                    prowizja=Decimal(w.get("execFee") or "0"),
                    prowizja_w=w.get("feeCurrency") or None,
                    czas=datetime.fromtimestamp(int(w.get("execTime", 0)) / 1000,
                                                tz=timezone.utc),
                ))
            kursor = res.get("nextPageCursor") or None
            if not kursor:
                break
        return out

    async def filtry_instrumentow(self, kategoria: str | None = None, *,
                                  host: str | None = None) -> dict[str, dict]:
        """Reguly, ktorych gielda pilnuje przy KAZDYM zleceniu.

        Krok ilosci, krok ceny i minimalna wartosc zlecenia nie sa szczegolem
        technicznym - sa warunkiem przyjecia zlecenia. Bez nich:

          * ilosc niezaokraglona do kroku zostaje ODRZUCONA, a odrzucone
            zlecenie wyglada w logu jak brak sygnalu, nie jak blad,
          * zlecenie ponizej minimalnej wartosci tez zostaje odrzucone - i to
            jest przypadek drogi, bo gdy odrzucone jest WYJSCIE, pozycja
            zostaje otwarta wbrew decyzji.

        HOST TEGO PODMIOTU, KTORY PRZYJMIE ZLECENIE - i to jest tu sedno.

        Dane rynkowe bierzemy z podmiotu globalnego, bo to jego ksiega
        posluzyla do trenowania modelu. REGULY ZLECEN SA CZYM INNYM: obowiazuja
        tam, gdzie zlecenie ladzie. Zmierzone 2026-09-07 na ADAUSDC:

            api.bybit.com      basePrecision 0.01
            api-demo.bybit.eu  basePrecision 0.1
            api.bybit.eu       basePrecision 0.1

        Pierwsza wersja czytala host globalny i zapisywala 0.01, wiec gielda
        unijna odbijala kazde zlecenie komunikatem "Order quantity has too many
        decimals". KOMUNIKAT WSKAZUJE NA NASZE ZAOKRAGLANIE, a wina jest w tym,
        KOGO PYTAMY o regule - szukanie idzie wiec od razu w zla strone.

        Endpoint jest publiczny, wiec nie wymaga klucza takze na hoscie podmiotu.
        """
        res = await self._get("/v5/market/instruments-info",
                              {"category": kategoria or self._category},
                              private=False, host=host or self._host)
        out: dict[str, dict] = {}
        for i in res.get("list", []):
            lot = i.get("lotSizeFilter") or {}
            price = i.get("priceFilter") or {}
            out[i.get("symbol", "")] = {
                "krok_ilosci": _dec_albo_nic(lot.get("basePrecision")
                                             or lot.get("qtyStep")),
                "min_ilosc": _dec_albo_nic(lot.get("minOrderQty")),
                "min_wartosc": _dec_albo_nic(lot.get("minOrderAmt")
                                             or lot.get("minNotionalValue")),
                "krok_ceny": _dec_albo_nic(price.get("tickSize")),
                "status": i.get("status"),
            }
        return out

    async def cena_odniesienia(self, symbol: str,
                               kategoria: str | None = None) -> Decimal | None:
        """Ostatnia cena Z KSIEGI TEGO PODMIOTU, w ktorym ladzie zlecenie.

        DLACZEGO NIE Z HOSTA PUBLICZNEGO. Swiece i arkusz bierzemy swiadomie
        z podmiotu globalnego - to jego ksiega posluzyla do trenowania modelu.
        Do sprawdzenia, czy cena limitu ma sens, sluzy jednak wylacznie ksiega,
        W KTOREJ ZLECENIE WYLADUJE. Porownanie z ta sama ksiega, z ktorej cena
        powstala, jest porownaniem liczby z sama soba: przejdzie zawsze, takze
        dla ticka zepsutego.

        Zmierzone 2026-09-07 na 1600 swiecach 4h: mediana rozjazdu zamkniec
        miedzy podmiotami 5,9 pb, p90 28,4 pb, a w 16,8% swiec rozjazd
        przekracza cala runde kosztowa. To nie jest szum pomiarowy.

        Endpoint jest publiczny, wiec nie wymaga klucza. Zwraca None, gdy nie da
        sie odczytac - BRAK ODCZYTU TO LUKA, NIE ZGODA, i wolajacy ma o tym
        wiedziec.
        """
        try:
            res = await self._get("/v5/market/tickers",
                                  {"category": kategoria or self._category,
                                   "symbol": symbol},
                                  private=False, host=self._host)
        except ExchangeError:
            return None
        lista = res.get("list") or []
        if not lista:
            return None
        cena = lista[0].get("lastPrice") or lista[0].get("markPrice")
        return Decimal(str(cena)) if cena else None

    async def dryf_zegara(self) -> tuple[int, str | None]:
        """Roznica miedzy naszym zegarem a zegarem gieldy, w milisekundach.

        DLACZEGO TO JEST OSOBNE SPRAWDZENIE

        Podpis zapytania prywatnego zawiera znacznik czasu, a `recv_window` to
        piec sekund. Zegar rozjechany o wiecej powoduje odrzucanie WSZYSTKICH
        zapytan prywatnych - i objawia sie jako blad uwierzytelnienia, czyli
        kieruje szukanie na klucz zamiast na czas. Kontener nie ma wlasnego NTP;
        polega na zegarze wezla.

        Endpoint czasu jest PUBLICZNY, wiec dziala takze wtedy, gdy podpis juz
        nie przechodzi - i to jest cala wartosc tego sprawdzenia. Gdyby wymagal
        podpisu, nie odpowiedzialby dokladnie w sytuacji, dla ktorej istnieje.
        """
        lokalnie = int(time.time() * 1000)
        odp = await self._get("/v5/market/time", private=False)
        # Bybit oddaje czas w sekundach i w nanosekundach; bierzemy to, co jest.
        gieldy = odp.get("timeSecond") or odp.get("timeNano")
        if gieldy is None:
            return 0, "host nie wystawia czasu serwera"
        gieldy = int(gieldy)
        gieldy_ms = gieldy * 1000 if gieldy < 10**12 else gieldy // 10**6
        # CZAS W PELNYCH SEKUNDACH daje do jednej sekundy pozornego dryfu
        # z samego obciecia. Progi ponizej sa na tyle luzne, ze to nie generuje
        # falszywych alarmow - ale przy zaostrzaniu ich trzeba o tym pamietac.
        dryf = gieldy_ms - lokalnie

        prog = int(RECV_WINDOW)
        if abs(dryf) > prog:
            return dryf, (
                f"ZEGAR ROZJECHANY o {dryf/1000:+.1f} s przy oknie {prog/1000:.0f} s. "
                f"Zapytania prywatne beda odrzucane jako bledny podpis - to NIE "
                f"jest problem z kluczem. Zsynchronizuj zegar wezla.")
        if abs(dryf) > prog // 2:
            return dryf, (f"zegar rozjechany o {dryf/1000:+.1f} s - miesci sie "
                          f"w oknie {prog/1000:.0f} s, ale zapas topnieje")
        return dryf, None

    async def check_connection(self) -> ConnectionCheck:
        """Sprawdza poswiadczenia i raportuje uprawnienia.

        Swiadomie uzywa endpointu informacyjnego o kluczu, nie skladania zlecen -
        test polaczenia nie moze miec skutkow ubocznych.
        """
        started = time.perf_counter()
        info: dict = {}
        note = ""

        # DRYF ZEGARA PRZED PODPISEM. Sprawdzenie idzie endpointem publicznym,
        # wiec odpowie takze wtedy, gdy zapytania prywatne juz sa odrzucane -
        # a wtedy komunikat mowi o czasie, a nie o kluczu.
        try:
            _, ostrzezenie = await self.dryf_zegara()
            if ostrzezenie:
                note += " " + ostrzezenie
        except ExchangeError:
            # Brak endpointu czasu nie jest powodem, zeby uznac polaczenie za
            # nieudane - to sprawdzenie pomocnicze.
            pass
        try:
            info = await self._get("/v5/user/query-api")
        except AuthError as e:
            return ConnectionCheck(ok=False, message=f"Klucz odrzucony: {e}")
        except ExchangeError as e:
            if e.code != "404":
                return ConnectionCheck(ok=False, message=f"Nie udalo sie polaczyc: {e}")
            # Host bez endpointu metadanych klucza - tak zachowuje sie demo
            # podmiotu unijnego. Poswiadczenia sprawdzamy wtedy dowolnym innym
            # zapytaniem PODPISANYM: przechodzi tylko z poprawnym podpisem,
            # a stawek prowizji nie da sie niczym zmienic.
            try:
                await self._get("/v5/account/fee-rate", {"category": self._category})
            except AuthError as e2:
                return ConnectionCheck(ok=False, message=f"Klucz odrzucony: {e2}")
            except ExchangeError as e2:
                return ConnectionCheck(ok=False, message=f"Nie udalo sie polaczyc: {e2}")
            note = (" Ten host nie wystawia metadanych klucza ani salda, "
                    "wiec uprawnien nie da sie tu odczytac - sprawdz je w panelu gieldy.")

        perms_raw = info.get("permissions") or {}
        perms = sorted({p for group in perms_raw.values() if isinstance(group, list) for p in group})
        can_withdraw = "Withdraw" in perms if info else None

        balances: list[Balance] = []
        try:
            balances = await self.fetch_balances()
        except ExchangeError as e:
            # Klucz dziala, ale saldo jest nieczytelne - albo brak uprawnienia,
            # albo host go nie wystawia. To wazna informacja dla uzytkownika,
            # a nie powod, zeby uznac polaczenie za nieudane.
            if e.code != "404":
                note += f" Odczyt salda niedostepny: {e}."

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

    # ---------------------------------------------------------------- zlecenia
    async def _post(self, path: str, body: dict) -> dict:
        """POST BEZ PONAWIANIA. Brak ponawiania jest tu funkcją, nie brakiem.

        Ponawianie GET-a jest niegroźne - najwyżej pobierzemy te same świece
        dwa razy. Ponawianie POST-a po przekroczeniu czasu jest DOKŁADNIE tym
        mechanizmem, który tworzy drugą pozycję: odpowiedź mogła nie dotrzeć,
        mimo że zlecenie weszło.

        Kto chce ponowić, musi najpierw SPRAWDZIĆ - patrz `zloz_zlecenie`.
        """
        query = json.dumps(body, separators=(",", ":"))
        ts = str(int(time.time() * 1000))
        headers = {
            "X-BAPI-API-KEY": self._key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": RECV_WINDOW,
            "X-BAPI-SIGN": self._sign(ts, query),
            "Content-Type": "application/json",
        }
        try:
            r = await self._client.post(f"{self._host}{path}", content=query, headers=headers)
        except httpx.TimeoutException as e:
            # `retryable=False`: ponowienie w gorę stosu byłoby podwójnym
            # zleceniem. Odpowiedź na przekroczenie czasu to SPRAWDZENIE, nie
            # powtórzenie - i o tym mówi komunikat.
            raise ExchangeError(
                f"brak odpowiedzi na zlecenie ({e}) - zlecenie MOGLO wejsc, "
                f"sprawdz je po id wlasnym przed jakakolwiek proba ponowienia",
                code="timeout", retryable=False) from e
        except httpx.HTTPError as e:
            raise ExchangeError(f"blad polaczenia przy zleceniu: {e}",
                                code="network", retryable=False) from e

        try:
            data = r.json()
        except ValueError as e:
            raise ExchangeError(f"odpowiedz na zlecenie nie jest JSON-em "
                                f"({r.status_code})") from e
        ret = data.get("retCode")
        if ret != 0:
            msg = data.get("retMsg", "nieznany blad")
            if ret in (10003, 10004, 10005, 33004):
                raise AuthError(f"{msg} (kod {ret})", code=str(ret))
            raise ExchangeError(f"{msg} (kod {ret})", code=str(ret))
        return data.get("result") or {}

    async def stan_zlecenia(self, id_wlasne: str, kategoria: str = "spot") -> StanZlecenia | None:
        """Stan zlecenia po NASZYM identyfikatorze. None, gdy giełda go nie zna.

        Odpytujemy po `orderLinkId`, nie po identyfikatorze giełdy - ten drugi
        poznajemy dopiero z odpowiedzi, więc w sytuacji, dla której to istnieje,
        jeszcze go nie mamy.

        Sprawdzamy zlecenia otwarte ORAZ historię: zlecenie mogło się wypełnić
        i zniknąć z listy otwartych między naszym timeoutem a zapytaniem.
        """
        for sciezka in ("/v5/order/realtime", "/v5/order/history"):
            odp = await self._get(sciezka, {"category": kategoria,
                                            "orderLinkId": id_wlasne})
            lista = odp.get("list") or []
            if lista:
                o = lista[0]
                ilosc = Decimal(str(o.get("qty") or 0))
                wyp = Decimal(str(o.get("cumExecQty") or 0))
                cena = o.get("avgPrice")
                return StanZlecenia(
                    id_wlasne=id_wlasne,
                    id_gieldy=o.get("orderId"),
                    status=_status_bybita(o.get("orderStatus", "")),
                    ilosc=ilosc, wypelniono=wyp,
                    srednia_cena=Decimal(str(cena)) if cena else None,
                    prowizja=Decimal(str(o.get("cumExecFee") or 0)),
                    # WALUTA PROWIZJI, NIE ZALOZENIE. Na spocie Bybit pobiera
                    # prowizje od kupna W KUPIONYM AKTYWIE, a nie w walucie
                    # kwotowanej. `Wypelnienie` mialo to pole od poczatku
                    # i uzgodnienie z niego korzysta; sciezka egzekucji czytala
                    # sama kwote i zapisywala ja jako USDC.
                    #
                    # Skutek byl podwojny i oba razy w te sama strone: koszt
                    # wejscia zanizony okolo stukrotnie (0,01 SOL zapisane jako
                    # 0,01 USDC zamiast ~1 USDC), a ilosc w dzienniku wyzsza od
                    # faktycznego salda o wartosc prowizji - dokladnie na progu
                    # tolerancji uzgodnienia.
                    prowizja_w=o.get("feeCurrency") or None,
                )
        return None

    async def zloz_zlecenie(self, *, symbol: str, strona: str, ilosc: Decimal,
                            cena_limitu: Decimal, id_wlasne: str,
                            kategoria: str = "spot") -> ZlozoneZlecenie:
        """Składa zlecenie z limitem. IDEMPOTENTNIE.

        `id_wlasne` musi być wyprowadzone z decyzji, a NIE z czasu ani losowo -
        inaczej ponowienie tworzy nowy identyfikator i cała ochrona znika.

        SPRAWDŹ, POTEM SKŁADAJ. Zamiast ponawiać na ślepo, przy braku odpowiedzi
        pytamy giełdę, czy zna to `id_wlasne`. Trzy możliwe odpowiedzi i każda
        prowadzi gdzie indziej:
          * zna i wypełnione  -> nic nie robimy, zwracamy stan
          * zna i otwarte     -> nic nie robimy, zwracamy stan
          * nie zna           -> zlecenie nie weszło, wolno złożyć
        """
        # Giełda też sprawdza unikalność orderLinkId, ale dopiero PO przyjęciu
        # zapytania. Sprawdzenie u nas oszczędza jedno okrążenie i - co ważniejsze -
        # daje ten sam wynik, gdy poprzednia próba skończyła się timeoutem.
        istniejace = await self.stan_zlecenia(id_wlasne, kategoria)
        if istniejace is not None:
            return ZlozoneZlecenie(id_wlasne=id_wlasne, id_gieldy=istniejace.id_gieldy,
                                   status=istniejace.status, juz_istnialo=True)

        wynik = await self._post("/v5/order/create", {
            "category": kategoria,
            "symbol": symbol,
            # STRONA MAPOWANA JAWNIE, bez galezi "wszystko inne to sprzedaz".
            #
            # Poprzednia wersja brzmiala `"Buy" if strona == "buy" else "Sell"`,
            # wiec KAZDA wartosc poza doslownym "buy" - w tym "Buy" - zamieniala
            # kupno w sprzedaz. Objaw nie wyglada na blad: gielda przyjmuje takie
            # zlecenie i wykonuje je, tylko w druga strone niz decyzja.
            "side": _strona_bybita(strona),
            "orderType": "Limit",
            "qty": str(ilosc),
            "price": str(cena_limitu),
            # POSTONLY, NIE GTC. Zlecenie, ktore przecina spread, jest przez
            # gielde wypelniane NATYCHMIAST jako taker - u podmiotu unijnego po
            # 0,250% zamiast 0,100% makera. Dzieje sie to po cichu: w dzienniku
            # wyglada jak zwykle wypelnienie.
            #
            # Zdarza sie to czesto, bo cene limitu wyliczamy z ksiegi podmiotu
            # GLOBALNEGO, a zlecenie ladzie w ksiedze UNIJNEJ. Zmierzone
            # 2026-09-07 na 1600 swiecach 4h: mediana rozjazdu zamkniec 5,9 pb,
            # p90 28,4 pb, a w 16,8% swiec rozjazd przekracza CALA runde
            # kosztowa (20 pb). Dla NEARUSDC w polowie swiec.
            #
            # DLACZEGO TO NIE ZMIENIA STRATEGII, TYLKO JA URZECZYWISTNIA:
            # symulacja juz zaklada wypelnienie makerem po cenie limitu i bez
            # poslizgu (POSLIZG_MAKER = 0). PostOnly wymusza dokladnie to
            # zalozenie - albo jestesmy makerem po naszej cenie, albo nie
            # handlujemy. Bez niego produkcja czasem placi takera, czego
            # symulacja nie modeluje w ogole, a projekt zmierzyl, ze przy
            # takerze Sharpe jest ujemny.
            #
            # Odrzucenie zamiast cichego takera jest przy tym WIDOCZNE: trafia
            # do czujki zdrowia egzekucji, ktora liczy odrzucone do przyjetych.
            "timeInForce": "PostOnly",
            "orderLinkId": id_wlasne,
        })
        return ZlozoneZlecenie(id_wlasne=id_wlasne, id_gieldy=wynik.get("orderId"),
                               status="new")

    async def anuluj_zlecenie(self, *, symbol: str, id_wlasne: str,
                              kategoria: str = "spot") -> bool:
        """Anuluje zlecenie po naszym identyfikatorze. False, gdy nie było czego.

        Zlecenie z limitem jest planem na JEDNĄ świecę (patrz README). Bez
        anulowania niewypełnione plany zbierałyby się na giełdzie i wypełniały
        po cenach, których żadna decyzja już nie popiera.
        """
        try:
            await self._post("/v5/order/cancel", {
                "category": kategoria, "symbol": symbol, "orderLinkId": id_wlasne})
            return True
        except ExchangeError as e:
            # 110001 to "zlecenia nie ma" - przy anulowaniu to sukces, nie awaria.
            if e.code == "110001":
                return False
            raise

    async def close(self) -> None:
        await self._client.aclose()
