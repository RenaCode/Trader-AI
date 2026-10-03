"""Egzekucja na giełdzie: składanie planów i rozstrzyganie ich z giełdy.

CO SIĘ ZMIENIA WOBEC RACHUNKU PAPIEROWEGO

Rachunek papierowy rozstrzyga plan patrząc na świecę: jeśli cena dotknęła
limitu, uznaje zlecenie za wypełnione w całości. To założenie jest optymistyczne
w dwie strony naraz — zakłada wypełnienie, którego mogło nie być, i zakłada
całość tam, gdzie weszła część.

Tutaj rozstrzyga GIEŁDA. Pytamy ją o stan zlecenia po NASZYM identyfikatorze
i przyjmujemy to, co odpowie: ilość, cenę i prowizję. Żadna z tych trzech liczb
nie jest już naszym założeniem.

DLACZEGO IDENTYFIKATOR JEST WYPROWADZANY Z PLANU

`tai-p{id planu}` jest deterministyczny, więc ponowne złożenie tego samego planu
trafia w to samo zlecenie zamiast tworzyć drugie. Identyfikator z czasu albo
losowy zniósłby całą ochronę: przy każdej próbie powstawałoby nowe zlecenie,
a dwa zlecenia to podwójna pozycja — objaw nie wyglądający na awarię, tylko na
strategię, która weszła mocniej, niż zakładał risk kernel.

CZEGO TEN MODUŁ NIE ROBI

Nie decyduje, czy wejść. Decyzja jest już podjęta i zapisana w dzienniku, zanim
cokolwiek tutaj się wydarzy. Ten moduł wykonuje ją i wiernie zapisuje, co z niej
wyszło — łącznie z tym, że nie wyszło nic.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from services.engine.egzekucja import co_z_wypelnieniem, rozlicz_prowizje
from services.engine.sesja import StanSesji
from services.engine.loop import (
    trwale_niesprzedawalna, zapisz_pominiecie, zapisz_wejscie, zapisz_wyjscie)
from services.engine.zlecenia import (
    Filtry, Oferty, ocena_zwloki, sprawdz_zlecenie)
from services.exchanges.base import ExchangeError, OdmowaGieldy, ZlozoneZlecenie
# `recv_window` to jedyna liczba, ktora opisuje dopuszczalny rozjazd naszego
# zegara z zegarem gieldy — ta sama, ktorej pilnuje `BybitAdapter.dryf_zegara`.
from services.exchanges.bybit import RECV_WINDOW


def filtry_z_bazy(cur, engine: str) -> dict[int, Filtry]:
    """Reguły giełdy per instrument, wprost z `ref.instrument`.

    Brak reguły zostaje None i blokuje zlecenie na tym instrumencie. Zamiana go
    na wartość domyślną byłaby zgadywaniem cudzej reguły — a odrzucone zlecenie
    wygląda w nocnym logu jak brak sygnału.
    """
    cur.execute(
        "SELECT id, lot_size, tick_size, min_notional, (meta->>'min_ilosc') AS min_ilosc"
        " FROM ref.instrument WHERE engine::text = %s AND active", (engine,))
    out = {}
    for r in cur.fetchall():
        out[r["id"]] = Filtry(
            krok_ilosci=r["lot_size"], krok_ceny=r["tick_size"],
            min_wartosc=r["min_notional"],
            min_ilosc=Decimal(r["min_ilosc"]) if r["min_ilosc"] else None)
    return out


def id_zlecenia(plan_id: int) -> str:
    """Nasz identyfikator zlecenia. Z PLANU, nie z czasu i nie losowy."""
    return f"tai-p{plan_id}"


async def potwierdzone_saldo(adapter, aktywo: str | None) -> Decimal | None:
    """Ile tego aktywa POTWIERDZA gielda. None znaczy „nie da sie zapytac".

    NONE I ZERO TO DWIE ROZNE ODPOWIEDZI i mylenie ich kosztowaloby pozycje.
    Zero znaczy „gielda mowi, ze nie mamy nic" — wtedy sprzedawac nie ma czego.
    None znaczy „nie wiemy" — a na tym wlasnie hoscie jest to stan CODZIENNY,
    nie awaryjny: `api-demo.bybit.eu` oddaje 404 na kazdej sciezce do salda
    (patrz `uzgodnienie.uzgodnij`). Podstawienie zera za brak odpowiedzi
    zamienialoby niewiedze w twierdzenie i zatrzymywalo wyjscie tam, gdzie
    pozycja moze byc calkiem w porzadku.

    BIERZEMY `total`, TE SAMA LICZBE CO UZGODNIENIE (`uzgodnij`: `b.total`).
    Pytanie brzmi „ile aktywa mamy", a nie „ile da sie wyplacic";
    `availableToWithdraw` bywa na koncie zunifikowanym puste i schodzi wtedy
    do `walletBalance` — czyli i tak do `total`, tyle ze niejawnie. Jedna
    liczba w obu miejscach znaczy, ze rozjazd zglaszany przez uzgodnienie
    i rozjazd widziany przez egzekucje to TEN SAM rozjazd.
    """
    if not aktywo:
        return None
    try:
        salda = await adapter.fetch_balances()
    except ExchangeError:
        return None
    for b in salda:
        if b.currency.upper() == aktywo.upper():
            return Decimal(str(b.total))
    # Aktywa nie ma na liscie sald. `fetch_balances` pomija monety o zerowym
    # `walletBalance`, wiec to JEST odpowiedz gieldy: nie trzymamy nic.
    return Decimal(0)


# Stany, w ktorych gielda NIE MA JUZ NIC DO ZROBIENIA z tym zleceniem.
# `partial` swiadomie tu nie ma — patrz `gielda_juz_rozstrzygnela`.
STANY_ROZSTRZYGNIETE = ("filled", "cancelled", "rejected")


async def gielda_juz_rozstrzygnela(adapter, plan: dict, *, kategoria: str,
                                   verbose: bool = False) -> bool:
    """Czy gielda zamknela sprawe tego zlecenia, zanim doszlismy do terminu.

    DLACZEGO SYMETRIA Z BACKTESTEM TU NIE OBOWIAZUJE — I CZEMU TO NIE JEST
    ROZLUZNIENIE REGULY, TYLKO INNE PYTANIE

    `termin_rozstrzygniecia` czeka na zamkniecie swiecy i ma racje, bo
    odpowiada na pytanie „CZY LIMIT ZOSTAL DOTKNIETY". Na rachunku papierowym
    odpowiedz da wylacznie swieca domknieta: `portfolio._wypelnione` czyta
    `high`/`low`, a zagladanie do swiecy trwajacej byloby przeciekiem
    przyszlosci w druga strone. Ta funkcja NIE zmienia tam niczego i nie jest
    stamtad wolana — `loop.rozstrzygnij_plany` czeka dokladnie tak jak czekalo.

    Na gieldzie tego pytania NIE ZADAJEMY. Gielda nie szacuje, czy limit
    zostal dotkniety — ona to WYKONALA i podaje `orderStatus` oraz
    `cumExecQty`. Czekanie z zapisem faktu, ktory juz mamy, nie jest symetria
    z symulacja; jest trzymaniem dziennika w stanie ZNANYM-NIEPRAWDZIWYM przez
    czas do dlugosci swiecy — do 4 h przy H4H.

    Zmierzone na produkcji 2026-09-23 (`exec.order` 48, ETHUSDC sell):
    wypelnienie na gieldzie ~13:26, `closed_at` dopisane 16:03:42, bo plan 36
    mial `valid_until = 16:00`. Przez te 2 h 37 min zachodzilo jednoczesnie:

      1. `RiskKernel._open_exposure` liczyl ekspozycje z pozycji, ktorej juz
         nie bylo — czyli blokowal wejscia, ktorych nic nie blokowalo. To jest
         dokladnie stan z naglowka `uzgodnienie.py`: „System stoi i nie ma
         o tym pojecia";
      2. uzgodnienie SLUSZNIE widzialo rozjazd i slalo `killswitch.triggered`
         co 10 minut bez wyciszenia (`always_on`, `cooldown 0`);
      3. CronJob czujki zywotnosci konczyl sie kodem niezerowym —
         `trader-zywotnosc-29836320` ma status `Failed`, a ArgoCD `Degraded`.

    `partial` NIE JEST STANEM ROZSTRZYGNIETYM i celowo go tu nie ma. Zlecenie
    czesciowo wypelnione nadal lezy w arkuszu i moze sie dopelnic do konca
    swiecy; wczesniejsze przyjecie przyjeloby to, co weszlo, i ANULOWALO
    reszte (`egzekucja.co_z_wypelnieniem`), czyli systematycznie zmniejszalo
    pozycje wzgledem planu. Zmieniamy chwile zapisu FAKTU DOKONANEGO, a nie
    regule egzekucji.

    BLAD ODCZYTU ZOSTAWIA DOTYCHCZASOWE ZACHOWANIE. Gdy gieldy nie da sie
    zapytac, czekamy tak jak dotad — niewiedza nie moze rozstrzygac planu.
    Wyjatkiem jest awaria wymagajaca czlowieka (np. zignorowany filtr po
    `orderLinkId`): ta ma isc w gore natychmiast, bo zaden restart jej nie
    naprawi, a wyciszenie jej na cala swiece to dokladnie ta cisza, przed
    ktora ostrzega `stan_zlecenia`.
    """
    try:
        stan = await adapter.stan_zlecenia(id_zlecenia(plan["id"]),
                                           kategoria=kategoria)
    except ExchangeError as e:
        if e.wymaga_czlowieka:
            raise
        if verbose:
            print(f"  [czeka] {plan['symbol']} — nie da się sprawdzić stanu "
                  f"na giełdzie ({e}); plan czeka na swoją świecę")
        return False
    if stan is None:
        return False
    return stan.status in STANY_ROZSTRZYGNIETE


async def prowizja_z_rejestru(adapter, *, symbol: str, id_wlasne: str,
                              submitted_at: datetime | None, kategoria: str):
    """Kwota i WALUTA prowizji z rejestru wykonan. Zwraca (kwota, waluta, uwaga).

    DLACZEGO TO MUSI BYC DRUGIE ZAPYTANIE, A NIE ZALOZENIE

    `/v5/order/realtime` i `/v5/order/history` oddaja `cumExecFee` — samą kwotę,
    BEZ jednostki. `feeCurrency` wystawia wylacznie `/v5/execution/list`.
    `stan_zlecenia` czyta te pierwsze, wiec `prowizja_w` wraca puste, a sciezka
    egzekucji podstawiala za nie walute kwotowana instrumentu.

    Zmierzone na produkcji 2026-09-23, WSZYSTKIE trzy kupna gieldowe w bazie:

        symbol   | side |   qty    |      fee       | fee_ccy | fee/qty
        SOLUSDC  | buy  | 12.138   | 0.012138000000 | USDC    | 0.00100000
        BTCUSDC  | buy  | 0.015319 | 0.000015319000 | USDC    | 0.00100000
        ETHUSDC  | buy  | 0.3395   | 0.000339500000 | USDC    | 0.00100000

    `fee / qty` rowne DOKLADNIE 0,001 to stawka makera liczona od ILOSCI, czyli
    prowizja pobrana w SOL/BTC/ETH — a zapisana jako USDC. Skutki byly trzy
    i wszystkie ciche:

      1. koszt wejscia zanizony ~2700x dla ETH (0,00034 USDC zamiast 0,93),
         bo `exec.fill.fee` sumuje sie jako liczba w walucie kwotowanej.
         `exec.position.fees_paid` = 0,92250977 przy prawdziwym koszcie rundy
         ok. 1,85 USDC — czyli polowa. Ta liczba idzie do `realized_pnl`
         i na krzywa kapitalu, na ktorej stoi kill-switch;
      2. `rozlicz_prowizje` nie odejmowalo prowizji od ilosci, bo nie widzialo
         waluty bazowej — `exec.position.qty` zostawala BRUTTO;
      3. tolerancja uzgodnienia (0,5%) maskowala roznice 0,1%, wiec zaden
         nadzor tego nie zglosil. Tolerancja przestala byc marginesem, a stala
         sie pokrywa.

    ZGADYWANIE PRZY NIEZGODNYCH WALUTACH JEST ZAKAZANE. Gdy wypelnienia jednego
    zlecenia podaja rozne waluty prowizji, jedna liczba w `exec.fill.fee_ccy`
    nie opisuje ich obu — oddajemy wtedy `uwaga` i zostawiamy walute nieznana,
    zamiast wybrac jedna i nazwac wybor pomiarem.
    """
    od_ms = None
    if submitted_at is not None:
        # `startTime` porownuje gielda SWOIM zegarem. Margines to `recv_window`,
        # czyli granica, powyzej ktorej gielda odrzuca kazde nasze podpisane
        # zapytanie — zegar zdolny do handlu miesci sie i tutaj.
        od_ms = int((submitted_at - timedelta(milliseconds=int(RECV_WINDOW)))
                    .timestamp() * 1000)
    wyp = [w for w in await adapter.wypelnienia(od_ms=od_ms, symbol=symbol)
           if w.id_wlasne == id_wlasne]
    if not wyp:
        return None, None, (
            f"rejestr wykonan nie zna wypelnien zlecenia {id_wlasne} — waluta "
            f"prowizji pozostaje NIEZNANA")
    waluty = {w.prowizja_w.upper() for w in wyp if w.prowizja_w}
    if len(waluty) > 1:
        return None, None, (
            f"wypelnienia zlecenia {id_wlasne} podaja ROZNE waluty prowizji "
            f"({', '.join(sorted(waluty))}) — jedna kolumna `fee_ccy` nie "
            f"opisuje ich obu, wiec nie wybieramy zadnej")
    if not waluty:
        return None, None, (
            f"rejestr wykonan tez nie podal waluty prowizji dla {id_wlasne}")
    return sum((w.prowizja for w in wyp), Decimal(0)), waluty.pop(), None


# ILE MOZE MIEC ARKUSZ, ZANIM PRZESTANIE BYC "CENA Z CHWILI WYSYLKI".
#
# ZMIERZONE, nie przyjete. Roznica miedzy znacznikiem oferty a chwila zapisu
# (`market.book_snapshot`: ingested_at - event_time), 3 dni, cale uniwersum
# `active AND tradeable`, ~3200 migawek na instrument:
#
#     mediana |roznicy|   0,07 s
#     p99                 1,1-3,5 s (zaleznie od pary)
#     skrajnosci          od -13,0 s do +3,9 s
#
# Znak ujemny to znacznik gieldy PRZED naszym zegarem, czyli rozjazd zegarow -
# i dlatego wiek liczymy z modulu. 60 s to ponad czterokrotnosc zmierzonej
# skrajnosci, a jednoczesnie cena, ktora w tym czasie zdazy odjechac, to dla
# SOL-a okolo 0,04% (mediana dryfu 75-minutowego 0,33%, skalowana pierwiastkiem
# czasu), czyli jedna piata rundy kosztowej.
#
# DLACZEGO PROG MUSI BYC LUZNY, A NIE CIASNY. On nie chroni przed niczym
# realnym przy 30 s zamiast 60 s - on ma odciac odczyt ZEPSUTY: `ts` rowne zero
# (znacznik 1970) albo arkusz zamrozony przez posrednika. Prog ciasny
# zatrzymywalby handel na szumie zegara, czyli zamienialby wade w awarie.
#
# TEN PROG JEST FAIL-OPEN, I TO NIE JEST TO, CO TU WCZESNIEJ STALO.
#
# Do 2026-09-27 stalo tu zdanie, ze poza sesja kazdy arkusz akcji bedzie
# starszy niz 60 s, wiec wejscia silnika `equity` beda ODMAWIANE, i ze jest to
# "bezpieczny kierunek". Oba twierdzenia byly FALSZYWE, a zapisane w tym
# miejscu trafily tez do uzasadnienia migracji 0060.
#
# Falszywe, bo odmowa `brak_ceny_wysylki` wymaga, zeby zawiodly OBA odczyty
# ceny (patrz `if oferty is None and odniesienie is None` w `zloz_plan`), a ten
# prog oslania tylko PIERWSZY. Zrodlo zapasowe `adapter.cena_odniesienia`
# oddaje goly `Decimal` BEZ ZNACZNIKA CZASU (`alpaca.py`: zwraca `t.get("p")`
# z `/v2/stocks/trades/latest`), wiec jego wieku NIE DA SIE sprawdzic
# strukturalnie - a poza sesja ten odczyt SIE UDAJE, oddajac cene zamkniecia.
# Zmierzone: `zloz_plan` z arkuszem w wieku 120 s (dwukrotnosc progu) sklada
# zlecenie po cenie ostatniej transakcji i NIE zapisuje pominiecia.
#
# Skutek dla silnika gieldowego zamyka bramka SESJI w `zloz_plan`, a nie ten
# prog: `loop.run_once` czyta `stan_sesji` powtornie bezposrednio przed
# `zloz_oczekujace` i wejscia sa wstrzymywane przy rynku zamknietym
# (`rynek_zamkniety`) oraz przy stanie nieznanym (`sesja_nieznana`). Prog wieku
# NIE musi wiec pochodzic z kalendarza sesji - i nie pochodzi.
#
# CO ZOSTAJE OTWARTE: `cena_odniesienia` nadal jest bez bramki wieku i nie da
# sie jej dodac bez zmiany sygnatury adapterow (brak znacznika czasu w
# odpowiedzi). Dla rynku ciaglego oznacza to, ze zamrozony arkusz przy zywym
# `/trades/latest` przepusci cene, ktorej wieku nikt nie zna. Osobno: zatrzymanie
# notowan (halt) w OTWARTEJ sesji zamraza arkusz przy `is_open=true`, wiec daje
# `brak_ceny_wysylki` - poprawna odmowe, ktora w dzienniku czyta sie jak awaria.
WIEK_ARKUSZA_MAX_S = 60

# ILE NASZEGO CZASU MOZE MINAC MIEDZY ODCZYTEM ARKUSZA A POST-EM (2026-10-03).
#
# To jest INNY pomiar niz `WIEK_ARKUSZA_MAX_S`, i dlatego osobna stala, a nie
# zacisniecie tamtej do 5 s. Tamta porownuje znacznik GIELDY z naszym zegarem,
# wiec niesie rozjazd zegarow (zmierzone od -13,0 s do +3,9 s, opis wyzej) -
# prog 5 s odrzucalby na niej zdrowe odczyty i spychal wejscia na `lastPrice`,
# ktore na zleceniu 54 lezalo 11 tickow nad askiem. Ta mierzy `time.monotonic()`
# miedzy KONCEM naszego odczytu a POCZATKIEM wysylki: rozjazd zegarow jej nie
# dotyczy, a to jest dokladnie okno, w ktorym rynek moze przejsc o spread i tick
# i zamienic cene pasywna w odrzucenie PostOnly (audyt 2026-10-03, E1).
#
# Do tej zmiany miedzy arkuszem a POST-em staly dwa GET-y `stan_zlecenia`
# z ponowieniami po 1 s i 2 s (`bybit.py`), czyli kilka sekund w zlym dniu.
# Dzis sprawdzenie stanu idzie PRZED arkuszem, a miedzy arkuszem a wysylka
# jest juz tylko liczenie ceny. Prog lapie wiec wylacznie przypadek
# nienormalny (zawieszona petla zdarzen, wolna baza przy zapisie pominiecia)
# - i wtedy NIE WYSYLAMY, zamiast wysylac cene z odczytu, ktorego wiek znamy.
WIEK_ODCZYTU_PRZY_WYSYLCE_S = 5.0

# Zegar tego pomiaru jako nazwa modulu, zeby test mogl go podmienic bez
# podmieniania `time.monotonic` calej petli zdarzen.
_zegar_wysylki = time.monotonic


class BladPrzedWyslaniem(Exception):
    """Odczyt stanu zlecenia PRZED wysylka sie nie udal - NIC nie poszlo na gielde.

    DLACZEGO TO NIE JEST `unknown`

    Do 2026-10-03 wyjatek z wstepnego `stan_zlecenia` (wolanego w srodku
    `zloz_zlecenie`) wpadal do tego samego `except ExchangeError` co timeout
    POST-a i zapisywal `status='unknown'` z etapem `odpowiedz_nie_dotarla`.
    Oba slowa byly nieprawda: nie wyslalismy niczego, wiec nie bylo czego
    "nie dostac". A wiersz `unknown` zamyka plan przed ponowieniem, bo
    `zloz_oczekujace` szuka planow BEZ wiersza zlecenia.

    Dlatego wiersza nie ma: plan zostaje `pending` bez zlecenia, a nastepny
    cykl sprobuje od nowa - "nie probowalismy" znaczy tu doslownie to, co
    znaczy brak wiersza (docstring `zloz_oczekujace`). Etap trafia do licznika
    `przed_wyslaniem` w pulsie silnika, zeby powtarzajaca sie awaria odczytu
    nie wygladala jak spokoj.
    """

    def __init__(self, przyczyna: ExchangeError):
        super().__init__(f"etap przed_wyslaniem: stanu zlecenia nie da się "
                         f"odczytać ({przyczyna}) — NIC nie wysłano, plan "
                         f"zostaje do ponowienia w następnym cyklu")
        self.przyczyna = przyczyna


async def arkusz_wysylki(adapter, symbol: str, kategoria: str, teraz: datetime
                         ) -> tuple[Oferty | None, str, dict]:
    """Najlepsze oferty z ksiegi EGZEKUCJI w chwili wysylki.

    Zwraca (oferty, skad, slad). `slad` to ten sam odczyt W POSTACI NADAJACEJ
    SIE DO `exec.order.raw` - patrz sekcja "TRZECI ELEMENT" nizej.

    `ksiega_egzekucji=True` NIE JEST TU DOMYSLNA WYGODA, TYLKO CALY SENS
    ODCZYTU. Host publiczny (api.bybit.com) i host egzekucji (api.bybit.eu
    z `ref.venue`) to dwie rozne ksiegi; zmierzony rozjazd ich zamkniec to
    mediana 5,9 pb i p90 28,4 pb (patrz `zlecenia.ROZJAZD_GIELD`), czyli rzad
    calej rundy kosztowej. Pytanie brzmi "po ktorej stronie SPREADU, w ktorym
    zlecenie wyladuje, stanie nasza cena" - na to odpowiada wylacznie arkusz
    tej ksiegi. `fetch_order_book` pilnuje przy tym, ze host ten symbol
    naprawde LISTUJE: api.bybit.eu oddaje pelny arkusz takze dla BTCUSDT,
    ktorego nie prowadzi (patrz `bybit.fetch_order_book`).

    GLEBOKOSC 1, bo pytamy o najlepsza oferte po kazdej stronie i o nic wiecej.
    Glebszy arkusz opisywalby poslizg zlecenia rynkowego, a nasze zlecenia
    z definicji rynkowe nie sa.

    BRAK ODCZYTU ODDAJEMY JAKO None RAZEM Z POWODEM, a nie jako wyjatek.
    Wywolujacy ma dwie rozne reakcje zaleznie od roli zlecenia (wyjscia nie
    wolno zatrzymac, wejscia nie wolno wyslac po cenie sprzed godziny), wiec
    decyzje o tym podejmuje on, a nie ten odczyt.

    TRZECI ELEMENT: SLAD ODCZYTU, BO BEZ NIEGO ODRZUCENIA SIE NIE DA PRZYPISAC
    (2026-09-27)

    `skad` jest ZDANIEM DLA CZLOWIEKA i do rozstrzygania przyczyn nie sluzy:
    nie da sie po nim filtrowac, a znacznika arkusza w ogole nie zawiera.
    Zmierzone na produkcji: `exec.order.raw` zlecen 61, 62 i 64 (odrzucone
    przez `EC_PostOnlyWillTakeLiquidity`) ma `powod_odmowy_gieldy`
    i `venue_order_id`, ale ANI BIDU, ANI ASKU, ANI WIEKU ODCZYTU. Dwie
    przyczyny o PRZECIWNYCH lekarstwach daja wiec identyczny wiersz:

      * arkusz byl STARY - `WIEK_ARKUSZA_MAX_S` dopuszcza odczyt do minuty,
        a to znaczy "zacisnij prog wieku";
      * rynek ODJECHAL miedzy odczytem a przyjeciem zlecenia - to znaczy
        "jeden tick marginesu to za malo".

    Dlatego odczyt oddaje te same liczby DRUGI RAZ, w postaci do zapisania.
    ZERO dodatkowych zapytan do gieldy: `slad` powstaje z tej samej odpowiedzi
    `fetch_order_book`, ktora i tak juz mamy w rekach.

    `odczyt_at` JEST TU PO TO, ZEBY NIE LICZYC WIEKU Z `submitted_at`.
    `exec.order.submitted_at` to `now()`, czyli znacznik TRANSAKCJI cyklu -
    wszystkie zlecenia jednego cyklu maja go identyczny co do mikrosekundy
    (zmierzone: zlecenia 63 i 64 oba 2026-09-27 05:11:28.059192+00) i lezy on
    PRZED odczytem arkusza, nie w jego chwili. `odczyt_at` to `teraz` podane
    przez wywolujacego, czyli zegar z chwili odczytu.
    """
    slad: dict = {"odczyt_at": teraz.isoformat(), "bid": None, "ask": None,
                  "arkusz_at": None, "wiek_s": None, "powod_braku": None}
    try:
        ksiega = await adapter.fetch_order_book(symbol, 1, ksiega_egzekucji=True)
    except ExchangeError as e:
        # `SymbolNieListowany` wpada tutaj jako podklasa i to jest poprawne:
        # z punktu widzenia CENY oba stany znacza to samo - arkusza nie ma.
        # Rozniaca je trwalosc ma znaczenie dla workera rynku, nie dla nas.
        skad = f"arkusz księgi egzekucji NIEODCZYTANY ({e})"
        return None, skad, {**slad, "powod_braku": skad}
    if not ksiega.bids or not ksiega.asks:
        skad = "arkusz księgi egzekucji przyszedł BEZ ofert po jednej ze stron"
        return None, skad, {**slad, "powod_braku": skad,
                            "arkusz_at": ksiega.event_time.isoformat()}
    wiek = abs((teraz - ksiega.event_time).total_seconds())
    # ZNACZNIK I WIEK ZAPISUJEMY TAKZE WTEDY, GDY ODCZYT ODRZUCAMY. Bez tego
    # "arkusz byl za stary" zostawaloby wiedza wylacznie tej funkcji, a wlasnie
    # ona rozstrzyga miedzy dwoma przeciwnymi lekarstwami.
    slad = {**slad, "arkusz_at": ksiega.event_time.isoformat(),
            "wiek_s": round(wiek, 3),
            "bid": str(ksiega.bids[0].price), "ask": str(ksiega.asks[0].price)}
    if wiek > WIEK_ARKUSZA_MAX_S:
        # SWIEZA CENA Z ODCZYTU SPRZED GODZINY TO TEN SAM BLAD W NOWYM MIEJSCU.
        skad = (f"arkusz księgi egzekucji ma {wiek:.0f} s "
                f"(granica {WIEK_ARKUSZA_MAX_S} s) — znacznik oferty "
                f"{ksiega.event_time:%Y-%m-%d %H:%M:%S}, to nie jest "
                f"cena z chwili wysyłki")
        return None, skad, {**slad, "powod_braku": skad}
    oferty = Oferty(bid=ksiega.bids[0].price, ask=ksiega.asks[0].price)
    if not oferty.kompletne:
        skad = (f"arkusz księgi egzekucji ODWRÓCONY albo zerowy "
                f"(bid {oferty.bid}, ask {oferty.ask}) — odczyt zepsuty")
        return None, skad, {**slad, "powod_braku": skad}
    return oferty, (f"arkusz księgi egzekucji {oferty.bid}/{oferty.ask} "
                    f"(wiek {wiek:.1f} s)"), slad


async def arkusz_po_wysylce(adapter, symbol: str, kategoria: str) -> dict:
    """DRUGI odczyt najlepszych ofert, ZARAZ PO odpowiedzi gieldy na POST.

    PO CO. Odrzucenie PostOnly zapada w silniku gieldy w chwili PRZYJECIA
    zlecenia, a my dowiadujemy sie o nim dopiero przy rozstrzyganiu, w kolejnym
    cyklu - za pozno, zeby zobaczyc, jak wtedy wygladala ksiega. Odczyt
    sprzed wysylki (`arkusz_wysylki`) mowi, gdzie byl rynek, kiedy liczylismy
    cene; ten - gdzie byl, kiedy gielda ja oceniala (z dokladnoscia do jednego
    obiegu sieci). Dopiero obie pary bid/ask razem rozstrzygaja miedzy „rynek
    przeszedl przez nasza cene w trakcie wysylki" a „cena byla zla juz przy
    liczeniu".

    DLA KAZDEGO ZLOZONEGO ZLECENIA, nie tylko odrzuconego - z tego samego
    powodu, co `slad_wysylki`: bez grupy odniesienia odsetek nic nie mowi.

    NIGDY NIE PODNOSI WYJATKU. To jest diagnostyka PO wyslaniu: cena juz
    poszla, a blad tego odczytu nie moze zatrzymac zapisu zlecenia, ktore lezy
    na gieldzie. Kazda awaria wraca jako `powod_braku`.
    """
    teraz = datetime.now(timezone.utc)
    try:
        _, _, slad = await arkusz_wysylki(adapter, symbol, kategoria, teraz)
        return slad
    except Exception as e:  # noqa: BLE001 - diagnostyka nie wywraca egzekucji
        return {"odczyt_at": teraz.isoformat(), "bid": None, "ask": None,
                "arkusz_at": None, "wiek_s": None,
                "powod_braku": f"odczyt po wysyłce nie powiódł się: {e!r}"}


def slad_wysylki(arkusz: dict, ceny: dict, plan: dict,
                 cena: Decimal | None) -> dict:
    """Blok `exec.order.raw.arkusz_wysylki`: arkusz, jego WIEK i cena wyslana.

    DLACZEGO PRZY KAZDYM ZLECENIU, A NIE TYLKO PRZY ODRZUCONYM. Bo pojedynczy
    wiersz odrzucenia nie mowi nic o tym, czy ten arkusz byl NIETYPOWY. Odsetek
    odrzucen PostOnly po poprawce z 2026-09-26 07:47:39Z to 3 na 6 zlecen
    z limitem - zeby ustalic, czym te trzy roznily sie od tamtych trzech,
    potrzebne sa liczby dla WSZYSTKICH szesciu. Zapis tylko przy odmowie daje
    zbior bez grupy odniesienia, czyli pomiar, z ktorego nie wynika nic.

    `cena is None` ZNACZY "W TYM CYKLU NIC NIE WYSLALISMY" - tak wchodzi tu
    sciezka, w ktorej sprawdzenie odmowilo, a zlecenie i tak lezy u gieldy
    z poprzedniego cyklu. Zapisanie tam ceny byloby zapisaniem nieprawdy:
    ta liczba nie poszla nigdzie.

    ZERO DODATKOWYCH ZAPYTAN DO GIELDY. Wszystko, co tu wchodzi, pochodzi
    z odpowiedzi, ktore `zloz_plan` i tak juz odebral: `arkusz` z `arkusz_wysylki`,
    `ceny` z `sprawdz_zlecenie`.
    """
    return {**arkusz, **ceny,
            "cena_planu": str(plan["limit_price"]),
            "cena_wyslana": None if cena is None else str(cena)}


async def zloz_plan(cur, adapter, plan: dict, f: Filtry, *, engine: str,
                    mode: str, kategoria: str,
                    sesja: StanSesji | None = None) -> tuple[bool, str]:
    """Składa jeden plan na giełdzie. Zwraca (czy_zlozono, komunikat).

    ODMOWA ZŁOŻENIA JEST ZAPISYWANA, nie pomijana. Plan, który nie poszedł na
    giełdę, musi mieć w dzienniku powód — inaczej jedyną różnicą między
    „giełda odrzuciła" a „nikt nie próbował" jest cisza.

    CENA LIMITU POCHODZI Z CHWILI WYSYŁKI, NIE Z ZAMKNIĘCIA ŚWIECY (2026-09-26)

    `journal.planned_order.limit_price` to cena zamknięcia świecy decyzyjnej
    (`loop.run_once`: `price = swieca_dec["close"]` → `zaplanuj`). Zlecenie idzie
    na giełdę 71–82 minuty później, bo pętla czeka na komplet cech, a przez te
    godzinę rynek się rusza — zmierzona mediana dryfu 75-minutowego to 0,33% na
    SOL-u wobec 20 pb całej rundy kosztowej. Egzekucja wysyła
    `timeInForce: PostOnly`, więc cena przecinająca spread MUSI zostać
    odrzucona: wszystkie trzy odrzucenia w historii produkcji (55, 57, 58) to
    kupna SOLUSDC z limitem nad askiem, a oba wypełnienia (54, 56) miały limit
    po stronie pasywnej. Separacja zupełna, 3/3 kontra 2/2.

    Dlatego ta funkcja czyta arkusz księgi egzekucji (`arkusz_wysylki`) i cena
    limitu powstaje z NIEGO, po pasywnej stronie spreadu. Decyzja się przy tym
    NIE ZMIENIA — zmienia się cena, po której ją wykonujemy. Wyjątkiem jest
    jawny próg: gdy cena odjechała od ceny decyzyjnej dalej niż 1,5×ATR pary
    (`zlecenia.prog_zwloki`), wejścia NIE składamy i zapisujemy pominięcie.
    """
    wyjscie = plan["trigger"]["rodzaj"] == "wyjscie"
    kupno = plan["side"] == "buy"

    # STAN ZLECENIA NAJPIERW, ARKUSZ POTEM, POST ZARAZ PO ARKUSZU (2026-10-03).
    #
    # Idempotencja wymaga zapytania "czy gielda zna `tai-p{id}`" przed kazdym
    # zlozeniem. Do tej zmiany zadawal je `zloz_zlecenie` w adapterze - czyli
    # PO odczycie arkusza, a PRZED POST-em: dwa GET-y z ponowieniami po 1 s
    # i 2 s staly dokladnie w oknie, w ktorym cena wyliczona jako bid-1 tick
    # przestaje byc pasywna. Kolejnosc odwrocona: pytamy tu, a adapter dostaje
    # `istnienie_sprawdzone=True` i wysyla bez drugiego pytania.
    #
    # ODPOWIEDZ SLUZY TEZ GALEZI ODMOWY nizej (zlecenie zywe mimo odmowy
    # sprawdzenia), ktora dotad pytala o to samo drugi raz.
    #
    # Awaria wymagajaca czlowieka idzie w gore od razu. Zwykly blad odczytu
    # zapamietujemy: galaz odmowy traktuje go jak dotad (odmowa i tak sie
    # zapisuje), a sciezka wysylki NIE wysle bez odpowiedzi - patrz
    # `BladPrzedWyslaniem`.
    istnieje = None
    blad_odczytu: ExchangeError | None = None
    try:
        istnieje = await adapter.stan_zlecenia(id_zlecenia(plan["id"]),
                                               kategoria=kategoria)
    except ExchangeError as e:
        if e.wymaga_czlowieka:
            raise
        blad_odczytu = e

    # CENA ODNIESIENIA Z KSIEGI, W KTOREJ ZLECENIE WYLADUJE.
    #
    # Cena limitu powstaje ze swiec podmiotu GLOBALNEGO - to jego ksiega
    # posluzyla do trenowania modelu. Sprawdzanie jej wzgledem tej samej ksiegi
    # byloby porownaniem liczby z sama soba i przeszloby zawsze, takze dla ticka
    # zepsutego. Rozjazd miedzy ksiegami jest zmierzony i duzy: w 16,8% swiec
    # przekracza cala runde kosztowa.
    #
    # Brak odczytu NIE BLOKUJE zlecenia, ale zostawia slad - inaczej awaria
    # jednego endpointu zatrzymywalaby handel, a to gorsze lekarstwo niz choroba.
    # `ocena_pasma` przy braku odniesienia zwraca None i mowi o tym wprost.
    odniesienie = await adapter.cena_odniesienia(plan["symbol"], kategoria)
    atr = float(plan["atr_pct"]) if plan.get("atr_pct") is not None else None

    # ARKUSZ Z CHWILI WYSYLKI - DRUGI ODCZYT, BO ODPOWIADA NA INNE PYTANIE.
    #
    # `cena_odniesienia` oddaje `lastPrice`, czyli cene ostatniej TRANSAKCJI.
    # Do pasma to wystarcza (pasmo pyta o rzad wielkosci), ale do pytania "po
    # ktorej stronie spreadu stanie nasza cena" - nie: zmierzone na zleceniu 54
    # `lastPrice` lezalo 11 tickow NAD askiem. Bid i ask sa w arkuszu i tylko
    # tam.
    #
    # DWA ODCZYTY, DWIE NIEZALEZNE AWARIE - I TO JEST CELOWE. Padniecie
    # arkusza zostawia nam `lastPrice`, padniecie obu zatrzymuje WEJSCIA
    # (nizej), ale nie wyjscia. Jeden odczyt na wszystko znaczylby, ze awaria
    # jednego endpointu wstrzymuje handel - gorsze lekarstwo niz choroba.
    oferty, skad_ceny, slad_arkusza = await arkusz_wysylki(
        adapter, plan["symbol"], kategoria, datetime.now(timezone.utc))
    # Zegar MONOTONICZNY, nie `datetime.now` - patrz `WIEK_ODCZYTU_PRZY_WYSYLCE_S`.
    odczyt_arkusza = _zegar_wysylki()

    # SLAD ZRODLA CENY WYPELNIA `sprawdz_zlecenie`, bo tam ten wybor zapada.
    # Odtworzenie go tutaj byloby druga kopia regul kolejnosci zrodel.
    slad_ceny: dict = {}
    ilosc, cena, powod = sprawdz_zlecenie(
        Decimal(plan["qty"]), Decimal(plan["limit_price"]), f,
        kupno=kupno, wyjscie=wyjscie,
        cena_odniesienia=odniesienie, atr_pct=atr, najlepsze=oferty,
        slad=slad_ceny)

    # GRANICA, ZA KTORA NIE WCHODZIMY - I SLAD PO NIEJ W DZIENNIKU.
    #
    # Powyzsze podmienilo cene na te z chwili wysylki. Zostaje pytanie, czy po
    # TEJ cenie decyzja nadal cokolwiek popiera - i dotyczy ono wylacznie
    # WEJSCIA. Wyjscia nie wolno zatrzymac zadnym progiem: odmowa zostawia
    # pozycje otwarta wbrew decyzji, czyli kosztuje pieniadze, a nie okazje
    # (patrz naglowek `zlecenia.py`).
    #
    # POWODY SPRAWDZENIA KSZTALTU MAJA PIERWSZENSTWO. Zlecenie, ktorego ilosc
    # znika po zaokragleniu, nie ma ceny do porownania - a komunikat o dryfie
    # zamiast o zerowej ilosci kierowalby szukanie w zla strone.
    #
    # KTORE Z DWOCH POMINIEC ZAPISAC. Wartosc trzyma nazwe powodu, a nie
    # gotowe wywolanie, bo `powod` w `zapisz_pominiecie` MUSI byc literalem:
    # `test_slad_pominiecia` porownuje zbior literalow z kodu ze zbiorem
    # z CHECK-a w migracji, a powod zlozony ze zmiennej sprawdzilby sie
    # dopiero na produkcji - wywracajac caly cykl.
    pominiecie: str | None = None

    # BRAMKA SESJI STOI PRZED BRAMKAMI CENY, BO ODPOWIADA NA WCZESNIEJSZE PYTANIE.
    #
    # Prog wieku arkusza (`WIEK_ARKUSZA_MAX_S`) mierzy, czy odczyt jest SWIEZY.
    # Poza sesja gieldowa to pytanie nie ma dobrej odpowiedzi: arkusz akcji jest
    # wtedy z definicji stary, bo ostatnia oferta pochodzi z zamkniecia - a to
    # nie awaria, tylko rynek, ktory spi.
    #
    # PROG WIEKU SAM NIE WYSTARCZA, I TO JEST ZMIERZONE. `brak_ceny_wysylki`
    # wymaga, zeby zawiodly OBA odczyty ceny, a prog oslania tylko `arkusz_wysylki`.
    # Drugie zrodlo, `adapter.cena_odniesienia`, oddaje goly `Decimal` BEZ
    # znacznika czasu (oba adaptery), wiec jego wieku nie da sie sprawdzic - i poza
    # sesja ten odczyt SIE UDAJE, oddajac cene zamkniecia. Bez tej bramki wejscie
    # idzie wiec na gielde po cenie sprzed godzin, Alpaca kolejkuje zlecenie `day`
    # do nastepnego otwarcia (patrz naglowek `alpaca.py`) i pozycja powstaje
    # nazajutrz przez luke nocna, bez decyzji, ktora by ja popierala. Prog wieku
    # fail-open przez nieoslonione zrodlo zapasowe.
    #
    # DOTYCZY WYLACZNIE WEJSCIA. Wyjscia nie wolno zatrzymac zadnym progiem -
    # odmowa zostawia pozycje otwarta wbrew decyzji, czyli kosztuje pieniadze,
    # a nie okazje. Poza sesja zlecenie `day` czeka do otwarcia i dla wyjscia
    # jest to skutek pozadany.
    #
    # RYNEK CIAGLY NIE PRZECHODZI TU NICZEGO NOWEGO. `stan_sesji` konczy pytanie
    # na `is_24_7` i oddaje `otwarta=True`, wiec tor krypto nie dostaje ani
    # luzniejszego progu, ani jednego dodatkowego zapytania.
    #
    # `sesja is None` ZNACZY "WOLAJACY NIE PYTAL", nie "otwarte". Tak wchodza tu
    # `proba_egzekucji` i testy ksztaltu zlecenia, ktore o rynek nie pytaja
    # i nie musza; petla decyzyjna przekazuje stan zawsze (`loop.run_once`).
    if not powod and not wyjscie and sesja is not None and not sesja.wolno_decydowac:
        if sesja.otwarta is False:
            # TRZY STANY, TRZY MIEJSCA - a nie jeden powod na wszystko.
            # `brak_ceny_wysylki` w dzienniku czyta sie jak problem z gieldą,
            # wiec czujka alarmowalaby co noc na stan NORMALNY i nauczylaby
            # odklikiwac cala kategorie (ten wzorzec opisuje `dozor.py` przy
            # kill-switchu).
            powod = (f"wejście: rynek instrumentu jest ZAMKNIĘTY w chwili "
                     f"wysyłki — {sesja.opis()}. To NIE JEST AWARIĄ giełdy ani "
                     f"brakiem ceny; wejścia po godzinach nie składamy, bo "
                     f"zlecenie `day` czeka wtedy do otwarcia i wypełniłoby się "
                     f"po cenie, której decyzja dla świecy "
                     f"{plan['trigger'].get('bar')} już nie popiera")
            pominiecie = "rynek_zamkniety"
        else:
            # BRAK WIEDZY WSTRZYMUJE WEJSCIE, a nie przepuszcza go - ta sama
            # regula, ktora `sesja.wolno_decydowac` stosuje w petli. Domysl
            # "pewnie otwarte" zamienialby awarie odczytu zegara w zlecenia
            # skladane po godzinach.
            powod = (f"wejście: NIE WIEM, czy rynek instrumentu jest otwarty — "
                     f"{sesja.opis()}. Brak wiedzy wstrzymuje wejście przy "
                     f"operacji nieodwracalnej; to JEST usterka odczytu i "
                     f"wymaga reakcji, w odróżnieniu od rynku zamkniętego")
            pominiecie = "sesja_nieznana"

    if not powod and not wyjscie:
        if oferty is None and odniesienie is None:
            # ZADNEGO ODCZYTU Z CHWILI WYSYLKI. Cena w `plan["limit_price"]` to
            # zamkniecie swiecy sprzed 71-82 minut (zmierzone, patrz
            # `zlecenia.py`), a PostOnly odrzuca cene przecinajaca spread.
            # Wyslanie jej jest wiec zakladem o kierunek rynku przez te
            # godzine, a nie wykonaniem decyzji. NIE WYSYLAMY.
            powod = (f"wejście: brak ceny z chwili wysyłki — {skad_ceny}, "
                     f"a ostatnia cena z księgi egzekucji też nie przyszła; "
                     f"cena z planu {plan['limit_price']} pochodzi z zamknięcia "
                     f"świecy {plan['trigger'].get('bar')} i PostOnly odrzuci ją, "
                     f"jeśli w międzyczasie rynek przeszedł na drugą stronę")
            pominiecie = "brak_ceny_wysylki"
        else:
            zwloka = ocena_zwloki(Decimal(plan["limit_price"]), cena, atr)
            if zwloka is not None and zwloka.poza:
                powod = f"wejście: {zwloka.opis()} [{skad_ceny}]"
                pominiecie = "cena_odjechala"

    if powod:
        # ODMOWA WEJSCIA Z POWODU CENY ZOSTAWIA WIERSZ W `journal.pominiecie`.
        #
        # `exec.order` dostanie ten sam powod nizej, ale odpowiada na pytanie
        # "co sie stalo z tym zleceniem". Pytanie, ktore zadaje sie naprawde,
        # brzmi "czemu dla swiecy 08:00 nie weszlismy w SOLUSDC" - i na nie
        # odpowiada wylacznie tabela kluczowana (instrument, swieca, powod),
        # po ktorej da sie zlaczyc odmowe z decyzja, ktora ja zrodzila
        # (patrz migracja 0041).
        if pominiecie is not None:
            bar = datetime.fromisoformat(plan["trigger"]["bar"])
            if pominiecie == "brak_ceny_wysylki":
                zapisz_pominiecie(cur, engine, mode, plan["instrument_id"], bar,
                                  "brak_ceny_wysylki", powod)
            elif pominiecie == "rynek_zamkniety":
                zapisz_pominiecie(cur, engine, mode, plan["instrument_id"], bar,
                                  "rynek_zamkniety", powod)
            elif pominiecie == "sesja_nieznana":
                zapisz_pominiecie(cur, engine, mode, plan["instrument_id"], bar,
                                  "sesja_nieznana", powod)
            else:
                zapisz_pominiecie(cur, engine, mode, plan["instrument_id"], bar,
                                  "cena_odjechala", powod)
        # NAJPIERW PYTAMY GIELDE, CZY TO ZLECENIE JUZ U NIEJ NIE LEZY.
        #
        # Odmowa przed wysłaniem zapisuje plan jako `rejected`, a wiersz
        # zlecenia jako `rejected` — i od tej chwili nie sięga po nie NIKT:
        # `rozstrzygnij_oczekujace` wybiera plany `pending`, a oba sprzątacze
        # (`posprzataj_zlecenia`, `anuluj_wejscia`) wybierają wyłącznie
        # `status IN ('new','partial','unknown')`.
        #
        # Scenariusz, w którym to kosztuje: cykl N wysyła plan 77 na giełdę
        # (PostOnly leży w arkuszu), proces ginie przed wspólnym `conn.commit()`
        # po pętli `zloz_oczekujace`, więc wiersz `exec.order` nie powstaje.
        # Cykl N+1 widzi plan 77 nadal jako `pending` bez zlecenia i wywołuje
        # `zloz_plan` ponownie — ale sprawdzenie wstępne biegnie PRZED zapytaniem
        # giełdy i tym razem, przy przesuniętej cenie odniesienia albo
        # niespełnionym `min_notional`, plan odrzuca. ŻYWE zlecenie zostaje na
        # giełdzie i nikt po nie nie wróci. Jeśli się wypełni, mamy aktywo bez
        # wiersza `exec.position` i bez decyzji za nim — czyli ekspozycję poza
        # limitami, której kill-switch nie ma jak zdjąć.
        #
        # Odpowiedz mamy juz z poczatku funkcji (`istnieje`). Nieudany odczyt
        # NIE blokuje odmowy: o zleceniu wtedy nic nie wiemy, a zatrzymanie
        # rozstrzygania planu byłoby gorszym lekarstwem niż choroba — wiersz
        # `rejected` i tak zostaje z powodem.
        if istnieje is not None:
            # ZLECENIE ZYJE (albo sie wypelnilo) MIMO ODMOWY SPRAWDZENIA.
            #
            # Plan zostaje `pending`, a wiersz zlecenia powstaje ze stanem
            # z gieldy — od tej chwili widzi je i `rozstrzygnij_oczekujace`
            # (po planie), i `posprzataj_zlecenia` (po statusie). Zapisanie
            # odmowy byloby zapisaniem nieprawdy: sprawdzenie odmowilo
            # WYSLANIA, a wyslane juz jest.
            cur.execute(
                "INSERT INTO exec.order (decision_id, planned_order_id, engine, mode,"
                " instrument_id, venue_order_id, client_order_id, side, order_type,"
                " qty, limit_price, status, reject_reason, submitted_at, raw)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'limit',%s,%s,%s,%s,now(),%s)",
                (plan["decision_id"], plan["id"], engine, mode,
                 plan["instrument_id"], istnieje.id_gieldy, id_zlecenia(plan["id"]),
                 plan["side"], istnieje.ilosc, plan["limit_price"],
                 istnieje.status,
                 f"sprawdzenie przed wysłaniem odmówiłoby ({powod}), ale giełda"
                 f" zna to zlecenie (status {istnieje.status}, wypełnione"
                 f" {istnieje.wypelniono}/{istnieje.ilosc}) — plan zostaje"
                 f" do rozstrzygnięcia, żeby zlecenie nie zostało porzucone",
                 json.dumps({"execution": "gielda",
                             "etap": "odmowa_a_zlecenie_u_gieldy",
                             "powod_odmowy": powod,
                             "status_gieldy": istnieje.status,
                             # `cena=None`: w TYM cyklu nic nie poszlo na
                             # gielde, zlecenie lezy tam z poprzedniego.
                             "arkusz_wysylki": slad_wysylki(
                                 slad_arkusza, slad_ceny, plan, None)})))
            return True, (f"giełda zna to zlecenie mimo odmowy sprawdzenia"
                          f" ({powod}) — plan zostaje do rozstrzygnięcia")

        cur.execute("UPDATE journal.planned_order SET status='rejected',"
                    " resolved_at=now() WHERE id=%s", (plan["id"],))
        cur.execute(
            "INSERT INTO exec.order (decision_id, planned_order_id, engine, mode,"
            " instrument_id, client_order_id, side, order_type, qty, limit_price,"
            " status, reject_reason, submitted_at, raw)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,'limit',%s,%s,'rejected',%s,now(),%s)",
            (plan["decision_id"], plan["id"], engine, mode, plan["instrument_id"],
             id_zlecenia(plan["id"]), plan["side"], plan["qty"], plan["limit_price"],
             powod, json.dumps({"execution": "gielda", "etap": "przed_zlozeniem",
                                "arkusz_wysylki": slad_wysylki(
                                    slad_arkusza, slad_ceny, plan, cena)})))

        # POZYCJA, KTOREJ JUZ NIGDY NIE DA SIE SPRZEDAC, MUSI ZOSTAC DOMKNIETA.
        #
        # Trzecie wcielenie tej samej wady, co domykanie z resztka w
        # `loop.zapisz_wyjscie`: tam resztka zostaje PO sprzedazy, tu cala
        # pozycja jest mniejsza od kroku ilosci jeszcze zanim cokolwiek
        # wyslemy — tak powstaja wejscia `nie_do_zamkniecia`
        # (`egzekucja.co_z_wypelnieniem`). Odmowa przed wysłaniem zostawia
        # wtedy wiersz pozycji otwarty NA ZAWSZE, a `loop.open_position`
        # dalej go oddaje, wiec petla decyzyjna wybiera na tym instrumencie
        # wylacznie `exit` albo `hold` — instrument wypada z handlu, a kazda
        # swieca produkuje kolejne odrzucone zlecenie.
        #
        # KRYTERIUM JEST TRWALE, A NIE CHWILOWE. Domykamy wylacznie wtedy, gdy
        # ilosc znika po zaokragleniu do kroku albo lezy ponizej minimum ilosci
        # — tych dwoch zadna zmiana ceny nie odwroci. Sama `min_notional`
        # (wartosc ponizej minimum) NIE wystarcza: pozycja na kroku, ktora dzis
        # jest warta za malo, jutro po wzroscie ceny sprzeda sie normalnie,
        # a spisanie jej na strate byloby wyrzuceniem zywych pieniedzy.
        # Reguly niekompletne nie domykaja niczego — braku regul nie wolno
        # czytac jako "nie da sie sprzedac".
        #
        # TA SAMA FUNKCJA, CO PRZY RESZTCE PO SPRZEDAZY. Reguła „czego nie da
        # się sprzedać NIGDY" była tu i w `loop.zapisz_wyjscie` napisana dwa
        # razy — i rozjechała się: tamta kopia pytała `czy_zamykalna`, czyli
        # o kryterium CHWILOWE (`min_notional`), i odpowiadała „nie da się"
        # na reguły nieznane.
        if wyjscie and plan["trigger"].get("position_id"):
            if trwale_niesprzedawalna(Decimal(plan["qty"]), f):
                cur.execute(
                    "SELECT id, qty, avg_price FROM exec.position"
                    " WHERE id=%s AND closed_at IS NULL",
                    (plan["trigger"]["position_id"],))
                poz = cur.fetchone()
                if poz and Decimal(poz["qty"]) <= Decimal(plan["qty"]):
                    # PYL SPISUJEMY PO CENIE WEJSCIA — tak samo jak resztke
                    # w `loop.zapisz_wyjscie` i z tego samego powodu: samo
                    # `closed_at` skasowaloby niezrealizowana strate z
                    # `update_equity` i PODNIOSLO kapital w chwili, w ktorej
                    # cos tracimy.
                    strata = Decimal(poz["avg_price"]) * Decimal(poz["qty"])
                    cur.execute(
                        "UPDATE exec.position SET closed_at=now(),"
                        " realized_pnl = COALESCE(realized_pnl,0) - %s,"
                        " close_decision_id=%s WHERE id=%s",
                        (strata, plan["decision_id"], poz["id"]))
                    return False, (
                        f"{powod}; POZYCJA DOMKNIĘTA: {poz['qty']} nie da się"
                        f" sprzedać na tej giełdzie przy kroku {f.krok_ilosci}"
                        f" — resztka zostaje na giełdzie i jest spisana na"
                        f" stratę po cenie wejścia {poz['avg_price']},"
                        f" instrument wraca do handlu")
        return False, powod

    if blad_odczytu is not None:
        # BEZ ODPOWIEDZI O STANIE NIE WYSYLAMY. Zlecenie o tym id moze juz lezec
        # u gieldy (proces padl po POST-cie, a przed zapisem wiersza), a jedyna
        # ochrona przed drugim jest to pytanie. Wiersza NIE zapisujemy - patrz
        # `BladPrzedWyslaniem`.
        raise BladPrzedWyslaniem(blad_odczytu)

    if istnieje is not None:
        # GIELDA JUZ ZNA TO ZLECENIE - ta sama odpowiedz, ktora dotad dawal
        # `zloz_zlecenie` jako `juz_istnialo=True`, z tym samym zapisem nizej.
        z = ZlozoneZlecenie(id_wlasne=id_zlecenia(plan["id"]),
                            id_gieldy=istnieje.id_gieldy, status=istnieje.status,
                            juz_istnialo=True)
    else:
        wiek_odczytu = _zegar_wysylki() - odczyt_arkusza
        slad_arkusza = {**slad_arkusza, "wiek_przy_wysylce_s": round(wiek_odczytu, 3)}
        if wiek_odczytu > WIEK_ODCZYTU_PRZY_WYSYLCE_S:
            # Bez wiersza - plan wraca w nastepnym cyklu ze swiezym odczytem.
            # Cena z odczytu sprzed kilku sekund to dokladnie odrzucenie
            # PostOnly, ktorego ta kolejnosc ma unikac.
            return False, (f"odczyt arkusza ma {wiek_odczytu:.1f} s naszego "
                           f"czasu (granica {WIEK_ODCZYTU_PRZY_WYSYLCE_S:.0f} s) "
                           f"— NIE wysyłam, plan wróci w następnym cyklu")
        z = None

    # ZEGAR WYSYLKI I DRUGI ODCZYT KSIEGI - wylacznie gdy POST naprawde idzie.
    # Diagnostyka odrzucen PostOnly, patrz `arkusz_po_wysylce`. Cena i logika
    # zlecenia sa juz ustalone; nic ponizej ich nie zmienia.
    wysylamy = z is None
    try:
        if z is None:
            slad_arkusza = {**slad_arkusza,
                            "wyslano_at": datetime.now(timezone.utc).isoformat()}
            z = await adapter.zloz_zlecenie(
                symbol=plan["symbol"], strona=plan["side"],
                ilosc=ilosc, cena_limitu=cena, id_wlasne=id_zlecenia(plan["id"]),
                kategoria=kategoria, istnienie_sprawdzone=True)
            slad_arkusza = {**slad_arkusza,
                            "odpowiedz_at": datetime.now(timezone.utc).isoformat()}
    except OdmowaGieldy as e:
        slad_arkusza = {**slad_arkusza,
                        "odpowiedz_at": datetime.now(timezone.utc).isoformat(),
                        "po_wysylce": await arkusz_po_wysylce(
                            adapter, plan["symbol"], kategoria)}
        # GIELDA ODPOWIEDZIALA „NIE" — to jest wiedza, nie jej brak.
        # Cala obsluga jest nizej, w `odmowa_gieldy`; tutaj tylko rozdzielamy
        # odmowe od braku odpowiedzi, bo do 2026-09-24 obie wpadaly w ten sam
        # `except` i konczyly sie zapisem `status='unknown'`.
        return await odmowa_gieldy(
            cur, adapter, plan, f, e, ilosc=ilosc, cena=cena, engine=engine,
            mode=mode, kategoria=kategoria, wyjscie=wyjscie, kupno=kupno,
            odniesienie=odniesienie, atr=atr,
            slad_arkusza=slad_arkusza, slad_ceny=slad_ceny)
    except ExchangeError as e:
        # MARTWY KLUCZ, ZLE IP, ZAMROZONE KONTO - W GORE, NIE DO `unknown`.
        # Odpowiedz dotarla, wiec zlecenia nie ma; ale reakcja to czlowiek,
        # a nie zapis i spokoj. Do 2026-10-03 te bledy konczyly sie tu
        # wierszem `unknown` i cisza - ta sama klasa awarii, co 170131 przed
        # 2026-09-24.
        if e.wymaga_czlowieka:
            raise
        # NIE OZNACZAMY PLANU JAKO ODRZUCONEGO. Tu trafia juz WYLACZNIE stan
        # naprawde nieznany - timeout, blad sieci, odpowiedz nie-JSON albo
        # `retCode` 10000/10016 (`bybit.KODY_STAN_NIEZNANY`); kazdy inny kod
        # jest odmowa (`bybit._blad_zlecenia`) i idzie galezia wyzej. Zlecenie
        # moglo wejsc; `ponow_nieznane` w nastepnym cyklu zapyta gielde po
        # naszym identyfikatorze i - jesli go nie zna, a plan jest wazny -
        # zlozy je ponownie pod TYM SAMYM id.
        cur.execute(
            "INSERT INTO exec.order (decision_id, planned_order_id, engine, mode,"
            " instrument_id, client_order_id, side, order_type, qty, limit_price,"
            " status, reject_reason, submitted_at, raw)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,'limit',%s,%s,'unknown',%s,now(),%s)",
            (plan["decision_id"], plan["id"], engine, mode, plan["instrument_id"],
             id_zlecenia(plan["id"]), plan["side"], ilosc, cena, str(e),
             json.dumps({"execution": "gielda", "etap": "odpowiedz_nie_dotarla",
                         "arkusz_wysylki": slad_wysylki(
                             slad_arkusza, slad_ceny, plan, cena)})))
        return False, f"odpowiedź nie dotarła ({e}) — stan sprawdzimy po naszym id"

    if wysylamy:
        slad_arkusza = {**slad_arkusza, "po_wysylce": await arkusz_po_wysylce(
            adapter, plan["symbol"], kategoria)}

    cur.execute(
        "INSERT INTO exec.order (decision_id, planned_order_id, engine, mode,"
        " instrument_id, venue_order_id, client_order_id, side, order_type, qty,"
        " limit_price, status, submitted_at, raw)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'limit',%s,%s,%s,now(),%s)",
        (plan["decision_id"], plan["id"], engine, mode, plan["instrument_id"],
         z.id_gieldy, z.id_wlasne, plan["side"], ilosc, cena, z.status,
         json.dumps({"execution": "gielda", "juz_istnialo": z.juz_istnialo,
                     "arkusz_wysylki": slad_wysylki(
                         slad_arkusza, slad_ceny, plan, cena)})))
    return True, ("odnalezione u giełdy (idempotencja)" if z.juz_istnialo
                  else f"złożone {ilosc} @ {cena}")


async def odmowa_gieldy(cur, adapter, plan: dict, f: Filtry, blad: OdmowaGieldy, *,
                        ilosc: Decimal, cena: Decimal, engine: str, mode: str,
                        kategoria: str, wyjscie: bool, kupno: bool,
                        odniesienie: Decimal | None, atr: float | None,
                        slad_arkusza: dict | None = None,
                        slad_ceny: dict | None = None) -> tuple[bool, str]:
    """Giełda ODMÓWIŁA przyjęcia zlecenia. Zapisuje to jako fakt, nie jako niewiedzę.

    DLACZEGO TO JEST OSOBNA ŚCIEŻKA, A NIE GAŁĄŹ W `except ExchangeError`

    Do 2026-09-24 każda odpowiedź niebędąca sukcesem kończyła się zapisem
    `status='unknown'` z etapem `odpowiedz_nie_dotarla`. Dla przekroczenia czasu
    to jest jedyna uczciwa odpowiedź — zlecenie mogło wejść. Dla `retCode`
    z powodem odmowy jest to zapisanie w bazie NIEPRAWDY o stanie, który akurat
    znamy dokładnie, i właśnie ta nieprawda blokowała wyjście przez 19 godzin:

        SOLUSDC, pozycja 32, zlecenia 49 / 51 / 52 / 53
        2026-09-23 17:18:52 .. 2026-09-24 05:15:52, za każdym razem qty 12.138
        „Insufficient balance. (kod 170131)"  ->  status='unknown'

    Stan „nieznany" nie może niczego przerwać, bo nie jest rozstrzygnięciem:
    każda świeca widziała pozycję nadal otwartą, planowała wyjście od nowa
    i dostawała tę samą odmowę. Licznik odrzuceń czujki egzekucji nie drgnął
    ani razu, bo `status='rejected'` nie został zapisany ANI RAZU w całej
    historii produkcji.

    CO ROBI TA FUNKCJA PRZY ODMOWIE ZA SALDO — I CZEGO CELOWO NIE ROBI

    Odmowa za saldo jest jedyną, przy której giełda kwestionuje LICZBĘ Z NASZYCH
    KSIĄG. Pytamy więc o stan rachunku i składamy to, co giełda potwierdza —
    zamiast powtarzać tę samą ilość co świecę. Wynik zmierzony na tej awarii:
    księgi mówią 12.138 SOL, rachunek ma 12.125862 (Bybit pobrał prowizję kupna
    W KUPIONYM AKTYWIE), więc sprzedaż całości musi padać, a sprzedaż 12.125
    przechodzi normalnie — brakuje dokładnie prowizji, nie płynności.

    RÓŻNICA NIE JEST PRZY TYM ZAMIATANA. `exec.position.qty` ZOSTAJE NIETKNIĘTE,
    a rozjazd idzie do `reject_reason` i do `raw.rozjazd_ksiag` z obiema
    liczbami. Skorygowanie ilości „przy okazji" byłoby dopasowaniem ksiąg do
    giełdy bez śladu — czyli zamienieniem awarii widocznej w awarię cichą,
    dokładnie tak jak zrobiła to korekta z 2026-09-24 07:45, która poprawiła
    jednostkę prowizji i zostawiła ilość brutto, nie zapisawszy ani jednej linii
    w logu. Ilość naprawia człowiek albo uzgodnienie; egzekucja ma ją POKAZAĆ.

    JEDEN WIERSZ `exec.order` NA JEDEN `client_order_id`. Baza ma UNIQUE
    (mode, client_order_id), więc nie wolno zapisać najpierw odmowy, a potem
    zlecenia zastępczego: to byłby drugi INSERT z tym samym identyfikatorem
    i wywróciłby całą transakcję cyklu. Najpierw więc próbujemy złożyć mniejszą
    ilość, a dopiero potem zapisujemy JEDEN wiersz opisujący to, co naprawdę
    się stało.
    """
    id_w = id_zlecenia(plan["id"])
    slad_arkusza = {} if slad_arkusza is None else slad_arkusza
    slad_ceny = {} if slad_ceny is None else slad_ceny
    meta = {"execution": "gielda", "etap": "odmowa_gieldy",
            "rodzaj_odmowy": blad.rodzaj, "kod_gieldy": blad.code,
            "ilosc_z_ksiag": str(ilosc)}
    powod = str(blad)
    zastepcze = None            # ZlozoneZlecenie, gdy udalo sie zlozyc mniej

    if wyjscie and blad.brak_salda:
        potwierdzone = await potwierdzone_saldo(adapter, plan.get("base_ccy"))
        if potwierdzone is None:
            # NIEWIEDZA NAZWANA NIEWIEDZA. Host demo podmiotu unijnego nie
            # wystawia sald (404), wiec to nie jest przypadek brzegowy, tylko
            # stan codzienny — i jedyne, co wtedy wolno zrobic, to zapisac
            # odmowe z prawdziwym powodem i zostawic ja widoczna dla nadzoru.
            meta["saldo_gieldy"] = "nie_do_odczytania"
            powod += (" | STANU RACHUNKU NIE DA SIE ODCZYTAC na tym hoscie,"
                      " wiec nie wiemy, o ile ksiegi sie rozjechaly")
        elif potwierdzone >= ilosc:
            # Gielda potwierdza tyle, ile chcemy sprzedac — wiec odmowa NIE
            # dotyczy naszej ilosci (srodki moga byc zablokowane innym
            # zleceniem). Zgadywanie mniejszej liczby byloby tu sprzedaniem
            # pozycji bez powodu.
            meta["saldo_gieldy"] = str(potwierdzone)
            powod += (f" | rachunek potwierdza {potwierdzone}, czyli NIE MNIEJ"
                      f" niz {ilosc} — odmowa nie dotyczy naszej ilosci")
        else:
            meta["saldo_gieldy"] = str(potwierdzone)
            meta["rozjazd_ksiag"] = {
                "ksiegi": str(ilosc), "gielda": str(potwierdzone),
                "roznica": str(ilosc - potwierdzone),
                "instrument": plan["symbol"],
                "position_id": plan["trigger"].get("position_id"),
            }
            rozjazd = (
                f"ROZJAZD KSIĄG: wyjście prosiło o {ilosc} {plan.get('base_ccy')}, "
                f"a giełda potwierdza {potwierdzone} (różnica "
                f"{ilosc - potwierdzone}). Księgi mówią WIĘCEJ niż rachunek, "
                f"więc sprzedaż całości nie przejdzie NIGDY. `exec.position.qty` "
                f"ZOSTAJE bez zmian — różnica ma zostać wyjaśniona, a nie "
                f"domknięta sprzedażą reszty")
            powod += " | " + rozjazd
            # Ta sama funkcja, co przy pierwszym podejsciu — reguly gieldy
            # sprawdzamy dla NOWEJ ilosci, bo nizsza ilosc potrafi wpasc pod
            # `min_notional` albo minimum ilosci.
            # `slad=slad_ceny` NADPISUJE zrodlo ceny, i to jest celowe: ta
            # proba idzie BEZ `najlepsze`, czyli po cenie z `cena_odniesienia`.
            # Zostawienie tu sladu z pierwszego podejscia opisywaloby arkuszem
            # cene, ktora z arkusza nie wyszla.
            ilosc2, cena2, powod2 = sprawdz_zlecenie(
                potwierdzone, Decimal(plan["limit_price"]), f,
                kupno=kupno, wyjscie=True,
                cena_odniesienia=odniesienie, atr_pct=atr, slad=slad_ceny)
            if powod2:
                meta["zastepcze"] = f"niemozliwe ({powod2})"
                powod += (f" | ilości potwierdzonej też nie da się wysłać:"
                          f" {powod2}")
            else:
                try:
                    zastepcze = await adapter.zloz_zlecenie(
                        symbol=plan["symbol"], strona=plan["side"],
                        ilosc=ilosc2, cena_limitu=cena2, id_wlasne=id_w,
                        kategoria=kategoria)
                    ilosc, cena = ilosc2, cena2
                    meta["zastepcze"] = "zlozone"
                    meta["ilosc_wyslana"] = str(ilosc2)
                except ExchangeError as e2:
                    # Druga proba tez padla. NIE zapisujemy jej jako `unknown`
                    # nawet przy bledzie nieokreslonym: pierwsza odpowiedz byla
                    # jednoznaczna, a ta jest dopiskiem do niej.
                    meta["zastepcze"] = f"nie_powiodlo_sie ({e2})"
                    powod += f" | próba na ilość potwierdzoną też padła ({e2})"

    if zastepcze is not None:
        # ZLECENIE JEST NA GIELDZIE. Plan zostaje `pending`, zeby
        # `rozstrzygnij_oczekujace` zapytalo o nie po naszym identyfikatorze —
        # dokladnie tak jak przy zlozeniu bez odmowy.
        cur.execute(
            "INSERT INTO exec.order (decision_id, planned_order_id, engine, mode,"
            " instrument_id, venue_order_id, client_order_id, side, order_type, qty,"
            " limit_price, status, reject_reason, submitted_at, raw)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'limit',%s,%s,%s,%s,now(),%s)",
            (plan["decision_id"], plan["id"], engine, mode, plan["instrument_id"],
             zastepcze.id_gieldy, id_w, plan["side"], ilosc, cena,
             zastepcze.status, powod,
             json.dumps({**meta, "arkusz_wysylki": slad_wysylki(
                 slad_arkusza, slad_ceny, plan, cena)})))
        return True, (f"giełda odmówiła ({blad}); złożone {ilosc} @ {cena} —"
                      f" tyle, ile giełda potwierdza. " + powod)

    cur.execute("UPDATE journal.planned_order SET status='rejected',"
                " resolved_at=now() WHERE id=%s", (plan["id"],))
    cur.execute(
        "INSERT INTO exec.order (decision_id, planned_order_id, engine, mode,"
        " instrument_id, client_order_id, side, order_type, qty, limit_price,"
        " status, reject_reason, submitted_at, raw)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,'limit',%s,%s,'rejected',%s,now(),%s)",
        (plan["decision_id"], plan["id"], engine, mode, plan["instrument_id"],
         id_w, plan["side"], ilosc, cena, powod,
         json.dumps({**meta, "arkusz_wysylki": slad_wysylki(
             slad_arkusza, slad_ceny, plan, cena)})))
    return False, f"giełda ODRZUCIŁA zlecenie: {powod}"


def _iso_z_ms(ms) -> str | None:
    """Znacznik giełdy (ms) jako ISO - diagnostyka, więc NIGDY nie rzuca.

    Absurdalna wartość (przepełnienie, ujemna, nie-liczba) nie może wywrócić
    rozstrzygnięcia zlecenia: wtedy None, a surowy stan i tak jest w `raw`.
    """
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError, TypeError):
        return None


async def rozstrzygnij_plan(cur, adapter, plan: dict, f: Filtry, *, engine: str,
                            mode: str, kategoria: str, teraz: datetime,
                            kind: str) -> tuple[str, str]:
    """Pyta giełdę o los planu i zapisuje wynik. Zwraca (rodzaj, komunikat).

    Rodzaj to zwykle wynik `co_z_wypelnieniem` ('pelne', 'czesciowe', 'brak',
    'nie_do_zamkniecia'), ale dwa przypadki mają własne nazwy, bo NIE SĄ
    rozstrzygnięciem planu i muszą być widoczne w statystyce cyklu:

      * 'nie_zdjete'          — nic nie weszło, ale zlecenia nie udało się
                                anulować; zostaje w arkuszu giełdy, więc plan
                                zostaje `pending` i wracamy do niego w następnym
                                cyklu,
      * 'wyjscie_bez_pozycji' — sprzedaż wykonała się na giełdzie, a w bazie nie
                                ma otwartej pozycji, którą miała zamknąć.
    """
    id_w = id_zlecenia(plan["id"])
    stan = await adapter.stan_zlecenia(id_w, kategoria=kategoria)

    if stan is None:
        # GIELDA "NIE ZNA" ZLECENIA, KTOREMU SAMA NADALA NUMER = NIE WIEM.
        #
        # Wygaszamy wylacznie plan, ktorego zlecenie nie mialo kontaktu z gielda
        # (brak wiersza albo wiersz bez `venue_order_id`): wtedy "nie zna"
        # znaczy "nie weszlo" i to jest jedyna bezpieczna interpretacja.
        #
        # Wiersz z `venue_order_id` to zlecenie, ktore gielda PRZYJELA. Jej
        # "nie ma" jest wtedy luka w odczycie, nie faktem: `/v5/order/realtime`
        # bez `openOnly` oddaje tylko otwarte, a `/v5/order/history` "may
        # delay" - zlecenie wypelnione chwile przed odczytem znika z obu. Do
        # 2026-10-03 plan dostawal tu `expired`, a wiersz zostawal `new`;
        # `posprzataj_zlecenia` widzialo potem `filled` i meldowalo "pozycja
        # poza dziennikiem", ale NIE ksiegowalo - na gieldzie pozycja, w
        # dzienniku nic, a risk kernel liczyl zanizona ekspozycje (audyt E3).
        #
        # Plan zostaje wiec `pending` i wraca tu w nastepnym cyklu. Gdyby luka
        # trwala, `dozor.sprawdz_egzekucje` alarmuje o planach `pending` po
        # terminie waznosci - niewiedza zostaje widoczna, zamiast zamienic sie
        # w zgubiona pozycje.
        cur.execute("SELECT id, venue_order_id FROM exec.order"
                    " WHERE planned_order_id = %s ORDER BY id LIMIT 1", (plan["id"],))
        wiersz = cur.fetchone()
        if wiersz and wiersz.get("venue_order_id"):
            return "nie_wiem", (f"giełda nie oddała zlecenia {wiersz['venue_order_id']},"
                                f" które sama przyjęła — NIE WIEM, co się z nim stało;"
                                f" plan zostaje do rozstrzygnięcia w następnym cyklu")
        cur.execute("UPDATE journal.planned_order SET status='expired',"
                    " resolved_at=%s WHERE id=%s", (teraz, plan["id"]))
        return "brak", "giełda nie zna tego zlecenia — plan wygasa"

    # WIERSZ ZLECENIA JUZ ISTNIEJE - powstal przy zlozeniu. Bez tego odczytu
    # `zapisz_wejscie` zalozylby drugi i jeden plan mialby w exec.order dwa
    # zlecenia: tabela przestalaby znaczyc "jedno zlecenie", a czujka zdrowia
    # egzekucji widzialaby dwa razy wiecej przyjec, niz bylo.
    # `submitted_at` SLUZY DO ZAWEZENIA REJESTRU WYKONAN, a nie do ksiegowania.
    # Gielda nie moze wypelnic zlecenia, zanim je do niej wyslemy, wiec to jest
    # dokladna dolna granica okna w `prowizja_z_rejestru` — bez niej pytanie
    # o wykonania sciagaloby caly dostepny rejestr strona po stronie.
    cur.execute("SELECT id, qty, submitted_at FROM exec.order"
                " WHERE planned_order_id = %s ORDER BY id LIMIT 1", (plan["id"],))
    wiersz = cur.fetchone()
    id_zlecenia_w_bazie = wiersz["id"] if wiersz else None

    # ULAMEK LICZYMY OD ILOSCI FAKTYCZNIE WYSLANEJ, nie od ilosci z planu.
    #
    # Plan trzyma ilosc SPRZED zaokraglenia do kroku gieldy, a na gielde poszla
    # ilosc zaokraglona w dol. Gielda nigdy nie odda wiecej, niz zamowiono, wiec
    # warunek "wypelniono >= planowano" nie bylby spelniony NIGDY - kazde pelne
    # wypelnienie zapisywaloby sie jako czesciowe.
    #
    # Skutki tego byly trzy i kazdy inny: zbedne anulowanie przy kazdym wejsciu,
    # wyjscie z calej pozycji zostawiajace pylek (bo zapisz_wyjscie dostawaloby
    # ilosc mniejsza niz qty i nie zamykalo wiersza), oraz systematycznie
    # falszywy `ulamek_wypelnienia` - czyli jedyna deklarowana miara rozjazdu
    # backtestu z produkcja.
    wyslano = Decimal(wiersz["qty"]) if wiersz else Decimal(plan["qty"])

    r = co_z_wypelnieniem(stan.wypelniono, wyslano,
                          Decimal(plan["limit_price"]), f)

    # NIEUDANE ANULOWANIE ZAPAMIETUJEMY, bo decyduje o losie planu nizej.
    # Sam dopisek do powodu trafia tylko na ekran, a ekran nikogo nie obudzi.
    anulowanie_padlo: ExchangeError | None = None
    anulowano = False
    if r.anuluj_reszte and stan.status not in ("filled", "cancelled", "rejected"):
        try:
            await adapter.anuluj_zlecenie(symbol=plan["symbol"], id_wlasne=id_w,
                                          kategoria=kategoria)
            anulowano = True
        except ExchangeError as e:
            # Nieudane anulowanie NIE UNIEWAZNIA reszty zapisu. Wypelnienie, ktore
            # juz weszlo, JEST NASZE - udawanie, ze rozstrzygniecia nie bylo,
            # zgubiloby pozycje, ktora naprawde mamy.
            anulowanie_padlo = e
            r = type(r)(r.rodzaj, r.ilosc, r.anuluj_reszte, r.ulamek,
                        r.powod + f"; UWAGA: anulowanie reszty nie powiodło się ({e})")

    meta = {"execution": "gielda", "kind": kind, "order": "limit",
            "ulamek_wypelnienia": str(r.ulamek), "rodzaj": r.rodzaj,
            "status_gieldy": stan.status, "venue_order_id": stan.id_gieldy}
    # ZEGAR GIELDY - przy KAZDYM rozstrzygnieciu, nie tylko przy odrzuceniu
    # (grupa odniesienia, jak w `slad_wysylki`). Z tej samej odpowiedzi
    # `stan_zlecenia`, bez nowego zapytania. `getattr`, bo atrapy adapterow
    # w testach i starsze implementacje moga tych pol nie miec.
    utw, akt = getattr(stan, "utworzono_ms", None), getattr(stan, "zaktualizowano_ms", None)
    if utw is not None or akt is not None:
        meta["czas_gieldy"] = {
            "utworzono_at": _iso_z_ms(utw),
            "zaktualizowano_at": _iso_z_ms(akt),
            "ms_od_przyjecia": (akt - utw) if utw is not None and akt is not None else None,
        }

    # POWOD ODMOWY GIELDY: SUROWY OBOK NASZEJ INTERPRETACJI.
    #
    # Do 2026-09-26 zlecenie odrzucone nie zapisywalo powodu ANI RAZU:
    # `exec.order.reject_reason` byl NULL na wszystkich trzech odrzuceniach
    # w historii produkcji (55, 57, 58), a `raw` mowil tylko `"rodzaj": "brak"`
    # i `"status_gieldy": "rejected"` — czyli "nie weszlo, giełda odmowila",
    # bez jednego slowa o tym, DLACZEGO. Trzy identyczne wiersze, zero wiedzy.
    #
    # SUROWA WARTOSC ZOSTAJE W `raw` NAWET GDY NASZE MAPOWANIE JEST DOBRE.
    # Interpretacja moze sie pomylic albo zmienic — wtedy jedynym punktem
    # zaczepienia jest to, co gielda POWIEDZIALA, a nie to, co z tego
    # zrozumielismy. Dlatego `powod_odmowy_gieldy` trzyma kod doslownie,
    # `powod_odmowy_rodzaj` naszą kategorię (albo 'nierozpoznane'), i zadne
    # z nich nie zastepuje drugiego.
    odmowa = stan.powod_odmowy
    powod_odrzucenia: str | None = None
    if odmowa is not None:
        meta["powod_odmowy_gieldy"] = odmowa.kod
        meta["powod_odmowy_rodzaj"] = odmowa.rodzaj
        powod_odrzucenia = odmowa.opis
    elif stan.status == "rejected":
        # NIEWIEDZA NAZWANA NIEWIEDZA, a nie NULL. Kolumna pusta nie odrozni
        # "gielda nie podala powodu" od "nie zapytalismy o powod" — a to sa dwa
        # rozne zadania do zrobienia i tylko jedno z nich jest po naszej stronie.
        meta["powod_odmowy_gieldy"] = None
        meta["powod_odmowy_rodzaj"] = "nie_podany"
        powod_odrzucenia = ("giełda odrzuciła zlecenie i NIE PODAŁA powodu "
                            "(brak `rejectReason` w odpowiedzi o stan zlecenia)")

    if r.rodzaj == "brak":
        if anulowanie_padlo is not None:
            # PLAN NIE WYGASA, DOPOKI ZLECENIE LEZY W ARKUSZU.
            #
            # Nic nie weszlo, wiec nie ma czego ksiegowac - ale zlecenie NADAL
            # JEST NA GIELDZIE i nadal moze sie wypelnic po cenie, ktorej zadna
            # decyzja juz nie popiera. Wygaszenie planu w tym miejscu zrywaloby
            # jedyna nitke prowadzaca do tego zlecenia: rozstrzyganie szuka
            # planow o statusie 'pending', wiec plan 'expired' nie zostalby juz
            # nigdy dotkniety, a jedyne inne miejsce ponawiajace anulowanie
            # (`anuluj_wejscia`) rusza wylacznie po kill-switchu.
            #
            # Skutek byl kumulatywny, a nie jednorazowy: nastepna swieca widzi
            # brak pozycji, sygnal wciaz trzyma, wiec powstaje NOWY plan i NOWE
            # zlecenie - i tak co swiece. Skok ceny wypelnia wszystkie naraz,
            # czyli dokladnie ta nadmiarowa ekspozycja, ktorej risk kernel nie
            # policzyl, bo w dzienniku nie bylo ani jednej pozycji.
            #
            # Zostawiony 'pending' plan wraca tu w kazdym cyklu i PONAWIA
            # anulowanie az do skutku. Gdyby giełda nie dala go zdjac przez
            # dluzszy czas, dozor zglosi to sam: `sprawdz_egzekucje` alarmuje
            # o planach 'pending' po terminie ich waznosci.
            cur.execute(
                "UPDATE exec.order SET status=%s, updated_at=%s, reject_reason=%s,"
                " raw = raw || %s WHERE planned_order_id=%s",
                (stan.status, teraz,
                 # POWOD ODMOWY DOPISANY, A NIE ZASTAPIONY. Nieudane anulowanie
                 # i powod odrzucenia to dwa osobne fakty o tym samym zleceniu;
                 # zapisanie tylko jednego z nich kasuje drugi bezpowrotnie, bo
                 # kolumna jest jedna.
                 f"anulowanie przy rozstrzygnięciu nie powiodło się: {anulowanie_padlo}"
                 + (f" | {powod_odrzucenia}" if powod_odrzucenia else ""),
                 json.dumps(dict(meta, anulowanie="nie_powiodlo_sie",
                                 plan="pozostaje pending — ponowimy w następnym cyklu")),
                 plan["id"]))
            return "nie_zdjete", (r.powod + " — ZLECENIE ZOSTAJE W ARKUSZU GIEŁDY,"
                                  " plan pozostaje do rozstrzygnięcia i anulowanie"
                                  " zostanie ponowione")

        cur.execute("UPDATE journal.planned_order SET status='expired',"
                    " resolved_at=%s WHERE id=%s", (teraz, plan["id"]))
        # WIERSZ ZLECENIA TEZ ZAMYKAMY. Zostawiony na 'new' opisywalby zlecenie
        # otwarte, ktorego juz nie ma: kill-switch probowalby je co raz zdejmowac,
        # a czujka zdrowia egzekucji liczylaby je jako przyjete i wiszace.
        cur.execute(
            "UPDATE exec.order SET status=%s, updated_at=%s,"
            # `COALESCE`, zeby zapis bez powodu (zwykle wygasniecie limitu,
            # ktore anulowalismy sami) nie kasowal powodu wpisanego wczesniej
            # np. przez kill-switch albo sprzatanie.
            " reject_reason = COALESCE(%s, reject_reason), raw = raw || %s"
            " WHERE planned_order_id=%s",
            (stan.status if stan.status in ("cancelled", "rejected") else "cancelled",
             teraz, powod_odrzucenia,
             json.dumps(dict(meta, anulowanie="zdjete" if anulowano
                                    else f"niepotrzebne (giełda: {stan.status})")),
             plan["id"]))
        return r.rodzaj, (r.powod + (f" — {powod_odrzucenia}"
                                     if powod_odrzucenia else ""))

    cena = stan.srednia_cena or Decimal(plan["limit_price"])
    prowizja = stan.prowizja
    prowizja_w = stan.prowizja_w
    zrodlo_waluty = "zlecenie"
    uwaga_prowizji = None

    # WALUTA PROWIZJI Z REJESTRU WYKONAN, GDY ZLECENIE JEJ NIE PODALO.
    #
    # To nie jest zabezpieczenie na wszelki wypadek — tak zachowuje sie Bybit
    # ZAWSZE: `feeCurrency` jest wylacznie w `/v5/execution/list`, a
    # `stan_zlecenia` czyta `/v5/order/*`. Na produkcji `prowizja_w` bylo puste
    # na WSZYSTKICH trzech kupnach gieldowych, wiec podstawiana byla waluta
    # kwotowana i `rozlicz_prowizje` nigdy nie odejmowalo prowizji od ilosci.
    # Pelny rachunek skutkow: docstring `prowizja_z_rejestru`.
    #
    # Pytamy TYLKO wtedy, gdy cos weszlo i gdy zlecenie waluty nie podalo —
    # jedno dodatkowe zapytanie na rozstrzygniecie z wypelnieniem.
    if prowizja_w is None and stan.wypelniono > 0:
        try:
            kwota, waluta, uwaga_prowizji = await prowizja_z_rejestru(
                adapter, symbol=plan["symbol"], id_wlasne=id_w,
                submitted_at=wiersz["submitted_at"] if wiersz else None,
                kategoria=kategoria)
            if waluta is not None:
                prowizja, prowizja_w = kwota, waluta
                zrodlo_waluty = "rejestr_wykonan"
        except ExchangeError as e:
            # NIEUDANY ODCZYT REJESTRU NIE UNIEWAZNIA ROZSTRZYGNIECIA.
            # Wypelnienie juz zaszlo i aktywo jest nasze niezaleznie od tego,
            # czy poznamy jednostke prowizji. Zapisujemy to, co wiemy, i mowimy
            # glosno, czego nie wiemy — cisza w tym miejscu wygladalaby
            # identycznie jak poprawny odczyt.
            uwaga_prowizji = (f"nie udalo sie odczytac waluty prowizji "
                              f"z rejestru wykonan ({e})")

    if uwaga_prowizji:
        zrodlo_waluty = "nieznane"
        print(f"  UWAGA: {plan['symbol']} — {uwaga_prowizji}; koszt wejścia "
              f"zostanie zapisany w walucie kwotowanej {plan['quote_ccy']}, "
              f"a jeśli giełda pobrała go w aktywie bazowym, jest to "
              f"ZANIŻENIE o około dwa rzędy wielkości")

    # Brak waluty po obu odczytach zostaje przy dotychczasowym domysle: waluta
    # kwotowana. Domysl jest ZLY, ale jest jedynym, ktory pozwala zsumowac
    # koszt — i teraz zostawia po sobie slad w `raw` oraz linie na ekranie.
    prowizja_ccy = prowizja_w or plan["quote_ccy"]

    # ILE NAPRAWDE MAMY I ILE TO KOSZTOWALO. Na spocie kupno placi prowizje
    # w kupionym aktywie, wiec zlecenie na 1,0 daje nam 0,999. Zapis brutto
    # tworzy pozycje NIE DO SPRZEDANIA: wyjscie prosi o calosc, gielda odrzuca
    # za brak salda, plan wygasa i nastepna swieca powtarza to samo bez konca.
    ilosc_netto, prowizja_kwot = rozlicz_prowizje(
        r.ilosc, cena, prowizja, prowizja_w,
        plan.get("base_ccy") or "", plan["quote_ccy"])
    # SKAD WZIELA SIE JEDNOSTKA PROWIZJI, ZAPISANE PRZY ZLECENIU. Bez tego
    # `fee_ccy` = USDC z odczytu i `fee_ccy` = USDC z domyslu wygladaja w bazie
    # identycznie — a to jest roznica miedzy pomiarem a zgadnieciem, i to ona
    # decyduje, czy `exec.position.qty` jest netto, czy brutto.
    meta["prowizja_ccy_zrodlo"] = zrodlo_waluty
    if uwaga_prowizji:
        meta["prowizja_ccy_uwaga"] = uwaga_prowizji

    uwaga = None
    rodzaj_zwrotny, powod_zwrotny = r.rodzaj, r.powod

    if plan["trigger"]["rodzaj"] == "wyjscie":
        cur.execute("SELECT id, side, qty, avg_price FROM exec.position"
                    " WHERE id=%s AND closed_at IS NULL",
                    (plan["trigger"].get("position_id"),))
        poz = cur.fetchone()
        if poz:
            # WIERSZ ZLECENIA JUZ ISTNIEJE - powstal przy zlozeniu wyjscia na
            # gielde, dokladnie tak jak przy wejsciu. Podajemy go, zeby jedno
            # wyjscie nie zostawilo w `exec.order` dwoch zlecen; wypelnienie
            # dopisuje sie do niego.
            #
            # PROWIZJA IDZIE SUROWA, razem z waluta z gieldy. Wejscie przelicza
            # ja przez `rozlicz_prowizje`, bo kupno na spocie placi prowizje
            # w KUPIONYM aktywie i musi ona zejsc z ilosci, ktora naprawde mamy.
            # Sprzedaz takiego problemu nie ma - wychodzi z niej waluta kwotowana
            # - wiec przeliczanie byloby tu zgadywaniem kursu bez potrzeby.
            #
            # `filtry` PRZEKAZUJEMY, ZEBY WYJSCIE MOGLO DOMKNAC POZYCJE.
            # Sprzedajemy `r.ilosc`, czyli ilosc zaokraglona W DOL do kroku
            # gieldy, a na pozycji stoi ilosc NETTO po prowizji pobranej
            # w kupionym aktywie - te dwie liczby nigdy nie sa rowne. Bez regul
            # instrumentu `zapisz_wyjscie` porownuje je wprost, `closed_at`
            # zostaje NULL na zawsze i instrument wypada z handlu na stale
            # (pelny opis w docstringu `zapisz_wyjscie`).
            zapisz_wyjscie(cur, poz, cena, prowizja, teraz, plan["decision_id"],
                           ilosc=r.ilosc, meta=meta,
                           order_id=id_zlecenia_w_bazie,
                           planned_order_id=plan["id"],
                           venue_order_id=stan.id_gieldy,
                           client_order_id=id_w,
                           venue_fill_id=f"{stan.id_gieldy}",
                           prowizja_ccy=prowizja_ccy,
                           filtry=f)
        else:
            # SPRZEDAZ SIE WYKONALA, A NIE MA CZEGO ZAMKNAC.
            #
            # Aktywo wyszlo z rachunku naprawde - niezaleznie od tego, czy
            # znajdziemy pozycje, ktorej dotyczylo. Milczace przejscie dalej
            # (samo `if poz:` bez `else`) zapisywalo plan jako 'triggered',
            # czyli "rozstrzygniety pomyslnie", nie zostawiajac ani linijki
            # na ekranie, ani wiersza w bazie. Rozjazd z gielda ujawnialby sie
            # dopiero przy uzgodnieniu sald i wygladal jak blad uzgodnienia.
            #
            # DOKSIEGOWAC SIE TEGO NIE DA I NIE WOLNO ZGADYWAC. Zamkniecie
            # dowolnej innej otwartej pozycji na tym instrumencie przypisaloby
            # sprzedaz cudzej decyzji i sfalszowalo jej wynik. Zapisujemy wiec
            # to, co wiemy, tam, gdzie ktos tego szuka: w wierszu zlecenia
            # (`reject_reason` + `raw`), i zwracamy wlasny rodzaj, zeby petla
            # wypisala to na ekranie i policzyla w statystyce cyklu, ktora idzie
            # do heartbeatu silnika.
            #
            # Plan zostaje 'triggered', bo zlecenie FAKTYCZNIE sie wykonalo.
            # 'pending' oznaczaloby "czekamy na gielde" i wracaloby tu co cykl,
            # odczytujac w kolko to samo wypelnienie - bez szansy na zmiane.
            uwaga = ("wyjście wykonane na giełdzie BEZ otwartej pozycji"
                     f" (position_id={plan['trigger'].get('position_id')}):"
                     f" sprzedano {r.ilosc} @ {cena}, nie ma czego zamknąć")
            meta = dict(meta, anomalia="wyjscie_bez_pozycji",
                        position_id=plan["trigger"].get("position_id"),
                        sprzedano=str(r.ilosc), cena_wykonania=str(cena),
                        prowizja=str(prowizja))
            rodzaj_zwrotny = "wyjscie_bez_pozycji"
            powod_zwrotny = uwaga + f" ({r.powod})"
    else:
        zapisz_wejscie(cur, engine, mode, plan["instrument_id"], plan["decision_id"],
                       plan["side"], ilosc_netto, cena, prowizja_kwot, teraz,
                       plan["quote_ccy"], meta, planned_order_id=plan["id"],
                       venue_order_id=stan.id_gieldy, client_order_id=id_w,
                       venue_fill_id=f"{stan.id_gieldy}",
                       order_id=id_zlecenia_w_bazie,
                       prowizja_ccy=prowizja_ccy,
                       # SUROWA KWOTA DO `exec.fill`, PRZELICZONA DO
                       # `exec.position.fees_paid`. Wiersz wypelnienia trzyma
                       # pare (`fee`, `fee_ccy`) i `sprzatanie` czyta ja jako
                       # pare; kapital sumuje `fees_paid` bez jednostki.
                       prowizja_surowa=prowizja)

    if anulowanie_padlo is not None:
        # RESZTA ZLECENIA ZOSTAJE ZYWA NA GIELDZIE.
        #
        # Wypelnienie juz zaksiegowalismy - jest nasze. Ale reszta nadal lezy
        # w arkuszu i nadal moze wejsc po cenie, ktorej zadna decyzja nie
        # popiera. Plan idzie na 'triggered', bo wracanie do niego odczytywaloby
        # w kolko to samo wypelnienie; wiersz ZLECENIA zostaje wiec otwarty
        # i to on jest nitka, po ktorej sprzatanie (`posprzataj_zlecenia`)
        # wraca tu w kazdym cyklu, a nie dopiero po kill-switchu.
        cur.execute(
            "UPDATE exec.order SET status=%s, updated_at=%s, reject_reason=%s,"
            " raw = raw || %s WHERE planned_order_id=%s",
            (stan.status, teraz,
             f"anulowanie reszty nie powiodlo sie: {anulowanie_padlo}",
             json.dumps(dict(meta, anulowanie="nie_powiodlo_sie",
                             reszta="zostaje w arkuszu — sprzątanie ponowi")),
             plan["id"]))

    cur.execute("UPDATE journal.planned_order SET status='triggered',"
                " resolved_at=%s WHERE id=%s", (teraz, plan["id"]))
    # STATUS PO ANULOWANIU, A NIE SPRZED NIEGO.
    #
    # `stan.status` odczytalismy PRZED anulowaniem reszty - przy wypelnieniu
    # czesciowym jest to `partial`. Wpisanie go z powrotem opisuje zlecenie,
    # ktorego reszta nadal lezy w arkuszu, choc zdjelismy ja kilkadziesiat
    # linii wyzej w tym samym wywolaniu. Kosztowalo to dwa razy:
    #
    #   * `RiskKernel._open_exposure` liczy ten sam kapital DWA RAZY - raz jako
    #     otwarta pozycje (krok 1, wycena rynkowa), raz jako zlecenie zlozone
    #     i nierozstrzygniete (krok 2, `status IN ('new','partial','unknown')`).
    #     Ekspozycja wychodzi okolo dwukrotnie zawyzona, wiec
    #     `max_total_exposure` blokuje wejscia, ktorych nic nie blokuje -
    #     a w dzienniku wyglada to na poprawnie dzialajacy limit;
    #   * `posprzataj_zlecenia` wybiera dokladnie te trzy statusy, wiec co cykl
    #     pyta gielde o zlecenie, o ktorym sami wiemy, ze go nie ma.
    #
    # `anulowano` jest ustawiane WYLACZNIE po udanym `anuluj_zlecenie`. Gdy
    # anulowanie padlo, zostaje status z gieldy - reszta naprawde zyje.
    status_koncowy = "cancelled" if anulowano else stan.status
    # `COALESCE`, zeby zapis bez uwagi nie kasowal powodu wpisanego wczesniej
    # (np. przez kill-switch albo poprzednie nieudane anulowanie).
    cur.execute("UPDATE exec.order SET status=%s, updated_at=%s,"
                " reject_reason = COALESCE(%s, reject_reason), raw = raw || %s"
                " WHERE planned_order_id=%s",
                # `uwaga` (anomalia) ma pierwszenstwo, ale gdy jej nie ma,
                # a gielda podala powod odmowy — zapisujemy powod. Zlecenie
                # odrzucone PO czesciowym wypelnieniu przechodzi wlasnie tu
                # i bez tego zostawaloby bez powodu, tak samo jak wczesniej
                # zlecenie odrzucone bez wypelnienia.
                (status_koncowy, teraz, uwaga or powod_odrzucenia,
                 json.dumps(dict(meta, status_po_anulowaniu=status_koncowy)
                            if anulowano else meta),
                 plan["id"]))

    return rodzaj_zwrotny, powod_zwrotny


async def zloz_oczekujace(cur, adapter, *, engine: str, mode: str,
                          kategoria: str, h, verbose: bool = True,
                          tylko_wyjscia: bool = False,
                          sesja: StanSesji | None = None) -> dict:
    """Wysyła na giełdę plany, które jeszcze tam nie poszły.

    ROZPOZNANIE PO BRAKU WIERSZA ZLECENIA, nie po fladze na planie. Flaga
    mogłaby skłamać: proces mógł ją ustawić i paść przed wysłaniem albo wysłać
    i paść przed jej ustawieniem. Wiersz w `exec.order` powstaje w tej samej
    transakcji co wysłanie, więc jego brak znaczy dokładnie „nie próbowaliśmy".

    A gdyby mimo to zlecenie u giełdy było — złożenie jest idempotentne po
    naszym identyfikatorze i po prostu je odnajdzie.

    `tylko_wyjscia` obsługuje stan po zadziałaniu kill-switcha: nowych pozycji
    nie otwieramy, ale zamykanie istniejących MUSI iść dalej. Zatrzymanie
    wszystkiego bez wyjątku uwięziłoby pozycje w chwili, w której system właśnie
    stwierdził, że ryzyko jest za duże.

    `h` JEST WYMAGANY, bo od niego zależy nazwa cechy ATR — a od ATR pasmo
    ceny. Wartość domyślna byłaby tu cichym „przy innym barze pasmo znika".
    """
    # `przed_wyslaniem` od 2026-10-03 - patrz `BladPrzedWyslaniem`. Zero tez
    # jest wynikiem. Wiersze `unknown` waznych planow zwalnia wczesniej w tym
    # samym cyklu `ponow_nieznane` (loop.run_once), wiec tu wracaja jako plany
    # bez wiersza.
    st = {"zlozone": 0, "odrzucone": 0, "przed_wyslaniem": 0}
    filtry = filtry_z_bazy(cur, engine)
    # NAZWA CECHY Z HORYZONTU, NIE NA SZTYWNO.
    #
    # Wpisane wprost `atr_pct__4h` dzialalo wylacznie dla domyslnego bara. Przy
    # `--bar 1h` podzapytanie oddawalo NULL, `atr_pct` schodzilo do None, a
    # `zlecenia.prog_pasma` zwracalo wtedy pasmo AWARYJNE (SUFIT_PASMA = 0,25)
    # zamiast policzyc wlasciwe ~0,105. Pasmo przestawalo wykluczac cokolwiek
    # ponizej rzedu wielkosci - czyli tracilo swoja funkcje - i robilo to cicho,
    # bo brak cechy wyglada identycznie jak zmiennosc nieznana.
    #
    # `Horizon.name` jest tu jedynym zrodlem prawdy o tej nazwie; tak samo
    # wyprowadza ja `loop.atr_percentile_80` (services/engine/loop.py:394).
    nazwa_atr = h.name("atr_pct")
    cur.execute(
        "SELECT p.id, p.decision_id, p.instrument_id, p.side, p.qty, p.limit_price,"
        "       p.trigger, p.valid_until, i.symbol, i.quote_ccy, i.base_ccy,"
        # ATR do pasma ceny. Nieznana zmiennosc nie wylacza pasma - daje pasmo
        # awaryjne - ale zawezenie go realna zmiennoscia pary jest cala wartoscia
        # tego progu: stala liczba jest albo krzykliwa, albo bezuzyteczna.
        "       (SELECT fv.value FROM feat.feature_value fv"
        "         JOIN feat.feature_def fd ON fd.id = fv.feature_def_id"
        "        WHERE fv.instrument_id = p.instrument_id AND fd.name = %s"
        "        ORDER BY fv.event_time DESC LIMIT 1) AS atr_pct"
        "  FROM journal.planned_order p"
        "  JOIN ref.instrument i ON i.id = p.instrument_id"
        # SILNIK I TRYB Z DECYZJI, KTORA PLAN ZRODZILA - patrz komentarz
        # przy `rozstrzygnij_oczekujace` nizej.
        "  JOIN journal.decision d ON d.id = p.decision_id"
        " WHERE d.engine = %s AND d.mode = %s"
        "   AND p.status = 'pending'"
        "   AND NOT EXISTS (SELECT 1 FROM exec.order o WHERE o.planned_order_id = p.id)"
        "   AND (NOT %s OR p.trigger->>'rodzaj' = 'wyjscie')"
        " ORDER BY p.id", (nazwa_atr, engine, mode, tylko_wyjscia))
    for plan in cur.fetchall():
        f = filtry.get(plan["instrument_id"], Filtry())
        if plan.get("atr_pct") is None and verbose:
            # BRAK CECHY DLA ZNANEGO HORYZONTU ZOSTAWIA SLAD. Pasmo awaryjne
            # jest szersze od policzonego, wiec ta sciezka nie odrzuca zlecen,
            # tylko przepuszcza wiecej - a to jest dokladnie ten rodzaj awarii,
            # ktory bez linijki na ekranie wyglada jak spokojny dzien.
            print(f"  UWAGA: {plan['symbol']} — brak {nazwa_atr} w feat."
                  f"feature_value; pasmo ceny będzie AWARYJNE (szersze)")
        try:
            ok, msg = await zloz_plan(cur, adapter, plan, f, engine=engine,
                                      mode=mode, kategoria=kategoria,
                                      sesja=sesja)
        except BladPrzedWyslaniem as e:
            # Nic nie wyslano i nic nie zapisano - plan zostaje bez wiersza,
            # wiec nastepny cykl sprobuje sam. Linia na ekranie ZAWSZE, takze
            # przy --quiet: to awaria, nie rutyna.
            st["przed_wyslaniem"] += 1
            print(f"  [przed wysłaniem] {plan['symbol']} {plan['side']} — {e}")
            continue
        st["zlozone" if ok else "odrzucone"] += 1
        if verbose:
            print(f"  [{'złożone' if ok else 'ODRZUCONE'}] {plan['symbol']} "
                  f"{plan['side']} — {msg}")
    return st


async def ponow_nieznane(cur, adapter, *, engine: str, mode: str, kategoria: str,
                         verbose: bool = True, tylko_wyjscia: bool = False) -> dict:
    """Zlecenia `unknown` waznego planu: odnajdz u gieldy albo zloz ponownie POD TYM SAMYM id.

    DLACZEGO TO MUSI ISTNIEC

    Wiersz `unknown` (timeout POST-a, `retCode` 10000/10016) zamykal plan przed
    `zloz_oczekujace`, bo ono szuka planow BEZ wiersza zlecenia. Jesli zlecenie
    naprawde nie weszlo, plan czekal bezczynnie do konca swiecy, a potem
    wygasal - wyjscie przepadalo na cala swiece (1-4 h), choc wystarczylo
    zapytac i wyslac jeszcze raz (audyt 2026-10-03, E2).

    DLACZEGO TO JEST BEZPIECZNE

    Identyfikator jest ten sam (`tai-p{id planu}`), a przed wysylka idzie
    sprawdzenie stanu po nim (`zloz_plan`) - z trzecim odczytem `openOnly=1`,
    ktory domyka luke "history may delay". Gdyby mimo to zlecenie bylo u gieldy,
    Bybit odrzuci duplikat `orderLinkId` (170141), a `_blad_zlecenia` zamieni
    to w stan nieznany, NIE w odmowe - wiec zywe zlecenie nie zostanie porzucone.

    STARY WIERSZ ZOSTAJE, ZAMKNIETY I OPISANY. Baza ma UNIQUE (mode,
    client_order_id), a `INSERT` w `zloz_plan` jest jedyna droga zapisu
    zlozenia; drugi wiersz pod tym samym id wywrocilby transakcje cyklu.
    Stary wiersz dostaje wiec dokladnie to, co dalo mu dotad sprzatanie
    (`cancelled`, etap `zamkniete_bez_kontaktu_z_gielda` - czujka liczy go
    w `bez_kontaktu`), a do tego `client_order_id` z sufiksem `~{id wiersza}`
    i odpiety `planned_order_id`. Oryginalny identyfikator i plan zostaja
    w `raw`, a sufiks jest jawny w nazwie - grep po `tai-p77` znajduje oba.

    TYLKO PLAN WAZNY. Plan po terminie rozstrzyga `rozstrzygnij_oczekujace`
    (idzie w cyklu wczesniej), a wiersz bez planu `pending` - sprzatanie.

    WOLANE PRZED `zloz_oczekujace` (loop.run_once): plan zwolniony tutaj nie ma
    juz wiersza, wiec ta sama wysylka zlozy go jeszcze w tym cyklu.
    """
    st = {"odnalezione": 0, "ponowione": 0, "unknown_bez_odczytu": 0}
    cur.execute(
        "SELECT o.id, o.client_order_id, o.status, o.reject_reason, p.id AS plan_id,"
        "       i.symbol"
        "  FROM exec.order o"
        "  JOIN journal.planned_order p ON p.id = o.planned_order_id"
        "  JOIN journal.decision d ON d.id = p.decision_id"
        "  JOIN ref.instrument i ON i.id = p.instrument_id"
        " WHERE d.engine = %s AND d.mode = %s"
        "   AND p.status = 'pending'"
        "   AND (p.valid_until IS NULL OR p.valid_until > now())"
        "   AND o.status = 'unknown' AND o.venue_order_id IS NULL"
        "   AND (NOT %s OR p.trigger->>'rodzaj' = 'wyjscie')"
        " ORDER BY o.id", (engine, mode, tylko_wyjscia))
    for o in cur.fetchall():
        try:
            stan = await adapter.stan_zlecenia(o["client_order_id"], kategoria=kategoria)
        except ExchangeError as e:
            if e.wymaga_czlowieka:
                raise
            st["unknown_bez_odczytu"] += 1
            print(f"  [unknown] {o['symbol']}: stanu nadal nie da się odczytać "
                  f"({e}) — ponowimy w następnym cyklu")
            continue
        if stan is not None:
            # WESZLO. Od teraz wiersz jest zwyklym zlozonym zleceniem i
            # rozstrzyga go `rozstrzygnij_oczekujace` jak kazde inne.
            cur.execute(
                "UPDATE exec.order SET status=%s, venue_order_id=%s, updated_at=now(),"
                " raw = COALESCE(raw,'{}'::jsonb) || %s WHERE id=%s",
                (stan.status, stan.id_gieldy,
                 json.dumps({"etap": "odnalezione_po_unknown",
                             "status_przed_odczytem": o["status"]}),
                 o["id"]))
            st["odnalezione"] += 1
            if verbose:
                print(f"  [unknown] {o['symbol']}: giełda zna zlecenie "
                      f"(status {stan.status}) — wiersz uzupełniony")
            continue
        cur.execute(
            "UPDATE exec.order SET status='cancelled', updated_at=now(),"
            " planned_order_id=NULL, client_order_id = client_order_id || %s,"
            " reject_reason=%s, raw = COALESCE(raw,'{}'::jsonb) || %s WHERE id=%s",
            (f"~{o['id']}",
             "ponowienie: giełda nie zna zlecenia o stanie nieznanym, a plan jest "
             "ważny — ZLECENIE NIGDY NIE DOTARŁO DO GIEŁDY (brak venue_order_id); "
             "plan złożony ponownie pod tym samym identyfikatorem",
             json.dumps({"etap": "zamkniete_bez_kontaktu_z_gielda",
                         "status_przed_sprzataniem": o["status"],
                         "ponowione": True,
                         "client_order_id_wyslany": o["client_order_id"],
                         "planned_order_id": o["plan_id"],
                         "poprzedni_powod": o["reject_reason"]}),
             o["id"]))
        st["ponowione"] += 1
        print(f"  [unknown] {o['symbol']}: giełda nie zna {o['client_order_id']} "
              f"— składam ponownie pod tym samym id")
    return st


async def rozstrzygnij_oczekujace(cur, adapter, *, engine: str, mode: str,
                                  h, now, kategoria: str, kind: str,
                                  verbose: bool = True) -> dict:
    """Pyta giełdę o los planów, którym minęła świeca rozstrzygająca.

    TERMIN BIERZEMY Z TEJ SAMEJ FUNKCJI CO RACHUNEK PAPIEROWY. Gdyby jedna
    ścieżka pytała wcześniej, backtest i produkcja oglądałyby inny wycinek
    świecy — a różnica byłaby niewidoczna, bo obie wersje wyglądałyby poprawnie.

    PLANY BIERZEMY TYLKO SWOJE — patrz komentarz przy zapytaniu niżej.
    """
    from services.engine.loop import termin_rozstrzygniecia

    # `nie_zdjete` i `wyjscie_bez_pozycji` SA W SLOWNIKU OD POCZATKU, z zerem.
    # Statystyka idzie do heartbeatu silnika; klucz pojawiajacy sie dopiero przy
    # awarii nie da sie odroznic od klucza, ktorego nikt nie liczy.
    #
    # `nie_wiem` (E3) i `blad_odczytu` (E5) dolaczyly 2026-10-03 z tego samego
    # powodu: oba znacza "plan zostaje, wrocimy", i oba bez licznika wygladalyby
    # w pulsie jak cykl bez zdarzen.
    st = {"wypelnione": 0, "niewypelnione": 0, "czekaja": 0, "czesciowe": 0,
          "nie_do_zamkniecia": 0, "nie_zdjete": 0, "wyjscie_bez_pozycji": 0,
          "nie_wiem": 0, "blad_odczytu": 0}
    filtry = filtry_z_bazy(cur, engine)
    # SILNIK I TRYB Z DECYZJI, KTORA PLAN ZRODZILA.
    #
    # `journal.planned_order` nie ma kolumn engine/mode (0005, linia 68), wiec
    # sam status 'pending' wybieral KAZDY plan w bazie - takze cudzego silnika
    # i cudzego trybu. Asymetria byla jednostronna i wlasnie dlatego grozna:
    # `RiskKernel._open_exposure` dochodzi do tych samych wierszy przez
    # `JOIN journal.decision d ... WHERE d.engine=%s AND d.mode=%s`
    # (services/engine/risk.py:393-402), wiec limit ekspozycji widzi wylacznie
    # WLASNE plany, a egzekutor wykonywal WSZYSTKIE. Dopoki petla jest jedna
    # (values.yaml:298) nie ma to skutku; adapter Alpaki i `--silniki
    # crypto,equity` sa juz w repozytorium, wiec druga petla zapalilaby to
    # przy pierwszym wspolnym cyklu: zlecenia silnika equity poszlyby na NASZ
    # rachunek, a ich pozycje zaksiegowalyby sie jako crypto.
    #
    # KSZTALT JOIN-a JEST CELOWO TEN SAM CO W `_open_exposure`. Denormalizacja
    # engine/mode do `planned_order` byloby prostsze w zapisie, ale to osobna
    # decyzja (migracja + backfill + zgodnosc z dzisiejszymi wierszami), a nie
    # poprawka przy okazji; dopoki jej nie ma, obie strony musza czytac te sama
    # regule z tego samego miejsca, inaczej powstanie druga obok istniejacej.
    cur.execute(
        "SELECT p.id, p.decision_id, p.instrument_id, p.side, p.qty, p.limit_price,"
        "       p.trigger, p.valid_until, i.symbol, i.quote_ccy, i.base_ccy"
        "  FROM journal.planned_order p"
        "  JOIN ref.instrument i ON i.id = p.instrument_id"
        "  JOIN journal.decision d ON d.id = p.decision_id"
        " WHERE d.engine = %s AND d.mode = %s"
        "   AND p.status = 'pending' ORDER BY p.id", (engine, mode))
    for plan in cur.fetchall():
        _, czekamy = termin_rozstrzygniecia(plan, h, now)
        if czekamy and not await gielda_juz_rozstrzygnela(
                adapter, plan, kategoria=kategoria, verbose=verbose):
            st["czekaja"] += 1
            continue
        f = filtry.get(plan["instrument_id"], Filtry())
        # JEDEN PLAN Z BLEDEM ODCZYTU NIE ZATRZYMUJE POZOSTALYCH (audyt E5).
        #
        # Bez tego `ExchangeError` z `stan_zlecenia` (po trzech ponowieniach)
        # wychodzil z calego `run_once`: w tym cyklu nie zapadala decyzja dla
        # ZADNEGO instrumentu, takze wyjscie, a piec takich cykli konczylo
        # proces. Wyjatek lapiemy PRZED pierwszym zapisem tego planu - odczyt
        # stanu jest w `rozstrzygnij_plan` pierwszy - wiec transakcja cyklu
        # nie niesie polowy rozstrzygniecia. Awaria wymagajaca czlowieka idzie
        # w gore, bo nie dotyczy jednego planu, tylko wszystkich.
        try:
            rodzaj, powod = await rozstrzygnij_plan(
                cur, adapter, plan, f, engine=engine, mode=mode,
                kategoria=kategoria, teraz=now, kind=kind)
        except ExchangeError as e:
            if e.wymaga_czlowieka:
                raise
            st["blad_odczytu"] += 1
            print(f"  [błąd odczytu] {plan['symbol']} {plan['side']} — stanu "
                  f"zlecenia nie da się odczytać ({e}); plan zostaje, "
                  f"pozostałe rozstrzygamy dalej")
            continue
        if rodzaj == "brak":
            st["niewypelnione"] += 1
        elif rodzaj == "nie_zdjete":
            # NIE JEST TO ANI WYPELNIENIE, ANI ROZSTRZYGNIECIE. Zlecenie zostalo
            # na gieldzie, plan wraca tu w nastepnym cyklu. Doliczenie go do
            # `niewypelnione` mowiloby "zamkniete i nic nie weszlo", czyli
            # dokladnie to, czego jeszcze nie wiemy.
            st["nie_zdjete"] += 1
        elif rodzaj == "nie_wiem":
            st["nie_wiem"] += 1
        else:
            st["wypelnione"] += 1
            if rodzaj in st:
                st[rodzaj] += 1
        if verbose:
            print(f"  [{rodzaj}] {plan['symbol']} {plan['side']} — {powod}")
    return st


async def anuluj_wejscia(cur, adapter, *, kategoria: str, powod: str,
                         engine: str, mode: str,
                         verbose: bool = True) -> tuple[int, int]:
    """Zdejmuje z giełdy oczekujące zlecenia WEJŚCIA. Wyjścia zostawia.

    `engine` I `mode` NIE MAJĄ WARTOŚCI DOMYŚLNEJ, I TO JEST CELOWE. Rozstrzygają
    one, CZYJE zlecenia zdejmujemy z giełdy — to ten sam klucz, przez który plany
    trafiają do właściwego silnika, tyle że tutaj schowany w sygnaturze zamiast
    w zapytaniu. Domyślne `crypto`/`demo` sprawiały, że wywołanie, które o nich
    zapomni, zdejmuje cudze zlecenia albo nie znajduje żadnych — i w obu
    przypadkach NIE WYGLĄDA NA AWARIĘ, tylko na kill-switch, który nie miał
    czego zdejmować. Brak argumentu ma być błędem wywołania, a nie cichym
    wyborem jednego z silników.

    KILL-SWITCH, KTORY ZOSTAWIA ZLECENIA NA GIELDZIE, NIE JEST KILL-SWITCHEM.
    Wstrzymanie wejść zatrzymuje podejmowanie NOWYCH decyzji, ale zlecenie
    złożone minutę wcześniej nadal leży w arkuszu i nadal może się wypełnić —
    już po tym, jak system uznał, że handlować nie wolno. Pozycja powstałaby
    wtedy wbrew działającemu zabezpieczeniu i bez żadnej decyzji za sobą.

    ALE WYJSCIA ZOSTAJA, I TO JEST ROWNIE WAZNE. Kill-switch ma wyprowadzić
    z ryzyka, a nie uwięzić w nim. Zdjęcie zlecenia sprzedaży zostawiłoby
    otwartą pozycję bez czegokolwiek, co ją zamknie — czyli bezpiecznik
    zwiększyłby ekspozycję zamiast ją ściąć.

    ZWRACANA PARA LICZY WYŁĄCZNIE STANY, KTÓRE ZNAMY: `zdjete` to zlecenia
    zdjęte z arkusza, `wypelnione` — te, o których giełda powiedziała, że już
    weszły. Zlecenie, którego stanu NIE UDAŁO SIĘ ODCZYTAĆ, nie trafia do
    żadnej z nich: doliczenie go gdziekolwiek byłoby zaraportowaniem sukcesu
    tam, gdzie nie wiemy nawet, czy mamy pozycję.

    DLATEGO SAMA PARA NIE WYSTARCZA DO OCENY PRZEBIEGU i funkcja dopisuje
    brakującą liczbę na ekran sama. „Zdjęto 3" przy pięciu rozpatrzonych
    zleceniach czyta się jak komplet, a znaczy „3 z 5, o dwóch nie wiemy nic" —
    i to jest różnica między kill-switchem, który zadziałał, a takim, który
    zostawił dwa żywe zlecenia w arkuszu. Trwałym śladem po każdym z nich jest
    `reject_reason` w `exec.order`, zapisany BEZ zmiany statusu.
    """
    cur.execute(
        "SELECT o.client_order_id, i.symbol, o.planned_order_id"
        "  FROM exec.order o"
        "  JOIN ref.instrument i ON i.id = o.instrument_id"
        "  LEFT JOIN journal.planned_order p ON p.id = o.planned_order_id"
        " WHERE o.status IN ('new','partial','unknown')"
        "   AND o.client_order_id IS NOT NULL"
        "   AND o.engine::text = %s AND o.mode::text = %s"
        "   AND coalesce(p.trigger->>'rodzaj', 'wejscie') <> 'wyjscie'",
        (engine, mode))
    # `nieznane` NIE JEST TRZECIM ELEMENTEM ZWRACANEJ KROTKI, tylko liczba
    # do komunikatu. Krotka jest czescia interfejsu tej funkcji i jej
    # rozszerzenie nalezy do osobnej decyzji; pominiecie tej liczby na ekranie
    # nie naleza do zadnej - bez niej operator nie ma jak odroznic "zdjeto
    # wszystko" od "zdjeto tyle, ile sie udalo".
    zdjete, wypelnione, nieznane = 0, 0, 0
    zlecenia = cur.fetchall()
    for o in zlecenia:
        # NAJPIERW PYTAMY GIELDE, POTEM ANULUJEMY.
        #
        # Nasz status jest stary: wiersz siedzi na 'new' przez cale dwie swiece,
        # bo nikt go nie odswieza miedzy zlozeniem a rozstrzygnieciem. Zlecenie
        # moglo sie w tym czasie wypelnic.
        #
        # Zapisanie takiego zlecenia jako anulowanego GUBI WYPELNIENIE
        # BEZPOWROTNIE: rozstrzyganie szuka planow o statusie 'pending', wiec
        # juz nigdy go nie zaksieguje. Trzymalibysmy aktywo, ktorego dziennik
        # nie widzi - a po zwolnieniu kill-switcha sygnal nadal dodatni dalby
        # DRUGIE wejscie. Na gieldzie dwie pozycje, w dzienniku jedna.
        #
        # Nie mozna tez polegac na odpowiedzi samego anulowania: Bybit na
        # zlecenie juz wypelnione odpowiada kodem 110001, ktory adapter
        # traktuje jako "nie bylo czego zdejmowac" - nie do odroznienia od
        # zlecenia, ktorego nigdy nie bylo.
        try:
            stan = await adapter.stan_zlecenia(o["client_order_id"],
                                               kategoria=kategoria)
        except ExchangeError as e:
            # NIEUDANY ODCZYT TO NIE JEST "NIC NIE WESZLO".
            #
            # Poprzednia wersja ustawiala tu `stan = None` i szla dalej, czyli
            # w galaz anulujaca - a ta zapisuje `exec.order='cancelled'`
            # i `planned_order='cancelled'`. Tym jednym zapisem obsluga bledu
            # przewracala rozumowanie stojace 15 linii wyzej: plan przestaje
            # byc 'pending', wiec `rozstrzygnij_oczekujace` juz NIGDY go nie
            # zaksieguje, a wypelnienie, ktore moglo wejsc, przepada
            # bezpowrotnie. Na gieldzie pozycja, w dzienniku nic.
            #
            # O stanie tego zlecenia nie wiemy NICZEGO - ani ze weszlo, ani ze
            # nie. Jedynym ruchem, ktory niczego nie przesadza, jest NIE
            # ZAPISYWAC STATUSU: `exec.order` zostaje na 'new'/'partial',
            # wiec zapytanie na gorze zlapie to zlecenie w nastepnym cyklu
            # i ponowi probe, a plan zostaje 'pending', wiec rozstrzyganie
            # wciaz go widzi i zaksieguje to, co naprawde weszlo.
            #
            # Anulowania TEZ nie probujemy. Bybit na zlecenie juz wypelnione
            # oddaje 110001, ktore adapter zamienia na `return False` bez
            # wyjatku - odpowiedz nie do odroznienia od "nie bylo czego
            # zdejmowac". Nie doniosloby to wiec niczego, czego nie wiemy,
            # a przy zleceniu czesciowo wypelnionym zdjeloby reszte w oparciu
            # o zgadywanie.
            #
            # Powod zapisujemy BEZ zmiany statusu - tym samym wzorcem, co
            # nieudane anulowanie nizej. Bez tego jedynym sladem po nieznanym
            # stanie bylaby linia na ekranie, a ekran nikogo nie obudzi.
            cur.execute(
                "UPDATE exec.order SET reject_reason=%s, updated_at=now()"
                " WHERE client_order_id=%s AND mode::text=%s",
                (f"kill-switch: stanu zlecenia nie udalo sie odczytac ({e})"
                 f" - status BEZ ZMIANY, ponowimy w nastepnym cyklu",
                 o["client_order_id"], mode))
            if verbose:
                print(f"  UWAGA: {o['symbol']} {o['client_order_id']} — stanu"
                      f" zlecenia nie udało się odczytać ({e}); zostaje"
                      f" w arkuszu i do rozstrzygnięcia w następnym cyklu")
            # NIE LICZYMY GO ANI JAKO ZDJETE, ANI JAKO WYPELNIONE. Obie te
            # liczby ida do komunikatu kill-switcha; zlecenie o nieznanym
            # stanie w ktorejkolwiek z nich opisywaloby jako rozstrzygniete
            # cos, czego nie rozstrzygnelismy. Osobny licznik idzie na ekran
            # nizej - inaczej ta sama cisza wrocilaby w postaci zawyzonego
            # poczucia, ze kill-switch zamknal sprawe.
            nieznane += 1
            continue
        # `stan is None` ZNACZY JUZ TYLKO JEDNO: gielda nie zna tego zlecenia,
        # czyli nie weszlo. Nieudany odczyt wyszedl wyzej przez `continue`,
        # wiec ta sama wartosc nie miesza juz dwoch roznych rzeczy - tak samo
        # jak w `rozstrzygnij_plan`, gdzie brak stanu wygasza plan.
        if stan is not None and stan.wypelniono > 0:
            # Reszte i tak zdejmujemy, zeby nie doszlo wiecej - ale planu NIE
            # ruszamy, zeby rozstrzygniecie zaksiegowalo to, co weszlo.
            try:
                await adapter.anuluj_zlecenie(symbol=o["symbol"],
                                              id_wlasne=o["client_order_id"],
                                              kategoria=kategoria)
            except ExchangeError:
                pass
            wypelnione += 1
            continue
        try:
            await adapter.anuluj_zlecenie(symbol=o["symbol"],
                                          id_wlasne=o["client_order_id"],
                                          kategoria=kategoria)
        except ExchangeError as e:
            # NIEUDANE ANULOWANIE JEST ZAPISYWANE, nie pomijane. Zlecenie zostaje
            # w arkuszu, wiec musi zostac slad - inaczej "kill-switch zadzialal"
            # znaczyloby cos innego, niz sie wydaje.
            # FILTR PO TRYBIE, bo klucz unikalny to (mode, client_order_id),
            # a nie sam identyfikator. `tai-p77` z demo i `tai-p77` z live to
            # dwa rozne wiersze i oba pasuja do samego `client_order_id`:
            # zapis bez trybu dopisalby powod takze cudzemu zleceniu.
            cur.execute("UPDATE exec.order SET reject_reason=%s, updated_at=now()"
                        " WHERE client_order_id=%s AND mode::text=%s",
                        (f"anulowanie po kill-switchu nie powiodlo sie: {e}",
                         o["client_order_id"], mode))
            continue
        cur.execute("UPDATE exec.order SET status='cancelled', updated_at=now(),"
                    " reject_reason=%s WHERE client_order_id=%s AND mode::text=%s",
                    (powod, o["client_order_id"], mode))
        if o["planned_order_id"]:
            cur.execute("UPDATE journal.planned_order SET status='cancelled',"
                        " resolved_at=now() WHERE id=%s", (o["planned_order_id"],))
        zdjete += 1

    # PODSUMOWANIE Z MIANOWNIKIEM, nie sama liczba zdjetych. Wywolujacy
    # (`loop.run_once`) wypisuje "zdjeto z gieldy N zlecen" z samego `zdjete`
    # i nie ma skad wiedziec, ilu zlecen ta liczba nie obejmuje.
    if nieznane and verbose:
        print(f"  UWAGA: kill-switch NIE ROZSTRZYGNĄŁ {nieznane} z "
              f"{len(zlecenia)} rozpatrzonych zleceń — stanu nie udało się "
              f"odczytać, więc NIE MA ich w liczbie zdjętych; zostają "
              f"w arkuszu giełdy do następnego cyklu")
    return zdjete, wypelnione


async def posprzataj_zlecenia(cur, adapter, *, engine: str, mode: str,
                              kategoria: str, verbose: bool = True) -> dict:
    """Zamyka zlecenia, które przeżyły swój plan. Zwraca statystykę.

    DLACZEGO TO MUSI ISTNIEĆ OSOBNO

    Plan jest rozstrzygnięty i nikt do niego nie wróci — rozstrzyganie szuka
    planów `pending`. Zlecenie może jednak dalej żyć na giełdzie, i to na dwa
    sposoby, z których żaden nie miał dotąd sprzątacza:

      * anulowanie reszty przy wypełnieniu CZĘŚCIOWYM nie powiodło się. Reszta
        leży w arkuszu i wypełni się po cenie, której żadna decyzja już nie
        popiera — a risk kernel jej nie policzy, bo w dzienniku pozycja ma
        wielkość samego wypełnienia,
      * odpowiedź na złożenie nie dotarła i wiersz został na `unknown`. Jeżeli
        giełda tego zlecenia nie zna, wiersz jest już tylko fałszywym alarmem:
        czujka zdrowia egzekucji zgłasza go CODZIENNIE jako „kandydata na
        pozycję-widmo", a lista rośnie bez końca i uczy odbiorcę ignorować.

    Dotąd jedynym miejscem ponawiającym anulowanie było `anuluj_wejscia`, które
    rusza WYŁĄCZNIE po kill-switchu. Zlecenie porzucone przy zwykłym błędzie
    sieci czekało więc na obsunięcie kapitału, żeby ktoś je zdjął.

    PYTAMY GIEŁDĘ, ZANIM COKOLWIEK ZAPISZEMY. Nasz status jest stary z definicji
    — to on właśnie okazał się nieprawdziwy.
    """
    st = {"zdjete": 0, "domkniete": 0, "wypelnione_po_fakcie": 0,
          "bez_kontaktu": 0}
    cur.execute(
        "SELECT o.id, o.client_order_id, o.status, o.venue_order_id,"
        "       i.symbol, i.lot_size,"
        # ILE JUZ ZAKSIEGOWALISMY z tego zlecenia. Bez tej liczby jedynym
        # sygnalem bylby status wiersza - a status mowi "bylo czesciowe",
        # nie "ile z tego trafilo do dziennika".
        #
        # PROWIZJA W AKTYWIE BAZOWYM WRACA DO SUMY, bo porownujemy te liczbe
        # z `stan.wypelniono`, czyli z `cumExecQty` gieldy - a to jest BRUTTO.
        # `exec.fill.qty` dla wejscia jest NETTO (`loop.zapisz_wejscie` dostaje
        # ilosc po `egzekucja.rozlicz_prowizje`, bo kupno na spocie placi
        # prowizje w kupionym aktywie). Roznica wynosi DOKLADNIE prowizje
        # bazowa: dla ADAUSDC (`lot_size` 0.1) zlecenie na 2000 ADA daje ~2 ADA
        # przy progu 0,05 - czyli warunek "pozycja poza dziennikiem" byl
        # spelniony PRZY KAZDYM WEJSCIU. Czujka meldowala wtedy nieistniejaca
        # pozycje poza dziennikiem, uzgodnienie z gielda nic nie znajdowalo
        # (odejmuje prowizje po obu stronach), a odbiorca uczyl sie, ze te
        # linie mozna pomijac - te sama, ktora ma wylapac prawdziwe wypelnienie
        # po fakcie, czyli ekspozycje niepoliczona przez risk kernel.
        #
        # ODTWARZAMY BRUTTO, ZAMIAST ZMIENIAC ZNACZENIE KOLUMNY. Wpisywanie do
        # `exec.fill.qty` ilosci brutto naprawiloby to samo, ale wylacznie dla
        # wierszy NOWYCH - kilka miesiecy dotychczasowego przebiegu demo
        # zostaloby netto i ta sama kolumna znaczylaby dwie rozne rzeczy
        # w zaleznosci od daty. Prowizja i jej waluta sa w wierszu wypelnienia
        # (`fee`, `fee_ccy`), wiec brutto da sie policzyc dokladnie - i dla
        # wiersza starego, i dla nowego.
        "       coalesce((SELECT sum(f.qty + CASE WHEN upper(f.fee_ccy)"
        "                                         = upper(i.base_ccy)"
        "                                    THEN f.fee ELSE 0 END)"
        "                  FROM exec.fill f"
        "                  WHERE f.order_id = o.id), 0) AS zaksiegowane"
        "  FROM exec.order o"
        "  JOIN ref.instrument i ON i.id = o.instrument_id"
        "  LEFT JOIN journal.planned_order p ON p.id = o.planned_order_id"
        " WHERE o.status IN ('new','partial','unknown')"
        "   AND o.engine::text = %s AND o.mode::text = %s"
        "   AND o.client_order_id IS NOT NULL"
        # WYLACZNIE zlecenia, ktorych plan JUZ sie rozstrzygnal. Zlecenia
        # czekajace na swoja swiece nie sa sierotami i ruszanie ich zabieraloby
        # rozstrzyganiu jego wlasna prace.
        "   AND (p.id IS NULL OR p.status::text <> 'pending')",
        (engine, mode))

    for o in cur.fetchall():
        try:
            stan = await adapter.stan_zlecenia(o["client_order_id"],
                                               kategoria=kategoria)
        except ExchangeError as e:
            if verbose:
                print(f"  [sprzątanie] {o['symbol']}: nie da się odczytać stanu ({e})")
            continue

        if stan is None:
            # GIELDA GO NIE ZNA. Zlecenie albo nigdy nie powstalo, albo dawno
            # zniknelo - w obu przypadkach wiersz otwarty jest juz tylko
            # zrodlem falszywego alarmu.
            #
            # SPRZATANIE ZOSTAWIA SLAD, KTOREGO CZUJKA NIE ZGUBI.
            #
            # Do 2026-09-24 ta jedna instrukcja KASOWALA DOWOD AWARII, zanim
            # ktokolwiek zdazyl go zobaczyc. Czujka egzekucji liczy zlecenia
            # `unknown` starsze niz 4 h; sprzatanie chodzi co cykl i przestawia
            # je na `cancelled` po ~2 h 45 min. Zmierzone na tej awarii:
            #
            #     order 49  unknown 17:18:52 -> cancelled 20:03:48  (2 h 45)
            #     order 51  unknown 21:28:58 -> cancelled 00:04:46  (2 h 35)
            #     order 52  unknown 01:14:54 -> cancelled 04:00:44  (2 h 46)
            #     order 53  unknown 05:15:52 -> cancelled 08:01:42  (2 h 46)
            #
            # Prog 4 h nie zostal przekroczony ANI RAZU — sprzatanie scigalo sie
            # z czujka i wygrywalo za kazdym razem. Gorzej: `cancelled` wpadalo
            # u czujki do licznika PRZYJETYCH, wiec cztery zlecenia, ktore nigdy
            # nie dotarly do gieldy, podbijaly licznik sukcesu.
            #
            # Sladem jest BRAK `venue_order_id` — fakt strukturalny, ktorego
            # zadne sprzatanie nie moze nadpisac, bo identyfikator gieldy nigdy
            # nie powstal. `raw.etap` dopisujemy, zeby dalo sie to znalezc
            # gremem, a nie tylko zapytaniem o NULL-a. Czujka liczy to osobnym
            # licznikiem (`dozor.sprawdz_egzekucje`: `bez_kontaktu`).
            bez_kontaktu = not o.get("venue_order_id")
            cur.execute(
                "UPDATE exec.order SET status='cancelled', updated_at=now(),"
                " reject_reason=%s, raw = COALESCE(raw,'{}'::jsonb) || %s"
                " WHERE id=%s",
                ("sprzątanie: giełda nie zna tego zlecenia — wiersz zamknięty, "
                 "żeby przestał być zgłaszany jako o nieznanym stanie"
                 + ("; ZLECENIE NIGDY NIE DOTARŁO DO GIEŁDY (brak venue_order_id)"
                    if bez_kontaktu else ""),
                 json.dumps({"etap": ("zamkniete_bez_kontaktu_z_gielda"
                                      if bez_kontaktu else "zamkniete_przez_sprzatanie"),
                             "status_przed_sprzataniem": o["status"]}),
                 o["id"]))
            st["domkniete"] += 1
            if bez_kontaktu:
                st["bez_kontaktu"] += 1
                if verbose:
                    print(f"  [sprzątanie] {o['symbol']}: zamknięte BEZ KONTAKTU "
                          f"Z GIEŁDĄ (było {o['status']}, brak venue_order_id)")
            continue

        # ILE WYPELNILO SIE PONAD TO, CO JUZ ZAKSIEGOWANE.
        #
        # Warunek brzmial tu wczesniej `o["status"] != "partial"` i gubil przez
        # to CALA reszte zlecenia czesciowo wypelnionego: wiersz `partial` mial
        # juz zaksiegowane pierwsze wypelnienie, wiec kazde nastepne bylo
        # wyciszane. Reszta dopelniala sie po rozstrzygnieciu planu, wiersz
        # przechodzil na `filled` jako "domkniety" - i roznica miedzy dziennikiem
        # a rachunkiem zostawala BEZ SLADU. Risk kernel liczy wtedy ekspozycje
        # mniejsza niz faktyczna, a to jest wielkosc, na ktorej stoi kill-switch.
        #
        # Pytanie brzmi "czy jest wypelnienie, ktorego nie ma w dzienniku",
        # a nie "czy wiersz byl czesciowy". Status odpowiada na drugie.
        zaksiegowane = Decimal(str(o.get("zaksiegowane") or 0))
        niezaksiegowane = stan.wypelniono - zaksiegowane
        # Ponizej polowy kroku ilosci nie da sie kupic ani sprzedac, wiec taka
        # roznica to zaokraglenie, nie wypelnienie. Bez kroku porownujemy scisle.
        krok = Decimal(str(o.get("lot_size") or 0))
        poza_dziennikiem = niezaksiegowane > (krok / 2 if krok > 0 else Decimal(0))
        opis_roznicy = (
            f"pozycja poza dziennikiem: {niezaksiegowane} "
            f"(giełda {stan.wypelniono}/{stan.ilosc}, dziennik {zaksiegowane}) "
            f"— wyłapie to uzgodnienie z giełdą")

        if stan.status in ("cancelled", "rejected", "filled"):
            # ZLECENIE SIE JUZ NIE RUSZY. Zostaje tylko pytanie, czy zostawilo
            # po sobie wypelnienie, ktorego nie ma w dzienniku. Nie ksiegujemy
            # go tutaj - ksiegowanie nalezy do rozstrzygania i robienie tego
            # w dwoch miejscach rozjechaloby sie przy pierwszej zmianie.
            if poza_dziennikiem:
                cur.execute(
                    "UPDATE exec.order SET status=%s, updated_at=now(),"
                    " reject_reason=%s WHERE id=%s",
                    (stan.status, f"sprzątanie: {opis_roznicy}", o["id"]))
                st["wypelnione_po_fakcie"] += 1
                if verbose:
                    print(f"  [sprzątanie] {o['symbol']}: WYPEŁNIŁO SIĘ po fakcie "
                          f"{stan.wypelniono}/{stan.ilosc}, poza dziennikiem "
                          f"{niezaksiegowane}")
            else:
                # POWOD ODMOWY TAKZE TUTAJ. Sprzatanie dochodzi do zlecen,
                # ktorych rozstrzyganie juz nie dotknie (plan nie jest
                # 'pending'), wiec bez tego zapisu zlecenie odrzucone,
                # zamkniete ta gałęzią, zostawaloby z `reject_reason` NULL —
                # ta sama cisza, tylko w drugim miejscu.
                odmowa = stan.powod_odmowy
                cur.execute("UPDATE exec.order SET status=%s, updated_at=now(),"
                            " reject_reason = COALESCE(%s, reject_reason)"
                            " WHERE id=%s",
                            (stan.status, odmowa.opis if odmowa else None,
                             o["id"]))
                st["domkniete"] += 1
            continue

        # ZLECENIE NADAL ZYJE - reszte trzeba zdjac z arkusza NIEZALEZNIE od
        # tego, czy cos juz sie z niego wypelnilo. To sa dwie osobne sprawy
        # i potraktowanie ich jako jednej zostawialo zywa reszte w arkuszu.
        try:
            await adapter.anuluj_zlecenie(symbol=o["symbol"],
                                          id_wlasne=o["client_order_id"],
                                          kategoria=kategoria)
        except ExchangeError as e:
            if verbose:
                print(f"  [sprzątanie] {o['symbol']}: anulowanie nadal nie "
                      f"wychodzi ({e}) — spróbujemy w następnym cyklu")
            continue
        powod = "sprzątanie: zlecenie przeżyło swój plan i zostało zdjęte"
        if poza_dziennikiem:
            powod += f"; UWAGA: {opis_roznicy}"
            st["wypelnione_po_fakcie"] += 1
        cur.execute("UPDATE exec.order SET status='cancelled', updated_at=now(),"
                    " reject_reason=%s WHERE id=%s", (powod, o["id"]))
        st["zdjete"] += 1
        if verbose:
            print(f"  [sprzątanie] {o['symbol']}: zdjęte z arkusza"
                  + (f" (poza dziennikiem {niezaksiegowane})"
                     if poza_dziennikiem else ""))
    return st
