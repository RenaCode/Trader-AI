"""Arkusz per LISTING: giełda, która symbolu nie prowadzi, nie jest o niego pytana.

CO SIĘ STAŁO (awaria 2026-09-24 → 2026-09-25, 242 restarty)

cd77710 przestawił `fetch_order_book` z hosta globalnego na `self._host`, czyli
na podmiot, na którym stoi konto. Zamiar był słuszny i dla par USDC działa:
mierzony spread spadł 10,57 → 5,85 pb, bo to naprawdę księga wypełniająca nasze
zlecenia. Zabrakło jednego rozróżnienia.

`market_worker` zbiera arkusz dla wszystkich instrumentów **active**, a to nie to
samo co **tradeable** (0020): produkcja ma 10 par USDC (active + tradeable) i 24
pary USDT (active, NIE tradeable). Podmiot unijny jako podmiot MiCA nie listuje
ANI JEDNEJ pary *USDT — zmierzone na żywym API:

    api.bybit.com   ADAUSDT, TRXUSDT -> retCode 0
    api.bybit.eu    te same symbole  -> retCode 10001 "Not supported symbols"
    api.bybit.eu    instruments-info -> 128 symboli, wszystkie USDC/EUR/PLN

Dwadzieścia twardych błędów na cykl, pięć cykli i koniec procesu — w kółko, bo
restart tego nie naprawia.

TRZY RZECZY, KTÓRYCH TEN PLIK PILNUJE, I DLACZEGO ŻADNEJ NIE MOŻNA POMINĄĆ

1. HOST PER INSTRUMENT, wybierany flagą `tradeable`. Dla instrumentu, którym
   handlujemy — księga egzekucji (rachunek kosztu opisuje księgę, w której
   staje nasze zlecenie). Dla pozostałych — host globalny, czyli zachowanie
   sprzed cd77710; nie z wygody, ale dlatego, że ich wartości `book_spread_bps`
   sprzed 24.09 też pochodzą stamtąd, więc szereg zostaje ciągły.

2. SYMBOL NIELISTOWANY TO NIE AWARIA, ALE TEŻ NIE ZERO. Docstring workera mówi
   wprost, że liczenie porażki jednego instrumentu jako awarii cyklu jest
   DECYZJĄ: „inaczej instrument, którego giełda nie zna, mógłby milczeć
   miesiącami przy zerowym liczniku restartów". Wyłączenie tego zamieniłoby
   awarię w ciszę, czyli w gorszy problem. Dlatego cykl kończy się POWODZENIEM,
   a liczba takich symboli idzie do `detail` pulsu jako `bez_arkusza` — log
   ginie przy restarcie, baza nie. Zabezpieczenie „pięć nieudanych cykli kończy
   proces" ZOSTAJE i działa dla błędów prawdziwych.

3. NIE PRZYJMUJEMY ARKUSZA OD GIEŁDY, KTÓRA SYMBOLU NIE LISTUJE — i to jest
   reguła osobna od wyboru hosta, bo sam błąd jej nie zapewnia. Cztery majory
   (BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT) nie są listowane na bybit.eu, a endpoint
   oddaje tam PEŁNY arkusz: od 24.09 zapisano ~3089 takich snapshotów i spread
   majorów przeskoczył 0,39 → 2,29 pb. Odpowiedź wyglądała poprawnie, więc nic
   nie zepsuło się w sposób widoczny — dokładnie ta klasa błędu, którą tropi
   `koszty.py`: liczba prawdziwa, tylko o czym innym.

ATRAPY ODDAJĄ TO, CO ODDAJE REALNY ENDPOINT, łącznie z `retCode 10001` i pełnym
arkuszem dla symbolu nielistowanego. Giełdy nie pytamy: cały ruch idzie przez
`httpx.MockTransport`.
"""
from __future__ import annotations

import asyncio
import sys

import httpx
import pytest

from services.exchanges.base import (
    ExchangeError, OdmowaGieldy, SymbolNieListowany,
)
from services.exchanges.bybit import PUBLIC_HOST, BybitAdapter

HOST_EU = "https://api-demo.bybit.eu"

