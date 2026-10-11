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
    # Czy gielda pobiera prowizje od KUPNA w walucie BAZOWEJ (Bybit spot:
    # kupujac 10 ADA dostajemy 10 ADA minus prowizja). Wtedy na koncie zostaje
    # ilosc spoza kroku gieldy i po sprzedazy zostaje PYL - reszta ponizej
    # `lot_size`, ktorej sprzedac sie nie da. Flaga, a nie lista nazw, z tego
    # samego powodu co `krotka_pozycja` przy koszcie pozyczki papieru: skladnik
    # karty ma sie pojawiac i znikac razem z przyczyna. Kontrakt (perp) i akcje
    # na Alpace (prowizja zero, nie w papierach) pylu nie zostawiaja.
    prowizja_w_bazowej: bool = False


# Klucz: ref.instrument.kind
CENNIK = {
    "spot": Koszty(maker=0.0010, taker=0.0010, krotka_pozycja=False,
                   prowizja_w_bazowej=True),
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
    #
    # SPROSTOWANIE (2026-10-05): MAKER TEZ 0,250%. Ten sam endpoint, wszystkie
    # 135 par (USDC, USDT i EUR jednakowo): maker 0,250%, taker 0,250%. Wykryla
    # to `mozliwosci.py --stawki` w `trader-zywotnosc`. Ostatni akapit wyzej juz
    # nie obowiazuje: limit nie daje rabatu prowizji, a runda kosztuje 0,500%
    # zamiast 0,200% - wiecej niz cala zmierzona przewaga (docs/08, Brama 0).
    "spot_eu": Koszty(maker=0.0025, taker=0.0025, krotka_pozycja=False,
                      prowizja_w_bazowej=True),
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
    #
    # OD 2026-09-25 TO ZDANIE MA SWOJ SKLADNIK W KARCIE. Samo stwierdzenie
    # "finansowania nie ma" zostawialo koszt pozyczki papieru poza rachunkiem,
    # a `krotka_pozycja=True` znaczy, ze symulacja te noge otwiera - wiec brak
    # pozycji w karcie wliczal go jako zero. `karta_rundy` dokleja go teraz jako
    # skladnik BEZ WARTOSCI, czyli trafia do `karta.nieznane` i suma podpisuje
    # sie jako DOLNE oszacowanie.
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
    # Czy SYMULACJA JUZ TEN KOSZT NALICZYLA w zwrocie z transakcji (`res.trades`
    # -> `"ret"`), a nie tylko w krzywej kapitalu. Rozroznienie jest tu po to,
    # zeby dalo sie policzyc DOPLATE - czesc rachunku, ktorej w zmierzonym
    # Sharpie jeszcze nie ma - BEZ dopasowywania nazw skladnikow tekstem.
    #
    # Nazwa skladnika jako kryterium byla by cicha pulapka: przemianowanie
    # „poslizg" albo dolozenie nowej pozycji zmienialoby po cichu doplate,
    # a wiec i werdykt bramki promocji, bez zadnego objawu.
    w_symulacji: bool = False


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
    def doplata_poza_symulacja(self) -> float:
        """Suma skladnikow, ktore MAJA wartosc, a symulacja ich NIE naliczyla.

        DLACZEGO TA WIELKOSC ISTNIEJE OSOBNO OD `policzone`

        `policzone` to koszt calej rundy - razem z prowizja i poslizgiem, ktore
        `simulate_portfolio` juz odjela od `ret`. Odjecie `policzone` od zwrotu
        liczyloby prowizje DWA RAZY. A zero w tym miejscu znaczyloby, ze koszt
        poza symulacja nie istnieje.

        To jest liczba, ktora trzeba odjac od zwrotu z transakcji, zeby Sharpe
        (a przez niego deflowany Sharpe i bramka promocji) mowil o kosztach,
        ktore SA ZMIERZONE - a nie tylko o tych, ktore symulacja umie naliczyc.
        Zmierzony spread 5,85 pb zbija Sharpe/transakcje z +0,0952 na +0,0823
        i to wystarcza, zeby DSR przestal przechodzic prog przy KAZDYM N.

        CZEGO TA LICZBA NADAL NIE ZAWIERA: skladnikow bez wartosci. Te zostaja
        w `nieznane` i musza byc wypisane slowami, bo doplata ich nie widzi -
        patrz `koszt_progowy` i drabina wrazliwosci w `train.opis_przewagi`.
        """
        return sum(s.wartosc for s in self.skladniki
                   if s.wartosc is not None and not s.w_symulacji)

    @property
    def pelna(self) -> bool:
        return not self.nieznane


def pyl_per_instrument(kroki: dict[str, float | None],
                       ceny: dict[str, float | None],
                       notional: float) -> dict[str, float] | None:
    """Oczekiwana strata na PYLE per instrument, jako ulamek rundy.

    Reszta po sprzedazy lezy w [0, krok), srednio krok/2 - zmierzone na 12
    resztach demo: srednia reszta/krok = 0,500. W walucie `krok * cena / 2`,
    wzgledem notionalu pozycji `krok * cena / (2 * notional)`. Placona raz na
    runde (przy wyjsciu).

    None, gdy dla KTOREGOKOLWIEK instrumentu brakuje kroku albo ceny: srednia
    po zbiorze z dziura nie jest srednia po uniwersum, a zero byloby klamstwem.
    """
    if not kroki or notional <= 0:
        return None
    out = {}
    for sym, krok in kroki.items():
        cena = ceny.get(sym)
        if krok is None or cena is None or krok <= 0 or cena <= 0:
            return None
        out[sym] = float(krok) * float(cena) / (2 * notional)
    return out


def pyl_rundy(kroki: dict[str, float | None], ceny: dict[str, float | None],
              notional: float) -> float | None:
    """Pyl jako JEDNA liczba karty toru: SREDNIA po uniwersum.

    DO 2026-10-03 BYLO TU MAKSIMUM i to bylo zawyzenie, ktore zmierzylismy.
    Tor crypto 4h, ceny max okna 1400 dni, notional 750: maksimum 3,29 pb
    (ETHUSDC) wobec sredniej 1,42 pb - przy demo 0,23 pb na runde (CI 0,11-0,36,
    14 rund). Maksimum zakladalo, ze kazda transakcja jest na instrumencie
    o najwiekszym pyle; tymczasem w walk-forwardzie (6 przesuniec, 240 okien)
    udzial instrumentu w rundach to 0,069-0,117 przy rownym 0,100, a srednia
    wazona tymi udzialami rozni sie od zwyklej o 1% (1,434 wobec 1,416 pb).
    Srednia rowna jest wiec srednia wazona z dokladnoscia ponizej szumu - i nie
    wymaga znajomosci liczby transakcji, ktorej przed walk-forwardem nie ma.

    RESZTA ROZJAZDU Z DEMO TO CENA, NIE WZOR. Ten sam wzor (z „/2") przy
    cenach biezacych daje srednio 0,544 pb, przy cenach maksymalnych okna
    1,343 pb - roznica x2,4 pochodzi w calosci z ceny. Ceny biezacej nie
    bierzemy, bo lamie warunek „model >= demo": NEARUSDC przy cenie biezacej
    0,294 pb wobec zmierzonego maksimum 0,337 pb.

    KIERUNEK ZOSTAJE OSTROZNY gdzie indziej: cena to maksimum okna, a notional
    liczony z mediany progow wejscia lezy ponizej zrealizowanego (patrz
    `train.pyl_toru`). Po zmianie model (1,34 pb przy notionalu 792) nadal stoi
    ponad gorna granica pomiaru demo dla KAZDEGO instrumentu, ktory ma rundy
    (najwyzej SOLUSDC 0,71 pb).
    """
    per = pyl_per_instrument(kroki, ceny, notional)
    if per is None:
        return None
    return sum(per.values()) / len(per)


def zwroty_po_doplacie(zwroty: list[float], doplata: float) -> list[float]:
    """Zwroty z transakcji PO doliczeniu `Karta.doplata_poza_symulacja`.

    JEDNA DEFINICJA, BO DO 2026-10-02 BYLY DWIE MIARY DWOCH SWIATOW.

    DSR odejmowal doplate od kazdej transakcji, a odsetek dodatnich okien,
    mediana Sharpe'a i porownanie z kup-i-trzymaj - nie. Bramka statusu
    skladala wiec werdykt z czterech liczb, z ktorych trzy opisywaly swiat
    bez spreadu. Kazda miara po doplacie z karty ma przejsc PRZEZ TE FUNKCJE
    (wprost albo przez `portfolio.wynik_po_doplacie`), a nie przez wlasne
    `r - doplata` wpisane w miejscu uzycia.

    Doplata jest ulamkiem NOTIONALU WEJSCIA na cala runde - w tej samej bazie
    co `ret` z `simulate_portfolio` - wiec odejmuje sie ja od kazdej transakcji
    z osobna, tak jak jest placona.
    """
    return [r - doplata for r in zwroty]


def koszt_niewypelnienia(wypelnialnosc: float | None,
                         utracone_ruchy: list[float]) -> float | None:
    """Koszt niewypelnienia jako ulamek rundy: (1 - wypelnialnosc) x sredni
    ruch, ktory przepadl przez wejscie, ktore nie weszlo.

    `wypelnialnosc` w tej samej definicji co `poslizg.wypelnialnosc`
    (wypelnione / rozstrzygniete); `utracone_ruchy` - zwrot ze strony pozycji
    od ceny limitu do ceny kilka swiec pozniej, po jednym na niewypelnione
    wejscie (`portfolio.PortfolioResult.utracone_ruchy`).

    ZE ZNAKIEM. Ujemna liczba znaczy, ze wejscia, ktore przepadly, stracilyby
    - czyli niewypelnienie oszczedzilo pieniadze. To jest wynik, nie blad, i
    przycinanie go do zera zawyzaloby koszt w jedna strone.

    None, gdy nie ma czego mierzyc (brak zlecen albo brak niewypelnien przy
    nieznanej wypelnialnosci) - zero znaczyloby „zmierzone i wyszlo zero".
    """
    if wypelnialnosc is None:
        return None
    if not utracone_ruchy:
        return 0.0 if wypelnialnosc >= 1.0 else None
    return (1.0 - wypelnialnosc) * (sum(utracone_ruchy) / len(utracone_ruchy))


def karta_rundy(kind: str, maker: bool = False, *,
                spread_bps: float | None = None,
                spread_nasza_ksiega: bool = True,
                niewypelnienie: float | None = None,
                niewypelnienie_w_symulacji: bool = False,
                finansowanie: float | None = None,
                pyl: float | None = None,
                wyjscia_takerem: float | None = None) -> Karta:
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
        ktory przez to przepadl (`koszt_niewypelnienia`).
      * `niewypelnienie_w_symulacji` - liczba wyzej pochodzi z symulacji, ktora
        niewypelnienia SAMA odgrywa (`Egzekucja.pesymistyczna`): transakcje,
        ktore nie weszly, nie maja zwrotu, a wyjscia, ktore nie weszly, trzymaja
        pozycje dalej. Skladnik jest wtedy POMIAREM, ale do doplaty nie
        wchodzi - odjecie go od zwrotow liczyloby te strate drugi raz.
      * `finansowanie` - laczna oplata za czas trzymania pozycji, jako ulamek
        ceny. Zrodlo: `market.funding_rate`.
      * `wyjscia_takerem` - UDZIAL wyjsc zlozonych agresywnie (GTC bez
        PostOnly po dwoch odrzuceniach, `gielda_exec.PROBY_POSTONLY_WYJSCIA`),
        0..1. Zrodlo: `poslizg.udzial_wyjsc_takerem` z `exec.order`. Skladnik
        to udzial razy (taker - maker): jedna noga rundy placi stawke takera
        zamiast makera. Tylko przy limicie - egzekucja rynkowa placi takera
        na obu nogach juz w `prowizja`.
      * `pyl` - oczekiwana strata na reszcie ponizej kroku ilosci po sprzedazy,
        jako ulamek rundy. Liczy ja `pyl_rundy`; None = kroku albo ceny nie ma
        w danych, i wtedy skladnik jest NIEZMIERZONY, a nie zerem.

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
        "fee/qty rowne stawce makera co do cyfry",
        w_symulacji=True)]

    if maker and niewypelnienie_w_symulacji:
        s.append(Skladnik(
            "niewypelnienie", niewypelnienie,
            POMIAR if niewypelnienie is not None else NIEZMIERZONE,
            "model pesymistyczny: limit z ceny po opoznieniu, pol spreadu "
            "instrumentu, wypelnienie tylko w godzinie po wysylce; wartosc = "
            "(1 - wypelnialnosc symulowana) x sredni ruch, ktory przepadl - "
            "juz w zwrotach symulacji, do doplaty nie wchodzi",
            w_symulacji=True))
    elif maker:
        s.append(Skladnik(
            "niewypelnienie", niewypelnienie,
            POMIAR if niewypelnienie is not None else NIEZMIERZONE,
            "symulacja przyjmuje, ze limit wchodzi ZAWSZE, gdy swieca dotknela "
            "ceny; `exec.fill` nie zawiera zlecen, ktore nie weszly"))
        # WYJSCIE TAKEREM (2026-10-03, audyt F-4.3). Wyjscie odrzucone dwa
        # razy przez PostOnly idzie trzeci raz po drugiej stronie spreadu, bez
        # PostOnly - czyli placi takera, ktorego symulacja (maker na obu
        # nogach) nie zna. Bez udzialu z produkcji skladnik jest NIEZMIERZONY,
        # a nie zerem: zero znaczyloby "zadne wyjscie nie eskalowalo".
        s.append(Skladnik(
            "wyjscie takerem",
            None if wyjscia_takerem is None
            else wyjscia_takerem * (k.taker - k.maker),
            POMIAR if wyjscia_takerem is not None else NIEZMIERZONE,
            "udzial wyjsc zlozonych agresywnie razy (taker - maker) na jednej "
            "nodze; symulacja zaklada makera na obu"))
    else:
        s.append(Skladnik(
            "poslizg", 2 * POSLIZG_TAKER, ZALOZENIE,
            "stala 4 pb; zmierzona na n=0 wypelnieniach zleceniem rynkowym",
            w_symulacji=True))

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

    # KOSZT POZYCZKI PAPIERU - SKLADNIK BEZ WARTOSCI, DLA INSTRUMENTOW,
    # KTORYCH NOGE KROTKA SYMULACJA OTWIERA, A ICH CENNIK NIE MA FINANSOWANIA.
    #
    # Naglowek tego modulu mowi wprost: "krotka sprzedaz akcji kosztuje odsetki
    # od pozyczki papieru, ale to inna wielkosc, z innego zrodla i naliczana
    # inaczej". To zdanie tlumaczylo, dlaczego kosztu NIE MA w `CENNIK` - i na
    # tym sie konczylo. `koszty("stock").krotka_pozycja` jest `True`, wiec
    # symulacja noge krotka OTWIERA, a karta kosztu tej pozycji nie wymieniala
    # ani jako liczby, ani jako braku. Czyli dokladnie stan, przed ktorym ten
    # plik ostrzega w naglowku sekcji: skladnik nieobecny w karcie jest w sumie
    # zerem, a "im wiecej rzeczy nie policzylismy, tym tanszy koszt wyglada".
    #
    # WARUNEK CZYTA FLAGE, A NIE LISTE NAZW. `k.krotka_pozycja` to ta sama
    # flaga, ktora DECYDUJE o otwarciu nogi krotkiej, wiec skladnik pojawia sie
    # i znika RAZEM z powodem swojego istnienia: nowy rodzaj instrumentu
    # z pozycja krotka dostanie go bez dopisywania do listy, a wylaczenie
    # `krotka_pozycja` usunie go razem z pozycja, ktorej dotyczy.
    #
    # `not k.finansowanie` odcina perpetuala: tam kosztem trzymania nogi krotkiej
    # JEST finansowanie i jest ono naliczane (skladnik nizej). Dwie pozycje
    # rachunku o tym samym znaczeniu policzylyby ten koszt dwa razy.
    if k.krotka_pozycja and not k.finansowanie:
        s.append(Skladnik(
            "koszt pozyczki papieru", None, NIEZMIERZONE,
            "krotka sprzedaz akcji wymaga pozyczenia papieru i odsetek od niej; "
            "symulacja noge krotka OTWIERA (`krotka_pozycja=True`), a tego "
            "kosztu NIE nalicza. Stawka zalezy od dostepnosci konkretnego "
            "papieru u brokera i od CZASU TRZYMANIA, wiec nie jest stala rundy - "
            "zrodlo docelowe: pozycje `FEE` rejestru Alpaki po pierwszych "
            "krotkich sprzedazach na koncie paper"))

    # PYL - TYLKO TAM, GDZIE PROWIZJA OD KUPNA IDZIE W WALUCIE BAZOWEJ.
    #
    # Zmierzone 2026-10-02 na 14 pozycjach demo: reszta po sprzedazy
    # (`raw->>'resztka_nierozliczalna'`) to 0,23-0,37 pb rundy, okolo 1,1%
    # prowizji, a stosunek reszty do kroku wychodzi srednio blisko 1/2 - czyli
    # tyle, ile daje model "reszta jednostajna na [0, krok)". Symulacja liczy
    # ilosci ciagle i tej straty NIE zna, wiec skladnik wchodzi do doplaty
    # (`w_symulacji=False`). Poprawka „sprzedaj ilosc netto po prowizji"
    # (133fc58) usuwa pyl z nadwyzki, nie z zaokraglenia do kroku - ten zostaje.
    if k.prowizja_w_bazowej:
        s.append(Skladnik(
            "pyl", pyl, ZALOZENIE if pyl is not None else NIEZMIERZONE,
            "reszta ponizej kroku ilosci (`ref.instrument.lot_size`) po "
            "sprzedazy: oczekiwana strata polowy kroku, krok*cena/(2*notional); "
            "maksimum po uniwersum przy najwyzszej cenie okna - patrz `pyl_rundy`"
            if pyl is not None else
            "krok ilosci albo cena instrumentu nieznane w danych trenera - "
            "strata na reszcie ponizej kroku po sprzedazy NIE jest naliczana"))

    if k.finansowanie:
        s.append(Skladnik(
            "finansowanie", finansowanie,
            POMIAR if finansowanie is not None else W_SYMULACJI,
            f"placone co {INTERWAL_FINANSOWANIA_H} h niezaleznie od wyniku "
            f"pozycji, wiec zalezy od CZASU TRZYMANIA, a nie od liczby zlecen - "
            f"stala rundy nie istnieje. Naliczane od 2026-09-24 w "
            f"`portfolio.simulate_portfolio` z cechy `funding_rate`",
            w_symulacji=True))
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


