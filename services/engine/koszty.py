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
kosztu symulacja NIE NALICZALA (fees_paid zbieralo wylacznie prowizje).

LUKA FINANSOWANIA ZAMKNIETA (2026-09-24)

Zdanie wyzej stalo tu od 2026-09-04 jako "luka do domkniecia" i przez ten czas
KAZDY pomiar wariantu perp byl liczony bez trzeciej pozycji rachunku. Od tej
zmiany `portfolio.simulate_portfolio` nalicza finansowanie z cechy
`funding_rate`, po stronie i po chwilach wyplaty gieldy - patrz
`stawka_finansowania_na_ulamek` i `GODZINY_FINANSOWANIA` nizej.

Czego to NIE zmienia: zdania o pomiarze z czterech przesuniec. Tamten wynik
byl liczony bez finansowania, wiec wariant perp mogl wtedy wyjsc tylko LEPIEJ,
niz wyjdzie teraz - przegrana ze spotem w 3 przesunieciach na 4 zostaje
w mocy tym bardziej. Domkniecie luki jest warunkiem PONOWNEGO pomiaru, nie
jego wynikiem.

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
    # Czy instrument placi FINANSOWANIE. Trzecia pozycja rachunku obok prowizji
    # i poslizgu - i jedyna, ktora zalezy od CZASU TRZYMANIA, a nie od liczby
    # zlecen. Pozycja trzymana dobe placi trzy razy, niezaleznie od tego, czy
    # zarabia. Naliczanie: `portfolio.simulate_portfolio`.
    finansowanie: bool = False


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
    "perp": Koszty(maker=0.0002, taker=0.00055, krotka_pozycja=True,
                   finansowanie=True),
    # AKCJE I ETF-Y NA ALPACE. Zero, i to jest LICZBA, a nie brak liczby.
    #
    # Do 2026-09-24 stalo tu 0,0005 z podpisem "zaslepka IBKR do czasu wdrozenia
    # silnika equity". Silnik equity wlasnie rusza, a konto stoi na Alpace, nie
    # na IBKR - stawka prowizyjna tamtego brokera opisywala wiec rachunek, ktory
    # w tym projekcie nie istnieje. Zaslepka, ktora przezyla powod swojego
    # istnienia, jest gorsza od jej braku: wyglada jak pomiar.
    #
    # Alpaca nie pobiera prowizji od akcji i ETF-ow notowanych w USA, ani od
    # strony makerskiej, ani od takerskiej. To jest cala prowizja i dlatego
    # obie stawki sa zerami.
    #
    # CZEGO TE ZERA NIE MOWIA - i dlatego `karta_rundy` dokleja dla tych dwoch
    # rodzajow osobny skladnik BEZ WARTOSCI:
    #   * oplaty regulacyjne (SEC Section 31, FINRA TAF) obciazaja WYLACZNIE
    #     noge sprzedazy, a TAF liczy sie od LICZBY AKCJI, nie od notionalu -
    #     zadna z tych dwoch wielkosci nie miesci sie w symetrycznym ulamku
    #     ceny, ktorym jest ta tabela. Wpisanie ich tutaj jako sredniej
    #     zamienioby rachunek gieldy w nasze zalozenie,
    #   * spread, ktorego na equity nikt jeszcze nie zmierzyl - dokladnie ta
    #     sama luka co na krypto (docs/08, brama 0).
    #
    # FINANSOWANIA NIE MA: krotka sprzedaz akcji kosztuje odsetki od pozyczki
    # papieru, ale to inna wielkosc, z innego zrodla i naliczana inaczej -
    # podpiecie jej pod stawke perpetuala byloby nazwaniem dwoch roznych rzeczy
    # jedna nazwa.
    "stock": Koszty(maker=0.0, taker=0.0, krotka_pozycja=True),
    "etf": Koszty(maker=0.0, taker=0.0, krotka_pozycja=True),
}