# Produkcyjny kształt uniwersum (0020 + 0023): 10 par USDC w uniwersum
# decyzyjnym, 24 pary USDT tylko do historii.
USDC = ["ADAUSDC", "BTCUSDC", "DOGEUSDC", "ETHUSDC", "LINKUSDC",
        "LTCUSDC", "SOLUSDC", "TRXUSDC", "XLMUSDC", "XRPUSDC"]
USDT = ["ADAUSDT", "APTUSDT", "ATOMUSDT", "AVAXUSDT", "BCHUSDT", "BNBUSDT",
        "BTCUSDT", "DOGEUSDT", "DOTUSDT", "ETHUSDT", "FILUSDT", "INJUSDT",
        "LINKUSDT", "LTCUSDT", "NEARUSDT", "OPUSDT", "SEIUSDT", "SOLUSDT",
        "SUIUSDT", "TIAUSDT", "TRXUSDT", "UNIUSDT", "XLMUSDT", "XRPUSDT"]


# ------------------------------------------------------- atrapa realnego API

def _instruments_info(symbole, *, kursor: str = "") -> dict:
    """Kształt `/v5/market/instruments-info` (kategoria spot) wg V5."""
    return {"retCode": 0, "retMsg": "OK", "result": {
        "category": "spot",
        "list": [{"symbol": s, "status": "Trading",
                  "lotSizeFilter": {"basePrecision": "0.01", "minOrderQty": "1",
                                    "minOrderAmt": "1"},
                  "priceFilter": {"tickSize": "0.001"}} for s in symbole],
        "nextPageCursor": kursor}}


def _arkusz(symbol: str) -> dict:
    """Kształt `/v5/market/orderbook` wg V5."""
    return {"retCode": 0, "retMsg": "OK", "result": {
        "s": symbol, "ts": 1758700000000, "u": 1,
        "b": [["100.10", "5"], ["100.05", "7"]],
        "a": [["100.20", "4"], ["100.25", "6"]]}}


# Realna odpowiedź `api.bybit.eu` na parę USDT, zmierzona 2026-09-24.
NIE_LISTOWANY = {"retCode": 10001, "retMsg": "Not supported symbols", "result": {}}


def _swieca() -> dict:
    """Jedna świeca 1m z odległej przeszłości — `cycle` odrzuca bieżącą minutę."""
    return {"retCode": 0, "retMsg": "OK", "result": {"symbol": "X", "list": [
        ["1700000040000", "1", "2", "0.5", "1.5", "10", "15"]]}}


def gielda(*, listuje: dict[str, list[str]], arkusz_wszedzie: bool = False,
           padaj_na_arkuszu: bool = False):
    """Adapter na `MockTransport` + lista URL-i, o które naprawdę zapytał.

    `listuje` to mapa host -> symbole z `instruments-info`. `arkusz_wszedzie`
    odtwarza zmierzone zachowanie bybit.eu: pełny arkusz dla symbolu, którego
    ten podmiot NIE listuje.
    """
    zapytania: list[httpx.URL] = []

    def handler(req: httpx.Request) -> httpx.Response:
        zapytania.append(req.url)
        host = f"{req.url.scheme}://{req.url.host}"
        sciezka = req.url.path
        if sciezka == "/v5/market/instruments-info":
            if host not in listuje:
                return httpx.Response(200, json=_instruments_info([]))
            symbole = listuje[host]
            return httpx.Response(200, json=_instruments_info(
                symbole if isinstance(symbole, list) else list(symbole)))
        if sciezka == "/v5/market/kline":
            return httpx.Response(200, json=_swieca())
        if sciezka == "/v5/market/orderbook":
            if padaj_na_arkuszu:
                return httpx.Response(500, json={"retCode": 0})
            symbol = req.url.params["symbol"]
            if symbol in listuje.get(host, []) or arkusz_wszedzie:
                return httpx.Response(200, json=_arkusz(symbol))
            return httpx.Response(200, json=NIE_LISTOWANY)
        raise AssertionError(f"atrapa nie zna endpointu {sciezka}")

    k = BybitAdapter(api_key="k", api_secret="s", mode="demo", host=HOST_EU)
    k._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return k, zapytania


