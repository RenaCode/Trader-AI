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
    AuthError, Balance, BladIpKlucza, BookLevel, Candle, ConnectionCheck, ExchangeError,
    KontoZamrozone, OdmowaGieldy, OrderBook, PowodOdmowy, StanZlecenia,
    SymbolNieListowany,
    Wypelnienie, ZlozoneZlecenie,
)

# SWIECE I CECHY bierzemy z podmiotu GLOBALNEGO niezaleznie od tego, gdzie stoi
# konto - to on ma ksiege, na ktorej model byl trenowany. Przepiecie tego zrodla
# wstawiloby w szereg uskok, ktorego kalibracja wag nie zaklada.
PUBLIC_HOST = "https://api.bybit.com"

# ARKUSZ JEST WYJATKIEM OD POWYZSZEGO, I TO NIE Z WYGODY (2026-09-24).
#
# `fetch_order_book` zasila WYLACZNIE rachunek kosztow: ceche `book_spread_bps`
# (ktora jest w `diagnose_signals.CECHY_BEZ_SYGNALU` - zaden sygnal jej nie
# czyta) oraz skladnik "spread" w `koszty.karta_rundy`. Kosztu nie opisuje
# ksiega, na ktorej model sie uczyl - opisuje go ksiega, NA KTOREJ SKLADAMY
# ZLECENIA.
#
# Do tej daty bylo odwrotnie i renacode-infra: docs/projekty/trader/08-przed-go-live.md nazywa to najdrozszym otwartym pytaniem
# projektu: spready zmierzono na ksiedze globalnej, a zlecenia ida na bybit.eu
# o okolo 0,1% tamtego wolumenu. `karta_rundy` oznaczala te liczbe jako
# INNY_RYNEK i traktowala jako PODLOGE kosztu, nie jego pomiar.
#
# Host bierze sie teraz z `self._host`, czyli z `ref.venue` - dzieki temu idzie
# ZA TRYBEM: w demo pytamy ksiegi demo (tej, ktora naprawde wypelnia nasze
# zlecenia), a po przejsciu na live - ksiegi live. Obie sprawdzone 2026-09-24:
# `api.bybit.eu` i `api-demo.bybit.eu` wystawiaja `/v5/market/orderbook`
# publicznie, bez klucza.
#
# CZEGO TA ZMIANA NIE ROBI: nie rusza szeregu `book_spread_bps` wstecz. Wartosci
# sprzed tej daty pochodza z ksiegi globalnej i opisuja co innego niz wartosci
# po niej. Przy czytaniu historii tej cechy trzeba o tym pamietac.
#
# ...ALE „ARKUSZ Z GIELDY EGZEKUCJI" DOTYCZY TYLKO TEGO, CZYM HANDLUJEMY (2026-09-25)
#
# Powyzsze bylo sluszne i dla par USDC dziala - mierzony spread spadl 10,57 ->
# 5,85 pb, bo to naprawde ksiega wypelniajaca nasze zlecenia. Zabraklo jednak
# jednego rozroznienia: `market_worker` zbiera arkusz dla WSZYSTKICH instrumentow
# `active`, a `active` to nie to samo co `tradeable` (migracja 0020). Podmiot
# unijny jako podmiot MiCA nie listuje ANI JEDNEJ pary *USDT:
#
#     api.bybit.com  ADAUSDT, TRXUSDT -> retCode 0
#     api.bybit.eu   te same symbole  -> retCode 10001 "Not supported symbols"
#     api.bybit.eu   instruments-info -> 128 symboli, wszystkie USDC/EUR/PLN
#
# 20 z 34 instrumentow odpadalo wiec twardym bledem, cykl byl „nieudany", piec
# takich pod rzad konczylo proces: 242 restarty od 24.09.
#
# Host wybiera odtad WYWOLUJACY, per instrument, flaga `ksiega_egzekucji`:
#
#     tradeable=true  -> `self._host` (ksiega, ktora wypelnia nasze zlecenia)
#     tradeable=false -> `PUBLIC_HOST` (ciaglosc szeregu `book_spread_bps`:
#                        ich wartosci sprzed 24.09 tez pochodzily stamtad)
#
# PROWENIENCJA BEZ KOLUMNY W SCHEMACIE. `market.book_snapshot` nie ma pola na
# zrodlo i nie dostanie go tutaj - bo nie musi. Zrodlo wynika z tej samej flagi,
# ktora o nim zdecydowala i ktora stoi w bazie: `ref.instrument.tradeable`.
# Uniwersum decyzyjne to wylacznie `tradeable`, wiec dla rachunku kosztu
# (`koszty.karta_rundy`) cala jego dziedzina pochodzi z naszej ksiegi. Okno
# siegajace przed 24.09 albo pomiar rozciagniety na instrumenty niehandlowalne
# to nadal INNY_RYNEK - i do tego sluzy `--spread-obca-ksiega`.
#
# NIE PRZYJMUJEMY ARKUSZA OD GIELDY, KTORA SYMBOLU NIE LISTUJE - i to jest
# regula osobna od wyboru hosta, bo sam blad jej nie zapewnia. Cztery majory
# (BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT) nie sa listowane na bybit.eu, a endpoint
# `/v5/market/orderbook` oddaje tam PELNY arkusz: od 24.09 zapisano ~3089 takich
# snapshotow i spread przeskoczyl 0,39 -> 2,29 pb. Liczba prawdziwa, tylko
# o czym innym - dokladnie ta klasa bledu, ktora tropi `koszty.py`. Dlatego
# `fetch_order_book` PYTA O LISTE (`instruments-info`) i odmawia, zanim
# zapisze cokolwiek.