# Chwile wyplaty finansowania na Bybicie: co osiem godzin, o pelnych 00:00,
# 08:00 i 16:00 UTC. Potwierdzone na zywo 2026-09-24 na
# /v5/market/funding/history (BTCUSDT): kolejne `fundingRateTimestamp`
# roznia sie o 28 800 000 ms i dziela sie bez reszty przez dobe.
#
# LICZYMY WYPLATY, A NIE ULAMEK STAWKI NA SWIECE. Koszt wyznacza gielda, nie
# nasza siatka: "stawka razy godziny_swiecy/8" dalaby dla tego samego okresu
# inna liczbe przy horyzoncie 1 h i przy 4 h, choc zaplacone zostaloby tyle
# samo. Zalezenie kosztu od czestotliwosci naszego patrzenia jest dokladnie ta
# wada, ktora `uniwersum.py` opisuje przy cechach przekrojowych.
GODZINY_FINANSOWANIA = (0, 8, 16)
INTERWAL_FINANSOWANIA_H = 8

DOMYSLNE = CENNIK["spot"]

# Poslizg zakladany przy zleceniu rynkowym, w ulamku ceny, jedna strona.
# Zlecenie z limitem nie ma poslizgu - placi za to ryzykiem niewypelnienia.
POSLIZG_TAKER = 0.0004
POSLIZG_MAKER = 0.0


def koszty(kind: str) -> Koszty:
    return CENNIK.get(kind, DOMYSLNE)


def stawka_finansowania_na_ulamek(stawka_pct: float) -> float:
    """Cecha `funding_rate` z panelu -> ulamek notionalu na JEDNA wyplate.

    DLACZEGO TO JEST OSOBNA FUNKCJA, A NIE `/ 100` W MIEJSCU UZYCIA

    Jednostka tej liczby zmienia sie po drodze i nigdzie nie jest zapisana
    w typie:

        Bybit `/v5/market/funding/history` -> `fundingRate` jako ULAMEK
            (zmierzone 2026-09-24 na BTCUSDT: -0.00001295, 0.00000678)
        `services/ingest/funding.py`       -> zapisuje ten ulamek bez zmian
            do `market.funding_rate.rate`
        `services/ingest/features.py`      -> zapisuje do cechy `last * 100`,
            czyli PROCENTY
        `services/engine/signals.py`       -> czyta jako `stawka_pct`

    Pomylka jest wiec stukrotna w obie strony i CICHA: przy wzieciu procentow
    za ulamek kazdy wariant perp wychodzi gleboko ujemny, przy odwrotnej -
    koszt finansowania praktycznie znika. Zadna z tych wersji nie wyglada na
    awarie, obie wygladaja na wynik.

    Nazwa parametru mowi jednostke wejscia, nazwa funkcji - jednostke wyjscia.
    To jedyne miejsce, w ktorym ta stala 100 ma prawo wystapic.
    """
    return stawka_pct / 100.0


def runda(kind: str, maker: bool = False) -> float:
    """Laczny koszt wejscia i wyjscia, w ulamku ceny - CZESC POLICZALNA.

    TO NIE JEST CALY KOSZT RUNDY i nie wolno tak tej liczby podpisywac. Brakuje
    w niej spreadu, niewypelnienia i finansowania - patrz `karta_rundy`, ktora
    te braki wymienia z nazwy zamiast wliczac je jako zera. Funkcja zostaje,
    bo petla decyzyjna i symulacja naliczaja dokladnie to, co ona liczy;
    zmiana jej wyniku rozjechalaby rachunek z tym, co naprawde jest pobierane.
    """
    k = koszty(kind)
    prowizja = k.maker if maker else k.taker
    poslizg = POSLIZG_MAKER if maker else POSLIZG_TAKER
    return 2 * (prowizja + poslizg)


# ----------------------------------------------------- karta kosztu rundy
#
# DLACZEGO KAZDA LICZBA MUSI PRZYJSC ZE SWOIM POCHODZENIEM
#
# `runda()` oddaje jedna liczbe i po jej obejrzeniu nie da sie powiedziec, czy
# jakis skladnik wyszedl zero, bo go zmierzono, czy dlatego, ze go nie ma
# w rachunku. Te dwa stany prowadza do przeciwnych decyzji, a wygladaja
# identycznie - i dokladnie tak zniknal z rachunku spread, finansowanie
# i niewypelnienie naraz.
#
# Skladnik bez wartosci NIE JEST ZEREM. Wliczony jako zero daje liczbe tym
# mniejsza, im wiecej rzeczy nie policzylismy - czyli im gorzej rozumiemy wlasny
# koszt, tym tanszy on wyglada.

