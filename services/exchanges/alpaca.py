"""Adapter Alpaca Markets - gielda tradycyjna (akcje USA), konto PAPER.

DLACZEGO ALPACA, A NIE INTERACTIVE BROKERS

README mowi o IBKR, research (renacode-infra: docs/projekty/trader/00-research.md, 2.3) rekomendowal Alpace
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
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from services.exchanges.base import (
    AuthError, Balance, BookLevel, Candle, ConnectionCheck, ExchangeError,
    KontoZamrozone, OdmowaGieldy, OrderBook, StanZlecenia, Wypelnienie,
    ZlozoneZlecenie,
)

HOSTS = {
    "demo": "https://paper-api.alpaca.markets",
    "live": "https://api.alpaca.markets",
}
DATA_HOST = "https://data.alpaca.markets"

# POTWIERDZENIE, ZE ZAPISANE SWIECE SA SKORYGOWANE O PODZIALY AKCJI.
#
# Klucz w `ref.instrument.meta`, wartosc `true` (jsonb). Nazwa stoi TUTAJ, a nie
# w trenerze, bo to ten modul decyduje o `adjustment` nizej - a wiec on jest
# zrodlem stanu, o ktory znacznik pyta. Dwa literaly w dwoch plikach rozjechalyby
# sie cicho: bramka pytalaby o klucz, ktorego nikt nie ustawia, i milczalaby
# ZAWSZE albo krzyczala ZAWSZE, w obu przypadkach niezaleznie od danych.
#
# ZNACZNIK STAWIA DOKLADNIE JEDNO MIEJSCE: `services/ingest/korekta_podzialow.py
# --znacznik`, i tylko instrumentowi, ktoremu PRZEPISAL caly szereg (slad
# w `market.korekta_swiec`, migracja 0065) oraz ktorego zapisane cechy zgadzaja
# sie z tymi swiecami. Zadna migracja go nie stawia i stawiac nie moze: migracja
# nie dotyka tresci swiec, wiec znacznik z migracji bylby klamstwem od pierwszej
# sekundy. Znacznik wpisany "zeby ruszylo" zamienilby nieznany stan danych
# w potwierdzony i to jest jedyny sposob, w jaki bramka w `train.py` moze zawiesc.
#
# ZNACZNIK ZDEJMUJE `services/ingest/backfill.py`, gdy zauwazy, ze dostawca
# oddaje przeskalowane ceny dla swiec, ktore juz mamy - czyli po kolejnym
# podziale. Bez tego znacznik raz postawiony mowilby `true` na zawsze, takze
# o szeregu, ktory nowy podzial znowu zatrul.
ZNACZNIK_KOREKTY_PODZIALOW = "swiece_skorygowane_o_podzialy"

# JAKA KOREKTA IDZIE DO ALPACA DATA.
#
# Alpaca przyjmuje `adjustment` w czterech wartosciach: `raw` (BRAK korekty),
# `split`, `dividend` i `all`. DOMYSLNA PO STRONIE DOSTAWCY JEST `raw` - dlatego
# ten parametr byl tu zawsze wpisany JAWNIE, do 2026-09-27 wlasnie jako `raw`,
# i dlatego zapisana historia jest nieskorygowana. Nie opieramy tego na
# dokumentacji: w `market.candle` widac trzy skoki x10 (zmierzone nizej).
#
# ZGODA WLASCICIELA NA ZMIANE: 2026-09-27, wprost.
#
# DLACZEGO `split`, A NIE `all`. TO ROZSTRZYGNIECIE O TYM, CO MIERZYMY.
#
#   1. `all` doklada do ceny takze dywidendy, a wynik silnika liczy sie ze ZMIAN
#      CENY: `train.py` sumuje zwroty ze swiec i odejmuje koszt rundy, a zaden
#      rachunek silnika nie przyjmuje dywidendy. Szereg skorygowany o dywidendy
#      pokazywalby wiec zwrot, ktorego pozycja NIE OSIAGA - dla dlugiej zawyzony
#      o stope dywidendy, dla krotkiej zanizony o te sama wielkosc, bo krotka
#      dywidende PLACI. Taki blad nie objawia sie awaria: Sharpe wychodzi
#      wyzszy i nic nie pada.
#   2. Alpaca koryguje wzgledem DZIS, wiec wspolczynnik zmienia sie przy KAZDYM
#      zdarzeniu, ktore obejmuje korekta. Dla `split` to trzy zdarzenia na 2,5
#      roku i 20 instrumentow (NVDA 2024-06-10, AVGO 2024-07-15, NFLX
#      2025-11-17). Dla `all` to KAZDA dywidenda, czyli kilkadziesiat zdarzen
#      w roku - a `history-equity` chodzi z `--days 3`, czyli utrzymuje sam
#      ogon szeregu. Przy `all` ogon bylby wiecznie skorygowany inaczej niz
#      korpus, a nieciaglosc siedzialaby w srodku okna treningowego i nic by
#      o niej nie mowilo.
#
# CZEGO TA STALA NIE ZALATWIA. Przelaczenie jej dotyczy WYLACZNIE swiec
# pobieranych OD TERAZ. Historia zapisana wczesniej zostaje nieskorygowana,
# dopoki nie przepisze jej `services/ingest/korekta_podzialow.py` - i dopiero
# ten skrypt stawia `ZNACZNIK_KOREKTY_PODZIALOW`, zdejmujac bramke z `train.py`.
KOREKTA_SWIEC = "split"

# Dopuszczalne wartosci `adjustment`. Whitelista trzyma rozstrzygniecie u nas:
# jak Alpaca zachowuje sie przy wartosci spoza tej listy, NIE JEST ZMIERZONE,
# a odpowiedz "200 OK z cenami nieskorygowanymi" byla by cicha i nierozpoznawalna
# od poprawnej.
KOREKTY_DOPUSZCZALNE = ("raw", "split", "dividend", "all")

# Nazwy taktow po stronie Alpaca Data. Klucze to nasze `timeframe`
# z `market.candle` - te same, ktorych uzywa adapter Bybita.
TIMEFRAMES = {
    "1m": "1Min", "5m": "5Min", "15m": "15Min", "1h": "1Hour",
    "4h": "4Hour", "1d": "1Day",
}

# Jak daleko wstecz siega zapytanie o NAJNOWSZE swiece (bez `start`). Cztery doby
# przykrywaja weekend ze swietem w poniedzialek: pierwszy cykl wtorku nadal widzi
# ogon piatkowej sesji. O liczbie swiec decyduje `limit` i `sort=desc`, wiec
# szersze okno nie kosztuje ani jednego zapytania wiecej.
OKNO_NAJNOWSZYCH = timedelta(days=4)

# LIMIT ZAPYTAN JEST MINUTOWY I WSPOLNY DLA KLUCZA (2026-10-03, audyt 3 T1).
#
# Jednym kluczem paper pytaja rownolegle `market-equity` (co minute swieca
# i oferta na kazdy z ~21 symboli), `history-equity`, silnik, `kapital-equity`
# i jednorazowe narzedzia (korekta podzialow padla na tym dwa razy). Limit
# procesowy nic tu nie da, bo pozostale procesy go nie widza - wspolna jest
# tylko odpowiedz Alpaki: naglowki `X-RateLimit-Remaining` i `X-RateLimit-Reset`
# (sekundy epoki). Dlatego:
#   * gdy po udanym zapytaniu zostaje `ZAPAS_LIMITU` lub mniej, czekamy do
#     odnowienia okna, zamiast zuzyc reszte i wywrocic zapytanie innego procesu;
#   * 429 czeka do `X-RateLimit-Reset` (albo `Retry-After`), a nie 0,5-1-2 s,
#     ktore przy limicie MINUTOWYM wyczerpuja trzy proby w 3,5 s.
# Oba czekania sa przyciete do `CZEKANIE_LIMITU_MAX_S`: zegar Alpaki o godzine
# do przodu nie moze zawiesic workera na godzine.
ZAPAS_LIMITU = 10
CZEKANIE_LIMITU_MAX_S = 60.0


def _do_odnowienia_limitu(r: httpx.Response) -> float | None:
    """Ile sekund do odnowienia okna limitu wg naglowkow Alpaki. None = nie wiadomo."""
    reset = r.headers.get("X-RateLimit-Reset")
    if reset:
        try:
            return max(0.0, min(float(reset) - time.time(), CZEKANIE_LIMITU_MAX_S))
        except ValueError:
            pass
    ponow = r.headers.get("Retry-After")
    if ponow:
        try:
            return max(0.0, min(float(ponow), CZEKANIE_LIMITU_MAX_S))
        except ValueError:
            pass
    return None

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


# Odmowa DOTYCZACA JEDNEGO SYMBOLU, ktorej nie wolno zglosic jako blokady konta.
#
# Alpaca odpowiada `403 "position is not shortable"` albo
# `"asset is not shortable"`, gdy tego JEDNEGO papieru nie da sie sprzedac
# krotko (brak pozyczki, lista trudno dostepnych, ETF bez shortu). Do
# 2026-09-26 wpadalo to w `KontoZamrozone` z `wymaga_czlowieka=True`
# i komunikatem "KONTO ZABLOKOWANE PO STRONIE GIELDY (compliance / wsparcie)",
# czyli odmowa dla jednego symbolu byla zglaszana jako blokada CALEGO konta.
NIESHORTOWALNE = ("not shortable", "not easy to borrow", "htb")


def _blad_z_odpowiedzi(r: httpx.Response) -> ExchangeError:
    """HTTP Alpaki -> wyjatek, ktory MOWI, CO ZROBIC (ta sama rodzina co Bybit).

    ODMOWA ZLOZENIA JEST ODPOWIEDZIA, NIE JEJ BRAKIEM - i to jest cala tresc
    poprawki z 2026-09-26.

    Do tego dnia ta funkcja NIE PODNOSILA `OdmowaGieldy` ANI RAZU (`bybit.py`
    podnosi ja trzy razy), wiec kazde 4xx poza 401/403-zamrozeniem wracalo jako
    `ExchangeError(retryable=False)`. W `gielda_exec.zloz_plan` wpada to
    w `except ExchangeError` i zapisuje `status='unknown'` z etapem
    `odpowiedz_nie_dotarla`. Dla zerwanego polaczenia to jedyna uczciwa
    odpowiedz; dla `HTTP 422 "qty must be integer"` jest to zapisanie NIEPRAWDY
    o stanie, ktory znamy dokladnie.

    CO Z TEGO WYNIKALO NAPRAWDE - i czego NIE, zeby nie uzasadniac tej poprawki
    rzeczami, ktore sie nie potwierdzaja:

      * plan NIE zostawal `pending`. `rozstrzygnij_plan` (gielda_exec.py:585-590)
        ustawia `expired`, gdy gielda nie zna zlecenia.
      * czujka NIE milczala. `posprzataj_zlecenia` zapisuje `cancelled`
        z `venue_order_id IS NULL`, a `dozor.py:1694-1701` liczy `bez_kontaktu`
        bez filtra po silniku i zapala alarm od pierwszej sztuki.

    REALNA SZKODA TO BRAK ADAPTACJI. `odmowa_gieldy` (gielda_exec.py:423+) pyta
    gielde o saldo i przeplanowuje z ilościa, ktora gielda potwierdza. Sciezka
    `ExchangeError` tego nie robi, a odmowa za parametr jest deterministyczna -
    wiec system wysylalby TE SAMA niepoprawna ilosc co swiece, bez konca.
    Druga szkoda: alarm `bez_kontaktu` mowi "gielda ich nie widziala", co dla
    422 jest falszem - widziala i odmowila. Wlasciciel dostaje codziennie alarm
    o zlej przyczynie, czyli dokladnie ten ksztalt, ktory uczy ignorowac cala
    kategorie.
    """
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
    niski = msg.lower()
    if r.status_code in (401,):
        return AuthError(f"Alpaca odrzucila klucz ({r.status_code}): {msg}", code=kod)
    if r.status_code == 403:
        # 403 to u Alpaki trzy rozne rzeczy i kazda wymaga innej reakcji.
        #
        # BRAK SRODKOW -> `OdmowaGieldy(rodzaj='saldo')`, dokladnie jak kody
        # 170131 u Bybita. Do 2026-09-26 bylo to golym `ExchangeError`, czyli
        # "nie wiemy, czy weszlo" - a wiemy, ze nie weszlo, i wiemy dlaczego.
        # To `rodzaj='saldo'` wlacza w `odmowa_gieldy` zapytanie o stan
        # rachunku i zlozenie ilosci POTWIERDZONEJ przez gielde.
        if "buying power" in niski or "insufficient" in niski:
            return OdmowaGieldy(
                f"Alpaca ODMOWILA: za malo srodkow ({msg}). Zlecenie NIE WESZLO "
                f"i powtorzenie tej samej ilosci da to samo. Nasze ksiegi podaja "
                f"wiecej, niz gielda potwierdza - przy WYJSCIU znaczy to, ze "
                f"pozycji nie da sie zamknac w calosci, dopoki roznica nie "
                f"zostanie wyjasniona.",
                code=kod, rodzaj="saldo")
        # NIESHORTOWALNY PAPIER -> odmowa PER SYMBOL, nigdy `KontoZamrozone`.
        # `rodzaj='parametr'`, bo powtorzenie tego samego zlecenia da to samo:
        # zmienic musi sie strona albo instrument, nie ilosc.
        if any(f in niski for f in NIESHORTOWALNE):
            return OdmowaGieldy(
                f"Alpaca ODMOWILA KROTKIEJ SPRZEDAZY TEGO PAPIERU ({msg}). "
                f"Dotyczy JEDNEGO instrumentu, nie konta: klucz dziala, konto "
                f"handluje, tego papieru nie da sie pozyczyc. Zlecenie NIE "
                f"WESZLO; powtorzenie bez zmiany strony albo instrumentu da to "
                f"samo. NIE IDZ DO WSPARCIA GIELDY.",
                code=kod, rodzaj="parametr")
        return KontoZamrozone(f"Alpaca: konto zablokowane lub ograniczone: {msg}", code=kod)
    if r.status_code == 429:
        return ExchangeError(f"Alpaca: limit zapytan ({msg})", code=kod, retryable=True)
    if r.status_code >= 500:
        return ExchangeError(f"Alpaca {r.status_code}: {msg}", code=kod, retryable=True)
    if r.status_code == 422:
        # 422 to u Alpaki odmowa WALIDACJI: krok ilosci, precyzja ceny, minimum,
        # `qty must be integer`, duplikat `client_order_id`. W kazdym z tych
        # przypadkow gielda ODPOWIEDZIALA i zlecenia NIE MA.
        #
        # DUPLIKAT `client_order_id` JEST TU NADAL WIDOCZNY. `zloz_zlecenie`
        # rozpoznaje go po `e.code == "422"` i tresci, a `OdmowaGieldy` niesie
        # `code` bez zmian i jest podklasa `ExchangeError`, wiec ta galaz
        # (i `anuluj_zlecenie`, ktore lapie samo `422`) dziala jak dotad.
        return OdmowaGieldy(
            f"Alpaca ODMOWILA: zlecenie lamie jej regule ({msg}). Zlecenie NIE "
            f"WESZLO; ponowienie bez zmiany liczb da to samo.",
            code=kod, rodzaj="parametr")
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

        Ponawiamy 429 (po odnowieniu okna limitu) i 5xx z rosnacym odstepem.
        4xx poza tymi dwoma nie ponawiamy nigdy: bledne zlecenie zlozone trzy
        razy nie staje sie poprawne, a duplikat `client_order_id` (422) jest sygnalem, nie awaria.
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
                    await self._oszczedzaj_limit(r)
                    return r
                blad = _blad_z_odpowiedzi(r)
                if not blad.retryable:
                    raise blad
                ostatni = blad
                if r.status_code == 429:
                    # Limit minutowy: czekamy do odnowienia okna (patrz
                    # `ZAPAS_LIMITU`). Bez naglowka - pelne okno, nie 0,5 s.
                    czekaj = _do_odnowienia_limitu(r)
                    await asyncio.sleep(CZEKANIE_LIMITU_MAX_S if czekaj is None else czekaj)
                    continue
            await asyncio.sleep(0.5 * (2 ** proba))
        assert ostatni is not None
        raise ostatni

    async def _oszczedzaj_limit(self, r: httpx.Response) -> None:
        """Czeka do odnowienia okna, gdy klucz ma `ZAPAS_LIMITU` zapytan lub mniej."""
        try:
            zostalo = int(r.headers.get("X-RateLimit-Remaining", ""))
        except ValueError:
            return
        if zostalo > ZAPAS_LIMITU:
            return
        czekaj = _do_odnowienia_limitu(r)
        if czekaj:
            await asyncio.sleep(czekaj)

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
                            end: datetime | None = None, *,
                            korekta: str | None = None) -> list[Candle]:
        """Swiece z Alpaca Data. Rosnaco po czasie, jak u Bybita.

        Alpaca stronicuje przez `next_page_token` i oddaje najwyzej 10 000
        swiec na strone. `limit` jest u nas gorna granica LACZNA - paginacja
        zatrzymuje sie, gdy ja osiagnie, albo gdy token sie skonczy.

        BEZ `start` ODDAJEMY NAJNOWSZE `limit` SWIEC, jak Bybit (2026-10-03, audyt 3 T1).

        Do tej daty stalo tu `sort=asc` bez `start`, a docstring obiecywal, ze
        "cofamy sie o rozsadne okno" - kod tego nie robil. Alpaca bierze wtedy
        `start` = poczatek biezacego dnia i przy `sort=asc` oddaje PIERWSZE
        `limit` swiec dnia. `market_worker` (equity) prosi o 200 swiec 1m co
        minute, wiec zapisywal 13:30-16:50 UTC i nic wiecej: kazdy z 21 symboli
        mial dokladnie 200 swiec dziennie, ostatnia miedzy 16:37 a 19:09 UTC, przy
        sesji do 20:00. Silnik equity tracilby swieza cene na ostatnie 1-3,5 h
        kazdej sesji, a puls meldowal `bledy 0`.

        Teraz bez `start` pytamy `sort=desc` z oknem `OKNO_NAJNOWSZYCH` wstecz
        (przez weekend i swieto, zeby poniedzialkowy cykl widzial ogon piatku)
        i odwracamy wynik. Z `start` zachowanie jest jak dotad: rosnaco od
        `start`, bo tak czytaja backfill i korekta podzialow.
        """
        tf = TIMEFRAMES.get(timeframe)
        if not tf:
            raise ValueError(f"nieobslugiwany timeframe: {timeframe}")
        # KOREKTA O PODZIALY AKCJI - `KOREKTA_SWIEC` na gorze modulu, tam powod
        # wyboru `split` zamiast `all` i tam zgoda wlasciciela.
        #
        # CO ZALEZY OD TEJ JEDNEJ WARTOSCI. Podzial 10:1 w szeregu
        # NIESKORYGOWANYM wyglada jak spadek o 90%, bo cena z dnia na dzien
        # dzieli sie przez dziesiec, a my zapisujemy oba stany obok siebie.
        # Zmierzone w `market.candle` 2026-09-27 (zwrot ze swiecy na swieca,
        # timeframe 1h, uniwersum decyzyjne equity):
        #
        #     NVDA  2024-06-10 12:00Z   1208,42 -> 120,50   x0,0997
        #     AVGO  2024-07-15 13:00Z   1698,62 -> 169,99   x0,1001
        #     NFLX  2025-11-17 14:00Z   1112,11 -> 110,59   x0,0994
        #
        # To NIE sa ruchy rynku. `ret_long` na horyzoncie 1h siega 168 swiec,
        # a rozgrzewka 200, wiec jedno zdarzenie zatruwa okolo 670 swiec po
        # sobie - i wchodzi do treningu jako obserwacja kilkunastosigmowa,
        # ktora zdominuje kazda kalibracje nieodporna na wartosci skrajne.
        #
        # `korekta` PRZEKAZUJE SIE JAWNIE TYLKO DO POMIARU. Sluzy do zestawienia
        # "ile wierszy rozni sie miedzy `raw` i `split`" w
        # `services/ingest/korekta_podzialow.py --sucho`; sciezka ZAPISUJACA
        # swiece nie podaje go wcale i dostaje `KOREKTA_SWIEC`. Bez tego
        # zestawienia nie da sie sprawdzic, czy parametr w ogole dochodzi do
        # dostawcy - a parametr, ktory nie dochodzi, milczy.
        wybrana = KOREKTA_SWIEC if korekta is None else korekta
        if wybrana not in KOREKTY_DOPUSZCZALNE:
            raise ValueError(
                f"nieznana korekta swiec: {wybrana!r} "
                f"(dopuszczalne: {', '.join(KOREKTY_DOPUSZCZALNE)})")
        najnowsze = start is None
        if najnowsze:
            start = datetime.now(timezone.utc) - OKNO_NAJNOWSZYCH
        params: dict = {"symbols": symbol, "timeframe": tf, "feed": self._feed,
                        "adjustment": wybrana, "limit": min(limit, 10_000),
                        "sort": "desc" if najnowsze else "asc"}
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
        # Przy `desc` pierwsze `limit` to NAJNOWSZE; sortowanie po czasie oddaje
        # je rosnaco, jak obiecuje kontrakt, niezaleznie od kierunku zapytania.
        return sorted(out[:limit], key=lambda c: c.event_time)

    async def fetch_order_book(self, symbol: str, depth: int = 1, *,
                               ksiega_egzekucji: bool = True) -> OrderBook:
        """Ksiega JEDNOPOZIOMOWA z najlepszej oferty. Alpaca nie wystawia
        glebokosci w REST; `depth` > 1 nie da wiecej i jest przyjmowany dla
        zgodnosci sygnatury.

        `ksiega_egzekucji` NIE ZMIENIA TU NICZEGO i to jest stwierdzenie o Alpace,
        nie przeoczenie: u Bybita rozdzielil sie PODMIOT (globalny i unijny), wiec
        istnieja dwie rozne ksiegi tego samego symbolu i trzeba wybrac. Alpaca ma
        jedno zrodlo danych i jedno miejsce egzekucji, wiec wybor nie istnieje -
        a jedyne zastrzezenie do tej ksiegi (feed IEX to ulamek rynku) dotyczy
        obu odpowiedzi na to pytanie jednakowo. Patrz naglowek modulu.
        """
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
                            cena_limitu: Decimal | None, id_wlasne: str,
                            kategoria: str = "spot",
                            time_in_force: str = "day",
                            rynkowe: bool = False,
                            istnienie_sprawdzone: bool = False) -> ZlozoneZlecenie:
        """Zlecenie z limitem albo RYNKOWE, IDEMPOTENTNIE po `client_order_id`.

        SPRAWDZ, POTEM SKLADAJ - jak u Bybita. Alpaca sama odrzuca duplikat
        `client_order_id` (422), wiec nawet wyscig dwoch procesow nie zlozy
        dwoch zlecen; my zamieniamy te odmowe na `juz_istnialo=True`.
        `time_in_force='day'` to swiadomy domysl: plan jest na jedna swiece,
        a zlecenie, ktore przezyloby noc, wypelniloby sie po cenie, ktorej
        zadna decyzja juz nie popiera.

        SCIEZKA RYNKOWA JEST OSOBNA I NIE JEST WSPOLNA Z BYBITEM. Oba adaptery
        maja wlasne `zloz_zlecenie` i wlasne cialo zapytania (inne API), wiec
        dolozenie jej trzeba bylo zrobic DWA RAZY - to nie jest duplikacja do
        usuniecia, to dwa rozne protokoly. Wspolny jest wylacznie KONTRAKT:
        `rynkowe=True` wymaga `cena_limitu is None` i normalny handel go nie
        uzywa (domysl `False`).

        CZEGO TA SCIEZKA NIE ZALATWIA NA ALPACE: rynek akcji ma godziny.
        Zlecenie rynkowe zlozone po zamknieciu jest ODRZUCANE (`market` nie
        przyjmuje `extended_hours`), wiec likwidacja poza sesja zapisze odmowe,
        a pozycja zostanie otwarta do otwarcia rynku. Jest to wlasnosc rynku,
        nie tego kodu, i dlatego jest tu nazwana zamiast obejscia przez limit.
        """
        if rynkowe and cena_limitu is not None:
            raise ValueError(
                f"zlecenie RYNKOWE nie ma ceny limitu, a podano {cena_limitu} - "
                f"zapisanie jej jako `limit_price` byloby nieprawda o tym, co "
                f"gielda dostala")
        if not rynkowe and cena_limitu is None:
            raise ValueError("zlecenie z limitem bez ceny limitu")
        # `istnienie_sprawdzone=True`: wolajacy zapytal o stan TUZ PRZED odczytem
        # arkusza (`gielda_exec.zloz_plan`), zeby miedzy arkuszem a POST-em nie
        # stalo zadne zapytanie. Drugie sprawdzenie tutaj wstawialoby z powrotem
        # te sekundy, przez ktore cena PostOnly przestaje byc pasywna.
        # STRONA BEZ ZGADYWANIA (audyt 2026-10-09, N-4), jak `bybit._strona_bybita`:
        # `else "sell"` zamienialo literowke albo "Long" w zlecenie w PRZECIWNA
        # strone - i to zanim pojdzie jakiekolwiek zapytanie, ktorego nie da sie cofnac.
        side = strona.strip().lower()
        if side not in ("buy", "sell"):
            raise ValueError(f"nieznana strona zlecenia: {strona!r}")
        if not istnienie_sprawdzone:
            istnieje = await self.stan_zlecenia(id_wlasne)
            if istnieje is not None:
                return ZlozoneZlecenie(id_wlasne=id_wlasne, id_gieldy=istnieje.id_gieldy,
                                       status=istnieje.status, juz_istnialo=True)
        body = {"symbol": symbol, "qty": str(ilosc),
                "side": side,
                "type": "market" if rynkowe else "limit",
                "time_in_force": time_in_force,
                "client_order_id": id_wlasne}
        if not rynkowe:
            body["limit_price"] = str(cena_limitu)
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