# ----------------------------------------------------------------- Brama 0
#
# TWARDY WARUNEK DOPUSZCZENIA DALSZEJ PRACY NAD STRATEGIA (2026-10-11,
# docs/08-przed-go-live.md, Brama 0; docs/hipotezy.md, Z-1).
#
# Przy zmierzonym IC 0,03-0,04 i odchyleniu zwrotu 12 h 3,2% (docs/03)
# przewaga brutto to rzad 10-13 pb na runde. Runda drozsza niz 12 pb zjada ja
# w calosci, wiec zadne strojenie wag, progow ani horyzontu nie da wyniku
# dodatniego - rozstrzyga koszt, nie statystyka. Dlatego prog stoi PRZED
# kazda nowa hipoteza strategii, a nie po niej.
#
# Runda = 2 x maker (stawka z /v5/account/fee-rate KONTA DOCELOWEGO, nie
# demo) + spread wlasnej ksiegi z 30 dni + koszt niewypelnienia zmierzony na
# demo. bybit.eu tego nie spelnia: sam maker 0,25% to 50 pb.
PROG_BRAMY_0_PB = 12.0

OTWARTA = "OTWARTA"
ZAMKNIETA = "ZAMKNIETA"
NIEROZSTRZYGNIETA = "NIEROZSTRZYGNIETA"


