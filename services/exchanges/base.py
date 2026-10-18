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


class ExchangeError(Exception):
    """Blad po stronie gieldy albo transportu."""

    def __init__(self, message: str, *, code: str | None = None, retryable: bool = False,
                 wymaga_czlowieka: bool = False):
        super().__init__(message)
        self.code = code
        # Rozroznienie jest istotne: bledu walidacji zlecenia nie ma sensu ponawiac,
        # a chwilowego 5xx albo rate limitu - owszem.
        self.retryable = retryable
        # TRZECI STAN, obok "ponow" i "odpusc ten jeden odczyt": awaria, ktorej
        # zaden restart nie naprawi, bo zmiana musi zajsc POZA procesem - w panelu
        # gieldy albo w konfiguracji klastra.
        #
        # Bez tego rozroznienia "gielda nas zablokowala" wyglada w alarmie
        # dokladnie tak samo jak "padla baza": proces konczy sie po pieciu
        # cyklach w obu przypadkach, a to sa dwie rozne noce i dwa rozne miejsca,
        # do ktorych trzeba pojsc. Gorzej: przy martwym kluczu czujka zywotnosci
        # milczy, bo silnik zyje i bije - tylko gielda go nie slucha, wiec nie
        # da sie ZAMKNAC otwartych pozycji.
        self.wymaga_czlowieka = wymaga_czlowieka


class AuthError(ExchangeError):
    """Klucz odrzucony, wygasly albo bez wymaganych uprawnien."""

    def __init__(self, message: str, *, code: str | None = None, retryable: bool = False,
                 wymaga_czlowieka: bool = True):
        # Domyslnie `wymaga_czlowieka=True`: zadne ponowienie nie naprawi klucza,
        # ktory gielda odrzuca. Ponawianie tylko opoznia moment, w ktorym ktos
        # zobaczy prawdziwa przyczyne, i zjada limit zapytan.
        super().__init__(message, code=code, retryable=retryable,
                         wymaga_czlowieka=wymaga_czlowieka)


class BladIpKlucza(AuthError):
    """Zapytanie przyszlo z adresu spoza listy IP przypietej do klucza (10010).

    Osobna klasa, bo REAKCJA jest inna niz przy kazdym innym bledzie klucza:
    klucz jest poprawny, sekret jest poprawny, wygenerowanie nowego niczego nie
    zmieni. Zmienil sie adres wyjsciowy klastra albo lista IP w panelu gieldy.

    Sklejenie tego z ogolnym `AuthError` konczy sie noca spedzona na sprawdzaniu
    sekretu w Vaulcie, podczas gdy pozycje stoja otwarte, a wyjscia z nich sa
    odrzucane dokladnie tak samo jak wejscia.
    """