def bez_czekania(monkeypatch, maks: int = 200):
    """Ponawianie odczekuje sekundy — w teście nie ma na co czekać.

    LIMIT LICZBY UŚPIEŃ JEST CZĘŚCIĄ TEJ ATRAPY, nie ostrożnością na zapas.
    Pętla workera usypia raz na cykl, więc atrapa bez limitu zamienia zepsutą
    poprawkę („licznik awarii wyłączony dla wszystkiego") z PADAJĄCEGO testu
    w test, który WISI — a test, który wisi, nie mówi nic i blokuje cały
    przebieg. Wykryte kanarkiem, na własnym teście.
    """
    licznik = {"n": 0}

    async def natychmiast(_s):
        licznik["n"] += 1
        if licznik["n"] > maks:
            raise AssertionError(
                f"pętla nie skończyła się po {maks} uśpieniach — proces, który "
                f"miał się zakończyć, kręci się dalej")

    monkeypatch.setattr(asyncio, "sleep", natychmiast)


# ====================================================== 1. HOST PER INSTRUMENT

def test_arkusz_pary_TRADEABLE_idzie_z_ksiegi_egzekucji():
    """Sedno cd77710, które zostaje: koszt opisuje księga, która nas wypełnia."""
    k, zapytania = gielda(listuje={HOST_EU: USDC, PUBLIC_HOST: USDC + USDT})
    ksiega = asyncio.run(k.fetch_order_book("SOLUSDC", 25, ksiega_egzekucji=True))
    assert ksiega.spread > 0
    arkusze = [u for u in zapytania if u.path == "/v5/market/orderbook"]
    assert [u.host for u in arkusze] == ["api-demo.bybit.eu"]


def test_arkusz_pary_NIEHANDLOWALNEJ_idzie_z_hosta_globalnego():
    """Zachowanie sprzed cd77710 — i to jest powód, nie ustępstwo: wartości
    `book_spread_bps` tych instrumentów sprzed 24.09 też pochodzą stamtąd,
    więc szereg zostaje ciągły."""
    k, zapytania = gielda(listuje={HOST_EU: USDC, PUBLIC_HOST: USDC + USDT})
    asyncio.run(k.fetch_order_book("ADAUSDT", 25, ksiega_egzekucji=False))
    arkusze = [u for u in zapytania if u.path == "/v5/market/orderbook"]
    assert [u.host for u in arkusze] == ["api.bybit.com"]


def test_para_USDT_z_ksiegi_egzekucji_to_BRAK_LISTINGU_a_nie_awaria():
    """Dokładnie ta odpowiedź, która wywracała cykl: `retCode 10001`."""
    k, _ = gielda(listuje={HOST_EU: USDC, PUBLIC_HOST: USDC + USDT})
    with pytest.raises(SymbolNieListowany) as e:
        asyncio.run(k.fetch_order_book("ADAUSDT", 25, ksiega_egzekucji=True))
    assert not e.value.retryable, "ponowienie da to samo — stan jest trwały"
    assert not e.value.wymaga_czlowieka
    assert not isinstance(e.value, OdmowaGieldy), (
        "to nie jest odmowa przyjęcia ZLECENIA — sklejenie tych dwóch rzeczy "
        "wciąga brak listingu do liczników egzekucji")


# ============================ 3. ARKUSZ TYLKO OD GIEŁDY, KTÓRA SYMBOL LISTUJE

def test_gieldy_ktora_symbolu_NIE_LISTUJE_nie_pytamy_o_arkusz_WCALE():
    """Sedno punktu 3. bybit.eu nie listuje BTCUSDT, a mimo to oddaje PEŁNY
    arkusz — ~3089 takich snapshotów weszło do bazy i spread majorów przeskoczył
    0,39 → 2,29 pb. Błąd nie obroni nas tu przed niczym, bo błędu nie ma."""
    k, zapytania = gielda(listuje={HOST_EU: USDC, PUBLIC_HOST: USDC + USDT},
                          arkusz_wszedzie=True)
    with pytest.raises(SymbolNieListowany):
        asyncio.run(k.fetch_order_book("BTCUSDT", 25, ksiega_egzekucji=True))
    assert not [u for u in zapytania if u.path == "/v5/market/orderbook"], (
        "zapytaliśmy o arkusz giełdę, która tego instrumentu nie prowadzi — "
        "odpowiedź wygląda poprawnie i właśnie dlatego jest groźna")


