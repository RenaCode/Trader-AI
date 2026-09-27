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

Brakuje czwartej — nie ma jej i to trzeba powiedzieć wprost: ceny mid z arkusza
w chwili składania zlecenia. Bez niej nie da się rozdzielić poślizgu na spread
i na ruch rynku w czasie oczekiwania. Mierzymy więc SUMĘ obu, i to jest liczba
właściwa dla wyceny strategii — bo to ona jest faktycznie płacona.

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
    odniesienie, _ = cena_odniesienia(w)
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
    """
    for pole, skad in (("cena_zlecenia", "zlecenie"), ("cena_planu", "plan"),
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
    nieznany: dict[str, int] = {}
    for z in zlecenia:
        s = (z.get("status") or "").strip().lower()
        if s in STATUS_WYPELNIONE:
            liczniki["wypelnionych"] += 1
        elif s in STATUS_CZESCIOWE:
            liczniki["czesciowych"] += 1
        elif s in STATUS_NIEWYPELNIONE:
            liczniki["niewypelnionych"] += 1
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
            "udzial": udzial, "nieznany_status": nieznany} | liczniki


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


def pomiar(wiersze: list[dict]) -> dict:
    """Cały pomiar naraz: poślizg per rodzaj + ułamek wypełnienia."""
    return statystyki(wiersze) | {"ulamek": ulamki(wiersze),
                                  "rozjazd_ksiag": rozjazd_ksiag(wiersze)}


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

    for skad, n in sorted(p["odniesienia"].items()):
        linie.append(f"  cena odniesienia z: {skad} ({n})")
    for powod, n in sorted(p["pominiete"].items()):
        linie.append(f"  pominięto {n}: {powod}")
    if p["ulamek"]["bez_ulamka"]:
        linie.append(f"  bez zapisanego ułamka: {p['ulamek']['bez_ulamka']} "
                     f"— wypełnienie z giełdy bez `ulamek_wypelnienia` w raw")
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
       p.limit_price                    AS cena_planu,
       d.target_price                   AS cena_decyzji,
       f.price                          AS cena_wykonania,
       f.qty                            AS ilosc,
       f.event_time                     AS czas,
       i.symbol                         AS symbol
  FROM exec.fill f
  JOIN exec.order o           ON o.id = f.order_id
  JOIN ref.instrument i       ON i.id = o.instrument_id
  LEFT JOIN journal.planned_order p ON p.id = o.planned_order_id
  LEFT JOIN journal.decision d      ON d.id = o.decision_id
 WHERE o.engine::text = %(engine)s
   AND o.mode::text = %(mode)s
   AND (%(h)s::int IS NULL
        OR f.event_time > now() - make_interval(hours => %(h)s::int))
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
SELECT o.status AS status
  FROM exec.order o
 WHERE o.engine::text = %(engine)s
   AND o.mode::text = %(mode)s
   AND o.raw->>'execution' = 'gielda'
   AND (%(h)s::int IS NULL
        OR o.submitted_at > now() - make_interval(hours => %(h)s::int))
 ORDER BY o.submitted_at
"""


async def pobierz_zlecenia(fetch_all, *, engine: str = "crypto",
                           mode: str = "demo",
                           okno_h: int | None = None) -> list[dict]:
    """Cienka warstwa: statusy ZLOZONYCH zlecen. Bez rachunku."""
    return list(await fetch_all(ZAPYTANIE_ZLECENIA,
                                {"engine": engine, "mode": mode, "h": okno_h}))


async def pobierz(fetch_all, *, engine: str = "crypto", mode: str = "demo",
                  okno_h: int | None = None) -> list[dict]:
    """Cienka warstwa: wypełnienia z bazy. Bez rachunku — ten jest wyżej.

    OKNO DOMYSLNIE PUSTE, czyli od poczatku. Kazde inne domyslne okno wyrzucaloby
    czesc jedynych obserwacji, jakie sa - przy probie rzedu kilku sztuk to nie
    jest oszczednosc, tylko utrata pomiaru.
    """
    return list(await fetch_all(ZAPYTANIE,
                                {"engine": engine, "mode": mode, "h": okno_h}))


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Zmierzony poślizg i ułamek wypełnienia — z prawdziwych "
                    "wypełnień. Nie zmienia żadnej stałej.")
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--mode", default="demo")
    ap.add_argument("--okno-h", type=int, default=None, dest="okno_h",
                    help="okno w godzinach; domyslnie od poczatku")
    args = ap.parse_args()

    if not os.environ.get("DATABASE_URL"):
        sys.exit("DATABASE_URL nie jest ustawiony")

    from services.api.db import close_pool, fetch_all, open_pool

    await open_pool()
    try:
        wiersze = await pobierz(fetch_all, engine=args.engine, mode=args.mode,
                                okno_h=args.okno_h)
        zlecenia = await pobierz_zlecenia(fetch_all, engine=args.engine,
                                          mode=args.mode, okno_h=args.okno_h)
        okno = "od poczatku" if args.okno_h is None else f"{args.okno_h} h"
        print(f"poślizg ({args.engine}/{args.mode}, {okno}), "
              f"wypełnień w rejestrze: {len(wiersze)}, "
              f"złożonych zleceń: {len(zlecenia)}")
        pom = pomiar(wiersze) | {"wypelnialnosc": wypelnialnosc(zlecenia)}
        for linia in opis(pom):
            print(f"  {linia}")
    finally:
        await close_pool()
    # ZAWSZE ZERO. To jest raport, a nie czujka - kod niezerowy zapalilby
    # zadanie cykliczne na czerwono, czyli podnioslby alarm bez tresci.
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