POMIAR = "pomiar"          # policzone z prawdziwych danych TEGO rynku
TARYFA = "taryfa gieldy"   # stawka odczytana z gieldy - fakt, nie zalozenie
ZALOZENIE = "zalozenie"    # liczba, ktora ktos wybral
NIEZMIERZONE = "nie zmierzono"   # wartosci NIE MA
# CZWARTY STAN: liczba prawdziwa, tylko o czym innym.
#
# `db/migrations/0018_bybit_eu.sql`: dane publiczne - swiece i arkusz - ida
# z hosta GLOBALNEGO, a zlecenia podpisane na `bybit.eu`. Powod podany w samej
# migracji: podmiot unijny ma dla BTCUSDT rzedu 0,1% globalnego wolumenu, wiec
# "jego swiece opisywalyby inny rynek". Spread policzony z `market.book_snapshot`
# jest wiec spreadem ksiegi, w ktorej nasze zlecenie nigdy nie stanie.
#
# Taka liczba wchodzi do rachunku jako PODLOGA - rynek mniej plynny nie ma
# wezszego spreadu niz bardziej plynny - ale NIE domyka pytania. Karta zostaje
# niepelna, bo wartosc dla naszej ksiegi nadal jest nieznana.
INNY_RYNEK = "pomiar innej ksiegi"
# PIATY STAN: skladnik, ktorego NIE DA SIE podac jako stala rundy, bo zalezy od
# CZASU TRZYMANIA - a mimo to jest naliczany. Finansowanie perpetuala: od
# 2026-09-24 `portfolio.simulate_portfolio` liczy je z cechy `funding_rate`,
# po chwilach wyplaty gieldy. Wypisane jako NIE ZMIERZONO sugerowaloby luke,
# ktorej juz nie ma; wypisane jako liczba - stala, ktorej nigdy nie bylo.
W_SYMULACJI = "naliczane w symulacji, per pozycja"


@dataclass(frozen=True)
class Skladnik:
    """Jedna pozycja rachunku razem z tym, skad sie wziela.

    `wartosc` to ulamek ceny za CALA RUNDE (obie nogi), albo None, gdy liczby
    nie ma. None jest wartoscia pierwszoklasowa, nie brakiem danych do
    uzupelnienia zerem.
    """
    nazwa: str
    wartosc: float | None
    zrodlo: str
    uwaga: str = ""
    # Czy ta wartosc ODPOWIADA NA PYTANIE, czy jest tylko podloga. False znaczy:
    # liczba jest, ale dotyczy czegos innego niz rynek, na ktorym handlujemy
    # (patrz INNY_RYNEK). Skladnik wchodzi wtedy do sumy, ale karta zostaje
    # niepelna - inaczej cudza ksiega ogloszilaby rachunek zamknietym.
    wiarygodny: bool = True


@dataclass(frozen=True)
class Karta:
    """Rachunek kosztu rundy rozbity na skladniki z pochodzeniem."""
    skladniki: tuple[Skladnik, ...]

    @property
    def policzone(self) -> float:
        """Suma skladnikow, ktore MAJA wartosc. Dolne oszacowanie kosztu."""
        return sum(s.wartosc for s in self.skladniki if s.wartosc is not None)

    @property
    def nieznane(self) -> tuple[str, ...]:
        """Nazwy skladnikow, ktore NIE SA ROZSTRZYGNIETE.

        Dwa powody, jeden skutek: albo wartosci nie ma wcale, albo jest, ale
        pochodzi z innego rynku i jest wylacznie podloga. W obu wypadkach
        prawdziwy koszt zostaje nieznany, wiec karta nie moze ogłosic sie pelna.
        """
        return tuple(s.nazwa for s in self.skladniki
                     if s.zrodlo == NIEZMIERZONE or not s.wiarygodny)

    @property
    def pelna(self) -> bool:
        return not self.nieznane