def test_kod_10001_z_ARKUSZA_tez_jest_brakiem_listingu():
    """Druga warstwa tej samej reguły, na wypadek rozjazdu listy z arkuszem:
    listing mówi, że symbol jest, a endpoint odpowiada „Not supported symbols".
    Bez tego wpadałby w `OdmowaGieldy` i liczył się jako awaria cyklu."""
    def handler(req):
        if req.url.path == "/v5/market/instruments-info":
            return httpx.Response(200, json=_instruments_info(["ADAUSDT"]))
        return httpx.Response(200, json=NIE_LISTOWANY)

    k = BybitAdapter(api_key="k", api_secret="s", mode="demo", host=HOST_EU)
    k._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(SymbolNieListowany) as e:
        asyncio.run(k.fetch_order_book("ADAUSDT", 25))
    assert e.value.code == "10001"


def test_PUSTA_lista_instrumentow_JEST_awaria_a_nie_zbior_pusty():
    """Zbiór pusty znaczyłby „ten host nie listuje niczego", czyli uznałby KAŻDY
    symbol za nielistowany — nieudany odczyt zamieniłby się w ciszę dla całego
    cyklu, i to dokładnie w chwili, gdy giełda ma kłopot."""
    k, _ = gielda(listuje={})
    with pytest.raises(ExchangeError) as e:
        asyncio.run(k.fetch_order_book("SOLUSDC", 25))
    assert not isinstance(e.value, SymbolNieListowany)
    assert e.value.code == "listing-pusty"


def test_URWANA_lista_instrumentow_JEST_awaria():
    """Lista niepełna, użyta jako pełna, wypisuje z rynku instrumenty, które
    na nim są — ten sam kształt co `STRON_MAX` przy wypełnieniach."""
    def handler(req):
        return httpx.Response(200, json=_instruments_info(USDC, kursor="dalej"))

    k = BybitAdapter(api_key="k", api_secret="s", mode="demo", host=HOST_EU)
    k._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ExchangeError) as e:
        asyncio.run(k.fetch_order_book("SOLUSDC", 25))
    assert e.value.code == "listing-urwany"
    assert not isinstance(e.value, SymbolNieListowany)


def test_PRAWDZIWY_blad_odczytu_NIE_jest_brakiem_listingu(monkeypatch):
    """Kanarek do trzech powyższych: 5xx to odczyt, który się NIE UDAŁ. Dane
    miały przyjść i nie przyszły, więc licznik awarii musi je zobaczyć."""
    bez_czekania(monkeypatch)
    k, _ = gielda(listuje={HOST_EU: USDC}, padaj_na_arkuszu=True)
    with pytest.raises(ExchangeError) as e:
        asyncio.run(k.fetch_order_book("SOLUSDC", 25))
    assert not isinstance(e.value, SymbolNieListowany)
    assert e.value.retryable


def test_lista_instrumentow_pytana_RAZ_NA_HOST_a_nie_raz_na_symbol():
    """Inaczej każdy odczyt arkusza ciągnąłby za sobą drugie zapytanie, o całą
    listę instrumentów — 34 razy na minutę, przy limicie zapytań demo, którego
    nie da się podnieść."""
    k, zapytania = gielda(listuje={HOST_EU: USDC, PUBLIC_HOST: USDC + USDT})

    async def przebieg():
        for s in USDC[:4]:
            await k.fetch_order_book(s, 25, ksiega_egzekucji=True)
        await k.fetch_order_book("ADAUSDT", 25, ksiega_egzekucji=False)

    asyncio.run(przebieg())
    listy = [u for u in zapytania if u.path == "/v5/market/instruments-info"]
    assert sorted(u.host for u in listy) == ["api-demo.bybit.eu", "api.bybit.com"]


# =============================================== 2. WORKER: CYKL, PULS, LICZNIK

class Kursor:
    def __init__(self, wiersze, zapisy):
        self._wiersze = wiersze
        self._zapisy = zapisy
        self.rowcount = 0

    def execute(self, sql, params=None):
        self._zapisy.append((sql, params))
        self.rowcount = 1
        return self

    def executemany(self, sql, params=None):
        lista = list(params or [])
        self._zapisy.append((sql, lista))
        self.rowcount = len(lista)
        return self

    def fetchall(self):
        return self._wiersze

    def fetchone(self):
        return self._wiersze[0] if self._wiersze else None


