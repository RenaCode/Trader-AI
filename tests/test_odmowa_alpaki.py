"""Alpaca też musi umieć powiedzieć „NIE" — `OdmowaGieldy`, nie `unknown`.

CO BYŁO NIE TAK

`services/exchanges/alpaca.py` nie podnosiło `OdmowaGieldy` ANI RAZU
(`bybit.py` podnosi ją trzy razy). Każde 4xx poza 401 i 403-zamrożeniem
wracało jako `ExchangeError(retryable=False)`, co w `gielda_exec.zloz_plan`
wpada w `except ExchangeError` i zapisuje `status='unknown'` z etapem
`odpowiedz_nie_dotarla`. Dla zerwanego połączenia to jedyna uczciwa odpowiedź.
Dla `HTTP 422 "qty must be integer"` jest to zapisanie w bazie NIEPRAWDY
o stanie, który znamy dokładnie: giełda ODPOWIEDZIAŁA, a zlecenia NIE MA.

DWIE PREMISY, KTÓRE SIĘ NIE POTWIERDZAJĄ — i dlatego nie ma ich w tych testach

  * plan NIE zostaje `pending`. `rozstrzygnij_plan` (`gielda_exec.py:585-590`)
    ustawia `expired`, gdy giełda nie zna zlecenia.
  * czujka NIE milczy. `posprzataj_zlecenia` zapisuje `cancelled`
    z `venue_order_id IS NULL`, a `dozor.py:1694-1701` liczy `bez_kontaktu`
    bez filtra po silniku i zapala alarm od pierwszej sztuki.

REALNA SZKODA TO BRAK ADAPTACJI. `odmowa_gieldy` (`gielda_exec.py:423+`) pyta
giełdę o saldo i składa ilość, którą giełda POTWIERDZA. Ścieżka
`ExchangeError` tego nie robi, a odmowa za parametr jest deterministyczna —
więc system wysyłałby TĘ SAMĄ niepoprawną ilość co świecę, bez końca.
Druga szkoda: alarm `bez_kontaktu` mówi „giełda ich nie widziała", co dla 422
jest fałszem — widziała i odmówiła. Właściciel dostaje codziennie alarm o złej
przyczynie, czyli dokładnie ten kształt, który uczy ignorować kategorię.

OSOBNO: `position is not shortable` → `KontoZamrozone`

Warunek 403 łapał tylko `buying power`/`insufficient`, więc RESZTA 403 leciała
w `KontoZamrozone` z `wymaga_czlowieka=True` i komunikatem „KONTO ZABLOKOWANE
PO STRONIE GIEŁDY (compliance / wsparcie)". Odmowa krótkiej sprzedaży JEDNEGO
symbolu była więc zgłaszana jako blokada CAŁEGO konta — właściciel idzie do
wsparcia giełdy w sprawie papieru, którego nie da się pożyczyć.

DLACZEGO TO DZIŚ JEST UTAJONE, A NAPRAWA MIMO TO MA SENS

Żadne zlecenie equity dziś nie powstaje: uniwersum jest puste
(`tradeable = false` dla wszystkich spółek, migracja 0047) i pętla chodzi
z `--wymagaj-modelu`. Ale ta sama funkcja mapuje błędy także na ODCZYCIE
(`/v2/account`, `pozycje()`), którym chodzi worker `kapital-equity` co 900 s —
więc 403 na odczycie zgłaszało się jako `KontoZamrozone` już teraz.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal as D

import httpx
import pytest

from services.exchanges.alpaca import AlpacaAdapter
from services.exchanges.base import (
    ExchangeError, KontoZamrozone, OdmowaGieldy,
)

ZLECENIE = {"id": "904837e3-3b76-47ec-b432-046db621571b", "client_order_id": "dec-77",
            "status": "new", "qty": "10", "filled_qty": "0", "filled_avg_price": None,
            "symbol": "AAPL", "side": "buy", "type": "limit", "limit_price": "190.5"}


def _adapter(handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AlpacaAdapter("K", "S", mode="demo", http=http)


def _json(status, body):
    return httpx.Response(status, json=body)


def uruchom(coro):
    return asyncio.run(coro)


def _odmowa(status, body) -> OdmowaGieldy:
    """Składa zlecenie, którego giełda nie przyjmuje, i zwraca wyjątek.

    Pierwsze zapytanie `zloz_zlecenie` to `stan_zlecenia` (sprawdź, potem
    składaj) — musi wrócić 404, żeby dojść do POST-a.
    """
    kolejne = {"n": 0}

    def handler(r: httpx.Request):
        kolejne["n"] += 1
        if kolejne["n"] == 1:
            return _json(404, {"message": "order not found"})
        return _json(status, body)

    with pytest.raises(ExchangeError) as e:
        uruchom(_adapter(handler).zloz_zlecenie(
            symbol="AAPL", strona="sell", ilosc=D("10.5"),
            cena_limitu=D("190.50"), id_wlasne="dec-77"))
    return e.value


# =========================================== 422: zlecenie łamie regułę giełdy

def test_422_to_ODMOWA_o_rodzaju_parametr_a_nie_brak_odpowiedzi():
    """KANAREK. `qty must be integer` to odpowiedź, nie jej brak.

    Bez `OdmowaGieldy` wiersz w `exec.order` dostawał `status='unknown'`
    i etap `odpowiedz_nie_dotarla`, a `gielda_exec` nie miał jak odróżnić
    tego od przekroczenia czasu — czyli od jedynego przypadku, w którym
    zlecenie NAPRAWDĘ mogło wejść.
    """
    e = _odmowa(422, {"code": 40010001, "message": "qty must be integer"})
    assert isinstance(e, OdmowaGieldy), (
        f"422 wróciło jako {type(e).__name__}; `gielda_exec.zloz_plan` zapisze "
        f"to jako `unknown` i będzie wysyłać tę samą niepoprawną ilość co świecę")
    assert e.rodzaj == "parametr", (
        "422 to reguła giełdy (krok, precyzja, minimum) — powtórzenie tej samej "
        "liczby da to samo, więc `rodzaj='saldo'` byłby tu fałszem")
    assert not e.brak_salda
    assert not e.wymaga_czlowieka, (
        "odrzucone zlecenie nie jest awarią klucza ani konta — człowieka budzi "
        "POWTARZALNOŚĆ odmowy i to zadanie czujki egzekucji, nie adaptera")
    assert not e.retryable


def test_422_zachowuje_kod_HTTP_i_kod_Alpaki_w_tresci():
    """`zloz_zlecenie` i `anuluj_zlecenie` rozpoznają 422 po `e.code`.

    Podniesienie innej klasy wyjątku nie może zgubić tej informacji, bo
    obsługa duplikatu `client_order_id` (idempotencja) i „zlecenie już
    wypełnione" stoją właśnie na `e.code == "422"`.
    """
    e = _odmowa(422, {"code": 40010001, "message": "qty must be integer"})
    assert e.code == "422"
    assert "40010001" in str(e)


def test_duplikat_client_order_id_NADAL_konczy_sie_juz_istnialo():
    """KANAREK ODWROTNY: idempotencja nie może paść ofiarą tej zmiany.

    Alpaca odrzuca duplikat `client_order_id` przez 422, a `zloz_zlecenie`
    zamienia to na `juz_istnialo=True`. Gdyby `OdmowaGieldy` przerwała tę
    gałąź, wyścig dwóch procesów kończyłby się odrzuceniem zlecenia, które
    NA GIEŁDZIE JEST — czyli pozycją bez wiersza w naszym dzienniku.
    """
    kolejne = {"n": 0}

    def handler(r: httpx.Request):
        kolejne["n"] += 1
        if kolejne["n"] == 1:
            return _json(404, {"message": "order not found"})
        if kolejne["n"] == 2:
            return _json(422, {"code": 40010001,
                               "message": "client_order_id must be unique"})
        return _json(200, ZLECENIE)

    z = uruchom(_adapter(handler).zloz_zlecenie(
        symbol="AAPL", strona="buy", ilosc=D("10"), cena_limitu=D("190.50"),
        id_wlasne="dec-77"))
    assert z.juz_istnialo is True
    assert z.id_gieldy == ZLECENIE["id"]


def test_anulowanie_wypelnionego_zlecenia_NADAL_oddaje_False():
    """Druga gałąź stojąca na `e.code == "422"` — `anuluj_zlecenie`."""
    kolejne = {"n": 0}

    def handler(r: httpx.Request):
        kolejne["n"] += 1
        if kolejne["n"] == 1:
            return _json(200, ZLECENIE)
        return _json(422, {"message": "order is not cancelable"})

    assert uruchom(_adapter(handler).anuluj_zlecenie(
        symbol="AAPL", id_wlasne="dec-77")) is False


# ============================================ 403 brak środków: rodzaj 'saldo'

def test_brak_srodkow_to_ODMOWA_o_rodzaju_SALDO_i_wlacza_adaptacje():
    """To `rodzaj='saldo'` uruchamia jedyną adaptację, jaką system ma.

    `odmowa_gieldy` przy `blad.brak_salda` pyta giełdę o stan rachunku
    i składa ilość POTWIERDZONĄ, zamiast powtarzać tę samą liczbę co świecę.
    Odtworzona reguła Bybita: kody 170131 → `OdmowaGieldy(rodzaj='saldo')`.
    """
    e = _odmowa(403, {"code": 40310000, "message": "insufficient buying power"})
    assert isinstance(e, OdmowaGieldy), (
        f"brak środków wrócił jako {type(e).__name__} — czyli jako "
        f"„nie wiemy, czy weszło”. Wiemy, że nie weszło, i wiemy dlaczego")
    assert e.brak_salda and e.rodzaj == "saldo"
    assert not isinstance(e, KontoZamrozone)
    assert not e.wymaga_czlowieka


# =========================== 403 niesortowalny papier: JEDEN symbol, NIE konto

@pytest.mark.parametrize("komunikat", [
    "position is not shortable",
    "asset is not shortable",
    "AAPL is not easy to borrow (HTB)",
])
def test_odmowa_shorta_JEDNEGO_symbolu_NIE_jest_blokada_konta(komunikat):
    """KANAREK. Odmowa per instrument nie może zgłaszać się jako blokada konta.

    `wymaga_czlowieka=True` znaczy w tym systemie coś konkretnego: „żaden
    restart nie pomoże, IDŹ DO PANELU GIEŁDY (compliance / wsparcie)".
    Dla papieru, którego nie da się pożyczyć, jest to wysłanie właściciela
    w złe miejsce — konto działa, klucz działa, handluje się dalej wszystkim
    innym.
    """
    e = _odmowa(403, {"message": komunikat})
    assert not isinstance(e, KontoZamrozone), (
        f"{komunikat!r} zgłoszone jako blokada CAŁEGO konta")
    assert isinstance(e, OdmowaGieldy)
    assert e.rodzaj == "parametr", (
        "powtórzenie tego zlecenia da to samo — zmienić musi się strona albo "
        "instrument, nie ilość, więc to nie jest odmowa za saldo")
    assert not e.wymaga_czlowieka


def test_KANAREK_prawdziwa_blokada_konta_NADAL_jest_KontoZamrozone():
    """Kanarek odwrotny, bez którego poprawka mogłaby zdjąć cały `KontoZamrozone`.

    Przy blokadzie konta ŻADEN restart nie pomoże i otwarte pozycje zostają,
    a zlecenia WYJŚCIA są odrzucane tak samo jak wejścia. To jest jedyny stan
    403, w którym człowiek naprawdę musi pójść do panelu giełdy.
    """
    e = _odmowa(403, {"message": "account is not authorized to trade"})
    assert isinstance(e, KontoZamrozone)
    assert e.wymaga_czlowieka
    assert not isinstance(e, OdmowaGieldy)


# ====================================== czego ta zmiana NIE MOŻE ruszyć

def test_5xx_i_429_NADAL_sa_ponawialne_a_nie_odmowa(monkeypatch):
    """Brak odpowiedzi to nadal brak odpowiedzi.

    Gdyby 5xx zaczęło wracać jako `OdmowaGieldy`, `gielda_exec` zapisałby
    `rejected` dla zlecenia, które MOGŁO wejść — czyli dokładnie odwrotność
    naprawianego błędu, i o jedno piętro groźniejsza.
    """
    async def bez_snu(_):
        pass
    monkeypatch.setattr(asyncio, "sleep", bez_snu)

    for status in (429, 500, 502, 503):
        def handler(r, s=status):
            return _json(s, {"message": "kaboom"})

        with pytest.raises(ExchangeError) as e:
            uruchom(_adapter(handler).fetch_balances())
        assert not isinstance(e.value, OdmowaGieldy), (
            f"HTTP {status} zgłoszone jako ODMOWA — a przy nim zlecenie mogło wejść")
        assert e.value.retryable


def test_alpaca_zna_OdmowaGieldy_tyle_samo_rodzajow_co_bybit():
    """Rodzaje odmowy są dwa i Alpaca zna OBA — jak Bybit.

    Test na samym module, bo to jest pierwotne ustalenie: `grep -c OdmowaGieldy`
    dawało dla `alpaca.py` ZERO, a dla `bybit.py` trzy. Liczba odmian ma
    znaczenie: adapter znający tylko `parametr` nie uruchomi nigdy adaptacji za
    saldo, a znający tylko `saldo` będzie pytał o rachunek przy złym kroku ceny.
    """
    from services.exchanges import alpaca

    zrodlo = (alpaca.__file__).replace(".pyc", ".py")
    with open(zrodlo, encoding="utf-8") as f:
        tresc = f.read()
    assert "rodzaj=\"saldo\"" in tresc or "rodzaj='saldo'" in tresc
    assert "rodzaj=\"parametr\"" in tresc or "rodzaj='parametr'" in tresc