@dataclass(frozen=True)
class Brama0:
    stan: str
    runda_pb: float                       # suma skladnikow, ktore MAJA wartosc
    skladniki: dict[str, float | None]    # pb na runde; None = brak pomiaru
    powody: tuple[str, ...]


def brama_0(maker: float | None, *, stawka_konta_docelowego: bool,
            spread_pb: float | None, spread_nasza_ksiega: bool,
            niewypelnienie_pb: float | None,
            prog_pb: float = PROG_BRAMY_0_PB) -> Brama0:
    """Werdykt Bramy 0 z pomiarow. CZYSTA FUNKCJA - zadnych wywolan gieldy.

    `maker` - ulamek ceny na noge; `stawka_konta_docelowego` - czy odczytana
    z konta, na ktorym ma isc live (demo nie jest dowodem stawki live).
    `spread_pb` - PELNY spread na runde (jak `spread.Pomiar.bps`);
    `spread_nasza_ksiega` - z ksiegi, na ktorej skladamy zlecenia.
    `niewypelnienie_pb` - koszt niewypelnienia z demo (`koszt_niewypelnienia`
    z wypelnialnosci i ruchu, ktory przepadl), w pb na runde.

    NIEWYPELNIENIE WCHODZI JAKO max(0, x). Ze znakiem ujemnym oznacza, ze
    wejscia, ktore nie weszly, stracilyby - na spadajacym rynku tak jest
    zawsze, niezaleznie od egzekucji. Brama otwarta dzieki temu, ze rynek
    akurat spadal, bylaby otwarta przez pogode, nie przez koszt.

    ZAMKNIETA juz wtedy, gdy SUMA ZNANYCH skladnikow przekracza prog: pozostale
    sa z definicji nieujemne, wiec moga ja tylko podniesc. OTWARTA wylacznie
    przy komplecie pomiarow: stawka z konta docelowego, spread z wlasnej
    ksiegi, niewypelnienie z demo - i sumie <= progu.
    """
    sklad = {
        "prowizja": None if maker is None else 2 * maker * 1e4,
        "spread": spread_pb,
        "niewypelnienie": (None if niewypelnienie_pb is None
                           else max(0.0, niewypelnienie_pb)),
    }
    suma = sum(v for v in sklad.values() if v is not None)
    braki = []
    if maker is None:
        braki.append("stawka maker nieznana")
    elif not stawka_konta_docelowego:
        braki.append("stawka maker z konta demo, nie z konta docelowego")
    if spread_pb is None:
        braki.append("spread niezmierzony")
    elif not spread_nasza_ksiega:
        braki.append("spread z obcej ksiegi (tylko podloga)")
    if niewypelnienie_pb is None:
        braki.append("koszt niewypelnienia z demo niezmierzony")
    if suma > prog_pb:
        return Brama0(ZAMKNIETA, suma, sklad, (
            f"runda co najmniej {suma:.1f} pb > {prog_pb:g} pb",) + tuple(braki))
    if braki:
        return Brama0(NIEROZSTRZYGNIETA, suma, sklad, tuple(braki))
    return Brama0(OTWARTA, suma, sklad, (f"runda {suma:.1f} pb <= {prog_pb:g} pb",))