# Jak dlugo lista instrumentow danego hosta jest uznawana za aktualna. Lista
# zmienia sie listingami, czyli w skali dni - a nie odswiezana WCALE kazalaby
# restartowac workera po kazdym nowym listingu.
LISTING_WAZNOSC_S = 3600.0

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


# ------------------------------------------------- powod odrzucenia po przyjeciu
#
# Bybit podaje `rejectReason` w `/v5/order/realtime` i `/v5/order/history` —
# czyli w TEJ SAMEJ odpowiedzi, z ktorej `stan_zlecenia` czyta status i ilosci.
# Do 2026-09-26 pole bylo pomijane, wiec `exec.order.reject_reason` byl NULL na
# WSZYSTKICH odrzuceniach w calej historii produkcji (zlecenia 55, 57, 58).
# Zapisanie go nie kosztuje ani jednego dodatkowego zapytania.
#
# ODRZUCENIE PO PRZYJECIU TO INNA SCIEZKA NIZ `retCode != 0`. Tamta (kody
# 170131 itd., `_blad_z_kodu` nizej) znaczy "zlecenie nie powstalo". Tutaj
# zlecenie POWSTALO — `orderId` jest nadany — i zostalo odrzucone dopiero przez
# silnik dopasowan. Dlatego `OdmowaGieldy` sie tu nie nadaje: nie ma wyjatku do
# podniesienia, jest wiersz zlecenia do opisania.

# Wartosci, ktorymi Bybit mowi "NIE BYLO ODMOWY". Pole przychodzi takze przy
# zleceniu wypelnionym, wiec bez tego zbioru kazde wypelnienie dostawaloby
# powod odrzucenia — czyli dziennik klamalby w druga strone.
_BEZ_ODMOWY = {"", "EC_NoError", "0", "None", "null"}

# MAPOWANIE JEST KROTKIE CELOWO.
#
# Wpis w tej tablicy to zdanie "wiemy, co ta wartosc znaczy" — a wartosc,
# ktorej znaczenia nie potwierdzilismy ani na produkcji, ani w dokumentacji
# gieldy, ma wyjsc jako 'nierozpoznane' z kodem przepisanym doslownie. Kod
# nierozpoznany da sie zdiagnozowac; kod podstawiony pod zla kategorie wyglada
# jak diagnoza i konczy sledztwo w zlym miejscu.
#
# DOLOZENIE WPISU WYMAGA DOWODU, nie pamieci: wartosci zaobserwowanej w `raw`
# na produkcji albo cytatu z dokumentacji API. Do tego czasu 'nierozpoznane'
# jest uczciwsze i NIE JEST ciche — `gielda_exec` zapisuje je z adnotacja.
POWODY_ODMOWY: dict[str, tuple[str, str]] = {
    # Jedyna wartosc, ktora ten projekt POTRAFI wskazac u siebie: egzekucja
    # wysyla `timeInForce: PostOnly` (patrz `zloz_zlecenie`), a cena limitu to
    # zamkniecie swiecy sprzed kilkudziesieciu minut. Gdy rynek zszedl ponizej
    # tej ceny, kupno przecina spread — i PostOnly kaze gieldzie je odrzucic
    # ZAMIAST wypelnic takerem. To jest zachowanie ZAMOWIONE, nie awaria.
    "EC_PostOnlyWillTakeLiquidity": (
        "postonly",
        "zlecenie przecielo spread, a `timeInForce: PostOnly` zabrania "
        "wypelnienia takerem — gielda odrzucila je ZAMIAST zaplacic wyzsza "
        "prowizje. To zachowanie ZAMOWIONE (symulacja zaklada makera po cenie "
        "limitu), a nie awaria: cena limitu byla po prostu po zlej stronie "
        "rynku w chwili wyslania"),
}


def _ms_gieldy(surowy) -> int | None:
    """Znacznik czasu gieldy (ms od epoki, jako napis) -> int albo None.

    Diagnostyka nie ma prawa wywrocic odczytu stanu zlecenia: wartosc
    nieczytelna daje None, a nie wyjatek.
    """
    try:
        return int(surowy) if surowy not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _powod_odmowy(surowy) -> PowodOdmowy | None:
    """`rejectReason` giełdy -> (surowa wartość + nasza interpretacja).

    `None` znaczy "giełda nie podała powodu". Wartość nieznana NIE jest
    podstawiana pod najbliższą znaną kategorię — wychodzi jako
    `rodzaj='nierozpoznane'` z kodem przepisanym dosłownie.
    """
    if surowy is None:
        return None
    kod = str(surowy).strip()
    if kod in _BEZ_ODMOWY:
        return None
    znane = POWODY_ODMOWY.get(kod)
    if znane is None:
        return PowodOdmowy(
            kod=kod, rodzaj="nierozpoznane",
            # KOD W OPISIE, nie tylko w polu obok. Opis trafia do
            # `exec.order.reject_reason`, po ktorym szuka sie gremem — bez
            # doslownej wartosci w tresci nie da sie znalezc drugiego
            # wystapienia tej samej odmowy.
            opis=(f"giełda odrzuciła zlecenie z powodem {kod!r}, którego NIE "
                  f"ROZPOZNAJEMY — wartość zapisana DOSŁOWNIE, bez podstawiania "
                  f"pod żadną znaną kategorię. Sprawdź ją w dokumentacji API "
                  f"giełdy i dopisz do `POWODY_ODMOWY`, jeśli się powtarza"))
    rodzaj, opis = znane
    return PowodOdmowy(kod=kod, rodzaj=rodzaj, opis=f"{opis} [rejectReason={kod}]")