class Polaczenie:
    """Rozdziela zapisy puszczone PROSTO z połączenia (puls) od kursorowych."""

    def __init__(self, instrumenty):
        self.instrumenty = instrumenty
        self.zapytania: list[tuple] = []
        self.zapisy: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.zapytania.append((sql, params))
        return Kursor([], self.zapisy)

    def cursor(self, *a, **kw):
        return Kursor(self.instrumenty, self.zapisy)

    def commit(self):
        pass

    def rollback(self):
        pass


def instrument(i: int, symbol: str, *, tradeable: bool) -> dict:
    return {"id": i, "symbol": symbol, "tradeable": tradeable}


def _pulsy(conn) -> list[tuple[str, tuple]]:
    """(rodzaj, parametry) dla każdego zapisu do risk.worker_heartbeat."""
    return [("udany" if sql.lstrip().upper().startswith("INSERT") else "awaria", params)
            for sql, params in conn.zapytania if "risk.worker_heartbeat" in sql]


def _detail(params) -> dict:
    """`detail` z parametrów pulsu — psycopg pakuje słownik w `Jsonb`."""
    return params[-1].obj


def _snapshoty(conn) -> list[int]:
    """Identyfikatory instrumentów, dla których zapisał się arkusz."""
    return [p[0] for sql, p in conn.zapisy if "market.book_snapshot" in sql]


def uruchom(monkeypatch, conn, adapter, *reszta):
    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    from services.ingest import market_worker
    monkeypatch.setattr(market_worker.psycopg, "connect", lambda url, **kw: conn)
    monkeypatch.setattr(market_worker, "adapter_danych",
                        lambda conn, engine, **kw: adapter)
    monkeypatch.setattr(sys, "argv",
                        ["market_worker.py", "--interval", "60", *reszta])
    return asyncio.run(market_worker.main())


def test_cykl_z_20_NIELISTOWANYMI_i_10_poprawnymi_KONCZY_SIE_POWODZENIEM(monkeypatch):
    """Sedno naprawy awarii. Dwadzieścia symboli, których giełda egzekucji nie
    prowadzi, to stan TRWAŁY i ZNANY — nie pięć cykli do końca procesu.

    Zabezpieczenie nie jest wyłączone: ono po prostu nie ma tu czego liczyć,
    bo nic się nie zepsuło.
    """
    bez_czekania(monkeypatch, maks=20)
    symbole = USDC + USDT[:20]
    instrumenty = [instrument(i, s, tradeable=True)
                   for i, s in enumerate(symbole, start=1)]
    conn = Polaczenie(instrumenty)
    adapter, _ = gielda(listuje={HOST_EU: USDC, PUBLIC_HOST: symbole})

    assert uruchom(monkeypatch, conn, adapter, "--once") == 0, (
        "cykl z symbolami, których giełda nie listuje, nadal liczy się jako "
        "awaria — to zamienia trwały brak danych w CrashLoopBackOff")
    assert [r for r, _ in _pulsy(conn)] == ["udany"]
    assert len(_snapshoty(conn)) == 10, "arkusze par USDC nie zapisały się"


def test_liczba_symboli_BEZ_ARKUSZA_trafia_do_PULSU(monkeypatch):
    """Drugie sedno. Cykl udany bez tej liczby twierdziłby, że zebrano
    wszystko — czyli poprawka zamieniłaby awarię w ciszę, a cisza jest gorsza
    niż restart. Log ginie przy restarcie; `detail` pulsu nie."""
    bez_czekania(monkeypatch, maks=20)
    symbole = USDC + USDT[:20]
    instrumenty = [instrument(i, s, tradeable=True)
                   for i, s in enumerate(symbole, start=1)]
    conn = Polaczenie(instrumenty)
    adapter, _ = gielda(listuje={HOST_EU: USDC, PUBLIC_HOST: symbole})

    assert uruchom(monkeypatch, conn, adapter, "--once") == 0
    detail = _detail(_pulsy(conn)[0][1])
    assert detail["bez_arkusza"] == 20, (
        f"liczba symboli bez arkusza nie doszła do bazy: {detail}")
    assert detail["orderbooki"] == 10
    assert detail["instrumenty"] == 30