class OdmowaGieldy(ExchangeError):
    """Gielda ODPOWIEDZIALA i odmowila przyjecia zlecenia. Zlecenie NIE WESZLO.

    DLACZEGO TO NIE MOZE BYC ZWYKLY `ExchangeError`

    Wywolujacy ma dwie zupelnie rozne reakcje na dwa zdarzenia, ktore w kodzie
    wygladaly dotad identycznie:

      * odpowiedz NIE DOTARLA (timeout, zerwane polaczenie, 5xx) - zlecenie
        moglo wejsc, wiec zapisujemy `status='unknown'` i pytamy gielde
        w nastepnym cyklu po naszym identyfikatorze,
      * gielda ODMOWILA strukturalnym `retCode` - zlecenia NIE MA i nie bedzie,
        wiec zapisujemy `status='rejected'` z prawdziwym powodem.

    Sklejenie ich kosztowalo 19 godzin zamrozonej pozycji (SOLUSDC, 2026-09-23
    17:18 -> 2026-09-24 05:15). Bybit odpowiadal `Insufficient balance.
    (kod 170131)` - odpowiedz jednoznaczna - a kod zapisywal `unknown`
    z etapem `odpowiedz_nie_dotarla`, czyli "nie wiemy, czy weszlo". Stan
    nieznany nie moze niczego przerwac: kazda swieca planowala wyjscie od nowa,
    cztery zlecenia (49, 51, 52, 53) odbily sie identycznie, a zaden licznik
    odrzucen sie nie ruszyl, bo `status='rejected'` nie zostal zapisany ANI RAZU.

    `rodzaj` mowi, CO odmowa znaczy dla wywolujacego:

      * 'saldo'    - gielda policzyla srodki i jest ich za malo. Ilosc z ksiag
                     jest wieksza niz stan rachunku; wyjscie ma o ten stan
                     ZAPYTAC, a nie powtarzac te sama liczbe bez konca,
      * 'parametr' - ksztalt zlecenia lamie regule gieldy (krok, precyzja,
                     minimum). Powtorzenie tej samej liczby da to samo,
      * 'inne'     - odpowiedz DOTARLA z kodem, ktorego nie zaliczamy do zadnej
                     rodziny wyzej (limit zapytan, recv_window, nieznane
                     170xxx). Zlecenia nie ma; kod jest w `code` doslownie
                     (`bybit._blad_zlecenia`, 2026-10-03).

    `wymaga_czlowieka` zostaje FALSZEM: to nie jest awaria klucza ani konta,
    tylko zlecenie, ktorego gielda nie przyjela. Czlowieka budzi dopiero
    POWTARZALNOSC tej odmowy i to jest zadanie czujki egzekucji, nie adaptera.
    """

    def __init__(self, message: str, *, code: str | None = None,
                 rodzaj: str = "parametr", retryable: bool = False,
                 wymaga_czlowieka: bool = False):
        super().__init__(message, code=code, retryable=retryable,
                         wymaga_czlowieka=wymaga_czlowieka)
        self.rodzaj = rodzaj

    @property
    def brak_salda(self) -> bool:
        return self.rodzaj == "saldo"


class KontoZamrozone(ExchangeError):
    """Gielda wstrzymala handel na koncie (compliance, ban, blokada).

    Nie jest to `AuthError`: klucz dziala, podpis przechodzi, to KONTO jest
    zablokowane. Rozroznienie decyduje, gdzie isc - do panelu gieldy i jej
    wsparcia, a nie do klastra i nie do konfiguracji kluczy.
    """

    def __init__(self, message: str, *, code: str | None = None, retryable: bool = False,
                 wymaga_czlowieka: bool = True):
        super().__init__(message, code=code, retryable=retryable,
                         wymaga_czlowieka=wymaga_czlowieka)


