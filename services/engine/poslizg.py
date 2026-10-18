#!/usr/bin/env python3
"""Poślizg i ułamek wypełnienia MIERZONE, a nie zakładane.

DLACZEGO POMIAR, A NIE POPRAWIONA STAŁA

W `services/engine/koszty.py` stoją dwie liczby:

    POSLIZG_TAKER = 0.0004     # 4 punkty bazowe
    POSLIZG_MAKER = 0.0        # zero

Obie są ZAŁOŻENIAMI i nikt ich dotąd nie sprawdził, bo do dziś nie było czego
sprawdzać: egzekucja szła rachunkiem papierowym, który cenę wypełnienia sam
sobie wyliczał z tych właśnie stałych.

Waga jest policzalna. Runda kosztuje 0,200%, a zmierzona mediana zwrotu na
transakcję jest rzędu dziesiątych procenta — **błąd o cztery punkty bazowe
przesuwa wynik o tyle, ile cała zmierzona przewaga**. Podmiana jednej zgadywanej
liczby na inną zgadywaną nie byłaby więc poprawką, tylko zamianą jednego
założenia na drugie. Ten moduł nie zmienia żadnej stałej. Dostarcza liczbę,
którą da się z nimi zestawić — razem z uczciwą odpowiedzią „jeszcze nie wiadomo".

CO JEST CENĄ ODNIESIENIA I DLACZEGO TO NIE JEST OBOJĘTNE

Poślizg to odległość między ceną, NA KTÓREJ OPARTA BYŁA DECYZJA, a ceną, po
której transakcja faktycznie poszła. Pierwsza z nich mieszka w trzech miejscach
i wybór między nimi nie jest kosmetyczny:

  * `exec.order.limit_price` — cena, KTÓRĄ GIEŁDA DOSTAŁA. To jedyne
    zobowiązanie, jakie w ogóle istnieje: giełda ma obowiązek wypełnić po niej
    albo lepiej, więc poślizg maker powinien wychodzić ujemny lub zerowy. Wynik
    dodatni jest sam w sobie informacją: albo zlecenie przeszło przez spread
    (marketable limit, czyli faktycznie taker), albo coś jest nie tak z zapisem.
  * `journal.planned_order.limit_price` — cena, na której stanął PLAN. Dla
    KAŻDEGO wyjścia `zlecenia.sprawdz_zlecenie` podmienia ją na `cena_makera`
    z księgi docelowej (services/engine/zlecenia.py), więc dla wyjść to NIE
    JEST cena wysłana. Zostaje jako zapas dla wierszy bez zlecenia — i jako
    drugi człon `rozjazd_ksiag` niżej.
  * `journal.decision.target_price` — cena z chwili decyzji, jedyna dostępna dla
    zlecenia rynkowego. To DOKŁADNIE to, co modeluje `POSLIZG_TAKER`: koszt
    przejścia przez spread między chwilą decyzji a chwilą wykonania.

CO MIERZYŁ TEN MODUŁ DO 2026-09-22 I DLACZEGO TO BYŁ BŁĄD POMIARU

Odniesieniem była `cena_planu`, czyli `journal.planned_order.limit_price`.
Na giełdę szła jednak `cena_makera` z księgi docelowej, zapisana w
`exec.order.limit_price` — a rozjazd między księgami jest zmierzony i duży
(w 16,8% świec przekracza całą rundę kosztową, patrz gielda_exec.py). Poślizg
liczył się więc względem ceny, której giełda nigdy nie dostała: wyjście
wypełnione CO DO GROSZA po naszej cenie (−0,00 pb, idealny maker) raportowało
się jako +29,00 pb, czyli 1,45× cała runda kosztowa. Przy trzech takich
wyjściach moduł wydawał werdykt „założenie poza przedziałem" — formalne żądanie
podniesienia stałej kosztowej na podstawie własnego błędu pomiaru. Dokładnie ta
klasa pomyłki, przed którą ostrzega akapit o rachunku papierowym niżej: liczba
wyglądająca jak pomiar egzekucji, a będąca pomiarem nas samych.

Różnica `planned_order.limit_price − exec.order.limit_price` jest osobną,
sensowną wielkością — to rozjazd między księgą, na której stanął plan, a księgą,
do której poszło zlecenie. Mierzymy ją i wypisujemy pod własną nazwą
(`rozjazd_ksiag`), ale NIE jako poślizg: do kosztu egzekucji nie należy.

Czwarta jest od 2026-10-03 (audyt F-4.2): `mid_eu_przy_wysylce`, środek
arkusza księgi EGZEKUCJI w chwili składania zlecenia (`exec.order.raw.
arkusz_wysylki`). Poślizg wyżej nadal mierzy SUMĘ — to ona jest płacona —
a `rozbicie_do_celu` dzieli `fill − target_price` na ruch od zamknięcia świecy
do wysyłki i ruch od wysyłki do wypełnienia. Zlecenia sprzed tej daty pola nie
mają i wypadają z rozbicia z nazwanym powodem, a nie jako zero.

ZNAK: DODATNI ZAWSZE ZNACZY „NA NASZĄ NIEKORZYŚĆ"

Przy kupnie poślizg jest dodatni, gdy zapłaciliśmy więcej niż cena odniesienia;
przy sprzedaży — gdy dostaliśmy mniej. Bez tej normalizacji średnia z kupn
i sprzedaży kasowałaby się do zera przy dowolnie złej egzekucji.

PUŁAPKA, KTÓRA UNIEWAŻNIŁABY CAŁY POMIAR

Rachunek papierowy zapisuje cenę wypełnienia jako `cena * (1 ± POSLIZG)`.
Policzenie poślizgu z takich wierszy oddałoby więc DOKŁADNIE `POSLIZG_TAKER`
— i wyglądałoby jak świetne potwierdzenie założenia, będąc wyłącznie jego echem.
Dlatego mierzymy wyłącznie wiersze z `raw->>'execution' = 'gielda'`, a filtr
stoi w DWÓCH miejscach: w zapytaniu i w czystej funkcji. Zapytanie da się
przypadkiem poprawić, funkcja jest testowana.

CZEGO TEN MODUŁ NIE MIERZYŁ DO 2026-09-09 — I DLACZEGO TO WAŻNE PRZY CZYTANIU
STARYCH WYNIKÓW

Wyjść. `zapisz_wyjscie` nie zakładało wiersza w `exec.fill` (aktualizowało
wyłącznie `exec.position`), więc w rejestrze wypełnień były SAME WEJŚCIA:
18 pozycji zamkniętych, 18 wierszy `exec.fill`. Poślizg wyjścia był wtedy
niemierzalny — była to luka w zapisie, nie wynik pomiaru.

Zapis jest naprawiony i wyjścia trafiają tu na równi z wejściami. Ale wszystko,
co zmierzono PRZED tą datą, opisuje wyłącznie nogę wejściową, więc nie wolno
porównywać takiej liczby z nowszą jako „poprawy" ani „pogorszenia": zmieniła się
próba, nie egzekucja. Runda ma dwie nogi i dopiero od teraz obie są w próbie.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import statistics
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.engine.egzekucja import rozjazd_z_ulamkow  # noqa: E402
from services.engine.koszty import POSLIZG_MAKER, POSLIZG_TAKER  # noqa: E402
from services.engine.statystyka import przedzial_sredniej  # noqa: E402

# Jedyny zapis, ktory jest POMIAREM. Wszystko inne to nasze wlasne zalozenie
# zapisane do bazy i odczytane z powrotem.
ZRODLO_MIERZALNE = "gielda"

# Nazwa zrodla ceny odniesienia dla zlecen RYNKOWYCH (likwidacja po
# kill-switchu). Stala, a nie literal w dwoch miejscach: `cena_odniesienia`
# ja zwraca, a `tozsamosciowy` na niej rozstrzyga - rozjazd tych dwoch napisow
# odebralby probie takera kazde idealne wypelnienie i zrobil to po cichu.
ZRODLO_LIKWIDACJI = "arkusz_likwidacji"

# Zalozenie, ktore ten pomiar ma sprawdzic. IMPORTOWANE, nie przepisane -
# przepisana kopia rozjechalaby sie przy pierwszej zmianie stalej, a wtedy
# pomiar porownywalby sie z nieaktualnym zalozeniem i milczal.
ZALOZENIE: dict[str, float] = {"maker": POSLIZG_MAKER, "taker": POSLIZG_TAKER}

# Werdykty. Nazwane, bo to na nich stoi rozroznienie „nie zmierzono" od
# „zmierzono i wyszlo tyle" - i to one, a nie liczby, sa czytane przez czlowieka.
NIE_ZMIERZONO = "nie zmierzono"
ZA_MALO = "za malo obserwacji"
NIEODROZNIALNE = "nieodroznialne od zalozenia"
ROZBIEZNE = "zalozenie poza przedzialem"
# PIATY STAN: proba jest niepusta, ma rozrzut ZERO i to zero nie pochodzi
# z egzekucji, tylko z arytmetyki - patrz `werdykt` i `tozsamosciowy`.
TOZSAMOSC = "odniesienie rowne cenie wykonania"


def tozsamosciowy(w: dict) -> bool:
    """Czy cena odniesienia jest DOKLADNIE cena wykonania. CZYSTA FUNKCJA.

    Taki wiersz ma poslizg zero i to zero nie niesie zadnej informacji
    o egzekucji. Droga, ktora powstaje, jest dzis JEDNA i jest STRUKTURALNA:

      * zlecenie z limitem wypelnia sie PO CENIE LIMITU albo lepiej,
        a odniesieniem jest wlasnie `exec.order.limit_price`. Pytanie brzmi
        wtedy "czy limit wypelnil sie po cenie limitu" i odpowiedz jest znana
        przed pomiarem. Egzekucja sklada WYLACZNIE `limit` z `PostOnly`
        (`gielda_exec` ma `order_type` zapisany na sztywno), wiec dla toru
        gieldowego ta droga jest jedyna mozliwa.

    DRUGA DROGA ZOSTALA ZAMKNIETA 2026-09-26 i dlatego jej tu nie ma.
    `loop.zapisz_wejscie` NADPISYWALO `exec.order.limit_price` cena WYKONANIA;
    wtedy zero wychodzilo takze dla zlecenia, ktore przeszlo przez spread jako
    taker po gorszej cenie. Dzis ta galaz ma w sobie komentarz "`qty`
    I `limit_price` ZOSTAJA NIETKNIETE" i UPDATE rusza tylko `status`,
    `venue_order_id`, `updated_at` i `raw`. Sprawdzone na produkcji 2026-09-27:
    WSZYSTKIE 35 wypelnionych zlecen z limitem (takze cztery zlozone po tej
    poprawce) maja `limit_price` rowne `exec.fill.price` co do ostatniej cyfry.

    WNIOSEK, KTORY Z TEGO WYNIKA I KTORY TRZEBA POWIEDZIEC WPROST: dla zlecen
    PostOnly poslizg CENOWY makera nie jest mala liczba do zmierzenia - on
    nie istnieje. `POSLIZG_MAKER = 0` jest wiec poprawne przez konstrukcje,
    a koszt makera siedzi CALY w NIEWYPELNIENIU: w zleceniu, ktore nie weszlo,
    i w tym, ze wchodzi ono czesciej wtedy, gdy rynek idzie przeciw nam.
    Tej wielkosci `exec.fill` nie zawiera z definicji - odpowiada na nia
    wypelnialnosc liczona z `exec.order`.

    Porownujemy DZIESIETNIE, nie po przeliczeniu na float: `Decimal("100") ==
    Decimal("100.00")` jest prawda i o to chodzi, a `float` dorzucilby do tego
    pary rozne o ulamek ostatniego bitu i nazwal je rownymi.
    """
    surowa = w.get("cena_wykonania")
    if surowa is None:
        return False
    try:
        wykonana = Decimal(str(surowa))
    except (InvalidOperation, ValueError):
        return False
    odniesienie, skad = cena_odniesienia(w)
    if skad == ZRODLO_LIKWIDACJI:
        # ZERO Z ARKUSZA LIKWIDACJI JEST POMIAREM, NIE ARYTMETYKA.
        #
        # Odniesieniem jest tu bid (albo ask) z chwili PRZED wyslaniem, a cena
        # wypelnienia przychodzi od gieldy PO wykonaniu. Rownosc tych dwoch
        # liczb nie wynika z zadnej definicji - znaczy, ze zlecenie rynkowe
        # weszlo DOKLADNIE po cenie dotkniecia, czyli ze poslizg wyniosl zero.
        # To jest najlepszy mozliwy wynik egzekucji i musi zostac w probie.
        #
        # Wrzucenie go do `tozsamosciowych` mialoby skutek dokladnie odwrotny do
        # zamierzonego: proba samych idealnych wypelnien dostalaby werdykt
        # „odniesienie rowne cenie wykonania", czyli „NIE ZMIERZONO" - i pomiar
        # znikalby tym mocniej, im lepsza byla egzekucja.
        return False
    return odniesienie is not None and odniesienie == wykonana


def rodzaj_zlecenia(order_type: str | None) -> str:
    """'maker' | 'taker' | 'nieznany' — po typie zlecenia z `exec.order`.

    OBIE STALE DOTYCZA CZEGOS INNEGO, wiec wspolna srednia nie mialaby sensu:
    zlecenie z limitem nie przechodzi przez spread (zalozenie: zero), rynkowe
    przechodzi zawsze (zalozenie: 4 pb). Zmieszanie ich daloby liczbe, ktorej
    nie da sie zestawic z zadna z nich.

    Typ spoza pary limit/market NIE JEST ZGADYWANY. Trafia do „nieznany"
    i wypada z pomiaru z nazwanym powodem — zaliczenie go na chybil trafil do
    makera porownaloby go z zalozeniem zero i wygladalo na katastrofe.

    OGRANICZENIE, KTORE TRZEBA ZNAC: zlecenie z limitem zlozone po drugiej
    stronie spreadu wypelnia sie natychmiast i placi stawke TAKER, a my
    zaliczymy je do makera. Widac to po wyniku - dodatni poslizg na zleceniu
    z limitem jest wlasnie tym objawem, bo limit gwarantuje cene albo lepsza.
    """
    t = (order_type or "").strip().lower()
    if t == "limit":
        return "maker"
    if t == "market":
        return "taker"
    return "nieznany"


def cena_odniesienia(w: dict) -> tuple[Decimal | None, str]:
    """Cena, na ktorej oparta byla decyzja. Zwraca (cena, skad).

    KOLEJNOSC NIE JEST DOWOLNA I NIE JEST KOSMETYCZNA.

    1. `cena_zlecenia` (`exec.order.limit_price`) - cena, KTORA GIELDA DOSTALA.
       Zobowiazanie istnieje wylacznie wobec niej i tylko wzgledem niej da sie
       mierzyc jakosc wypelnienia.
    2. `cena_planu` (`journal.planned_order.limit_price`) - cena, na ktorej
       stanal plan. Dla wyjsc `zlecenia.sprawdz_zlecenie` podmienia ja przed
       wyslaniem (`cena_makera` z ksiegi docelowej), wiec jako odniesienie
       mierzylaby ROZJAZD KSIAG i nazywala go poslizgiem - patrz naglowek
       modulu. Zostaje jako zapas dla wierszy, ktore ceny zlecenia nie maja.
    3. `cena_decyzji` (`journal.decision.target_price`) - jedyna cena, jaka
       istnieje dla zlecenia bez limitu.

    ZERO: `cena_arkusza` (`exec.likwidacja.cena_odniesienia`) - cena Z ARKUSZA
    ksiegi egzekucji, odczytana TUZ PRZED wyslaniem zlecenia RYNKOWEGO, po tej
    stronie ksiegi, ktora zlecenie uderzy (bid przy sprzedazy, ask przy kupnie).
    Stoi PRZED cena zlecenia i to nie jest kosmetyka:

      * zlecenie rynkowe NIE MA `limit_price` (jest NULL-em), wiec punkt 1 dla
        niego nie istnieje. Bez tego zrodla wiersz spadalby na `target_price`,
        czyli na cene z chwili DECYZJI - a likwidacja zachodzi wiele godzin po
        niej i bez zwiazku z nia. Poslizg wychodzilby wtedy rowny dryfowi rynku
        przez te godziny, czyli o rzad wielkosci za duzy;
      * dla wierszy Z LIMITEM ta kolumna jest NULL-em (dziennik likwidacji
        dotyczy WYLACZNIE zlecen rynkowych), wiec pierwszenstwo nie zmienia
        niczego w pomiarze makera. Pilnuje tego test
        `test_zrodlo_likwidacji_nie_rusza_pomiaru_makera`.

    TO JEST PIERWSZE ZRODLO, KTORE CZYNI POSLIZG TAKERA MIERZALNYM. Przed nim
    `POSLIZG_TAKER` mial n=0 nie dlatego, ze zlecen rynkowych bylo malo, ale
    dlatego, ze nie bylo ANI JEDNEGO z ceną odniesienia z chwili wysylki.
    """
    for pole, skad in (("cena_arkusza", "arkusz_likwidacji"),
                       ("cena_zlecenia", "zlecenie"), ("cena_planu", "plan"),
                       ("cena_decyzji", "decyzja")):
        v = w.get(pole)
        if v is None:
            continue
        try:
            c = Decimal(str(v))
        except (InvalidOperation, ValueError):
            continue
        if c > 0:
            return c, skad
    return None, "brak"


def poslizg_wiersza(w: dict) -> tuple[float | None, str]:
    """Poślizg jednego wypełnienia, jako ułamek ceny odniesienia. CZYSTA FUNKCJA.

    Zwraca `(poślizg, skąd_odniesienie)` albo `(None, powód_odrzucenia)`.

    ODRZUCENIE NIE JEST ZEREM. Wiersz, którego nie da się zmierzyć, wypada
    z próby i jest policzony osobno wraz z powodem. Wliczenie go jako zera
    ściągałoby średnią do zera tym mocniej, im więcej rzeczy nie działa —
    czyli zepsuty zapis wyglądałby jak doskonała egzekucja.
    """
    if (w.get("zrodlo") or "") != ZRODLO_MIERZALNE:
        return None, ("zapis nie z giełdy — mierzyłby własne założenie, "
                      "bo rachunek papierowy wylicza cenę wprost z POSLIZG_*")
    strona = (w.get("strona") or "").strip().lower()
    if strona not in ("buy", "sell"):
        return None, f"nieznana strona zlecenia ({w.get('strona')!r})"
    surowa = w.get("cena_wykonania")
    if surowa is None:
        return None, "brak ceny wykonania w exec.fill"
    try:
        wykonana = Decimal(str(surowa))
    except (InvalidOperation, ValueError):
        return None, f"cena wykonania nie jest liczbą ({surowa!r})"
    if wykonana <= 0:
        return None, "cena wykonania niedodatnia"

    odniesienie, skad = cena_odniesienia(w)
    if odniesienie is None:
        return None, ("brak ceny odniesienia — ani planu zlecenia, "
                      "ani target_price w dzienniku decyzji")

    # Dodatni = na nasza niekorzysc, po obu stronach rynku. Bez tego znaku
    # kupna i sprzedaze kasowalyby sie w sredniej przy dowolnie zlej egzekucji.
    znak = Decimal(1) if strona == "buy" else Decimal(-1)
    return float(znak * (wykonana - odniesienie) / odniesienie), skad


def _proba(rodzaj: str, xs: list[float], tozsamosci: int = 0) -> dict:
    """Statystyki jednej próby razem z werdyktem wobec założenia."""
    zalozenie = ZALOZENIE[rodzaj]
    if not xs:
        # NIE ZMIERZONO to nie to samo co ZMIERZONO ZERO i musi wygladac inaczej.
        # Zero w tym miejscu czytaloby sie jako „poslizgu nie ma", czyli jako
        # potwierdzenie zalozenia makera - z zera obserwacji.
        return {"rodzaj": rodzaj, "n": 0, "srednia": None, "mediana": None,
                "odchylenie": None, "przedzial": None, "tozsamosciowych": 0,
                "zalozenie": zalozenie, "werdykt": NIE_ZMIERZONO}
    n = len(xs)
    przedzial = przedzial_sredniej(xs)
    return {
        "rodzaj": rodzaj,
        "n": n,
        # ILE Z TYCH OBSERWACJI TO ARYTMETYKA, A NIE EGZEKUCJA. Bez tej liczby
        # proba zlozona z samych tozsamosci wyglada identycznie jak proba
        # z idealna egzekucja - a to dwa zupelnie rozne stany wiedzy.
        "tozsamosciowych": tozsamosci,
        "srednia": statistics.fmean(xs),
        # Mediana obok sredniej, bo jedno wypelnienie w cienkim arkuszu potrafi
        # przesunac srednia o wiecej niz cala mierzona wielkosc.
        "mediana": statistics.median(xs),
        "odchylenie": statistics.stdev(xs) if n > 1 else None,
        "przedzial": przedzial,
        "zalozenie": zalozenie,
        "werdykt": werdykt(n, przedzial, zalozenie, tozsamosci),
    }


def werdykt(n: int, przedzial: tuple[float, float] | None,
            zalozenie: float, tozsamosci: int = 0) -> str:
    """Czy pomiar podważa założenie. CZYSTA FUNKCJA.

    PROGIEM JEST PRZEDZIAŁ UFNOŚCI, A NIE LICZBA DOBRANA NA OKO. To ta sama
    reguła, którą dozór stosuje do odsetka okien dodatnich, i z tego samego
    powodu: bez niej różnica dwóch punktów bazowych przy trzech obserwacjach
    wyglądałaby jak odkrycie.

    Trzy stany, nie dwa — i to jest tu istota:

      * `nie zmierzono` — próba pusta; o założeniu nie wiemy NIC,
      * `za mało obserwacji` — jedno wypełnienie. Ma cenę i ma poślizg, ale
        nie ma rozrzutu, więc nie jest pomiarem poślizgu; jest anegdotą,
      * `nieodróżnialne od założenia` — zmierzone, przedział obejmuje stałą.
        To NIE znaczy „założenie potwierdzone" przy szerokim przedziale;
        znaczy „nie mamy dość danych, żeby je odrzucić",
      * `założenie poza przedziałem` — dopiero to jest powodem, żeby stałą
        ruszyć, i dopiero wtedy jest czym ją zastąpić.

    PIĄTY STAN, DOŁOŻONY 2026-09-24: `odniesienie równe cenie wykonania`.

    Gdy KAŻDA obserwacja w próbie ma cenę odniesienia równą co do grosza cenie
    wykonania, wszystkie poślizgi są zerami z arytmetyki (patrz
    `tozsamosciowy`). Przedział ufności takiej próby ma zerową szerokość i
    obejmuje założenie zero, więc poprzednia wersja wydawała werdykt
    `nieodróżnialne od założenia`. Tak właśnie raportowała czujka produkcyjna:

        poślizg maker (założenie +0.00 pb): n=4, mediana +0.00 pb
        — NIEODROZNIALNE OD ZALOZENIA

    Czyta się to jako „zmierzone, zgadza się z założeniem". Nie było zmierzone.
    Ten stan należy do rodziny `nie zmierzono`, nie do rodziny werdyktów
    o założeniu — i musi wyprzedzać sprawdzenie przedziału, bo inaczej przedział
    o zerowej szerokości sam wyda werdykt potwierdzający.
    """
    if n == 0:
        return NIE_ZMIERZONO
    if tozsamosci >= n:
        return TOZSAMOSC
    if przedzial is None:
        return ZA_MALO
    lo, hi = przedzial
    return NIEODROZNIALNE if lo <= zalozenie <= hi else ROZBIEZNE


def statystyki(wiersze: list[dict]) -> dict:
    """Poślizg per rodzaj zlecenia, z listy wierszy. CZYSTA FUNKCJA — bez bazy.

    Rozdzielenie rachunku od pobierania jest tu warunkiem sprawdzalności:
    inaczej test tej logiki wymagałby bazy z wypełnieniami, a wypełnień
    jeszcze nie ma ani jednego — czyli narzędzie do ich mierzenia nie dałoby
    się sprawdzić przed pierwszym z nich. A po pierwszym byłoby za późno:
    właśnie ono jest tą obserwacją, której nie chcemy stracić.
    """
    proby: dict[str, list[float]] = {"maker": [], "taker": []}
    # TOZSAMOSCI LICZONE OSOBNO DLA KAZDEGO RODZAJU, nie razem. Maker i taker
    # maja osobne zalozenia i osobne werdykty, wiec wspolny licznik oznaczylby
    # probe takera jako tozsamosciowa na podstawie wierszy makera.
    tozsamosci: dict[str, int] = {"maker": 0, "taker": 0}
    zrodla: dict[str, int] = {}
    pominiete: dict[str, int] = {}

    for w in wiersze:
        rodzaj = rodzaj_zlecenia(w.get("rodzaj_zlecenia"))
        poslizg, info = poslizg_wiersza(w)
        if poslizg is None:
            pominiete[info] = pominiete.get(info, 0) + 1
            continue
        if rodzaj == "nieznany":
            powod = (f"nieznany typ zlecenia ({w.get('rodzaj_zlecenia')!r}) — "
                     f"nie wiadomo, z którym założeniem to zestawić")
            pominiete[powod] = pominiete.get(powod, 0) + 1
            continue
        proby[rodzaj].append(poslizg)
        if tozsamosciowy(w):
            tozsamosci[rodzaj] += 1
        zrodla[info] = zrodla.get(info, 0) + 1

    return {
        "maker": _proba("maker", proby["maker"], tozsamosci["maker"]),
        "taker": _proba("taker", proby["taker"], tozsamosci["taker"]),
        # Skad wzieto cene odniesienia. Widoczne, bo pomiar wzgledem limitu
        # i wzgledem ceny decyzji odpowiadaja na dwa rozne pytania.
        "odniesienia": zrodla,
        "pominiete": pominiete,
    }


def ulamki(wiersze: list[dict]) -> dict:
    """Ułamek wypełnienia z zapisanych wierszy. CZYSTA FUNKCJA.

    Symulacja przyjmuje 1.0 dla każdego zlecenia, które dotknęło limitu, więc
    średnia z ułamków faktycznych jest bezpośrednią miarą tego, o ile backtest
    zawyża wielkość pozycji.

    Rachunek jest TEN SAM co w cyklu — `rozjazd_z_ulamkow` z `egzekucja.py`.
    Ułamek liczy się tam z rozstrzygnięć trzymanych w pamięci i przepada wraz
    z procesem; tutaj odczytujemy go z `exec.order.raw`, gdzie go zapisano.
    Dwie implementacje tej samej średniej rozjechałyby się przy pierwszej
    zmianie, i to niewidocznie.

    Zlecenia o rodzaju `brak` nie mają wiersza w `exec.fill` i nie mogą się tu
    pojawić — próba jest więc z natury warunkowana na „coś weszło". To jest
    zgodne z pytaniem: ułamek zerowy jest niewypełnieniem, a nie wypełnieniem
    zerowej wielkości, i `rozjazd_z_ulamkow` odsiewa go tak samo.
    """
    pary: list[tuple[str, Decimal]] = []
    bez_ulamka = 0
    for w in wiersze:
        if (w.get("zrodlo") or "") != ZRODLO_MIERZALNE:
            continue
        surowy = w.get("ulamek")
        if surowy is None:
            bez_ulamka += 1
            continue
        try:
            u = Decimal(str(surowy))
        except (InvalidOperation, ValueError):
            bez_ulamka += 1
            continue
        pary.append((w.get("rodzaj_wypelnienia") or "pelne", u))
    return rozjazd_z_ulamkow(pary) | {"bez_ulamka": bez_ulamka}


# Statusy `exec.order` (schemat: migracja 0005, plus `expired` i `unknown`
# dopisane przez `gielda_exec`). Podzial na TRZY grupy, nie dwa, bo trzecia
# nie jest jeszcze wynikiem.
STATUS_WYPELNIONE = frozenset({"filled"})
STATUS_CZESCIOWE = frozenset({"partial"})
STATUS_NIEWYPELNIONE = frozenset({"cancelled", "rejected", "expired"})
# `new` lezy jeszcze w arkuszu, `unknown` to zerwana odpowiedz gieldy. Zadne
# z nich nie rozstrzygnelo sie w zadna strone - zaliczenie ich do niewypelnien
# zanizaloby wypelnialnosc o wszystko, co akurat jest w locie.
STATUS_NIEROZSTRZYGNIETE = frozenset({"new", "unknown"})
# `raw->>'powod_odmowy_rodzaj'` zapisywany przez `gielda_exec` (kategoria z
# `bybit.py`), gdy gielda odrzucila zlecenie PostOnly, ktore zabraloby plynnosc.
ODMOWA_POSTONLY = "postonly"


def wypelnialnosc(zlecenia: list[dict]) -> dict:
    """Ile ZE ZLOZONYCH zlecen sie wypelnilo. CZYSTA FUNKCJA.

    DLACZEGO OSOBNA PROBA, SKORO `ulamki()` JUZ COS LICZY

    Bo tamta proba nie moze zawierac niewypelnienia. `exec.fill` ma wiersz
    wylacznie dla zlecenia, ktore weszlo, wiec srednia z ulamkow jest
    warunkowana na „cos weszlo" i wychodzi 1,000 niezaleznie od tego, ile
    zlecen przepadlo. Przy tysiacu wypelnien byloby to dalej 1,000 - a raport
    stawia te jedynke obok zdania „symulacja przyjmuje 1.000", czyli obok
    zalozenia, ktore ta liczba rzekomo potwierdza.

    Pytanie backtestu brzmi inaczej: symulacja przyjmuje, ze zlecenie z limitem
    wchodzi ZAWSZE, gdy swieca dotknela ceny. Odpowiada na nie `exec.order`,
    gdzie zlecenie zostawia wiersz niezaleznie od losu.

    MIANOWNIKIEM SA ZLECENIA ROZSTRZYGNIETE, nie wszystkie zlozone. Inaczej
    jedno zlecenie lezace w arkuszu w chwili pomiaru liczyloby sie jak
    niewypelnione i wypelnialnosc spadalaby przy kazdym odczycie w trakcie
    cyklu. Czesciowe wchodzi do licznika - weszlo, choc nie w calosci; jego
    wlasna wielkosc jest w `ulamki()`.

    None przy pustej probie, a nie 1,0. Jedynka byloby tu najgorsza z mozliwych
    odpowiedzi: to dokladnie ta liczba, ktora symulacja zaklada.
    """
    liczniki = {"wypelnionych": 0, "czesciowych": 0, "niewypelnionych": 0,
                "nierozstrzygnietych": 0}
    # ROZBICIE NIEWYPELNIEN (2026-10-03, audyt 3 T2). „Przepadlo 9" nie mowi,
    # czy dzialaja poprawki E1-E5 (cena PostOnly z arkusza tuz przed wysylka):
    # odrzucenie PostOnly to cena, ktora przeciela spread, anulowanie to cena,
    # ktorej rynek nie dotknal przez swiece. To dwie rozne naprawy.
    rozbicie = {"odrzuconych": 0, "odrzuconych_postonly": 0, "anulowanych": 0,
                "wygaslych": 0}
    nieznany: dict[str, int] = {}
    for z in zlecenia:
        s = (z.get("status") or "").strip().lower()
        if s in STATUS_WYPELNIONE:
            liczniki["wypelnionych"] += 1
        elif s in STATUS_CZESCIOWE:
            liczniki["czesciowych"] += 1
        elif s in STATUS_NIEWYPELNIONE:
            liczniki["niewypelnionych"] += 1
            if s == "rejected":
                rozbicie["odrzuconych"] += 1
                if (z.get("powod_odmowy") or "") == ODMOWA_POSTONLY:
                    rozbicie["odrzuconych_postonly"] += 1
            elif s == "cancelled":
                rozbicie["anulowanych"] += 1
            else:
                rozbicie["wygaslych"] += 1
        elif s in STATUS_NIEROZSTRZYGNIETE:
            liczniki["nierozstrzygnietych"] += 1
        else:
            # NIE ZGADUJEMY. Status spoza znanego zbioru nie trafia do zadnej
            # grupy i jest widoczny z nazwa - tak samo jak nieznany typ zlecenia
            # w `statystyki()`. Cicha kwalifikacja na chybil trafil przesuwalaby
            # wypelnialnosc w strone zalezna od literowki w cudzym kodzie.
            nieznany[s or "(puste)"] = nieznany.get(s or "(puste)", 0) + 1
    rozstrzygniete = (liczniki["wypelnionych"] + liczniki["czesciowych"]
                      + liczniki["niewypelnionych"])
    udzial = (None if rozstrzygniete == 0 else
              (liczniki["wypelnionych"] + liczniki["czesciowych"]) / rozstrzygniete)
    return {"wyslanych": len(zlecenia), "rozstrzygnietych": rozstrzygniete,
            "udzial": udzial, "nieznany_status": nieznany} | liczniki | rozbicie


def rozjazd_ksiag(wiersze: list[dict]) -> dict:
    """Odleglosc miedzy cena PLANU a cena WYSLANA. CZYSTA FUNKCJA.

    TO NIE JEST POSLIZG I NIE WOLNO TEGO TAK NAZWAC. Poslizg jest kosztem
    egzekucji - placi sie go gieldzie. To tutaj jest roznica miedzy dwiema
    NASZYMI cenami: ta, na ktorej stanal plan (swiece podmiotu globalnego,
    na nich trenowany jest model), i ta, ktora `zlecenia.sprawdz_zlecenie`
    wyslalo po podmianie na `cena_makera` z ksiegi docelowej. Do 2026-09-22
    wielkosc ta byla wliczana w poslizg wyjscia i sama w sobie wystarczala,
    zeby moduł zazadal podniesienia stalej kosztowej.

    Mierzymy ja, bo jest czym: rozjazd ksiag jest jedynym powodem, dla ktorego
    podmiana w ogole istnieje, a jego skala decyduje o tym, jak daleko od
    modelu stoi realizacja. Wartosc BEZWZGLEDNA - kierunek zalezy od strony
    rynku i usredniony kasowalby sie do zera przy dowolnie duzym rozjezdzie.
    """
    xs: list[float] = []
    for w in wiersze:
        if (w.get("zrodlo") or "") != ZRODLO_MIERZALNE:
            continue
        plan, zlecenie = w.get("cena_planu"), w.get("cena_zlecenia")
        if plan is None or zlecenie is None:
            continue
        try:
            p, z = Decimal(str(plan)), Decimal(str(zlecenie))
        except (InvalidOperation, ValueError):
            continue
        if z <= 0:
            continue
        xs.append(abs(float((p - z) / z)))
    if not xs:
        return {"n": 0, "mediana": None, "najwiekszy": None, "zerowych": 0}
    # ILE Z TYCH ROZJAZDOW TO DEFINICYJNE ZERA.
    #
    # Podmiana ceny na `cena_makera` zachodzi WYLACZNIE przy wyjsciu
    # (`zlecenia.sprawdz_zlecenie`). Przy wejsciu plan ROWNA SIE cenie wyslanej
    # z zalozenia, wiec jego zero nie jest pomiarem rozjazdu - jest jego
    # definicyjnym brakiem. Zmierzone na komplecie pieciu wypelnien
    # produkcyjnych: dwa wyjscia daja 41,56 pb i 5,34 pb (mediana 23,45 pb),
    # a dwa wejscia dokladane do tej samej mediany sciagaja ja na 2,67 pb -
    # prawie dziesieciokrotnie, i tym mocniej, im wiecej wejsc w probie.
    #
    # Mediana zostaje liczona ze WSZYSTKICH wierszy, bo odsianie ich tutaj
    # wymagaloby wiedzy, ktorej ten modul nie ma (wejscie czy wyjscie nie stoi
    # w zadnej z pobieranych kolumn). Liczba zer stoi obok, zeby czytajacy
    # wiedzial, z czego ta mediana powstala.
    return {"n": len(xs), "mediana": statistics.median(xs), "najwiekszy": max(xs),
            "zerowych": sum(1 for x in xs if x == 0.0)}


def _dec(x) -> Decimal | None:
    if x is None:
        return None
    try:
        d = Decimal(str(x))
    except (InvalidOperation, ValueError):
        return None
    return d if d > 0 else None


def rozbicie_do_celu(wiersze: list[dict]) -> dict:
    """`fill - target_price` rozbite na DWA skladniki. CZYSTA FUNKCJA.

    PO CO (audyt F-4.2). Kupna wypelnialy sie 76-95 pb nad cena decyzji przy
    zwloce 63-88 min. Sama suma nie mowi, czy to zwloka (rynek odjechal, zanim
    wyslalismy), czy egzekucja (spread ksiegi EU, tick, baza USDC). Mid
    arkusza z chwili wysylki dzieli ja na:

      * `do_wysylki`   = (mid_wysylki - target) / target - ruch od zamkniecia
                         swiecy do wysylki; lekarstwo: szybszy potok (F-4.1),
      * `od_wysylki`   = (fill - mid_wysylki) / target - spread i ruch w czasie
                         lezenia zlecenia; lekarstwo: egzekucja.

    Ten sam mianownik w obu, wiec skladniki SUMUJA SIE dokladnie do calosci.
    Znak jak w calym module: dodatni = na nasza niekorzysc (kupno drozej,
    sprzedaz taniej).

    Per strona rynku, bo kryterium akceptacji jest na kupnach (srednia
    `fill/target - 1` <= 10 pb na 20 kupnach), a sprzedaze maja inny powod
    zwloki (wyjscie nie czeka na prog).
    """
    proby: dict[str, dict[str, list[float]]] = {
        s: {"calosc": [], "do_wysylki": [], "od_wysylki": []}
        for s in ("buy", "sell")}
    pominiete: dict[str, int] = {}

    def pomin(powod):
        pominiete[powod] = pominiete.get(powod, 0) + 1

    for w in wiersze:
        if (w.get("zrodlo") or "") != ZRODLO_MIERZALNE:
            continue
        strona = (w.get("strona") or "").strip().lower()
        if strona not in proby:
            pomin("nieznana strona")
            continue
        cel, mid, fill = (_dec(w.get("cena_decyzji")), _dec(w.get("mid_wysylki")),
                          _dec(w.get("cena_wykonania")))
        if cel is None or fill is None:
            pomin("brak target_price albo ceny wykonania")
            continue
        if mid is None:
            pomin("brak mid z chwili wysyłki (zlecenie sprzed 2026-10-03 "
                  "albo arkusz nieodczytany)")
            continue
        znak = Decimal(1) if strona == "buy" else Decimal(-1)
        p = proby[strona]
        p["calosc"].append(float(znak * (fill - cel) / cel))
        p["do_wysylki"].append(float(znak * (mid - cel) / cel))
        p["od_wysylki"].append(float(znak * (fill - mid) / cel))

    def opisz(xs):
        return {"n": len(xs),
                "srednia": statistics.fmean(xs) if xs else None,
                "przedzial": przedzial_sredniej(xs)}

    return {s: {k: opisz(v) for k, v in p.items()} for s, p in proby.items()} \
        | {"pominiete": pominiete}


def udzial_wyjsc_takerem(zlecenia: list[dict]) -> dict:
    """Jaki ulamek WYJSC poszedl agresywnie (GTC bez PostOnly). CZYSTA FUNKCJA.

    To jest wejscie `koszty.karta_rundy(wyjscia_takerem=...)`. Mianownik to
    wyjscia, ktore maja pole `wyjscie_agresywne` (pisane od 2026-10-03 przy
    kazdym wyjsciu); wiersze bez niego nie sa ani agresywne, ani pasywne -
    nie wiemy. None przy pustej probie: zero byloby twierdzeniem "zadne
    wyjscie nie eskalowalo".
    """
    def flaga(z):
        v = z.get("wyjscie_agresywne")
        if isinstance(v, bool) or v is None:
            return v
        return {"true": True, "false": False}.get(str(v).strip().lower())

    flagi = [f for f in (flaga(z) for z in zlecenia) if f is not None]
    agresywnych = sum(1 for f in flagi if f)
    return {"wyjsc": len(flagi), "agresywnych": agresywnych,
            "udzial": agresywnych / len(flagi) if flagi else None}


def pomiar(wiersze: list[dict]) -> dict:
    """Cały pomiar naraz: poślizg per rodzaj + ułamek wypełnienia."""
    return statystyki(wiersze) | {"ulamek": ulamki(wiersze),
                                  "rozjazd_ksiag": rozjazd_ksiag(wiersze),
                                  "do_celu": rozbicie_do_celu(wiersze)}


# ------------------------------------------------------------------ wypisanie

def _bp(x: float | None, znak: bool = True) -> str:
    """Punkty bazowe. Myslnik, gdy liczby NIE MA - zero byloby klamstwem.

    Znak jest wypisywany przy wielkosciach, ktore go maja (poslizg: dodatni to
    koszt, ujemny to zysk), i pomijany przy rozrzucie - odchylenie ze znakiem
    sugerowaloby kierunek, ktorego nie ma.
    """
    if x is None:
        return "—"
    return f"{x * 1e4:+.2f} pb" if znak else f"{x * 1e4:.2f} pb"


def opis(p: dict) -> list[str]:
    """Pomiar zamieniony na wiersze do wypisania.

    KAŻDY WIERSZ NIESIE LICZBĘ OBSERWACJI. Średnia bez `n` jest w tym miejscu
    najgorszym możliwym skrótem: przy trzech wypełnieniach wygląda dokładnie
    tak samo jak przy trzystu, a prowadzi do zupełnie innej decyzji.
    """
    linie = []
    for rodzaj in ("maker", "taker"):
        s = p[rodzaj]
        etykieta = f"poślizg {rodzaj} (założenie {_bp(s['zalozenie'])})"
        if s["werdykt"] == NIE_ZMIERZONO:
            linie.append(f"{etykieta}: n=0 — NIE ZMIERZONO. Stała pozostaje "
                         f"założeniem; to nie jest pomiar zerowego poślizgu.")
            continue
        if s["werdykt"] == TOZSAMOSC:
            # ANI SREDNIEJ, ANI MEDIANY, ANI PRZEDZIALU. Wszystkie trzy sa tu
            # zerami i kazda z nich, wypisana obok slowa „zmierzono", czyta sie
            # jako potwierdzenie stalej. Liczba, ktorej nie ma, ma wygladac na
            # nieobecna - to ta sama zasada, co przy pustej probie wyzej.
            linie.append(
                f"{etykieta}: n={s['n']}, ale we WSZYSTKICH {s['tozsamosciowych']} "
                f"wierszach cena odniesienia = cena wykonania — TO NIE JEST "
                f"POMIAR POŚLIZGU, tylko (x−x)/x. Zlecenie PostOnly wypełnia "
                f"się po cenie limitu Z DEFINICJI, więc poślizgu CENOWEGO "
                f"makera nie ma czego mierzyć — i żadna liczba wypełnień tego "
                f"nie zmieni. Koszt makera siedzi w NIEWYPEŁNIENIU: patrz "
                f"wypełnialność z `exec.order` niżej.")
            continue
        przedzial = ("—" if s["przedzial"] is None
                     else f"[{_bp(s['przedzial'][0])}, {_bp(s['przedzial'][1])}]")
        # Udzial tozsamosci wypisany TAKZE przy probie mieszanej: kazdy taki
        # wiersz ciagnie srednia do zera, wiec proba w polowie tozsamosciowa
        # raportuje polowe prawdziwego poslizgu i wyglada jak lepsza egzekucja.
        arytmetyka = (f", z tego {s['tozsamosciowych']} zer z arytmetyki "
                      f"(odniesienie = cena wykonania)"
                      if s["tozsamosciowych"] else "")
        linie.append(
            f"{etykieta}: n={s['n']}{arytmetyka}, mediana {_bp(s['mediana'])}, "
            f"średnia {_bp(s['srednia'])}, odch. {_bp(s['odchylenie'], znak=False)}, "
            f"przedział 95% {przedzial} — {s['werdykt'].upper()}")

    u = p["ulamek"]
    if u["n"] == 0:
        linie.append("ułamek wypełnienia: n=0 — NIE ZMIERZONO. Symulacja nadal "
                     "przyjmuje 1.0 i zawyża wielkość pozycji o nieznaną wartość.")
    else:
        przedzial = ("—" if u["przedzial"] is None
                     else f"[{u['przedzial'][0]:.3f}, {u['przedzial'][1]:.3f}]")
        odch = "—" if u["odchylenie"] is None else f"{u['odchylenie']:.3f}"
        linie.append(
            f"ułamek wypełnienia: n={u['n']}, średnio {float(u['sredni_ulamek']):.3f}"
            f" (symulacja przyjmuje 1.000), odch. {odch}, "
            f"przedział 95% {przedzial}; częściowych {u['czesciowych']}, "
            f"nie do zamknięcia {u['nie_do_zamkniecia']}")
        # ZDANIE, BEZ KTOREGO POWYZSZA LINIA CZYTA SIE JAK POTWIERDZENIE.
        #
        # `exec.fill` ma wiersz wylacznie dla zlecenia, ktore weszlo, wiec ta
        # srednia jest warunkowana na „cos weszlo" i wyjdzie 1,000 takze wtedy,
        # gdy przepadla wiekszosc zlecen. Docstring `ulamki()` mowi o tym
        # wprost - ale czytany jest raport, nie docstring.
        linie.append(
            "  UWAGA: ta próba nie zawiera NIEWYPEŁNIEŃ i zawierać ich nie "
            "może — `exec.fill` ma wiersz tylko dla zlecenia, które weszło. "
            "Założenia symulacji (limit wchodzi zawsze, gdy świeca dotknęła "
            "ceny) nie da się tą średnią ani potwierdzić, ani obalić; "
            "odpowiada na to wypełnialność z `exec.order` niżej.")

    # WYPELNIALNOSC: jedyna proba, w ktorej niewypelnienie MOZE sie pojawic.
    wyp = p.get("wypelnialnosc")
    if wyp is not None:
        if wyp["udzial"] is None:
            linie.append(
                f"wypełnialność: {wyp['wyslanych']} złożonych zleceń, "
                f"0 rozstrzygniętych — NIE ZMIERZONO. Symulacja nadal przyjmuje, "
                f"że zlecenie z limitem wchodzi zawsze, gdy świeca dotknęła ceny.")
        else:
            linie.append(
                f"wypełnialność: {wyp['wypelnionych'] + wyp['czesciowych']} "
                f"z {wyp['rozstrzygnietych']} rozstrzygniętych zleceń weszło "
                f"({wyp['udzial']:.1%}), symulacja przyjmuje 100,0%; "
                f"{wyp['wyslanych']} złożonych, w locie {wyp['nierozstrzygnietych']}, "
                f"przepadło {wyp['niewypelnionych']}")
        if wyp["niewypelnionych"]:
            linie.append(
                f"  przepadłe: odrzucone {wyp['odrzuconych']} (w tym PostOnly "
                f"{wyp['odrzuconych_postonly']}), anulowane {wyp['anulowanych']}, "
                f"wygasłe {wyp['wygaslych']}")
        for s, n in sorted(wyp["nieznany_status"].items()):
            linie.append(f"  nieznany status zlecenia {s!r}: {n} — poza rachunkiem")

    # OSOBNA LINIA, OSOBNA NAZWA. Wpisana w poslizg ta liczba kazalaby podniesc
    # stala kosztowa; wypisana obok mowi, jak daleko ksiega docelowa stoi od tej,
    # na ktorej stanal plan - i nic ponadto.
    r = p.get("rozjazd_ksiag") or {"n": 0}
    if r["n"]:
        # LICZBA ZER OBOK MEDIANY. Przy wejsciach podmiany ceny nie ma, wiec ich
        # zero jest definicyjne, a nie zmierzone - wrzucone do wspolnej mediany
        # rozciencza ja tym mocniej, im wiecej wejsc w probie.
        zera = (f", z tego {r['zerowych']} zerowe (plan = cena wysłana — przy "
                f"wejściach podmiany nie ma, więc to zero jest definicyjne)"
                if r.get("zerowych") else "")
        linie.append(
            f"rozjazd ksiąg (plan vs cena wysłana, NIE poślizg): n={r['n']}"
            f"{zera}, mediana {_bp(r['mediana'], znak=False)}, "
            f"największy {_bp(r['najwiekszy'], znak=False)}")

    # ROZBICIE `fill - target` (F-4.2): SKAD sie bierze odleglosc od ceny decyzji.
    dc = p.get("do_celu")
    if dc is not None:
        for strona, nazwa in (("buy", "kupna"), ("sell", "sprzedaże")):
            r = dc[strona]
            if not r["calosc"]["n"]:
                linie.append(f"fill − target ({nazwa}): n=0 — NIE ZMIERZONO "
                             f"(brak mid z chwili wysyłki)")
                continue

            def czlon(x):
                prz = x["przedzial"]
                return (f"{_bp(x['srednia'])}"
                        + ("" if prz is None else f" [{_bp(prz[0])}, {_bp(prz[1])}]"))
            linie.append(
                f"fill − target ({nazwa}): n={r['calosc']['n']}, średnio "
                f"{czlon(r['calosc'])} = do wysyłki {czlon(r['do_wysylki'])} "
                f"+ od wysyłki {czlon(r['od_wysylki'])}")
        for powod, n in sorted(dc["pominiete"].items()):
            linie.append(f"  fill − target, pominięto {n}: {powod}")

    tk = p.get("wyjscia_takerem")
    if tk is not None:
        if tk["udzial"] is None:
            linie.append("wyjścia takerem: n=0 — NIE ZMIERZONO (brak wyjść "
                         "z polem `wyjscie_agresywne`)")
        else:
            linie.append(f"wyjścia takerem: {tk['agresywnych']} z {tk['wyjsc']} "
                         f"({tk['udzial']:.1%}) — GTC bez PostOnly po dwóch "
                         f"odrzuceniach; do `karta_rundy(wyjscia_takerem=...)`")

    for skad, n in sorted(p["odniesienia"].items()):
        linie.append(f"  cena odniesienia z: {skad} ({n})")
    for powod, n in sorted(p["pominiete"].items()):
        linie.append(f"  pominięto {n}: {powod}")
    if p["ulamek"]["bez_ulamka"]:
        linie.append(f"  bez zapisanego ułamka: {p['ulamek']['bez_ulamka']} "
                     f"— wypełnienie z giełdy bez `ulamek_wypelnienia` w raw")
    return linie


# ------------------------------------------------- selekcja negatywna (F-4.5)
#
# CO TRACIMY PRZEZ NIEWYPELNIENIE (audyt Fable F-4.5, 2026-10-03).
# Wypelnialnosc mowi, ILE zlecen przepada, ale nie CZYM sie roznia. Zlecenie
# PostOnly pod rynkiem wypelnia sie wtedy, gdy rynek do niego zejdzie - czyli
# czesciej w zlych wejsciach niz w dobrych. Miara: sredni zwrot naprzod od
# ceny zlecenia dla wejsc wypelnionych i niewypelnionych oraz ich ROZNICA
# (niewypelnione - wypelnione; dodatnia = selekcja negatywna). Analiza z 03.10
# (§3.4) dala +407 pb / 48 h, CI [+70; +773] - policzona recznie, raz.
#
# BOOTSTRAP, NIE t-Studenta: kilkanascie obserwacji z grubymi ogonami, a przy
# roznicy dwoch prob o roznych wariancjach t i tak bylby przyblizeniem.
# Ziarno stale - ten sam stan bazy daje ten sam przedzial w kazdym przebiegu.
HORYZONTY_NAPRZOD_H = (24, 48)
BOOTSTRAP_LOSOWAN = 2000
BOOTSTRAP_ZIARNO = 20261003


def bootstrap_przedzial(xs: list[float], *, losowan: int = BOOTSTRAP_LOSOWAN,
                        ziarno: int = BOOTSTRAP_ZIARNO) -> tuple[float, float] | None:
    """Percentylowy przedzial 95% sredniej. None przy n < 2 - jedna obserwacja
    nie mowi o rozrzucie nic. CZYSTA FUNKCJA."""
    if len(xs) < 2:
        return None
    los = random.Random(ziarno)
    n = len(xs)
    srednie = sorted(statistics.fmean(los.choices(xs, k=n)) for _ in range(losowan))
    return srednie[int(0.025 * losowan)], srednie[int(0.975 * losowan) - 1]


def bootstrap_roznicy(a: list[float], b: list[float], *,
                      losowan: int = BOOTSTRAP_LOSOWAN,
                      ziarno: int = BOOTSTRAP_ZIARNO) -> tuple[float, float] | None:
    """Przedzial 95% dla srednia(a) - srednia(b), proby losowane niezaleznie."""
    if len(a) < 2 or len(b) < 2:
        return None
    los = random.Random(ziarno)
    roznice = sorted(statistics.fmean(los.choices(a, k=len(a)))
                     - statistics.fmean(los.choices(b, k=len(b)))
                     for _ in range(losowan))
    return roznice[int(0.025 * losowan)], roznice[int(0.975 * losowan) - 1]


def _grupa(xs: list[float]) -> dict:
    return {"n": len(xs), "srednia": statistics.fmean(xs) if xs else None,
            "przedzial": bootstrap_przedzial(xs)}


def zwrot_naprzod(wiersze: list[dict]) -> dict:
    """Zwrot naprzod wejsc wypelnionych i niewypelnionych. CZYSTA FUNKCJA.

    Wiersz: `status`, `strona` (buy/sell), `cena` (limit zlecenia), `cena_24h`,
    `cena_48h` (zamkniecie swiecy 1h najblizej chwili wysylki + H; None, gdy
    tej chwili jeszcze nie bylo). Zwrot ze znakiem strony: dla sprzedazy
    spadek ceny jest zyskiem. Zlecenia w locie (`new`, `unknown`) poza proba.
    """
    wynik = {}
    for godz in HORYZONTY_NAPRZOD_H:
        grupy: dict[str, list[float]] = {"wypelnione": [], "niewypelnione": []}
        for w in wiersze:
            s = (w.get("status") or "").strip().lower()
            if s in STATUS_WYPELNIONE | STATUS_CZESCIOWE:
                grupa = "wypelnione"
            elif s in STATUS_NIEWYPELNIONE:
                grupa = "niewypelnione"
            else:
                continue
            cena, potem = w.get("cena"), w.get(f"cena_{godz}h")
            if not cena or potem is None:
                continue
            znak = 1 if w.get("strona") == "buy" else -1
            grupy[grupa].append(znak * float(Decimal(potem) / Decimal(cena) - 1))
        a, b = grupy["niewypelnione"], grupy["wypelnione"]
        wynik[godz] = {
            "wypelnione": _grupa(b), "niewypelnione": _grupa(a),
            "roznica": {"srednia": (statistics.fmean(a) - statistics.fmean(b))
                        if a and b else None,
                        "przedzial": bootstrap_roznicy(a, b)}}
    return wynik


def _ci(p: tuple[float, float] | None) -> str:
    return "CI —" if p is None else f"CI [{_bp(p[0])}; {_bp(p[1])}]"


def opis_selekcji(z: dict) -> list[str]:
    """Wiersze raportu. Kazdy niesie `n` - srednia z dwoch obserwacji wyglada
    tak samo jak z dwustu."""
    if all(z[g]["wypelnione"]["n"] + z[g]["niewypelnione"]["n"] == 0 for g in z):
        return ["selekcja negatywna (zwrot naprzód wejść): NIE ZMIERZONO — brak "
                "rozstrzygniętych wejść z ceną po 24 h"]
    linie = ["selekcja negatywna — zwrot naprzód od ceny zlecenia, wejścia "
             "giełdowe (różnica = niewypełnione − wypełnione; dodatnia = "
             "przepadają lepsze wejścia):"]
    for godz, g in z.items():
        w, nw, r = g["wypelnione"], g["niewypelnione"], g["roznica"]
        linie.append(
            f"  {godz} h: wypełnione n={w['n']} {_bp(w['srednia'])} {_ci(w['przedzial'])}"
            f" | niewypełnione n={nw['n']} {_bp(nw['srednia'])} {_ci(nw['przedzial'])}"
            f" | różnica {_bp(r['srednia'])} {_ci(r['przedzial'])}")
    return linie


# ------------------------------------- realizacja wobec modelu pesymistycznego

# CO ZAKLADAL MODEL, POD KTORYM LICZONO H-1 I H-3 (renacode-infra: docs/projekty/trader/hipotezy.md). Przebieg
# demo strategii `momentum_filtr_trendu` (E-1) istnieje po to, zeby te liczby
# zestawic z gielda. IMPORTOWANE tam, gdzie sa stalymi kodu; wypelnialnosc to
# WYNIK symulacji H-3 (wariant F, 5 przesuniec: 77,0-79,6%), wiec stoi tu jako
# liczba z rejestru, nie z kodu.
def model_pesymistyczny(kind: str = "spot_eu") -> dict:
    from services.engine.koszty import koszty
    from services.engine.r4_momentum import PARAMETRY_R4
    k = koszty(kind)
    return {"opoznienie_min": PARAMETRY_R4["opoznienie_min"],
            "wypelnialnosc_wejsc": (0.770, 0.796),
            "prowizja_wejscia": k.maker, "prowizja_wyjscia": k.taker,
            "poslizg_wyjscia": POSLIZG_TAKER}


def koniec_swiecy_decyzji(decyzja, bar_minutes: int = 240):
    """Zamkniecie swiecy, ktorej dotyczy decyzja. CZYSTA FUNKCJA.

    Decyzja dla swiecy B zapada w oknie [B + dlugosc, B + 2 dlugosci)
    (`loop.decided_for_bar`), wiec zamkniecie to `decided_at` zaokraglone
    w dol do siatki swiec - bez kolumny `bar_start`, ktorej dziennik nie ma.
    """
    from datetime import datetime, timezone
    krok = bar_minutes * 60
    return datetime.fromtimestamp(int(decyzja.timestamp()) // krok * krok,
                                  tz=timezone.utc)


def _plany_wejsc(zlecenia: list[dict]) -> list[dict]:
    """Zlecenia WEJSC, jeden wiersz na plan (najwczesniejsza wysylka, najlepszy
    status). Wiecej niz jeden wiersz `exec.order` na plan liczylby jedno
    wejscie kilka razy - w opoznieniu i w wypelnialnosci."""
    ranga = {**{x: 3 for x in STATUS_WYPELNIONE}, **{x: 2 for x in STATUS_CZESCIOWE},
             **{x: 1 for x in STATUS_NIEROZSTRZYGNIETE}}
    plany: dict = {}
    for z in zlecenia:
        if (z.get("rodzaj_planu") or "wejscie") != "wejscie":
            continue
        klucz = z.get("planned_order_id") or id(z)
        dot = plany.get(klucz)
        if dot is None:
            plany[klucz] = dict(z)
            continue
        if ranga.get((z.get("status") or "").lower(), 0) > \
                ranga.get((dot.get("status") or "").lower(), 0):
            dot["status"] = z.get("status")
        if z.get("submitted_at") and (dot.get("submitted_at") is None
                                      or z["submitted_at"] < dot["submitted_at"]):
            dot["submitted_at"] = z["submitted_at"]
    return list(plany.values())


def opoznienie_wysylki(zlecenia: list[dict], bar_minutes: int = 240) -> dict:
    """Minuty od zamkniecia swiecy decyzyjnej do wyslania wejscia. CZYSTA.

    Model pesymistyczny przyjal 20 min i ta liczba nie byla dotad zmierzona
    (H-1, "czego wynik nie rozstrzyga"). Tu jest jej pomiar.
    """
    minuty = sorted(
        (z["submitted_at"] - koniec_swiecy_decyzji(z["decided_at"], bar_minutes))
        .total_seconds() / 60
        for z in _plany_wejsc(zlecenia)
        if z.get("submitted_at") is not None and z.get("decided_at") is not None)
    if not minuty:
        return {"n": 0, "mediana": None, "p90": None, "max": None}
    return {"n": len(minuty), "mediana": statistics.median(minuty),
            "p90": minuty[min(len(minuty) - 1, int(0.9 * len(minuty)))],
            "max": minuty[-1]}


def stawka_prowizji(w: dict) -> float | None:
    """Prowizja wypelnienia jako ulamek notionalu, w walucie kwotowania.

    Bybit spot pobiera prowizje kupna w walucie BAZOWEJ (BTC przy BTCUSDC),
    sprzedazy w kwotowanej - `fee_ccy` mowi ktora. Waluta, ktora nie jest
    sufiksem symbolu, jest bazowa i przeliczamy ja po cenie wypelnienia.
    """
    cena, ilosc, fee = (_dec(w.get("cena_wykonania")), _dec(w.get("ilosc")),
                        _dec(w.get("prowizja")))
    if cena is None or ilosc is None or fee is None or cena <= 0 or ilosc <= 0:
        return None
    waluta = (w.get("waluta_prowizji") or "").strip().upper()
    symbol = (w.get("symbol") or "").upper()
    kwota = fee if (not waluta or symbol.endswith(waluta)) else fee * cena
    return float(kwota / (cena * ilosc))


def prowizje(wiersze: list[dict]) -> dict:
    """Zrealizowana stawka prowizji osobno dla wejsc i wyjsc. CZYSTA."""
    out = {}
    for rodzaj in ("wejscie", "wyjscie"):
        xs = [x for x in (stawka_prowizji(w) for w in wiersze
                          if (w.get("rodzaj_planu") or "wejscie") == rodzaj)
              if x is not None]
        out[rodzaj] = {"n": len(xs),
                       "srednia": statistics.fmean(xs) if xs else None}
    return out


def poslizg_od_decyzji(wiersze: list[dict], rodzaj: str) -> dict:
    """Wypelnienie wzgledem ceny decyzji (zamkniecia swiecy), ze znakiem
    "dodatni = na nasza niekorzysc". CZYSTA FUNKCJA.

    Dla WYJSC to jest wprost to, co modeluje H-3: wyjscie takerem po cenie
    z chwili wysylki minus `POSLIZG_TAKER` - czyli oczekiwany koszt wzgledem
    decyzji to 4 pb plus dryf, ktory w sredniej znika. Typ zlecenia nie
    rozstrzyga tu niczego: wyjscie agresywne (T6) idzie limitem GTC przez
    spread i placi takera, a `statystyki` zaliczylyby je do makera.
    """
    xs = []
    for w in wiersze:
        if (w.get("rodzaj_planu") or "wejscie") != rodzaj:
            continue
        cel, wyk = _dec(w.get("cena_decyzji")), _dec(w.get("cena_wykonania"))
        if cel is None or wyk is None or cel <= 0:
            continue
        roznica = float((wyk - cel) / cel)
        xs.append(roznica if (w.get("strona") or "").lower() == "buy" else -roznica)
    return {"n": len(xs), "srednia": statistics.fmean(xs) if xs else None,
            "przedzial": przedzial_sredniej(xs) if len(xs) >= 2 else None}


def wobec_modelu(wiersze: list[dict], zlecenia: list[dict],
                 bar_minutes: int = 240, kind: str = "spot_eu") -> dict:
    """Realizacja z gieldy obok zalozen modelu pesymistycznego. CZYSTA."""
    return {"model": model_pesymistyczny(kind),
            "wypelnialnosc_wejsc": wypelnialnosc(_plany_wejsc(zlecenia)),
            "opoznienie": opoznienie_wysylki(zlecenia, bar_minutes),
            "prowizje": prowizje(wiersze),
            "wyjscia": poslizg_od_decyzji(wiersze, "wyjscie"),
            "wejscia": poslizg_od_decyzji(wiersze, "wejscie")}


def opis_wobec_modelu(m: dict) -> list[str]:
    """Wiersze raportu. Kazdy z `n`; brak pomiaru jest napisany slowami."""
    mod = m["model"]
    lo, hi = mod["wypelnialnosc_wejsc"]
    w = m["wypelnialnosc_wejsc"]
    o = m["opoznienie"]
    linie = ["realizacja wobec modelu pesymistycznego (H-1/H-3; strategia "
             "demo E-1):"]
    linie.append(
        "  wypełnialność wejść: "
        + ("NIE ZMIERZONO (brak rozstrzygniętych wejść)" if w["udzial"] is None else
           f"{w['udzial']:.1%} z {w['rozstrzygnietych']} planów")
        + f" — model {lo:.1%}–{hi:.1%}")
    linie.append(
        "  opóźnienie wysyłki wejścia od zamknięcia świecy: "
        + ("NIE ZMIERZONO" if not o["n"] else
           f"n={o['n']}, mediana {o['mediana']:.1f} min, p90 {o['p90']:.1f}, "
           f"max {o['max']:.1f}")
        + f" — model {mod['opoznienie_min']} min")
    for rodzaj, zal in (("wejscie", mod["prowizja_wejscia"]),
                        ("wyjscie", mod["prowizja_wyjscia"])):
        p = m["prowizje"][rodzaj]
        linie.append(
            f"  prowizja {'wejścia' if rodzaj == 'wejscie' else 'wyjścia'}: "
            + ("NIE ZMIERZONO" if not p["n"] else
               f"n={p['n']}, średnio {_bp(p['srednia'], znak=False)}")
            + f" — model {_bp(zal, znak=False)}")
    t = m["wyjscia"]
    linie.append(
        "  wyjście względem ceny decyzji: "
        + ("NIE ZMIERZONO" if not t["n"] else
           f"n={t['n']}, średnio {_bp(t['srednia'])} {_ci(t['przedzial'])}")
        + f" — model {_bp(mod['poslizg_wyjscia'])} (taker) plus dryf 20 min")
    e = m["wejscia"]
    linie.append(
        "  wypełnione wejście względem ceny decyzji: "
        + ("NIE ZMIERZONO" if not e["n"] else
           f"n={e['n']}, średnio {_bp(e['srednia'])} {_ci(e['przedzial'])}")
        + " — model: limit pół spreadu pod ceną z chwili wysyłki, wypełnienie "
          "tylko przy ruchu przeciw nam (selekcja: patrz zwrot naprzód)")
    return linie


# -------------------------------------------------------------- warstwa danych

# Zapytanie zakotwiczone na `exec.fill`, NIE na `exec.order`. Jeden plan ma
# w `exec.order` DWA wiersze - ten ze zlozenia i ten z rozstrzygniecia - a oba
# dostaja to samo `raw` przez `raw = raw || meta`. Liczenie po zleceniach
# podwajaloby wiec kazda obserwacje, i to niewidocznie: srednia wyszlaby ta sama,
# a `n` dwa razy za duze, czyli przedzial ufnosci o 40% za waski.
ZAPYTANIE = """
SELECT o.order_type                     AS rodzaj_zlecenia,
       o.side                           AS strona,
       o.raw->>'execution'              AS zrodlo,
       o.raw->>'ulamek_wypelnienia'     AS ulamek,
       o.raw->>'rodzaj'                 AS rodzaj_wypelnienia,
       o.limit_price                    AS cena_zlecenia,
       l.cena_odniesienia               AS cena_arkusza,
       l.bid                            AS bid_wysylki,
       l.ask                            AS ask_wysylki,
       p.limit_price                    AS cena_planu,
       d.target_price                   AS cena_decyzji,
       -- Mid ksiegi egzekucji z chwili wysylki - tylko z odczytu, ktory
       -- `arkusz_wysylki` przyjal (bez `powod_braku`): mid z arkusza
       -- odrzuconego jako stary opisywalby inna chwile niz wysylka.
       CASE WHEN o.raw->'arkusz_wysylki'->>'powod_braku' IS NULL
            THEN o.raw->'arkusz_wysylki'->>'mid_eu_przy_wysylce' END
                                        AS mid_wysylki,
       f.price                          AS cena_wykonania,
       f.qty                            AS ilosc,
       f.fee                            AS prowizja,
       f.fee_ccy                        AS waluta_prowizji,
       f.event_time                     AS czas,
       i.symbol                         AS symbol,
       p.trigger->>'rodzaj'             AS rodzaj_planu
  FROM exec.fill f
  JOIN exec.order o           ON o.id = f.order_id
  JOIN ref.instrument i       ON i.id = o.instrument_id
  LEFT JOIN journal.planned_order p ON p.id = o.planned_order_id
  LEFT JOIN journal.decision d      ON d.id = o.decision_id
  -- Dziennik likwidacji trzyma jedyna cene odniesienia, jaka ma zlecenie
  -- RYNKOWE: bid albo ask z arkusza sprzed wyslania. `LEFT JOIN`, bo dla
  -- kazdego zlecenia z limitem tej tabeli nie ma i nie ma jej miec.
  LEFT JOIN exec.likwidacja l       ON l.order_id = o.id
 WHERE o.engine::text = %(engine)s
   AND o.mode::text = %(mode)s
   AND (%(h)s::int IS NULL
        OR f.event_time > now() - make_interval(hours => %(h)s::int))
   AND (%(strategia)s::text IS NULL OR d.strategia = %(strategia)s)
 ORDER BY f.event_time