def karta_rundy(kind: str, maker: bool = False, *,
                spread_bps: float | None = None,
                spread_nasza_ksiega: bool = True,
                niewypelnienie: float | None = None,
                finansowanie: float | None = None) -> Karta:
    """Koszt rundy rozbity na skladniki. Kazdy ze swoim pochodzeniem.

    ARGUMENTY OPCJONALNE SA POMIARAMI, KTORYCH JESZCZE NIE MA. Podanie ich
    zamienia skladnik z „nie zmierzono" na „pomiar"; pominiecie zostawia go bez
    wartosci - i wtedy `policzone` jest jawnie niepelne, zamiast udawac calosc.

      * `spread_bps` - polowa spreadu na noge, w punktach bazowych, razy dwie
        nogi. Zrodlo: `market.book_snapshot` / cecha `book_spread_bps`.
      * `spread_nasza_ksiega` - czy ta liczba pochodzi z ksiegi, NA KTOREJ
        SKLADAMY ZLECENIA. False zamienia ja z pomiaru w podloge: wchodzi do
        sumy, ale nie domyka karty.

        OD 2026-09-24 DOMYSLNE `True` JEST PRAWDA, a nie zalozeniem.
        `BybitAdapter.fetch_order_book` bierze arkusz z `self._host`, czyli
        z `ref.venue` - tej samej gieldy, na ktora ida zlecenia. Wczesniej
        czytal go z hosta globalnego i wtedy `False` bylo jedyna uczciwa
        odpowiedzia.

        SKAD WIADOMO, Z KTOREJ KSIEGI JEST WIERSZ, SKORO `market.book_snapshot`
        NIE MA KOLUMNY ZRODLA. Z flagi, ktora o zrodle zdecydowala i ktora stoi
        w bazie: `ref.instrument.tradeable`. Od 2026-09-25 `market_worker` pyta
        ksiegi EGZEKUCJI wylacznie o instrumenty `tradeable`, a pozostale (24
        pary USDT, ktorych podmiot unijny nie listuje) zostaja na hoscie
        globalnym. Uniwersum decyzyjne to dokladnie `tradeable`, wiec dla
        rachunku kosztu cala jego dziedzina pochodzi z naszej ksiegi.

        `False` jest wiec wlasciwe w DWOCH przypadkach i oba da sie wskazac
        zapytaniem, bez nowej kolumny:
          * okno siegajace przed 2026-09-24 - wtedy arkusz KAZDEGO instrumentu
            szedl z hosta globalnego,
          * pomiar rozciagniety na instrumenty `tradeable = false` - ich arkusz
            idzie z hosta globalnego nadal i celowo, zeby szereg
            `book_spread_bps` zostal ciagly.
      * `niewypelnienie` - koszt zlecen, ktore nie weszly, jako ulamek ceny na
        runde. Zrodlo: `poslizg.wypelnialnosc` zestawione z rozkladem ruchu,
        ktory przez to przepadl.
      * `finansowanie` - laczna oplata za czas trzymania pozycji, jako ulamek
        ceny. Zrodlo: `market.funding_rate`.

    DLACZEGO PRZY LIMICIE NIE MA POZYCJI „POSLIZG"

    Bo bylaby zerem z definicji, a nie z pomiaru: zlecenie z limitem wypelnia
    sie po cenie limitu albo lepiej. Wpisana do karty wygladalaby jak zmierzony
    skladnik o wartosci zero i zamykala pytanie, ktore jest tu wlasciwe.
    Kosztem strony makerskiej sa zlecenia, KTORE NIE WESZLY - i ta pozycja
    w karcie jest, bez wartosci, bo nikt jej dotad nie policzyl.
    """
    k = koszty(kind)
    s: list[Skladnik] = [Skladnik(
        "prowizja", 2 * (k.maker if maker else k.taker), TARYFA,
        "stawka odczytana z gieldy; cztery produkcyjne wypelnienia daja "
        "fee/qty rowne stawce makera co do cyfry")]

    if maker:
        s.append(Skladnik(
            "niewypelnienie", niewypelnienie,
            POMIAR if niewypelnienie is not None else NIEZMIERZONE,
            "symulacja przyjmuje, ze limit wchodzi ZAWSZE, gdy swieca dotknela "
            "ceny; `exec.fill` nie zawiera zlecen, ktore nie weszly"))
    else:
        s.append(Skladnik(
            "poslizg", 2 * POSLIZG_TAKER, ZALOZENIE,
            "stala 4 pb; zmierzona na n=0 wypelnieniach zleceniem rynkowym"))

    if spread_bps is None:
        # Flaga opisuje liczbe, ktorej nie ma - nie ma wiec czego opisywac
        # i stan zostaje trzeci, a nie czwarty.
        s.append(Skladnik(
            "spread", None, NIEZMIERZONE,
            "symulacja wchodzi i wychodzi po `close` swiecy, czyli po cenie "
            "ostatniej transakcji - polowa spreadu na noge nie jest naliczana"))
    elif spread_nasza_ksiega:
        s.append(Skladnik(
            "spread", spread_bps / 1e4, POMIAR,
            "polowa spreadu na noge razy dwie nogi, z ksiegi, na ktorej "
            "skladamy zlecenia"))
    else:
        s.append(Skladnik(
            "spread", spread_bps / 1e4, INNY_RYNEK,
            "zmierzone na ksiedze INNEJ niz ta, na ktorej skladamy zlecenia "
            "(do 2026-09-24 arkusz szedl z hosta globalnego, a zlecenia na "
            "bybit.eu - migracja 0018). Rynek o innej plynnosci ma inny spread, "
            "wiec to jest OSZACOWANIE, nie pomiar naszego kosztu",
            wiarygodny=False))

    # OPLATY REGULACYJNE - TYLKO AKCJE I ETF-Y, I TYLKO NOGA SPRZEDAZY.
    #
    # Skladnik bez wartosci, bo zadna wartosc nie byloby tu uczciwa. SEC
    # Section 31 liczy sie od notionalu, FINRA TAF od LICZBY AKCJI, obie
    # wylacznie przy sprzedazy, a stawka SEC zmienia sie raz do roku. Ulamek
    # ceny na runde - jedyna wielkosc, ktora ta karta umie dodac - nie opisuje
    # zadnej z nich. Jawne "nie zmierzono" zostawia `pelna` na False, wiec
    # `opis_karty` podpisze sume jako DOLNE oszacowanie, a nie jako koszt.
    #
    # Zamkniecie tej pozycji nie wymaga cennika: wymaga odczytania kolumny
    # oplat z `exec.fill` po pierwszych sprzedazach equity na koncie paper.
    if kind in ("stock", "etf"):
        s.append(Skladnik(
            "oplaty regulacyjne", None, NIEZMIERZONE,
            "SEC Section 31 i FINRA TAF obciazaja WYLACZNIE noge sprzedazy, "
            "a TAF liczy sie od liczby akcji, nie od notionalu - symetryczny "
            "ulamek ceny nie opisuje zadnej z nich. Zrodlo docelowe: oplaty "
            "z `exec.fill` po pierwszych sprzedazach na koncie Alpaca paper"))

    if k.finansowanie:
        s.append(Skladnik(
            "finansowanie", finansowanie,
            POMIAR if finansowanie is not None else W_SYMULACJI,
            f"placone co {INTERWAL_FINANSOWANIA_H} h niezaleznie od wyniku "
            f"pozycji, wiec zalezy od CZASU TRZYMANIA, a nie od liczby zlecen - "
            f"stala rundy nie istnieje. Naliczane od 2026-09-24 w "
            f"`portfolio.simulate_portfolio` z cechy `funding_rate`"))
    return Karta(tuple(s))