def opis_bramy_0(b: Brama0) -> list[str]:
    """Wiersze do logu. Skladnik bez wartosci slowami, nigdy zerem."""
    linie = [f"BRAMA 0 (runda <= {PROG_BRAMY_0_PB:g} pb, docs/08): {b.stan}"]
    for nazwa, v in b.skladniki.items():
        linie.append(f"  {nazwa}: " + ("NIE ZMIERZONO" if v is None else f"{v:.1f} pb"))
    linie.extend(f"  - {p}" for p in b.powody)
    return linie


# ------------------------------------------- kandydatki Bramy 0 (2026-10-11)
#
# CENNIKI GIELD DOSTEPNYCH Z UE, zebrane w docs/brama-0-gieldy.md. TO NIE JEST
# `CENNIK`: zaden z tych wpisow nie zasila petli, symulacji ani uzgodnienia -
# silnik demo nadal liczy `spot_eu`, a `koszty(kind)` tych nazw nie zna.
# Tabela istnieje po to, zeby werdykt Bramy 0 dla kazdej kandydatki byl
# LICZONY z tych samych liczb, ktore stoja w dokumencie, a nie przepisany
# recznie - test `tests/test_brama_0_kandydaci.py` pilnuje, ze dokument
# wymienia kazda kandydatke i ze zadna nie jest OTWARTA.
#
# SKAD SA LICZBY I CZEGO NIE DOWODZA:
#   * stawka: DEKLARACJA z publicznego cennika (URL i data w `zrodlo`), na
#     najnizszym poziomie bez VIP i bez tokenu gieldy. Jedyna stawka ZMIERZONA
#     z konta to `spot_eu` (/v5/account/fee-rate, api-demo.bybit.eu,
#     2026-10-05) - i to z konta DEMO. Brama wymaga stawki z konta docelowego,
#     wiec kazdy wpis wchodzi jako `stawka_konta_docelowego=False`;
#   * spread: MEDIANA PO UNIWERSUM z jednego odczytu publicznej ksiegi
#     (12 probek co 2 s, 2026-10-11 17:48 UTC, 10 aktywow `tradeable`;
#     skrypt w opisie PR). To nie jest 30 dni wlasnych zlecen, wiec wchodzi
#     jako `spread_nasza_ksiega=False` - podloga, nie pomiar;
#   * niewypelnienie: NIE DA SIE go zmierzyc z zewnatrz. Wymaga wlasnych
#     zlecen na tej gieldzie (demo). Zostaje None, a `zapas_na_niewypelnienie_pb`
#     mowi, ile pb z progu zostaje na ten skladnik.
#
# Z tego wynika, ze `brama_0_kandydatki` NIGDY nie oddaje OTWARTA - najlepszy
# mozliwy stan to NIEROZSTRZYGNIETA z dodatnim zapasem. Otworzyc brame moze
# tylko komplet pomiarow na koncie docelowym (docs/hipotezy.md, B-0).