class SymbolNieListowany(ExchangeError):
    """GIELDA NIE PROWADZI TEGO INSTRUMENTU. Stan TRWALY i ZNANY, nie awaria.

    DLACZEGO TO NIE MOZE BYC ZWYKLY `ExchangeError`

    Odbiorca odczytu rynkowego ma dwie zupelnie rozne reakcje:

      * odczyt sie NIE UDAL (siec, limit, 5xx, zly klucz, nieznany kod gieldy) -
        dane MIALY przyjsc i nie przyszly. Cykl jest nieudany, licznik awarii
        rosnie, po pieciu proces sie konczy i restart jest WIDOCZNY,
      * gielda tego symbolu NIE LISTUJE - dane nigdy nie przyjda i ponowienie
        za minute, za godzine i za miesiac da to samo. Liczenie tego jako awarii
        zamienia znany, trwaly brak w CrashLoopBackOff, czyli w alarm o czyms,
        czego nikt nie naprawi restartem.

    Zmierzone 2026-09-24: `api.bybit.eu` jako podmiot MiCA nie listuje ANI JEDNEJ
    pary *USDT (128 symboli, wszystkie USDC/EUR/PLN), a `market_worker` zbiera
    arkusz dla 34 instrumentow `active`, z czego 24 to pary USDT. Dwadziescia
    twardych bledow na cykl konczylo proces po pieciu cyklach - 242 restarty.

    CZEGO TEN WYJATEK NIE ZWALNIA: liczba symboli bez arkusza MUSI byc widoczna
    poza logiem (patrz `market_worker`: `bez_arkusza` w `detail` pulsu). Inaczej
    awaria zamienia sie w cisze, a cisza jest gorszym stanem niz restart.
    """

    def __init__(self, message: str, *, code: str | None = None,
                 retryable: bool = False, wymaga_czlowieka: bool = False):
        super().__init__(message, code=code, retryable=retryable,
                         wymaga_czlowieka=wymaga_czlowieka)


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
class PowodOdmowy:
    """POWOD, dla ktorego gielda odrzucila JUZ PRZYJETE zlecenie.

    DWIE RZECZY NARAZ, ZAWSZE RAZEM: `kod` to wartosc gieldy PRZEPISANA
    DOSLOWNIE, `rodzaj` i `opis` to nasza interpretacja tej wartosci. Jedno
    bez drugiego nie wystarcza i dlatego sa w jednej strukturze:

      * sam `rodzaj` jest bezwartosciowy, gdy mapowanie sie pomyli albo gdy
        gielda doda nowa wartosc — z 'nierozpoznane' nie da sie dojsc do tego,
        CO gielda powiedziala,
      * sam `kod` wymaga za kazdym razem czytania dokumentacji gieldy, zeby
        odpowiedziec na pytanie "czy to nasza wina, czy rynek".

    `rodzaj` NIE JEST ZGADYWANY. Wartosc, ktorej adapter nie zna, dostaje
    rodzaj 'nierozpoznane' i `kod` przepisany bez zmian. Podstawienie
    najblizszej znanej kategorii byloby gorsze niz brak kategorii: kod
    nierozpoznany widac jako nierozpoznany, a kod wpisany pod zla kategorie
    wyglada jak diagnoza i konczy sledztwo w zlym miejscu.

    ODRZUCENIE PO PRZYJECIU TO NIE `OdmowaGieldy`. Tamta klasa opisuje
    odpowiedz na SKLADANIE zlecenia (`retCode != 0`, zlecenie nie powstalo).
    Tu zlecenie POWSTALO — ma `venue_order_id` — i zostalo odrzucone przez
    silnik dopasowan juz po przyjeciu. Odczytujemy to z tej samej odpowiedzi,
    z ktorej czytamy status, wiec nie kosztuje to ani jednego zapytania.
    """
    kod: str                       # SUROWA wartosc gieldy, bez tlumaczenia
    rodzaj: str                    # nasza kategoria albo 'nierozpoznane'
    opis: str                      # zdanie dla czlowieka, zawiera `kod`

    @property
    def nierozpoznany(self) -> bool:
        return self.rodzaj == "nierozpoznane"


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
    # Waluta, w ktorej gielda pobrala prowizje. Na spocie kupno placi ja
    # W KUPIONYM AKTYWIE, wiec zalozenie "zawsze waluta kwotowana" zanizalo
    # koszt wejscia o dwa rzedy wielkosci i zawyzalo ilosc w dzienniku.
    prowizja_w: str | None = None
    # POWOD ODRZUCENIA, gdy `status == 'rejected'`. `None` znaczy "gielda go
    # nie podala", a nie "nie ma powodu" — do 2026-09-26 kolumna
    # `exec.order.reject_reason` byla NULL przy KAZDYM odrzuceniu, bo adapter
    # czytal z odpowiedzi status i ilosci, a pole z powodem pomijal.
    powod_odmowy: PowodOdmowy | None = None
    # ZEGAR GIELDY: kiedy przyjela zlecenie i kiedy zmienila jego stan ostatnio
    # (Bybit `createdTime` / `updatedTime`, ms od epoki, JEJ zegar). Diagnostyka
    # odrzucen PostOnly: roznica mowi, jak szybko po przyjeciu gielda je
    # odrzucila, a `createdTime` wobec naszego `arkusz_wysylki.odczyt_at` - ile
    # minelo od odczytu ceny do chwili, w ktorej gielda ja ocenila. None = adapter
    # tego nie podaje (Alpaca) albo pole nie przyszlo; NIE zero.
    utworzono_ms: int | None = None
    zaktualizowano_ms: int | None = None
    # CO TO ZA ZLECENIE, wedlug gieldy: symbol i strona ('buy'/'sell', male
    # litery). Na nich stoi `sprawdz_zastane` - samo `orderLinkId` mowi tylko,
    # ze gielda zna JAKIES zlecenie o tym id. None = adapter tego nie podal,
    # i to NIE przechodzi sprawdzenia (patrz `sprawdz_zastane`).
    symbol: str | None = None
    strona: str | None = None

    @property
    def czesciowe(self) -> bool:
        return Decimal(0) < self.wypelniono < self.ilosc