# --------------------------------------------------------------- kody bledow
#
# Kody trzymamy w NAZWANYCH ZBIORACH, nie w krotkach wpisanych w dwie galezie
# `if` (GET i POST mialy dotad wlasne, rozjezdzajace sie listy). Dolozenie kodu
# ma byc jedna linia w jednym miejscu, inaczej przy nastepnym rozjezdzie POST
# bedzie klasyfikowal inaczej niz GET - a to droga awaryjna, na ktorej najmniej
# widac, ze cos jest nie tak.

# Klucz albo jego uprawnienia: zly, wygasly, bez uprawnienia, zly podpis.
KODY_UWIERZYTELNIENIA = {"10003", "10004", "10005", "33004"}

# 10010 - "Unmatched IP". Klucz i podpis sa POPRAWNE, tylko przyszly z adresu
# spoza listy przypietej do klucza. Wlasna klasa, bo naprawa jest gdzie indziej.
KODY_NIEZGODNE_IP = {"10010"}

# Konto zablokowane przez gielde: ban uzytkownika, reguly compliance, zakaz
# handlu. Klucz dziala, podpis przechodzi - to konto jest wylaczone.
KODY_KONTO_ZAMROZONE = {"10008", "10024", "10027"}

# Limit zapytan zwrocony jako retCode przy HTTP 200 (Bybit robi i tak, i przez
# status 429). To stan PRZEJSCIOWY - do niedawna wpadal do rodziny
# uwierzytelnienia, wiec chwilowy limit wygladal jak zly klucz i nie byl
# ponawiany, mimo ze wystarczylo odczekac sekunde.
KODY_LIMIT = {"10006", "10018"}

# --------------------------------------------------- odmowa, a nie brak odpowiedzi
#
# GIELDA ODPOWIEDZIALA "NIE". To jest wiedza, nie jej brak - i do 2026-09-24
# byla ona TRACONA: kazdy kod spoza zbiorow wyzej wpadal w ogolny
# `ExchangeError`, a `gielda_exec.zloz_plan` zapisywal wtedy `status='unknown'`
# z etapem `odpowiedz_nie_dotarla`. Zmierzone skutki (SOLUSDC, pozycja 32):
# cztery wyjscia pod rzad - zlecenia 49, 51, 52, 53 - dostaly
# `Insufficient balance. (kod 170131)` i kazde zapisalo sie jako "stan
# nieznany". Pozycja stala 19 godzin, a licznik odrzucen czujki egzekucji
# pokazywal 0, bo `status='rejected'` nie powstal ani razu.
#
# ASYMETRIA RYZYKA DECYDUJE O TYM, CO WOLNO TU WPISAC. Zaliczenie kodu do tego
# zbioru oznacza zapisanie "zlecenia NA PEWNO nie ma". Pomylka w te strone
# porzuca zlecenie ZYWE na gieldzie - czyli ekspozycje, ktorej risk kernel nie
# policzy. Pomylka w druga strone daje tylko wiersz `unknown`, ktory sprzatanie
# i tak rozstrzygnie zapytaniem. Dlatego wchodza tu WYLACZNIE kody, ktorych
# tresc nie zostawia miejsca na domysl: gielda podaje POWOD odmowy, a powod
# odmowy istnieje tylko wtedy, gdy zlecenia nie przyjeto.
#
# Dolozenie kodu to jedna linia w jednym miejscu - tak samo jak wyzej.

# Gielda policzyla srodki i jest ich za malo. To jest jedyny rodzaj odmowy,
# przy ktorym ILOSC Z NASZYCH KSIAG jest kwestionowana przez druga strone,
# wiec ma wlasny zbior: `gielda_exec` pyta wtedy o stan rachunku zamiast
# powtarzac te sama liczbe co swiece.
KODY_BRAK_SALDA = {
    "170131",  # spot: "Insufficient balance." — zmierzone na produkcji 2026-09-23/24
    "170033",  # spot margin: "Margin Insufficient..."
    "110007",  # kontrakty: "ab not enough for new order"
    "110012",  # kontrakty: "Insufficient available balance"
    "110045",  # kontrakty: "Wallet balance is insufficient"
}

# Ksztalt zlecenia lamie regule gieldy. Powtorzenie tej samej liczby da to samo
# - i to jest cala roznica wobec `unknown`, ktore KAZE ponowic pytanie.
KODY_ZLY_PARAMETR = {
    "10001",   # "Request parameter error"
    "170136",  # "Order quantity exceeded lower limit" (minimum ilosci)
    "170137",  # "Order quantity has too many decimals" — patrz `zlecenia.bez_ogona`
    "170140",  # "Order value exceeded lower limit" (min_notional)
    "170193",  # "Buy order price cannot be higher than ..."
    "170194",  # "Sell order price cannot be lower than ..."
}