def opis_karty(k: Karta) -> list[str]:
    """Karta zamieniona na wiersze do wypisania.

    SKLADNIK BEZ WARTOSCI WYPISUJE SIE SLOWAMI, NIGDY LICZBA. Zero w tym
    miejscu czyta sie jako „policzone i wyszlo zero" - czyli dokladnie
    odwrotnie niz stan faktyczny.
    """
    linie = []
    for s in k.skladniki:
        if s.wartosc is None and s.zrodlo == W_SYMULACJI:
            linie.append(f"  {s.nazwa}: {s.zrodlo} — {s.uwaga}")
        elif s.wartosc is None:
            linie.append(f"  {s.nazwa}: NIE ZMIERZONO — {s.uwaga}")
        elif not s.wiarygodny:
            # CO NAJMNIEJ, nie „rowne". Liczba jest prawdziwa i dotyczy czego
            # innego; wypisana jak pomiar zamykalaby pytanie, ktore zostaje.
            linie.append(f"  {s.nazwa}: CO NAJMNIEJ {s.wartosc * 100:.4f}% "
                         f"({s.zrodlo}) — {s.uwaga}")
        else:
            linie.append(f"  {s.nazwa}: {s.wartosc * 100:.3f}% ({s.zrodlo})")
    if k.pelna:
        linie.append(f"  razem: {k.policzone * 100:.3f}% rundy")
    else:
        linie.append(
            f"  razem CO NAJMNIEJ {k.policzone * 100:.3f}% rundy — bez "
            f"{', '.join(k.nieznane)}. Prawdziwy koszt jest WIEKSZY o "
            f"nieznana wartosc; ta liczba jest dolnym oszacowaniem, nie kosztem.")
    return linie