"""


# Zakotwiczone na `exec.order`, NIE na `exec.fill` - i to jest cala roznica.
# Zlecenie zostawia tu wiersz niezaleznie od tego, czy weszlo, wiec dopiero ta
# proba moze zawierac niewypelnienie. Bez `JOIN exec.fill`, bo szukamy wlasnie
# tych, ktore wypelnienia nie maja.
#
# Filtr `raw->>'execution' = 'gielda'` jest tu z tego samego powodu, co przy
# poslizgu: rachunek papierowy zapisuje status z wlasnego rozstrzygniecia,
# wiec policzona z niego wypelnialnosc bylaby echem modelu symulacji.
ZAPYTANIE_ZLECENIA = """
SELECT o.status AS status,
       o.raw->>'powod_odmowy_rodzaj' AS powod_odmowy,
       -- Wyjscie i jego tryb (`gielda_exec.zloz_plan`, od 2026-10-03). Pole
       -- jest na KAZDYM wyjsciu - `false` tez - wiec brak klucza znaczy
       -- "wejscie albo zlecenie sprzed zmiany", nie "wyjscie pasywne".
       o.raw->>'wyjscie_agresywne'   AS wyjscie_agresywne,
       -- Do opoznienia wysylki i wypelnialnosci WEJSC (`wobec_modelu`).
       o.planned_order_id            AS planned_order_id,
       o.submitted_at                AS submitted_at,
       d.decided_at                  AS decided_at,
       p.trigger->>'rodzaj'          AS rodzaj_planu
  FROM exec.order o
  LEFT JOIN journal.planned_order p ON p.id = o.planned_order_id
  LEFT JOIN journal.decision d      ON d.id = o.decision_id
 WHERE o.engine::text = %(engine)s
   AND o.mode::text = %(mode)s
   AND o.raw->>'execution' = 'gielda'
   AND (%(h)s::int IS NULL
        OR o.submitted_at > now() - make_interval(hours => %(h)s::int))
   AND (%(strategia)s::text IS NULL OR d.strategia = %(strategia)s)
 ORDER BY o.submitted_at