@dataclass(frozen=True)
class KandydatGieldy:
    nazwa: str
    rynek: str                 # "spot" | "perp" | "stock"
    podmiot: str               # kto jest kontrahentem i pod jaka licencja
    maker: float | None        # ulamek na noge; None = cennik bez rozbicia
    taker: float | None
    spread_pb: float | None    # mediana pelnego spreadu po uniwersum, pb
    n_aktywow: int             # ile z 10 aktywow uniwersum ma tam ksiege
    dostepna_pl: bool | None   # None = niepotwierdzone
    zrodlo: str                # URL cennika + data odczytu
    uwaga: str = ""


KANDYDACI_BRAMY_0: tuple[KandydatGieldy, ...] = (
    KandydatGieldy(
        "bybit_eu", "spot", "Bybit EU GmbH, Wieden; MiCA (FMA AT)",
        0.0025, 0.0025, 2.46, 10, True,
        "/v5/account/fee-rate api-demo.bybit.eu 2026-10-05 (135 par jednakowo); "
        "cennik publiczny: 0,25% maker i taker od 2026-10-05",
        "JEDYNA stawka zmierzona z konta (demo). Spread karty 11.10: 7,03 pb "
        "z 30 dni (czesciowo obca ksiega); snapshot wlasnej ksiegi 2,46 pb"),
    KandydatGieldy(
        "okx_eu_usdc", "spot", "OKX Europe (OKCoin Europe Ltd), Malta; MiCA (MFSA)",
        0.0008, 0.0010, 3.21, 10, True,
        "okx.com/en-eu/fees, odczyt 2026-10-11 przez zrodla wtorne",
        "0,08/0,10% tylko z otwartym kontem X-Perps (MiFID); bez niego "
        "0,20/0,35% - do potwierdzenia na koncie"),
    KandydatGieldy(
        "okx_eu_eur", "spot", "jak okx_eu_usdc, pary EUR",
        0.0008, 0.0010, 7.91, 10, True,
        "jak okx_eu_usdc", "ksiega EUR 2,5x szersza niz USDC (ARB 32 pb)"),
    KandydatGieldy(
        "bitvavo", "spot", "Bitvavo B.V., Amsterdam; MiCA (AFM NL)",
        0.0015, 0.0025, 1.48, 10, True,
        "bitvavo.com/en/fees (403 dla robota) + zrodla wtorne, 2026-10-11",
        "tylko pary EUR; wolumen stablecoinow nie liczy sie do progow"),
    KandydatGieldy(
        "kraken_pro", "spot", "Payward Europe Solutions Ltd, Irlandia; MiCA (CBI)",
        0.0040, 0.0080, 1.13, 10, True,
        "kraken.com/features/fee-schedule, odczyt 2026-10-11 (poziomy "
        "miedzyproduktowe od 2026-07-09)",
        "Level 1 (> 0 USD): 0,40/0,80%; Level 3 (AoP >= 20 tys. USD) 0,22/0,38%"),
    KandydatGieldy(
        "coinbase_advanced", "spot", "Coinbase Europe Ltd, Luksemburg; MiCA (CSSF)",
        0.0060, 0.0120, 2.57, 7, True,
        "zrodla wtorne 2026-10-11 (Coinbase nie publikuje pelnej tabeli bez "
        "logowania; help.coinbase.com 403 dla robota)",
        "Intro 1 (>= 0 USD): 0,60/1,20%; SUI, NEAR, ARB bez ksiegi EUR"),
    KandydatGieldy(
        "bitstamp", "spot", "Bitstamp Europe S.A., Luksemburg; MiCA (CSSF)",
        0.0030, 0.0040, None, 0, True,
        "zrodla wtorne 2026-10-11",
        "0/0% tylko do 1000 USD obrotu w 30 dniach - przy 1,6 transakcji na "
        "dobe prog pada w pierwszym tygodniu; spread niepobrany"),
    KandydatGieldy(
        "kucoin_eu", "spot", "KuCoin EU Exchange GmbH, Wieden; MiCA (FMA AT)",
        0.0010, 0.0010, 4.85, 9, None,
        "zrodla wtorne 2026-10-11",
        "licencja jest, start operacji w EU niepotwierdzony (lipiec 2026); "
        "0,08% z oplata w KCS - token gieldy, ryzyko kontrahenta"),
    KandydatGieldy(
        "cryptocom_exchange", "spot", "Crypto.com (Foris DAX MT Ltd), Malta; MiCA (MFSA)",
        0.00075, 0.00075, 0.72, 10, None,
        "zrodla wtorne 2026-10-11 (crypto.com/exchange/document/fees-limits "
        "renderuje sie skryptem)",
        "czy Exchange (nie App) jest w zakresie licencji MiCA - niepotwierdzone"),
    KandydatGieldy(
        "binance", "spot", "Binance - BRAK licencji MiCA",
        0.0010, 0.0010, 0.89, 10, False,
        "parkiet.com / unchainedcrypto.com 2026-06",
        "od 2026-07-01 Binance Poland nie przyjmuje nowych klientow, uslugi "
        "ograniczone; wniosek MiCA w Grecji wycofany"),
    KandydatGieldy(
        "bitget", "spot", "Bitget - BRAK licencji MiCA (wniosek w FMA AT)",
        0.0010, 0.0010, 2.42, 8, False,
        "tangem.com/learning-hub, 2026", "poza zakresem do czasu licencji"),
    KandydatGieldy(
        "kraken_perp", "perp", "Payward Europe Digital Solutions (CY) Ltd; MiFID II (CySEC 342/17)",
        0.0002, 0.0005, 1.99, 10, True,
        "kraken.com/features/fee-schedule 2026-10-11: Level 1 futures 0,02/0,05%; "
        "support.kraken.com derivatives-eligibility-requirements-eea",
        "EEA pasportowane; klient indywidualny: pelna weryfikacja + kwestionariusz "
        "+ NIP. Zwolnienie z oplat EEA (0/0 do 10 mln USD) wygaslo 2026-07-19, "
        "tylko nowi klienci. Finansowanie POZA wzorem bramy, w symulacji. "
        "Demo: demo-futures.kraken.com. Spread z PF_*USD (multi-collateral)"),
    KandydatGieldy(
        "okx_xperps", "perp", "OKX (Malta) MiFID; X-Perps = kontrakt z 5-letnim terminem",
        0.0002, 0.0005, None, 0, True,
        "cointelegraph/fintechfutures 2026 (zrodla wtorne); okx.com/en-eu/fees",
        "30 krajow EEA, detal od 2026-07-28 (Simple Mode). Uniwersum X-Perps "
        "i ksiega niepobrane (instrumenty poza publicznym API globalnym). "
        "5-letni termin to konstrukcja omijajaca klasyfikacje CFD (ESMA "
        "24.02.2026) - ryzyko regulacyjne"),
    KandydatGieldy(
        "coinbase_futures_eu", "perp", "Coinbase Financial Services Europe Ltd; MiFID II (CySEC 374/19)",
        None, None, None, 0, None,
        "theblock.co / decrypt.co 2026-03-09",
        "cennik 'od 0,02% za kontrakt' bez rozbicia maker/taker; 26 krajow bez "
        "potwierdzenia PL; kontrakty BTC, ETH, SOL - nie cale uniwersum"),
    KandydatGieldy(
        "one_trading_perp", "perp", "One Trading (OTF, AFM NL); MiFID II",
        0.0010, 0.0020, None, 2, False,
        "support.onetrading.com art. 21953902771473 (zrodla wtorne; 403 dla robota)",
        "tylko BTC/EUR i ETH/EUR; detal tylko DE/NL/AT; API: -0,005/0,015% "
        "(wtorne, niepotwierdzone)"),
    KandydatGieldy(
        "gemini_perp", "perp", "Gemini Intergalactic EU Artemis; MiFID II (MFSA)",
        0.0002, 0.0007, None, 0, False,
        "gemini.com/fees/derivative-fees-table 2026-10-11",
        "konta EEA ZAMKNIETE 2026-04-06 - poza zakresem"),
    KandydatGieldy(
        "deribit", "perp", "Deribit FZE (Dubaj, VARA) / DRB Panama dla detalu",
        0.00015, 0.00035, 4.67, 10, False,
        "support.deribit.com restricted-jurisdictions; cennik 2026-08",
        "poza UE; po ESMA 24.02.2026 perp dla detalu z UE = CFD pod MiFID, "
        "DRB Panama go nie ma - nie kandydat"),
    KandydatGieldy(
        "alpaca_stock", "stock", "Alpaca Securities LLC (FINRA/SIPC), USA",
        0.0, 0.0, None, 0, None,
        "alpaca.markets/support/non-us-business; sec.gov fee-rate-advisories/2026-2; "
        "finra.org Information Notice 2026-03-17",
        "konto indywidualne non-US: tak, bez minimum; konto firmowe: beta "
        "tylko podmioty z USA, 30 tys. USD minimum - JDG handlowalaby jako "
        "osoba fizyczna. SEC Section 31: 20,60 USD/mln od 2026-04-04 = 0,206 pb "
        "na nodze sprzedazy; TAF 0,000195 USD/akcje (max 9,79 USD). Spread "
        "NIEZMIERZONY (feed IEX, docs/08). Uniwersum: akcje USA, nie krypto"),
)


