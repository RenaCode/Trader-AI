"""Koszty transakcyjne per rodzaj instrumentu i typ zlecenia.

DLACZEGO OSOBNY MODUL, A NIE STALA

Do tej pory w symulacji siedzialo `FEE = 0.00055`, czyli 0,055%. To jest stawka
TAKER dla kontraktow wieczystych Bybita - a handlujemy instrumentami SPOT,
gdzie Bybit liczy 0,1% i makerowi, i takerowi.

Symulacja zanizala wiec prowizje niemal dwukrotnie. Nie byl to blad
zaokraglenia: przy przewadze rzedu 0,12% na transakcje roznica miedzy 0,19%
a 0,28% kosztu rundy decyduje o tym, czy strategia ma jakikolwiek sens.

Stawki (Bybit, konto standardowe, bez rabatow VIP):

    spot        maker 0,100%   taker 0,100%    <- brak rabatu za maker
    perpetual   maker 0,020%   taker 0,055%    <- maker piec razy tanszy niz spot

Zrodlo: bybit.com/en/announcement-info/fee-rate (sprawdzone 2026-08-31).

DRUGA RZECZ, KTORA Z TEGO WYNIKA

Na spot nie da sie zajac pozycji KROTKIEJ bez pozyczki marzowej. Symulacja
otwierala je swobodnie - w dzienniku produkcyjnym 8 z 12 pozycji to krotkie
sprzedaze na instrumentach spotowych, czyli transakcje niewykonalne.

Strategia dwustronna wymaga wiec kontraktow wieczystych. Tam krotka pozycja jest
naturalna, a prowizja maker piec razy nizsza niz na spocie - kosztem finansowania
placonego co osiem godzin, ktore juz mierzymy jako ceche.

SPROSTOWANIE (2026-09-04): OSTATNI AKAPIT NIE PRZESZEDL POMIARU

Zdanie "strategia dwustronna wymaga kontraktow wieczystych" bylo rozumowaniem,
nie wynikiem. Zmierzone na czterech przesunieciach okien, uniwersum 10 par:
wariant perp - z shortami, z prowizja maker 0,020% i BEZ naliczonego
finansowania - przegrywa ze spotem dlugim w 3 przesunieciach na 4, na obu
miarach.

Krotka noga sygnalu traci wiec wiecej, niz wynosi oszczednosc na prowizji.
Prawdziwy perpetual, placacy finansowanie, wypadlby jeszcze gorzej - a tego
kosztu symulacja NIE NALICZA (fees_paid zbiera wylacznie prowizje). To luka
do domkniecia, zanim ktokolwiek wroci do tematu; stawki sa w schemacie `market`.

Praktyczny skutek: brak perpetualow na podmiocie unijnym nie jest wada, ktora
trzeba obchodzic zmiana gieldy. Pomiar w docs/03.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Koszty:
    maker: float          # ulamek, nie procent
    taker: float
    krotka_pozycja: bool  # czy da sie grac na spadek bez pozyczki


# Klucz: ref.instrument.kind
CENNIK = {
    "spot": Koszty(maker=0.0010, taker=0.0010, krotka_pozycja=False),
    # PODMIOT UNIJNY MA INNY CENNIK NIZ GLOBALNY.
    #
    # Odczytane z konta przez /v5/account/fee-rate na api-demo.bybit.eu
    # (2026-09-04, wszystkie 140 par jednakowo): maker 0,100%, taker 0,250%.
    # Taker jest wiec DWA I POL RAZA drozszy niz na podmiocie globalnym,
    # a runda zleceniem rynkowym rosnie z 0,280% do 0,580%.
    #
    # Konsekwencja jest jednoznaczna i wynika wprost z docs/03: przy 0,280%
    # zaden sygnal nie pokrywal wlasnych kosztow. Przy 0,580% zlecenia rynkowe
    # na tym podmiocie nie maja sensu w ogole - zostaje wylacznie egzekucja
    # z limitem, gdzie stawka maker jest identyczna jak globalnie (0,100%).
    "spot_eu": Koszty(maker=0.0010, taker=0.0025, krotka_pozycja=False),
    "perp": Koszty(maker=0.0002, taker=0.00055, krotka_pozycja=True),
    # Akcje i ETF-y przez IBKR - stawki prowizyjne, nie procentowe; zaslepka
    # do czasu wdrozenia silnika equity.
    "stock": Koszty(maker=0.0005, taker=0.0005, krotka_pozycja=True),
    "etf": Koszty(maker=0.0005, taker=0.0005, krotka_pozycja=True),
}

DOMYSLNE = CENNIK["spot"]

# Poslizg zakladany przy zleceniu rynkowym, w ulamku ceny, jedna strona.
# Zlecenie z limitem nie ma poslizgu - placi za to ryzykiem niewypelnienia.
POSLIZG_TAKER = 0.0004
POSLIZG_MAKER = 0.0


def koszty(kind: str) -> Koszty:
    return CENNIK.get(kind, DOMYSLNE)


def runda(kind: str, maker: bool = False) -> float:
    """Laczny koszt wejscia i wyjscia, w ulamku ceny."""
    k = koszty(kind)
    prowizja = k.maker if maker else k.taker
    poslizg = POSLIZG_MAKER if maker else POSLIZG_TAKER
    return 2 * (prowizja + poslizg)