"""


async def pobierz_zlecenia(fetch_all, *, engine: str = "crypto",
                           mode: str = "demo",
                           okno_h: int | None = None,
                           strategia: str | None = None) -> list[dict]:
    """Cienka warstwa: statusy ZLOZONYCH zlecen. Bez rachunku."""
    return list(await fetch_all(ZAPYTANIE_ZLECENIA,
                                {"engine": engine, "mode": mode, "h": okno_h,
                                 "strategia": strategia}))


# Wejscia gieldowe (jeden wiersz `exec.order` na plan) z cena po 24 i 48 h:
# zamkniecie ostatniej swiecy 1h, ktora skonczyla sie przed chwila wysylki + H.
# Dolna granica (H - 3 h) odcina dziure w danych - stara swieca udawalaby cene
# z innej chwili. Brak swiecy = None, czyli "jeszcze nie wiadomo".
ZAPYTANIE_ZWROT_NAPRZOD = """
SELECT o.status                                AS status,
       o.side                                  AS strona,
       coalesce(o.limit_price, d.target_price) AS cena,
       o.submitted_at                          AS czas,
       i.symbol                                AS symbol,
       (SELECT c.close FROM market.candle c
         WHERE c.instrument_id = o.instrument_id AND c.timeframe = '1h'
           AND c.event_time <= o.submitted_at + interval '23 hours'
           AND c.event_time >  o.submitted_at + interval '21 hours'
         ORDER BY c.event_time DESC LIMIT 1)   AS cena_24h,
       (SELECT c.close FROM market.candle c
         WHERE c.instrument_id = o.instrument_id AND c.timeframe = '1h'
           AND c.event_time <= o.submitted_at + interval '47 hours'
           AND c.event_time >  o.submitted_at + interval '45 hours'
         ORDER BY c.event_time DESC LIMIT 1)   AS cena_48h
  FROM exec.order o
  JOIN journal.decision d ON d.id = o.decision_id
  JOIN ref.instrument i   ON i.id = o.instrument_id
 WHERE o.engine::text = %(engine)s
   AND o.mode::text = %(mode)s
   AND o.raw->>'execution' = 'gielda'
   AND d.action IN ('enter_long', 'enter_short')
   AND (%(h)s::int IS NULL
        OR o.submitted_at > now() - make_interval(hours => %(h)s::int))
   AND (%(strategia)s::text IS NULL OR d.strategia = %(strategia)s)
 ORDER BY o.submitted_at