def brama_0_kandydatki(k: KandydatGieldy, *,
                       niewypelnienie_pb: float | None = None) -> Brama0:
    """Werdykt Bramy 0 z DEKLAROWANEGO cennika i spreadu z jednego odczytu.

    Nigdy nie oddaje OTWARTA: stawka nie jest z konta docelowego, spread nie
    jest z wlasnej ksiegi, niewypelnienia nikt tam nie zmierzyl. Odpowiada
    na jedno pytanie: czy kandydatka jest ZAMKNIETA juz na tym, co wiadomo,
    czy zostaje do zmierzenia. `niewypelnienie_pb` pozwala podstawic liczbe
    z innego rynku (np. 40 pb z symulacji na swiecach globalnych) jako test
    wrazliwosci - wynik z takim podstawieniem to scenariusz, nie pomiar.
    """
    return brama_0(k.maker, stawka_konta_docelowego=False,
                   spread_pb=k.spread_pb, spread_nasza_ksiega=False,
                   niewypelnienie_pb=niewypelnienie_pb)


def zapas_na_niewypelnienie_pb(k: KandydatGieldy,
                               prog_pb: float = PROG_BRAMY_0_PB) -> float | None:
    """Ile pb z progu zostaje na niewypelnienie po prowizji i spreadzie.

    None, gdy stawki albo spreadu nie ma - zapas liczony bez ktoregos z nich
    wygladalby na wiekszy, niz jest. Ujemny zapas = kandydatka zamknieta
    zanim ktokolwiek zmierzy niewypelnienie.
    """
    if k.maker is None or k.spread_pb is None:
        return None
    return prog_pb - (2 * k.maker * 1e4 + k.spread_pb)