def _blad_z_kodu(ret, msg: str) -> ExchangeError:
    """Zamienia `retCode` gieldy na wyjatek, ktory MOWI, CO ZROBIC.

    DLACZEGO TO NIE MOZE BYC JEDEN `ExchangeError`

    Odbiorca bledu ma dokladnie trzy sensowne reakcje: ponow, odpusc ten odczyt,
    albo obudz czlowieka. Gdy wszystko poza 404/429/5xx/kluczem wpada w ogolny
    `ExchangeError`, proces po pieciu cyklach po prostu sie konczy - i "gielda
    nas zablokowala" wyglada w alarmie tak samo jak "padla baza".

    Roznica jest praktyczna: przy zablokowanym koncie albo martwym kluczu silnik
    ZYJE i bije, wiec czujka zywotnosci milczy, a otwartych pozycji nie da sie
    zamknac, bo wyjscia sa odrzucane tak samo jak wejscia. Komunikat musi wiec
    o trzeciej w nocy powiedziec wprost, czy isc do panelu gieldy, czy do klastra.
    """
    kod = str(ret)
    if kod in KODY_NIEZGODNE_IP:
        return BladIpKlucza(
            f"{msg} (kod {kod}) — NIEZGODNOSC IP: gielda odrzuca WSZYSTKIE zapytania "
            f"prywatne, bo adres wyjsciowy nie jest na liscie IP przypietej do klucza. "
            f"To nie jest zly klucz ani awaria procesu — proces zyje, tylko gielda go "
            f"nie slucha, wiec nie da sie ZAMKNAC otwartych pozycji. "
            f"IDZ DO PANELU GIELDY (API -> lista IP klucza) albo sprawdz, czy klaster "
            f"nie zmienil adresu wyjsciowego. Restart i ponowienie nic nie zmienia.",
            code=kod)
    if kod in KODY_KONTO_ZAMROZONE:
        return KontoZamrozone(
            f"{msg} (kod {kod}) — KONTO ZABLOKOWANE PO STRONIE GIELDY: handel wstrzymany. "
            f"Klucz i podpis dzialaja, wiec to nie jest problem poswiadczen ani klastra. "
            f"Otwarte pozycje zostaja, a zlecenia WYJSCIA sa odrzucane tak samo jak "
            f"wejscia. IDZ DO PANELU GIELDY (compliance / wsparcie). "
            f"Restart i ponowienie nic nie zmienia.",
            code=kod)
    if kod in KODY_UWIERZYTELNIENIA:
        return AuthError(
            f"{msg} (kod {kod}) — KLUCZ ODRZUCONY. IDZ DO PANELU GIELDY i sprawdz klucz "
            f"(waznosc, uprawnienia, podmiot: klucz z bybit.eu na hoscie globalnym "
            f"dostaje 10003). Klaster jest tu bez winy.",
            code=kod)
    if kod in KODY_LIMIT:
        # `retryable=True`: limit mija sam. Bez tego chwilowe zdlawienie
        # wygladalo jak blad klucza i konczylo zapytanie przy pierwszej probie.
        return ExchangeError(f"{msg} (kod {kod}) — limit zapytan", code=kod, retryable=True)
    if kod in KODY_BRAK_SALDA:
        return OdmowaGieldy(
            f"{msg} (kod {kod}) — GIELDA ODMOWILA: za malo srodkow. Zlecenie NIE "
            f"WESZLO i powtorzenie tej samej ilosci da to samo. Nasze ksiegi "
            f"podaja wiecej, niz gielda potwierdza — przy WYJSCIU znaczy to, ze "
            f"pozycji nie da sie zamknac w calosci, dopoki roznica nie zostanie "
            f"wyjasniona.",
            code=kod, rodzaj="saldo")
    if kod in KODY_ZLY_PARAMETR:
        return OdmowaGieldy(
            f"{msg} (kod {kod}) — GIELDA ODMOWILA: zlecenie lamie jej regule "
            f"(krok, precyzja albo minimum). Zlecenie NIE WESZLO; ponowienie bez "
            f"zmiany liczb da to samo.",
            code=kod, rodzaj="parametr")
    return ExchangeError(f"{msg} (kod {kod})", code=kod)


# STAN NAPRAWDE NIEZNANY PRZY ZLECENIU - jedyne dwa `retCode`, ktore NIE sa
# odpowiedzia "nie". 10000 to "Server Timeout", 10016 to "Server error" - oba
# znacza, ze silnik dopasowania mogl zlecenie przyjac, zanim brama oddala blad.
# Do tej rodziny naleza jeszcze timeout, blad sieci i odpowiedz nie-JSON (5xx
# z posrednika), ale te nie maja `retCode` i obsluguje je `_post` osobno.
KODY_STAN_NIEZNANY = {"10000", "10016"}

# "Ten orderLinkId juz istnieje" (spot 170141, kontrakty 110072). To NIE jest
# odmowa: znaczy, ze zlecenie o naszym identyfikatorze JEST u gieldy - np. po
# timeoucie, ktorego odpowiedz nie dotarla, a nasz odczyt stanu jeszcze go nie
# widzial. Zapisanie tego jako `rejected` porzuciloby zywe zlecenie, wiec
# zostaje ogolnym `ExchangeError` (stan `unknown`), a nastepne sprawdzenie
# stanu po naszym id je odnajdzie.
KODY_DUPLIKAT_ID = {"170141", "110072"}

# "Zlecenia nie ma" przy anulowaniu. 110001 to kod KONTRAKTOW; spot V5 oddaje
# 170213 ("Order does not exist"). Do 2026-10-03 stal tu wylacznie 110001,
# a testy mockowaly ten sam kod, wiec na spocie (czyli w calym tym systemie)
# wyscig "wypelnilo sie / anulujemy" konczyl sie falszywym "anulowanie nie
# powiodlo sie" i jednym zbednym cyklem. Kodu spotowego nie zweryfikowano
# jeszcze na demo - stad oba w zbiorze, a nie podmiana.
KODY_ZLECENIA_NIE_MA = {"110001", "170213"}