"""


async def pobierz_zwrot_naprzod(fetch_all, *, engine: str = "crypto",
                                mode: str = "demo",
                                okno_h: int | None = None,
                                strategia: str | None = None) -> list[dict]:
    """Cienka warstwa: wejscia gieldowe z cena po 24 i 48 h. Bez rachunku."""
    return list(await fetch_all(ZAPYTANIE_ZWROT_NAPRZOD,
                                {"engine": engine, "mode": mode, "h": okno_h,
                                 "strategia": strategia}))


async def pobierz(fetch_all, *, engine: str = "crypto", mode: str = "demo",
                  okno_h: int | None = None,
                  strategia: str | None = None) -> list[dict]:
    """Cienka warstwa: wypełnienia z bazy. Bez rachunku — ten jest wyżej.

    OKNO DOMYSLNIE PUSTE, czyli od poczatku. Kazde inne domyslne okno wyrzucaloby
    czesc jedynych obserwacji, jakie sa - przy probie rzedu kilku sztuk to nie
    jest oszczednosc, tylko utrata pomiaru.
    """
    return list(await fetch_all(ZAPYTANIE,
                                {"engine": engine, "mode": mode, "h": okno_h,
                                 "strategia": strategia}))


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Zmierzony poślizg i ułamek wypełnienia — z prawdziwych "
                    "wypełnień. Nie zmienia żadnej stałej.")
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--mode", default="demo")
    ap.add_argument("--okno-h", type=int, default=None, dest="okno_h",
                    help="okno w godzinach; domyslnie od poczatku")
    # Filtr po `journal.decision.strategia` (0091). Z nim raport dostaje sekcje
    # "wobec modelu pesymistycznego" - po to chodzi strategia momentum na demo.
    # `beta_filtr_trendu` (0100): regula D-3 na demo equity.
    ap.add_argument("--strategia", default=None,
                    choices=("ensemble", "momentum_filtr_trendu", "beta_filtr_trendu"),
                    help="tylko zlecenia decyzji tej strategii (domyślnie wszystkie)")
    args = ap.parse_args()

    if not os.environ.get("DATABASE_URL"):
        sys.exit("DATABASE_URL nie jest ustawiony")

    from services.api.db import close_pool, fetch_all, open_pool

    await open_pool()
    try:
        filtr = {"engine": args.engine, "mode": args.mode, "okno_h": args.okno_h,
                 "strategia": args.strategia}
        wiersze = await pobierz(fetch_all, **filtr)
        zlecenia = await pobierz_zlecenia(fetch_all, **filtr)
        okno = "od poczatku" if args.okno_h is None else f"{args.okno_h} h"
        if args.strategia:
            okno += f", strategia {args.strategia}"
        print(f"poślizg ({args.engine}/{args.mode}, {okno}), "
              f"wypełnień w rejestrze: {len(wiersze)}, "
              f"złożonych zleceń: {len(zlecenia)}")
        pom = pomiar(wiersze) | {"wypelnialnosc": wypelnialnosc(zlecenia),
                                 "wyjscia_takerem": udzial_wyjsc_takerem(zlecenia)}
        for linia in opis(pom):
            print(f"  {linia}")
        wejscia = await pobierz_zwrot_naprzod(fetch_all, **filtr)
        for linia in opis_selekcji(zwrot_naprzod(wejscia)):
            print(f"  {linia}")
        for linia in opis_wobec_modelu(wobec_modelu(wiersze, zlecenia)):
            print(f"  {linia}")
    finally:
        await close_pool()
    # ZAWSZE ZERO. To jest raport, a nie czujka - kod niezerowy zapalilby
    # zadanie cykliczne na czerwono, czyli podnioslby alarm bez tresci.
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