def sprawdz_zastane(stan: StanZlecenia, *, symbol: str, strona: str,
                    ilosc: Decimal) -> None:
    """Zlecenie zastane u gieldy pod NASZYM id - czy to naprawde nasze. Rzuca, gdy nie.

    IDEMPOTENCJA PO SAMYM ID PRZYJMUJE CUDZE ZLECENIE JAKO SWOJE (audyt S-5).
    Gielda zna zlecenie o tym `orderLinkId` -> `juz_istnialo=True` i plan uchodzi
    za zlozony. Id moze sie jednak powtorzyc: druga instancja na tym samym
    koncie, reczne zlecenie z tym samym id, przenumerowanie planow po
    odtworzeniu bazy. Wtedy plan kupna uznaje za swoje zlecenie SPRZEDAZY innego
    instrumentu, a pozycja, o ktorej decydowalismy, nigdy nie powstaje - bez
    zadnego bledu.

    ROZJAZD WYMAGA CZLOWIEKA, nie ponowienia. Zlozenie drugiego zlecenia pod tym
    samym id i tak odrzuci gielda, a ponowienie odczytu da ten sam wynik.

    ILOSC: NIE WIECEJ NIZ W PLANIE, a nie "rowno". Pod tym samym id moze lezec
    zlecenie zastepcze na MNIEJSZA ilosc - `odmowa_gieldy` sklada je, gdy
    rachunek potwierdza mniej, niz wyjscie chcialo sprzedac. Wieksza ilosc
    naszym zleceniem byc nie moze: ilosc idzie przez `do_kroku_w_dol`.

    BRAK SYMBOLU ALBO STRONY TO ROZJAZD. Adapter, ktory ich nie podaje, nie daje
    podstaw do uznania zlecenia za nasze - domysl "pewnie sie zgadza" jest
    dokladnie ta wada, ktora to sprawdzenie zamyka.
    """
    roznice = []
    if stan.symbol is None or stan.symbol != symbol:
        roznice.append(f"symbol {stan.symbol!r} zamiast {symbol!r}")
    if stan.strona is None or stan.strona != strona.strip().lower():
        roznice.append(f"strona {stan.strona!r} zamiast {strona!r}")
    if not (Decimal(0) < stan.ilosc <= Decimal(ilosc)):
        roznice.append(f"ilosc {stan.ilosc} przy planie {ilosc}")
    if roznice:
        raise ExchangeError(
            f"gielda zna zlecenie {stan.id_wlasne!r} (id gieldy "
            f"{stan.id_gieldy}, status {stan.status}), ale to NIE jest zlecenie "
            f"z planu: {'; '.join(roznice)}. Uznanie go za zlozone zostawiloby "
            f"plan bez zlecenia, a cudze zlecenie bez wlasciciela. SPRAWDZ "
            f"KONTO GIELDY - ponowienie da ten sam wynik.",
            code="tozsamosc-zlecenia", wymaga_czlowieka=True)

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