def _blad_zlecenia(ret, msg: str) -> ExchangeError:
    """`retCode` != 0 na zlozeniu albo anulowaniu: ODPOWIEDZ DOTARLA, wiec to jest "nie".

    DLACZEGO OSOBNO OD `_blad_z_kodu`

    Na odczycie ogolny `ExchangeError` jest uczciwy - odczyt sie nie udal i nic
    sie nie zmienilo. Na ZAPISIE ogolny `ExchangeError` czytany jest przez
    `gielda_exec.zloz_plan` jako "odpowiedz nie dotarla" i konczy sie wierszem
    `unknown`, ktorego nikt juz nie ponawia (`zloz_oczekujace` szuka planow BEZ
    wiersza zlecenia). Do 2026-10-03 tak konczyl sie kazdy kod spoza dwoch
    zbiorow odmowy: limit zapytan, 10002 (recv_window), nieznane 170xxx - a to
    sa odpowiedzi, ktore DOTARLY i mowia, ze zlecenia nie przyjeto.

    Zostawiamy klasy, ktore niosa `wymaga_czlowieka` (martwy klucz, IP, konto):
    one tez znacza "nie", ale ich obsluga jest eskalacja, nie zapis i spokoj.
    Wszystko inne poza `KODY_STAN_NIEZNANY` staje sie `OdmowaGieldy` rodzaju
    "inne" z kodem doslownie - wolajacy zapisuje `rejected` i kod, zamiast
    nieprawdy o stanie, ktory akurat znamy.
    """
    kod = str(ret)
    blad = _blad_z_kodu(ret, msg)
    if kod in KODY_DUPLIKAT_ID:
        return ExchangeError(
            f"{msg} (kod {kod}) — zlecenie o tym orderLinkId JUZ JEST u gieldy; "
            f"stan sprawdzimy po naszym id, nie wolno go uznac za odrzucone",
            code=kod)
    if kod in KODY_STAN_NIEZNANY or isinstance(blad, OdmowaGieldy) or blad.wymaga_czlowieka:
        return blad
    return OdmowaGieldy(
        f"{msg} (kod {kod}) — GIELDA ODMOWILA bez powodu, ktory rozpoznajemy: "
        f"odpowiedz DOTARLA, wiec zlecenie NIE WESZLO. Kod zapisany doslownie; "
        f"jesli sie powtarza, dopisz go do wlasciwego zbioru w `bybit.py`.",
        code=kod, rodzaj="inne", retryable=blad.retryable)



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
        # Lista symboli listowanych na danym (host, kategoria), ze znacznikiem
        # czasu odczytu. Pamiec, a nie stala: `market_worker` pyta o arkusz co
        # minute dla kilkudziesieciu instrumentow i bez tego kazde zapytanie
        # o arkusz ciagnelo by za soba drugie, o cala liste instrumentow.
        self._listing: dict[tuple[str, str], tuple[float, frozenset[str]]] = {}

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
            # Klasyfikacja siedzi w jednym miejscu (`_blad_z_kodu`) i jest wspolna
            # z POST-em: rozjazd list kodow miedzy odczytem a skladaniem zlecen
            # znaczylby, ze ta sama awaria gieldy brzmi inaczej zaleznie od tego,
            # ktora sciezka ja napotkala.
            raise _blad_z_kodu(ret, data.get("retMsg", "nieznany blad"))
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

    async def fetch_positioning(self, symbol: str, start: datetime, end: datetime,
                                limit: int = 200) -> list[tuple[datetime, float | None,
                                                                float | None]]:
        """Pozycjonowanie tlumu na perpetualu: udzial kont dlugich i otwarte pozycje.

        Zwraca [(czas, udzial_dlugich, otwarte_pozycje)] rosnaco po czasie.
        None w polu znaczy, ze TO ZRODLO nie oddalo danych dla tej godziny -
        i jest to inna informacja niz zero. Zero w udziale dlugich znaczy
        "wszyscy sa krotcy", a to jest stan, ktory nigdy nie zachodzi.

        DWA WYWOLANIA, JEDEN WYNIK. Bybit wystawia te wielkosci osobno, ale
        obie sa godzinowe i dotycza tego samego kontraktu, wiec sklejamy je po
        znaczniku czasu. Rozdzielenie ich na dwie sciezki oznaczaloby dwa razy
        te sama logike uzupelniania luk.

        Kategoria zawsze `linear`: obie wielkosci istnieja wylacznie dla
        kontraktow wieczystych. Host publiczny, bo podmiot unijny perpetuali
        nie prowadzi - `BTCUSDT` zwraca tam "Symbol Invalid".
        """
        okno = {"startTime": int(start.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000)}

        udzial: dict[datetime, float] = {}
        res = await self._get(
            "/v5/market/account-ratio",
            {"category": "linear", "symbol": symbol, "period": "1h",
             "limit": min(limit, 500), **okno},
            private=False, host=PUBLIC_HOST,
        )
        for row in res.get("list", []):
            t = datetime.fromtimestamp(int(row["timestamp"]) / 1000, tz=timezone.utc)
            udzial[t] = float(row["buyRatio"])

        otwarte: dict[datetime, float] = {}
        res = await self._get(
            "/v5/market/open-interest",
            {"category": "linear", "symbol": symbol, "intervalTime": "1h",
             "limit": min(limit, 200), **okno},
            private=False, host=PUBLIC_HOST,
        )
        for row in res.get("list", []):
            t = datetime.fromtimestamp(int(row["timestamp"]) / 1000, tz=timezone.utc)
            otwarte[t] = float(row["openInterest"])

        return [(t, udzial.get(t), otwarte.get(t))
                for t in sorted(set(udzial) | set(otwarte))]

    async def _listowane(self, host: str, kategoria: str) -> frozenset[str]:
        """Symbole, ktore TEN host naprawde prowadzi. Z pamiecia na `LISTING_WAZNOSC_S`.

        PUSTA ODPOWIEDZ KONCZY SIE BLEDEM, a nie zbiorem pustym. Zbior pusty
        znaczylby „ten host nie listuje niczego", czyli uznalby KAZDY symbol za
        nielistowany - a to zamienia awarie odczytu w cisze dla calego cyklu.
        Tak samo urwana lista (`nextPageCursor`): lista niepelna, uzyta jako
        pelna, wypisuje z rynku instrumenty, ktore na nim sa.
        """
        klucz = (host, kategoria)
        teraz = time.monotonic()
        wpis = self._listing.get(klucz)
        if wpis is not None and teraz - wpis[0] < LISTING_WAZNOSC_S:
            return wpis[1]

        res = await self._get("/v5/market/instruments-info",
                              {"category": kategoria}, private=False, host=host)
        if res.get("nextPageCursor"):
            raise ExchangeError(
                f"lista instrumentow {host} ({kategoria}) jest urwana - "
                f"gielda oddala kursor nastepnej strony. Lista niepelna uznalaby "
                f"za nielistowane symbole, ktore sa listowane",
                code="listing-urwany")
        symbole = frozenset(s for s in (i.get("symbol", "") for i in res.get("list", [])) if s)
        if not symbole:
            raise ExchangeError(
                f"lista instrumentow {host} ({kategoria}) przyszla PUSTA - to jest "
                f"nieudany odczyt, nie gielda bez instrumentow",
                code="listing-pusty", retryable=True)
        self._listing[klucz] = (teraz, symbole)
        return symbole

    async def fetch_order_book(self, symbol: str, depth: int = 50, *,
                               ksiega_egzekucji: bool = True) -> OrderBook:
        """Arkusz z ksiegi EGZEKUCJI (`ksiega_egzekucji=True`) albo referencyjnej.

        Powod rozdzielenia i pomiary - w komentarzu na gorze modulu. Tutaj sedno:
        arkusz zasila rachunek kosztu, wiec dla instrumentu, ktorym HANDLUJEMY,
        pytamy ksiegi, ktora go wypelnia; dla pozostalych zostaje host globalny,
        bo to on da szeregowi `book_spread_bps` ciaglosc.

        GIELDA, KTORA SYMBOLU NIE LISTUJE, NIE JEST PYTANA O JEGO ARKUSZ. Nie
        dlatego, ze odmowi - czesto NIE odmawia. `api.bybit.eu` oddaje pelny
        arkusz dla BTCUSDT, ktorego nie listuje; przez dobe weszlo tak ~3089
        snapshotow i spread majorow przeskoczyl 0,39 -> 2,29 pb. Odpowiedz
        wygladala poprawnie, wiec nic sie nie zepsulo w sposob widoczny.
        """
        host = self._host if ksiega_egzekucji else PUBLIC_HOST
        if symbol not in await self._listowane(host, self._category):
            raise SymbolNieListowany(
                f"{symbol}: {host} nie prowadzi tego instrumentu (kategoria "
                f"{self._category}), wiec arkusz z tego hosta opisywalby co innego "
                f"niz pytanie. Stan TRWALY - ponowienie da to samo",
                code="nielistowany")
        try:
            res = await self._get(
                "/v5/market/orderbook",
                {"category": self._category, "symbol": symbol, "limit": min(depth, 200)},
                private=False, host=host,
            )
        except ExchangeError as e:
            # DRUGA WARSTWA TEJ SAMEJ REGULY, na wypadek rozjazdu miedzy lista
            # a arkuszem: `10001` na tym endpoincie to „Not supported symbols",
            # czyli ta sama wiadomosc co brak na liscie. Bez tego wpadalby
            # w `OdmowaGieldy` (rodzaj „parametr") i liczyl sie jako awaria
            # cyklu - to jest dokladnie przyczyna 242 restartow z 24.09.
            if e.code == "10001":
                raise SymbolNieListowany(
                    f"{symbol}: {host} odpowiedzial {e} - gielda tego symbolu nie "
                    f"prowadzi. Stan TRWALY - ponowienie da to samo",
                    code=e.code) from e
            raise
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
            # Ta sama klasyfikacja co przy GET. Wazniejsza tutaj: to na sciezce
            # skladania zlecen martwy klucz albo zamrozone konto zatrzymuje nie
            # tylko WEJSCIA, ale i WYJSCIA z otwartych pozycji. Kod spoza
            # znanych zbiorow jest tu ODMOWA, nie niewiedza - `_blad_zlecenia`.
            raise _blad_zlecenia(ret, data.get("retMsg", "nieznany blad"))
        return data.get("result") or {}

    async def stan_zlecenia(self, id_wlasne: str, kategoria: str = "spot") -> StanZlecenia | None:
        """Stan zlecenia po NASZYM identyfikatorze. None, gdy giełda go nie zna.

        Odpytujemy po `orderLinkId`, nie po identyfikatorze giełdy - ten drugi
        poznajemy dopiero z odpowiedzi, więc w sytuacji, dla której to istnieje,
        jeszcze go nie mamy.

        Sprawdzamy zlecenia otwarte ORAZ historię: zlecenie mogło się wypełnić
        i zniknąć z listy otwartych między naszym timeoutem a zapytaniem.

        TOŻSAMOŚĆ ZWRÓCONEGO ZLECENIA JEST SPRAWDZANA, NIE ZAKŁADANA

        Na tym odczycie stoi cała idempotencja: `zloz_zlecenie` pyta o stan ZANIM
        cokolwiek wyśle i przy odpowiedzi niepustej uznaje, że zlecenie już
        istnieje. Filtrowanie po `orderLinkId` robi giełda — my dostajemy tylko
        listę i wierzymy, że jest nasza.

        Gdyby giełda ten parametr zignorowała (inna kategoria, zmiana API,
        literówka w nazwie pola, zapytanie bez filtru po refaktorze), pierwszy
        wiersz z listy byłby CUDZYM zleceniem: przypisalibyśmy sobie cudze
        wypełnienie albo uznali za istniejące zlecenie, którego nie ma — i nigdy
        go nie złożyli, bo `juz_istnialo=True` kończy sprawę bez śladu awarii.
        Jedno porównanie zamienia to w błąd zamiast w pozycję, na obu endpointach.
        """
        # TRZECI ODCZYT: `openOnly=1`. Bez niego `/v5/order/realtime` oddaje
        # WYLACZNIE zlecenia otwarte, a `/v5/order/history` wedlug dokumentacji
        # Bybita "may delay" - zlecenie wypelnione chwile przed odczytem potrafi
        # zniknac z obu. `openOnly=1` czyta ostatnie 500 zamknietych z pamieci
        # podrecznej silnika, czyli dokladnie te luke. Pytamy o nia tylko, gdy
        # dwa pierwsze odczyty nic nie znalazly.
        for sciezka, dodatkowe in (("/v5/order/realtime", {}),
                                   ("/v5/order/history", {}),
                                   ("/v5/order/realtime", {"openOnly": "1"})):
            odp = await self._get(sciezka, {"category": kategoria,
                                            "orderLinkId": id_wlasne, **dodatkowe})
            lista = odp.get("list") or []
            nasze = [o for o in lista if (o.get("orderLinkId") or "") == id_wlasne]
            if lista and not nasze:
                # Odczyt PORZUCONY, a nie "brak zlecenia". Zwrócenie None
                # posłałoby `zloz_zlecenie` prosto w złożenie drugiego zlecenia
                # na podstawie odczytu, o którym już wiemy, że nic nie znaczy.
                obce = sorted({str(o.get("orderLinkId") or "") for o in lista})[:3]
                raise ExchangeError(
                    f"gielda zwrocila zlecenia o CUDZYCH orderLinkId {obce} na zapytanie "
                    f"o {id_wlasne!r} ({sciezka}) — filtr zostal zignorowany, wiec ten "
                    f"odczyt nie mowi nic o naszym zleceniu. Przyjecie go przypisaloby "
                    f"nam cudze wypelnienie albo uznaloby za istniejace zlecenie, "
                    f"ktorego nie ma. SPRAWDZ ADAPTER I API GIELDY — to nie jest stan "
                    f"przejsciowy i ponowienie nic nie da.",
                    code="tozsamosc-zlecenia", wymaga_czlowieka=True)
            if nasze:
                o = nasze[0]
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
                    prowizja_w=o.get("feeCurrency") or None,
                    # POWOD ODMOWY Z TEJ SAMEJ ODPOWIEDZI. Nie jest to nowe
                    # zapytanie — `rejectReason` lezy w tym samym wierszu, co
                    # `orderStatus`, ktory czytamy linie wyzej. Pomijanie go
                    # zostawialo `exec.order.reject_reason` NULL-em przy kazdym
                    # odrzuceniu i kazdy z nich wygladal identycznie.
                    powod_odmowy=_powod_odmowy(o.get("rejectReason")),
                    # Z TEJ SAMEJ ODPOWIEDZI, bez nowego zapytania - patrz
                    # `StanZlecenia.utworzono_ms`.
                    utworzono_ms=_ms_gieldy(o.get("createdTime")),
                    zaktualizowano_ms=_ms_gieldy(o.get("updatedTime")),
                )
        return None

    async def zloz_zlecenie(self, *, symbol: str, strona: str, ilosc: Decimal,
                            cena_limitu: Decimal | None, id_wlasne: str,
                            kategoria: str = "spot",
                            rynkowe: bool = False,
                            istnienie_sprawdzone: bool = False,
                            post_only: bool = True) -> ZlozoneZlecenie:
        """Składa zlecenie. Z limitem (domyślnie) albo RYNKOWE. IDEMPOTENTNIE.

        `id_wlasne` musi być wyprowadzone z decyzji, a NIE z czasu ani losowo -
        inaczej ponowienie tworzy nowy identyfikator i cała ochrona znika.

        SPRAWDŹ, POTEM SKŁADAJ. Zamiast ponawiać na ślepo, przy braku odpowiedzi
        pytamy giełdę, czy zna to `id_wlasne`. Trzy możliwe odpowiedzi i każda
        prowadzi gdzie indziej:
          * zna i wypełnione  -> nic nie robimy, zwracamy stan
          * zna i otwarte     -> nic nie robimy, zwracamy stan
          * nie zna           -> zlecenie nie weszło, wolno złożyć

        `rynkowe=True` TO OSOBNA ŚCIEŻKA, KTÓREJ NORMALNY HANDEL NIE UŻYWA

        Domyślnie `False` i to nie jest wygoda — to jest zabezpieczenie. Cena
        limitu z `PostOnly` jest w tym systemie WARUNKIEM zgodności z symulacją:
        `POSLIZG_MAKER = 0` jest poprawne dokładnie wtedy, gdy jesteśmy makerem
        po naszej cenie albo nie handlujemy wcale, a projekt zmierzył, że przy
        takerze Sharpe jest ujemny. Zlecenie rynkowe płaci spread i poślizg,
        których symulacja nie modeluje i których ten system NIGDY nie zmierzył
        (`POSLIZG_TAKER` = 4 pb, n=0). Jedynym wołającym z `rynkowe=True` jest
        likwidacja po kill-switchu (`services/engine/likwidacja.py`), gdzie
        NIEWYPEŁNIENIE kosztuje więcej niż poślizg.

        `post_only=False` (2026-10-03, audyt F-4.3) TO DRUGA TAKA ŚCIEŻKA, też
        z jednym wołającym: wyjście, którego dwie próby PostOnly giełda
        odrzuciła w tym samym cyklu (`gielda_exec.zloz_plan`). Limit zostaje,
        zmienia się tylko `timeInForce` na `GTC` - cena po drugiej stronie
        spreadu wypełnia się wtedy takerem zamiast wracać jako odrzucenie.
        Pozycja czekająca 4 h na następną świecę kosztowała zmierzone 85 USD
        na 4 przypadkach; taker na spot_eu to 25 pb, przy pozycji 1 250 USD
        około 3 USD. Wejście tej ścieżki nie dostaje nigdy.

        `cena_limitu` MUSI być `None` przy zleceniu rynkowym i jest to
        sprawdzane, nie zakładane. Przyjęcie ceny i zignorowanie jej kończy się
        wierszem `exec.order`, który ma `order_type='market'` i `limit_price`
        wypełnione — a to jest zapis nieprawdy o tym, co giełda dostała, i
        DOKŁADNIE ta liczba trafiłaby potem do pomiaru poślizgu jako cena
        odniesienia (`poslizg.cena_odniesienia`, źródło „zlecenie").

        `marketUnit: "baseCoin"` JEST TU KONIECZNE, NIE OZDOBNE. Na spocie Bybit
        interpretuje `qty` przy zleceniu rynkowym KUPNA jako kwotę w walucie
        KWOTOWANEJ, a przy sprzedaży jako ilość aktywa bazowego. Ta asymetria
        znaczy, że odkup pozycji krótkiej na 1000 jednostek zamówiłby 1000 USDC
        zamiast 1000 sztuk — bez błędu, bez odmowy, z inną wielkością niż
        zatwierdził risk kernel. Jawna jednostka usuwa różnicę między stronami.
        """
        # Giełda też sprawdza unikalność orderLinkId, ale dopiero PO przyjęciu
        # zapytania. Sprawdzenie u nas oszczędza jedno okrążenie i - co ważniejsze -
        # daje ten sam wynik, gdy poprzednia próba skończyła się timeoutem.
        # SPRZECZNE ARGUMENTY SA BLEDEM WYWOLANIA, nie do cichego pogodzenia.
        # Oba kierunki sa grozne z osobna: rynkowe z cena zapisuje limit,
        # ktorego gielda nie dostala, a limit bez ceny wyslalby `"price": "None"`.
        if rynkowe and cena_limitu is not None:
            raise ValueError(
                f"zlecenie RYNKOWE nie ma ceny limitu, a podano {cena_limitu} - "
                f"zapisanie jej jako `limit_price` byloby nieprawda o tym, co "
                f"gielda dostala, i weszloby do pomiaru poslizgu jako cena "
                f"odniesienia")
        if not rynkowe and cena_limitu is None:
            raise ValueError("zlecenie z limitem bez ceny limitu")

        # `istnienie_sprawdzone=True`: wolajacy zapytal o stan TUZ PRZED odczytem
        # arkusza (`gielda_exec.zloz_plan`), zeby miedzy arkuszem a POST-em nie
        # stalo zadne zapytanie. Drugie sprawdzenie tutaj wstawialoby z powrotem
        # te sekundy, przez ktore cena PostOnly przestaje byc pasywna.
        if not istnienie_sprawdzone:
            istniejace = await self.stan_zlecenia(id_wlasne, kategoria)
            if istniejace is not None:
                return ZlozoneZlecenie(id_wlasne=id_wlasne, id_gieldy=istniejace.id_gieldy,
                                       status=istniejace.status, juz_istnialo=True)

        if rynkowe:
            wynik = await self._post("/v5/order/create", {
                "category": kategoria,
                "symbol": symbol,
                "side": _strona_bybita(strona),
                "orderType": "Market",
                "qty": str(ilosc),
                # JEDNOSTKA ILOSCI JAWNIE - uzasadnienie w docstringu.
                "marketUnit": "baseCoin",
                # BEZ `price` I BEZ `timeInForce`. Zlecenie rynkowe jest z
                # definicji IOC; dopisanie `PostOnly` byloby sprzecznoscia,
                # ktora gielda odrzuca - czyli likwidacja nie wyszlaby wcale.
                "orderLinkId": id_wlasne,
            })
            return ZlozoneZlecenie(id_wlasne=id_wlasne, id_gieldy=wynik.get("orderId"),
                                   status="new")

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
            #
            # WYJATEK JEST JAWNY I JEDEN: `post_only=False` dla wyjscia po
            # dwoch odrzuceniach PostOnly (docstring). Taker jest wtedy
            # zamowiony, a nie cichy - `raw.wyjscie_agresywne` go oznacza.
            "timeInForce": "PostOnly" if post_only else "GTC",
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
            # "Zlecenia nie ma" - przy anulowaniu to sukces, nie awaria.
            if e.code in KODY_ZLECENIA_NIE_MA:
                return False
            raise

    async def close(self) -> None:
        await self._client.aclose()