def test_uklad_PRODUKCYJNY_10_USDC_i_24_USDT_zbiera_WSZYSTKIE_34_arkusze(monkeypatch):
    """Awaria z 24.09 w swoim prawdziwym kształcie. Pary USDT idą na host
    globalny, który je listuje, więc nie ma ani błędu, ani dziury w szeregu:
    34 arkusze, `bez_arkusza` = 0."""
    bez_czekania(monkeypatch, maks=20)
    instrumenty = ([instrument(i, s, tradeable=True)
                    for i, s in enumerate(USDC, start=1)]
                   + [instrument(100 + i, s, tradeable=False)
                      for i, s in enumerate(USDT, start=1)])
    conn = Polaczenie(instrumenty)
    adapter, zapytania = gielda(listuje={HOST_EU: USDC, PUBLIC_HOST: USDC + USDT})

    assert uruchom(monkeypatch, conn, adapter, "--once") == 0
    assert len(_snapshoty(conn)) == 34
    assert _detail(_pulsy(conn)[0][1])["bez_arkusza"] == 0

    # KTÓRA KSIĘGA DLA KTÓREGO INSTRUMENTU — proweniencja bez kolumny w schemacie.
    arkusze = {u.params["symbol"]: u.host
               for u in zapytania if u.path == "/v5/market/orderbook"}
    assert {arkusze[s] for s in USDC} == {"api-demo.bybit.eu"}
    assert {arkusze[s] for s in USDT} == {"api.bybit.com"}


def test_ksiege_wybiera_TRADEABLE_a_nie_sufiks_symbolu(monkeypatch):
    """Heurystyka „kończy się na USDC" dałaby ten sam wynik na produkcji i mylnie
    przy pierwszej parze USDC, której jeszcze nie handlujemy, oraz przy pierwszej
    parze USDT dopuszczonej do handlu. Myliłaby się przy tym CICHO, bo arkusz i tak
    by przyszedł — tylko z niewłaściwej księgi."""
    bez_czekania(monkeypatch, maks=20)
    instrumenty = [instrument(1, "ADAUSDT", tradeable=True),
                   instrument(2, "SOLUSDC", tradeable=False)]
    conn = Polaczenie(instrumenty)
    adapter, zapytania = gielda(
        listuje={HOST_EU: ["ADAUSDT", "SOLUSDC"], PUBLIC_HOST: ["ADAUSDT", "SOLUSDC"]})

    assert uruchom(monkeypatch, conn, adapter, "--once") == 0
    arkusze = {u.params["symbol"]: u.host
               for u in zapytania if u.path == "/v5/market/orderbook"}
    assert arkusze == {"ADAUSDT": "api-demo.bybit.eu", "SOLUSDC": "api.bybit.com"}


def test_PRAWDZIWY_blad_NADAL_konczy_proces_po_pieciu_cyklach(monkeypatch):
    """Zabezpieczenie zostaje. Gdyby padło razem z poprawką, worker mógłby stać
    tygodniami na martwej sieci przy zerowym liczniku restartów."""
    bez_czekania(monkeypatch, maks=20)
    from services.ingest import market_worker
    conn = Polaczenie([instrument(1, "SOLUSDC", tradeable=True)])
    adapter, _ = gielda(listuje={HOST_EU: USDC}, padaj_na_arkuszu=True)

    assert uruchom(monkeypatch, conn, adapter, "--interval", "0") == 1
    assert [r for r, _ in _pulsy(conn)] == ["awaria"] * market_worker.POD_RZAD_MAX


def test_worker_czyta_TRADEABLE_z_bazy_a_nie_domysla_sie_go(monkeypatch):
    """Kolumna istnieje od 0020 (NOT NULL DEFAULT true) i wystarczy ją wybrać.
    Bez niej worker musiałby trzymać drugą kopię uniwersum decyzyjnego."""
    from services.ingest import market_worker
    zrodlo = market_worker.__file__
    tekst = open(zrodlo, encoding="utf-8").read()
    wybor = tekst[tekst.index("SELECT i.id"):tekst.index("ORDER BY i.symbol")]
    assert "i.tradeable" in wybor
    assert "i.active" in wybor, "filtrem zbierania historii zostaje `active`"
