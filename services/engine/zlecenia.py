"""Reguły, które muszą być spełnione, ZANIM zlecenie pójdzie do giełdy.

DLACZEGO TO JEST OSOBNY MODUŁ, A NIE KILKA LINII PRZY SKŁADANIU

Giełda odrzuca zlecenie, którego ilość nie leży na jej kroku, którego cena nie
leży na kroku ceny albo którego wartość jest poniżej minimum. Odrzucenie
przychodzi jako błąd parametru — a w logu nocnego przebiegu wygląda to jak brak
sygnału, nie jak awaria. System „nie handluje" i nikt nie wie dlaczego.

TRZY ODRZUCENIA NIE SĄ SOBIE RÓWNE i to jest sedno tego modułu:

  * odrzucone WEJŚCIE  — kosztuje okazję; pozycji po prostu nie ma,
  * odrzucone WYJŚCIE  — kosztuje pieniądze; pozycja ZOSTAJE OTWARTA wbrew
    decyzji, a risk kernel liczy z niej ekspozycję,
  * przyjęte zlecenie o ilości ZAOKRĄGLONEJ W GÓRĘ — kosztuje ciszej niż oba:
    pozycja jest większa, niż zatwierdził risk kernel.

Dlatego ilość zaokrąglamy zawsze W DÓŁ, a wyjście, którego nie da się złożyć,
jest zdarzeniem krytycznym, nie pominięciem.

BRAK REGUŁY TO NIE JEST REGUŁA SPEŁNIONA. Gdy giełda nie podała kroku, nie
wolno przyjąć „to pewnie dowolna precyzja" — bo wtedy pierwsze zlecenie ginie
na produkcji. Nieznana reguła blokuje zlecenie tak samo jak reguła złamana,
tyle że z innym komunikatem.

REGUŁY GIEŁDY NIE PYTAJĄ, CZY CENA MA SENS. Krok, minimum ilości i minimum
wartości sprawdzają KSZTAŁT zlecenia; wszystkie cztery przechodzą dla zlecenia
sprzedaży po jednej dziesiątej ceny rynkowej. Cena limitu bierze się u nas
z `mark_price`, czyli z zamknięcia ostatniej świecy 1m — więc jeden zepsuty tick
w `market.candle` wystarczy, żeby zlecenie weszło natychmiast po drugiej stronie
księgi. Księga podmiotu unijnego jest dla naszych par kilkukrotnie płytsza niż
globalna (docs/03), więc przejechanie przez nią jest realną stratą, a w dzienniku
wygląda jak zwykła transakcja. Stąd PASMO CENY — patrz `ocena_pasma`.

PASMO NIE JEST JEDNAK ODPOWIEDZIĄ NA PYTANIE WYKONALNOŚCI. Zlecenia idą jako
`PostOnly`: cena zbyt daleko od rynku leży bezczynnie, a cena przecinająca
spread jest ODRZUCANA. Pasmo o tym nie mówi nic — 10,5% nad rynkiem mieści się
w nim bez problemu. Odpowiedzią jest cena wzięta z księgi docelowej W CHWILI
WYSYŁKI, po pasywnej stronie spreadu — patrz `cena_makera` i `Oferty`.

I DOTYCZY TO OBU RÓL, choć różne rzeczy nimi ryzykujemy. Wyjście MUSI się
wypełnić, bo inaczej pozycja zostaje otwarta wbrew decyzji, więc nie dostaje
żadnej granicy tolerancji. Wejście dostaje ją — `prog_zwloki` — bo jego
odrzucenie kosztuje tylko okazję, a wejście po cenie odjechanej od ceny
decyzyjnej jest zakładem, którego nikt nie zatwierdzał. Do 2026-09-26 wejście
nie dostawało ANI podmiany ceny, ANI progu: jechało z ceną zamknięcia świecy
sprzed 71–82 minut i to ona jest przyczyną wszystkich trzech odrzuceń PostOnly
w historii produkcji.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal


@dataclass(frozen=True)
class Filtry:
    """Reguły giełdy dla jednego instrumentu. None znaczy NIEZNANE, nie „brak"."""
    krok_ilosci: Decimal | None = None
    min_ilosc: Decimal | None = None
    min_wartosc: Decimal | None = None
    krok_ceny: Decimal | None = None

    @property
    def kompletne(self) -> bool:
        # Krok ceny i ilosci sa niezbedne do ZLOZENIA zlecenia. Minimalna
        # wartosc jest niezbedna do stwierdzenia, czy da sie z niego WYJSC.
        #
        # DODATNIE, nie tylko "nie None". Zero przechodzilo tedy jako wartosc
        # ZNANA, instrument byl dopuszczany do egzekucji, a dopiero gleboko
        # w zaokraglaniu lecial goly `ValueError("krok musi byc dodatni")`.
        # Nie jest to blad gieldy, wiec nie lapie go obsluga bledow egzekucji -
        # wywracal CALY cykl decyzyjny przez jeden instrument z zepsuta regula.
        #
        # Zero w `tick_size` nie jest hipoteza: kolumna jest NUMERIC bez
        # ograniczenia dodatniosci, a wartosci pochodza z odpowiedzi gieldy.
        return all(w is not None and w > 0
                   for w in (self.krok_ilosci, self.krok_ceny, self.min_wartosc))


def bez_ogona(d: Decimal) -> Decimal:
    """Ta sama liczba bez końcowych zer. Kształt, nie wartość.

    DLACZEGO TO W OGÓLE MA ZNACZENIE

    `lot_size` wraca z bazy jako NUMERIC, czyli `Decimal("0.001000000000")` —
    z zachowaną skalą kolumny. Mnożenie przenosi te dwanaście miejsc do wyniku,
    więc ilość `1.234` jedzie do giełdy jako `1.234000000000`, a Bybit odrzuca
    ją: „Order quantity has too many decimals" (170137).

    Wartość jest przy tym POPRAWNA — błędny jest sam zapis. Objaw nie wskazuje
    więc na przyczynę: wygląda na złe zaokrąglenie, a jest kwestią skali kolumny.

    `normalize()` samo nie wystarcza: dla liczb całkowitych daje postać
    wykładniczą (`2000` → `2E+3`), którą giełda też odrzuci.
    """
    d = d.normalize()
    return d.quantize(Decimal(1)) if d == d.to_integral_value() else d


def do_kroku_w_dol(wartosc: Decimal, krok: Decimal) -> Decimal:
    """Zaokrąglenie W DÓŁ do wielokrotności kroku.

    Kierunek nie jest kwestią gustu. W górę dałoby pozycję większą, niż
    zatwierdził risk kernel — a przekroczenie limitu przez zaokrąglenie jest
    najcichszym możliwym sposobem jego złamania.
    """
    if krok <= 0:
        raise ValueError("krok musi byc dodatni")
    return bez_ogona((wartosc / krok).to_integral_value(rounding=ROUND_DOWN) * krok)


def cena_do_kroku(cena: Decimal, krok: Decimal, kupno: bool) -> Decimal:
    """Cena limitu przyciągnięta do siatki, ZAWSZE na naszą korzyść.

    Przy kupnie w dół, przy sprzedaży w górę. Przeciwny kierunek płaciłby
    o jeden krok więcej za każdym razem — koszt mały, ale systematyczny
    i w całości po naszej stronie.
    """
    if krok <= 0:
        raise ValueError("krok musi byc dodatni")
    w_dol = do_kroku_w_dol(cena, krok)
    if kupno or w_dol == cena:
        return w_dol
    return bez_ogona(w_dol + krok)


# ------------------------------------------------------------------ pasmo ceny
#
# PRÓG WYPROWADZONY ZE ZMIENNOŚCI INSTRUMENTU, NIE Z LICZBY DOBRANEJ NA OKO.
#
# Stały próg procentowy jest w tym projekcie znanym antywzorcem (patrz
# `atr_percentile_80` w loop.py i SWIEC_ZWLOKI w dozor.py): 1% to szok dla pary
# stabilnej i szum dla SOL-a, więc jedna liczba jest albo krzykliwa, albo
# bezużyteczna. Jednostką odległości jest tu ATR instrumentu — ta sama wielkość,
# którą silnik już liczy (`atr_pct`, w PROCENTACH ceny) i którą już skaluje
# pierwiastkiem z liczby świec (`signals._oczekiwany_ruch`: `atr * sqrt(bars)`).
#
# DLACZEGO PIERWIASTEK Z LICZBY ŚWIEC. Cena limitu powstaje na zamknięciu świecy
# T, a plan żyje do końca świecy T+1 (`zaplanuj`: valid_until = bar_start
# + 2 * bar_minutes). W chwili wysyłki wolno mu więc być rozjechanym z rynkiem
# o tyle, ile rynek przechodzi w dwie świece — a to skaluje się pierwiastkiem,
# nie liniowo.
#
# DLACZEGO SZEŚĆ. Próg nie ma odróżniać dobrej ceny od kiepskiej — od tego jest
# model. Ma odróżniać cenę od AWARII DANYCH. Zwykły ruch to ułamek ATR, a błąd,
# którego się boimy, to rząd wielkości, czyli 900%. Między jednym a drugim są
# dwa rzędy wielkości zapasu, więc dokładna krotność w przedziale 4–10 niczego
# nie zmienia; zmienia to, że skaluje się ze zmiennością pary.
KROTNOSC_PASMA = 6

# ROZJAZD MIĘDZY KSIĘGAMI — SKŁADNIK DODAWANY, NIE PODŁOGA.
#
# Dane publiczne bierzemy z podmiotu GLOBALNEGO (`bybit.PUBLIC_HOST` to
# api.bybit.com — „to on ma księgę, na której model był trenowany"), a zlecenie
# ląduje w księdze UNIJNEJ (api.bybit.eu, host z `ref.venue`). To dwie różne
# księgi i różnica między nimi NIE JEST awarią, tylko stanem codziennym.
#
# Zmierzone na 1600 świecach 4h (2026-09-07):
#
#     razem       mediana  5,9 pb   p90 28,4 pb   max 153 pb   16,8% świec > 20 pb
#     NEARUSDC    mediana 18,8 pb   p90 51,3 pb   max 153 pb   49,5% świec > 20 pb
#
# Runda kosztowa to 20 pb, więc w jednej świecy na sześć sam rozjazd ksiąg
# przekracza całą przewagę strategii. Dla pasma znaczy to jedno: ten składnik
# musi w progu BYĆ, i to jako dodawany, a nie jako `max(...)`. Podłoga
# znikałaby dokładnie tam, gdzie zmienność jest duża — a rozjazd ksiąg nie
# znika wtedy, gdy rynek się rusza; wtedy jest największy.
#
# 200 pb to zmierzone maksimum (153 pb) z zapasem na dłuższą próbkę. Próg
# węższy odmawiałby zleceń w stanie, który sami zmierzyliśmy jako normalny —
# a alarm, który dzwoni co szóstą świecę, uczy odbiorcę, że można go pomijać.
ROZJAZD_GIELD = Decimal("0.02")

# SUFIT. Powyżej ćwierci ceny pasmo przestaje cokolwiek wykluczać, a sam ATR
# przestaje być wiarygodny — jeśli wyszedł tak duży, to prawdopodobnie policzono
# go z tych samych zepsutych świec. Ten sam sufit jest pasmem AWARYJNYM, gdy
# zmienności nie znamy: nie twierdzimy wtedy nic poza tym, że rzędu wielkości
# nie przepuścimy.
SUFIT_PASMA = Decimal("0.25")

# Ile świec plan może przeżyć między powstaniem ceny a wysyłką - patrz `zaplanuj`.
BARY_ZYCIA_PLANU = 2.0


def _zmiennosc_znana(atr_pct: float | None) -> bool:
    # NaN nie jest > 0, wiec ten warunek zalatwia takze wartosc zepsuta.
    return atr_pct is not None and atr_pct > 0


def prog_pasma(atr_pct: float | None, *, bary: float = BARY_ZYCIA_PLANU) -> Decimal:
    """Względna półszerokość pasma. CZYSTA FUNKCJA — bez bazy i bez sieci.

    Dwa składniki, bo dwie są przyczyny, dla których cena limitu ma prawo
    odbiegać od księgi docelowej, i żadna nie zastępuje drugiej:

      * RUCH RYNKU między powstaniem ceny a wysyłką — skaluje się ze
        zmiennością pary, `KROTNOSC_PASMA * atr * sqrt(bary)`,
      * ROZJAZD KSIĄG globalnej i unijnej — nie zależy od tego, ile świec
        minęło, więc wchodzi jako stały składnik `ROZJAZD_GIELD`.

    `atr_pct` w procentach ceny, tak jak w `feat.feature_value` (features.atr_pct
    mnoży przez 100). Nieznana zmienność daje pasmo awaryjne, nie brak pasma:
    „nie wiem, jak bardzo ta para się rusza" nie jest powodem, żeby przepuścić
    cenę dziesięciokrotnie oderwaną od rynku.
    """
    if not _zmiennosc_znana(atr_pct) or bary <= 0:
        return SUFIT_PASMA
    ruch = (Decimal(str(atr_pct)) / 100
            * Decimal(str(math.sqrt(bary))) * KROTNOSC_PASMA)
    return min(ROZJAZD_GIELD + ruch, SUFIT_PASMA)


def cena_makera(odniesienie: Decimal, krok: Decimal, *, kupno: bool) -> Decimal:
    """Najbliższa rynkowi cena po NASZEJ stronie księgi. CZYSTA FUNKCJA.

    DLACZEGO O KROK OD ODNIESIENIA, A NIE DOKŁADNIE NA NIM. Odniesienie to
    ostatnia cena transakcyjna z księgi docelowej, więc leży między najlepszym
    kupnem a najlepszą sprzedażą. Kupno o krok PONIŻEJ niej jest tym samym
    poniżej najlepszej sprzedaży, a sprzedaż o krok POWYŻEJ — powyżej
    najlepszego kupna; żadne z nich nie może się skojarzyć w chwili złożenia,
    więc `PostOnly` przyjmuje je jako zlecenie makera zamiast odrzucić.
    Postawione dokładnie na odniesieniu mogłoby już przecinać spread, bo nie
    wiadomo, po której jego stronie ta transakcja się odbyła.

    DLACZEGO DOKŁADNIE JEDEN KROK, A NIE ZAPAS. Każdy kolejny krok oddala
    zlecenie od rynku, a to zmniejsza szansę wypełnienia — czyli dokładnie to,
    czego wyjściu potrzeba najbardziej. Zapas kupowałby pewność ZŁOŻENIA za
    cenę pewności WYKONANIA.
    """
    if krok <= 0:
        raise ValueError("krok musi byc dodatni")
    if kupno:
        w_dol = do_kroku_w_dol(odniesienie, krok)
        return bez_ogona(w_dol - krok) if w_dol == odniesienie else w_dol
    w_gore = cena_do_kroku(odniesienie, krok, kupno=False)
    return bez_ogona(w_gore + krok) if w_gore == odniesienie else w_gore


@dataclass(frozen=True)
class Pasmo:
    """Rozstrzygnięcie pasma dla jednej ceny. Dane, nie decyzja."""
    odniesienie: Decimal
    prog: Decimal
    dolna: Decimal
    gorna: Decimal
    odchylenie: Decimal
    ze_zmiennosci: bool

    @property
    def poza(self) -> bool:
        return self.odchylenie > self.prog

    def opis(self) -> str:
        skad = ("rozjazd ksiąg + 6×ATR×√świec" if self.ze_zmiennosci
                else "pasmo awaryjne (ATR nieznany)")
        return (f"cena limitu odbiega od księgi docelowej o "
                f"{self.odchylenie * 100:.2f}% przy dopuszczalnych "
                f"{self.prog * 100:.2f}% [{skad}]; odniesienie "
                f"{self.odniesienie}, pasmo {self.dolna}–{self.gorna}")


def ocena_pasma(cena: Decimal, odniesienie: Decimal | None,
                atr_pct: float | None = None, *,
                bary: float = BARY_ZYCIA_PLANU) -> Pasmo | None:
    """Czy cena limitu leży w rozsądnej odległości od rynku. CZYSTA FUNKCJA.

    `odniesienie` MUSI POCHODZIĆ Z KSIĘGI, W KTÓREJ ZLECENIE WYLĄDUJE — czyli
    z podmiotu UNIJNEGO (api.bybit.eu; świece i arkusz wystawia publicznie, bez
    klucza). To nie jest szczegół implementacyjny, tylko cały sens tej funkcji:

      * cena limitu powstaje z `mark_price`, czyli z zamknięcia świecy 1m
        pobranej z `bybit.PUBLIC_HOST` = api.bybit.com — podmiotu GLOBALNEGO,
      * porównanie jej z tą samą księgą jest porównaniem liczby z samą sobą:
        przejdzie zawsze, także dla ticka zepsutego,
      * a rozjazd między księgą globalną a unijną jest zmierzony i duży
        (patrz ROZJAZD_GIELD) — to on jest tu głównym wykrywanym zjawiskiem,
        nie hipotetyczna awaria danych.

    Zwraca None, gdy odniesienia nie ma — bo wtedy nie ma czego porównywać.
    Brak odniesienia jest LUKĄ, nie zgodą; wywołujący, który go nie poda,
    zostaje z zabezpieczeniem wyłączonym.
    """
    if odniesienie is None or odniesienie <= 0:
        return None
    prog = prog_pasma(atr_pct, bary=bary)
    return Pasmo(
        odniesienie=odniesienie,
        prog=prog,
        dolna=odniesienie * (Decimal(1) - prog),
        gorna=odniesienie * (Decimal(1) + prog),
        odchylenie=abs(cena - odniesienie) / odniesienie,
        ze_zmiennosci=_zmiennosc_znana(atr_pct),
    )


# ------------------------------------------------- cena z CHWILI WYSYLKI
#
# CENA LIMITU POWSTAWALA NA ZAMKNIECIU SWIECY, A ZLECENIE SZLO 71-82 MINUT POZNIEJ.
#
# Zmierzone na produkcji (journal.pominiecie + exec.order, 25-26.09.2026):
#
#     bar 2026-09-25 08:00 -> za_malo_cech 12:01, niepelne_sygnaly 12:11,
#                             decyzja 13:11:48  (+71 min od zamkniecia swiecy)
#     bar 2026-09-25 20:00 -> decyzja 01:21:16  (+81 min)
#     bar 2026-09-26 00:00 -> decyzja 05:21:52  (+82 min)
#
# W tym czasie SOL zszedl o 13-18 centow, wiec limit kupna zostawal NAD askiem.
# Egzekucja wysyla `timeInForce: PostOnly` (`bybit.zloz_zlecenie`), a zlecenie
# przecinajace spread PostOnly ODRZUCA - to zachowanie zamowione, nie awaria
# gieldy. Wszystkie trzy odrzucenia w historii produkcji (zlecenia 55, 57, 58)
# to dokladnie ten przypadek; oba wypelnione zlecenia (54, 56) mialy limit po
# stronie pasywnej. Separacja zupelna: 3/3 kontra 2/2.
#
# Wyjscie tego nie mialo, bo `sprawdz_zlecenie` podmienialo mu cene na
# `cena_makera` z odczytu Z CHWILI WYSYLKI. Wejscie takiej podmiany nie
# dostawalo - i to jest cala roznica miedzy tymi dwiema sciezkami.


@dataclass(frozen=True)
class Oferty:
    """Najlepsze oferty z ksiegi EGZEKUCJI, odczytane W CHWILI WYSYLKI.

    DLACZEGO BID I ASK, A NIE OSTATNIA CENA. `cena_odniesienia` oddaje
    `lastPrice`, czyli cene ostatniej TRANSAKCJI - a ta nie musi lezec miedzy
    dzisiejszym bidem i askiem. Zmierzone na zleceniu 54 (SOLUSDC, sprzedaz):
    limit wyszedl 114,55 przy arkuszu 114,39 / 114,43, czyli `lastPrice`
    bylo JEDENASCIE tickow NAD askiem. Tym razem skonczylo sie zleceniem
    lezacym dalej od rynku niz trzeba; w druga strone (ksiega odjechala do
    gory po ostatniej transakcji) ta sama arytmetyka daje sprzedaz PONIZEJ
    bidu, czyli cene przecinajaca spread - i odrzucenie przez PostOnly.

    Pytanie "po ktorej stronie spreadu lezy moje zlecenie" ma odpowiedz
    wylacznie w arkuszu. Ostatnia transakcja jest jej przyblizeniem o bledzie
    rzedu spreadu i to przyblizenie zawodzi dokladnie wtedy, gdy rynek sie
    rusza.
    """
    bid: Decimal
    ask: Decimal

    @property
    def kompletne(self) -> bool:
        # ASK NIE MOZE LEZEC PONIZEJ BIDU. Arkusz odwrocony nie jest arkuszem
        # ciasnym - jest odczytem zepsutym, a podstawienie go dawaloby cene
        # po ZLEJ stronie ksiegi przy zachowaniu wszystkich pozostalych regul.
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid

    def pasywna(self, *, kupno: bool) -> Decimal:
        """Strona ksiegi, po ktorej NASZE zlecenie moze lezec bezczynnie."""
        return self.bid if kupno else self.ask


# ILE RAZY ATR WOLNO PRZEJSC CENIE MIEDZY DECYZJA A WYSYLKA.
#
# ZMIERZONE, NIE DOBRANE. Rozklad bezwzglednego dryfu ceny przez 75 minut
# (dokladnie tyle, ile wynosi zwloka na produkcji) policzony z `market.candle`
# 1m za 30 dni, dla calego uniwersum `active AND tradeable`, obok mediany
# `atr_pct__4h` z `feat.feature_value` za ten sam okres:
#
#     symbol     n      dryf p90   dryf p99   ATR 4h med   p99 / ATR
#     ADAUSDC   31600     1,55%      3,25%       2,58%        1,26
#     ARBUSDC   31600     3,08%      7,70%       5,22%        1,48
#     BTCUSDC   31600     0,58%      1,49%       1,13%        1,32
#     DOGEUSDC  31600     1,33%      3,11%       2,07%        1,50
#     ETHUSDC   31600     0,79%      2,13%       1,53%        1,39
#     LINKUSDC  31599     1,36%      3,14%       2,04%        1,54
#     NEARUSDC  31599     3,10%      5,84%       3,91%        1,49
#     SOLUSDC   31599     1,00%      2,34%       1,85%        1,26
#     SUIUSDC   31599     1,85%      4,22%       2,67%        1,58
#     XRPUSDC   31599     1,27%      3,00%       2,08%        1,44
#
# Ostatnia kolumna mieszka w przedziale 1,26-1,58 dla WSZYSTKICH DZIESIECIU
# instrumentow, rozniacych sie zmiennoscia pieciokrotnie (BTC 1,13% wobec ARB
# 5,22%). To jest cala podstawa tej stalej: 1,5 x ATR to p98-p99,5 zmierzonego
# dryfu 75-minutowego na kazdej z tych par. Prog odmawia wiec okolo jednego
# wejscia na sto - tych, w ktorych rynek przeszedl wiecej niz w 99 ze 100
# porownywalnych okien.
#
# DLACZEGO NIE STALY PROCENT. Mediana dryfu 75-minutowego to 0,17% dla BTC
# i 1,02% dla NEAR - szesciokrotnie. Jedna liczba bylaby albo krzykliwa dla
# BTC, albo bezuzyteczna dla NEAR; ten sam antywzorzec, ktory ten plik opisuje
# przy `KROTNOSC_PASMA`.
#
# DLACZEGO NIE PROG SKALOWANY SAMA ZWLOKA (atr * sqrt(zwloka / swieca)). Te
# same dane pasuja do obu postaci rownie dobrze, bo dla ustalonej zwloki 75 min
# sqrt(75/240) jest stalym czynnikiem (wtedy krotnosc wychodzi 2,25-2,83).
# Roznia sie zachowaniem, gdy potok cech sie pogorszy: postac ze zwloka
# ROZSZERZA tolerancje dokladnie wtedy, gdy decyzja jest bardziej przestarzala.
# Ta postac tego nie robi - dluzsza zwloka znaczy wiecej odmow, czyli nacisk
# w strone naprawy potoku, a nie w strone przyzwyczajenia sie do niego.
#
# DLACZEGO PROG JEST SYMETRYCZNY, choc kazda strona boli inaczej:
#
#   * rynek POSZEDL W GORE po decyzji o kupnie - kupujemy drozej, niz liczyl
#     risk kernel. `qty` zostaje z planu (zmiana ilosci byla by zmiana decyzji,
#     nie ceny), wiec wartosc pozycji przekracza zatwierdzona dokladnie o ten
#     dryf. Prog na dryfie JEST wiec zarazem sufitem na to przekroczenie;
#   * rynek POSZEDL W DOL - kupujemy taniej, ale cechy, z ktorych powstal
#     sygnal, opisuja rynek, ktorego juz nie ma. Wejscie po tej cenie jest nowym
#     zakladem, ktorego nikt nie zatwierdzil.
KROTNOSC_ZWLOKI = Decimal("1.5")

# PROG AWARYJNY, GDY ZMIENNOSCI NIE ZNAMY. `1,5 x mediana ATR uniwersum`
# (2,08% z tabeli wyzej) zaokraglone do 3%. Nie jest to pomiar tej pary - jest
# to zdanie "nie wiemy, ile ta para chodzi, wiec bierzemy pare typowa". Brak
# cechy nie moze ani wylaczac progu (bo wtedy awaria danych otwiera furtke),
# ani zatrzymywac handlu (bo `zloz_oczekujace` sciaga ATR bez ograniczenia
# wieku i pojedynczy brak wpisu wstrzymalby wszystkie wejscia).
PROG_ZWLOKI_BEZ_ATR = Decimal("0.03")

# SUFIT. Najwyzszy prog policzony ze zmierzonych ATR-ow to ARBUSDC 7,8%, wiec
# 10% NIE WIAZE dla zadnej pary z uniwersum - wiaze dopiero dla ATR ponad
# 6,67% na swiecy 4h, czyli dla wartosci, ktora sama jest podejrzana. Ten sam
# powod co przy `SUFIT_PASMA`: jesli ATR wyszedl tak duzy, to prawdopodobnie
# policzono go z zepsutych swiec, a prog liczony z zepsutej liczby nie jest
# progiem.
SUFIT_ZWLOKI = Decimal("0.10")


def prog_zwloki(atr_pct: float | None) -> Decimal:
    """Wzgledny prog dryfu ceny miedzy decyzja a wysylka. CZYSTA FUNKCJA.

    `atr_pct` w PROCENTACH ceny, tak jak w `feat.feature_value`
    (`features.atr_pct` mnozy przez 100) i tak samo jak w `prog_pasma`.
    """
    if not _zmiennosc_znana(atr_pct):
        return PROG_ZWLOKI_BEZ_ATR
    return min(KROTNOSC_ZWLOKI * Decimal(str(atr_pct)) / 100, SUFIT_ZWLOKI)


@dataclass(frozen=True)
class Zwloka:
    """Ile cena odjechala od ceny DECYZYJNEJ. Dane, nie decyzja."""
    cena_decyzji: Decimal
    cena_wysylki: Decimal
    prog: Decimal
    odchylenie: Decimal
    ze_zmiennosci: bool

    @property
    def poza(self) -> bool:
        return self.odchylenie > self.prog

    def opis(self) -> str:
        skad = ("1,5×ATR pary" if self.ze_zmiennosci
                else "prog awaryjny (ATR nieznany)")
        return (f"cena odjechala od ceny decyzyjnej o "
                f"{self.odchylenie * 100:.2f}% przy dopuszczalnych "
                f"{self.prog * 100:.2f}% [{skad}]; decyzja stala na "
                f"{self.cena_decyzji}, arkusz w chwili wysylki daje "
                f"{self.cena_wysylki} — wejscie po tej cenie nie jest tym, "
                f"co decyzja popierala")


def ocena_zwloki(cena_decyzji: Decimal, cena_wysylki: Decimal | None,
                 atr_pct: float | None) -> Zwloka | None:
    """Czy cena z chwili wysylki jest jeszcze ta, ktora decyzja popierala.

    POROWNUJEMY CENE, KTORA NAPRAWDE POJDZIE NA GIELDE - czyli to, co oddalo
    `sprawdz_zlecenie` - z cena, na ktorej stanela DECYZJA. Nie mid i nie sam
    bid: mid jest wielkoscia, ktorej nikt nie placi, a pytanie brzmi "o ile
    odjechala cena, po ktorej wejdziemy".

    DLATEGO TA FUNKCJA NIE PYTA, SKAD WZIELA SIE CENA WYSYLKI. Jest ona albo
    z arkusza, albo z ostatniej ceny tej samej ksiegi - i w obu przypadkach
    porownanie jest dokladnie to samo. Gdyby funkcja przyjmowala `Oferty`,
    sciezka zapasowa (ostatnia cena) zostawalaby BEZ progu, czyli bez
    zabezpieczenia, i nic by tego nie pokazalo.

    Zwraca None, gdy ceny z chwili wysylki nie ma - BRAK ODCZYTU TO LUKA,
    NIE ZGODA. Co wtedy zrobic z wejsciem, rozstrzyga `gielda_exec.zloz_plan`;
    tutaj nie ma czego porownywac.
    """
    if cena_wysylki is None or cena_wysylki <= 0 or cena_decyzji <= 0:
        return None
    return Zwloka(
        cena_decyzji=cena_decyzji,
        cena_wysylki=cena_wysylki,
        prog=prog_zwloki(atr_pct),
        odchylenie=abs(cena_wysylki - cena_decyzji) / cena_decyzji,
        ze_zmiennosci=_zmiennosc_znana(atr_pct),
    )


def sprawdz_zlecenie(ilosc: Decimal, cena: Decimal, f: Filtry, *,
                     kupno: bool, wyjscie: bool,
                     cena_odniesienia: Decimal | None = None,
                     atr_pct: float | None = None,
                     bary: float = BARY_ZYCIA_PLANU,
                     najlepsze: Oferty | None = None,
                     slad: dict | None = None) -> tuple[Decimal, Decimal, str | None]:
    """Zwraca (ilosc, cena, powod_odmowy). `powod_odmowy is None` znaczy: wolno.

    `slad` (OPCJONALNY) JEST WYPELNIANY W MIEJSCU, GDZIE ZRODLO CENY SIE
    ROZSTRZYGA - i tylko dlatego jest tu, a nie u wywolujacego. Egzekucja musi
    zapisac do `exec.order.raw`, czy cena wyszla z ARKUSZA, czy z zapasowej
    `cena_odniesienia` (`lastPrice`), bo te dwa zrodla roznia sie o rzad
    spreadu - na zleceniu 54 `lastPrice` lezalo JEDENASCIE tickow nad askiem.
    Gdyby wywolujacy odtwarzal ten wybor u siebie (`if oferty is not None ...`),
    mialby DRUGA kopie regul kolejnosci zrodel nizej i rozjechalaby sie z nia
    przy pierwszej zmianie - a rozjazd byloby widac dopiero w raporcie
    z produkcji, jako opis przyczyny wskazujacy nie to zrodlo, ktore zadzialalo.

    `kupno` i `wyjscie` SĄ ROZDZIELONE CELOWO. Kusi, żeby wyprowadzić jedno
    z drugiego — „wyjście to sprzedaż" — ale to prawda tylko dla pozycji
    długiej. Dla krótkiej wyjściem jest kupno, więc sklejenie tych dwóch pojęć
    przyciągałoby cenę limitu w złą stronę: o krok drożej przy każdym zamknięciu
    shorta. Koszt mały, systematyczny i niewidoczny.

    `wyjscie` nie zmienia reguł giełdy — zmienia WAGĘ odmowy, i dlatego wchodzi
    do komunikatu. Odmowa wyjścia zostawia otwartą pozycję, więc musi być
    czytelna jako zdarzenie krytyczne już w treści, a nie dopiero w kodzie,
    który ją odbiera.

    PRZY CENIE ODNIESIENIA `wyjscie` zmienia coś więcej — samą reakcję na cenę
    ABSURDALNĄ. Pasmo i cena z chwili wysyłki służą tu do dwóch różnych rzeczy:

      * PASMO odpowiada na pytanie „czy cena z planu to jeszcze cena, czy już
        awaria danych". Przy WEJŚCIU poza pasmem → ODMOWA; kosztuje okazję,
        czyli nic, czego byśmy już nie stracili, gdyby ceny nie było w ogóle.
        Przy WYJŚCIU pasmo nie odmawia niczego, bo odmowa zostawiłaby otwartą
        pozycję wbrew decyzji — dokładnie ten skutek, przed którym ostrzega
        docstring modułu.
      * CENA Z CHWILI WYSYŁKI (`najlepsze`, a zapasowo `pasmo.odniesienie`)
        odpowiada na pytanie „po ile da się dziś złożyć zlecenie, żeby PostOnly
        je przyjął". Dostają ją OBIE role i to jest poprawka z 2026-09-26.

    GRANICĘ TOLERANCJI DLA WEJŚCIA STAWIA `gielda_exec.zloz_plan`, nie ta
    funkcja — `ocena_zwloki` na cenie, którą tu zwracamy. Tu jej nie ma, bo ta
    funkcja nie wie, czy wywołujący pytał giełdy; gdyby zgadywała, `czy_zamykalna`
    i `proba_egzekucji` dostałyby próg liczony z ceny, której nikt nie odczytał.

    DLACZEGO NIE KRAWĘDŹ PASMA, choć tak to tu kiedyś działało. Półszerokość
    pasma to `ROZJAZD_GIELD + 6×ATR×√2`, czyli 10,5% przy ATR 1% i 25% przy ATR
    nieznanym. Sprzedaż wyjścia przyciągnięta do `gorna` stoi więc dziesięć
    procent NAD rynkiem — a jako `PostOnly` jest przez giełdę PRZYJMOWANA (nie
    przecina spreadu) i po prostu leży. Po dwóch świecach plan wygasa, zlecenie
    idzie do anulowania, POZYCJA ZOSTAJE OTWARTA, a następna świeca powtarza to
    samo: w trwającym spadku wyjście goni cenę, tracąc po cztery godziny za
    każdym razem. W dzienniku wygląda to na porządek, bo zlecenia były składane.
    Symetrycznie w dół: przyciągnięcie do `dolna` daje sprzedaż pod rynkiem,
    czyli cenę przecinającą spread, którą `PostOnly` ODRZUCA — wyjścia znowu
    nie ma. Krawędź pasma nie jest ceną, po której ktoś handluje; jest granicą
    tego, co jeszcze uznajemy za nie-awarię.

    DLACZEGO PODMIANA ZAWSZE, A NIE DOPIERO POZA PASMEM. Pasmo mierzy, czy cena
    jest SENSOWNA, a nie czy jest WYKONALNA; zlecenie wewnątrz pasma potrafi
    stać osiem procent od rynku i leżeć bezczynnie do wygaśnięcia planu albo —
    po drugiej stronie — przecinać spread i wracać jako odrzucone. Cena z planu
    nie niesie zresztą żadnej intencji poza kierunkiem: `loop.zaplanuj` dostaje
    dla wejścia i wyjścia tę samą cenę zamknięcia świecy z księgi GLOBALNEJ.
    Skoro znamy dzisiejszy arkusz księgi, w której zlecenie naprawdę wyląduje,
    to on jest lepszą odpowiedzią na pytanie „po ile da się teraz złożyć".

    ZEPSUTE ODNIESIENIE NIE KUPUJE NAM ZŁEGO WYPEŁNIENIA. Gdyby cena z księgi
    docelowej była nieprawdziwa, wyjście albo poleży (za daleko od rynku), albo
    zostanie odrzucone przez `PostOnly` (przecina spread). Ryzykiem zostaje więc
    brak wyjścia — ten sam, który mamy bez odniesienia — a nie sprzedaż poniżej
    rynku. Bez odniesienia (`ocena_pasma` zwraca None) cena zostaje planowa:
    tak działa `czy_zamykalna`, które pyta o KSZTAŁT zlecenia, nie o rynek.

    ŚLAD PO PODMIANIE CENY zostaje w danych: `journal.planned_order.limit_price`
    trzyma cenę pierwotną, `exec.order.limit_price` — wysłaną. Różnica między
    tymi dwiema kolumnami JEST zapisem zdarzenia.
    """
    rola = "WYJŚCIE" if wyjscie else "wejście"
    # DOMYSLNIE "plan", bo odmowy ponizej wychodza ZANIM cena zostanie
    # podmieniona - i wtedy cena naprawde jest ta z planu.
    if slad is not None:
        slad["zrodlo_ceny"] = "plan"
        slad["cena_rynkowa"] = None
    if not f.kompletne:
        brak = [n for n, v in (("krok ilości", f.krok_ilosci),
                               ("krok ceny", f.krok_ceny),
                               ("min. wartość", f.min_wartosc)) if v is None]
        return ilosc, cena, (
            f"{rola}: giełda nie podała reguł ({', '.join(brak)}) — zlecenie "
            f"złożone bez nich zostałoby odrzucone jako błąd parametru, "
            f"co w logu wygląda jak brak sygnału")

    i = do_kroku_w_dol(ilosc, f.krok_ilosci)
    c = cena_do_kroku(cena, f.krok_ceny, kupno=kupno)

    pasmo = ocena_pasma(c, cena_odniesienia, atr_pct, bary=bary)
    if pasmo is not None and not wyjscie and pasmo.poza:
        return i, c, f"{rola}: {pasmo.opis()}"

    # CENA Z CHWILI WYSYLKI ZASTEPUJE CENE Z PLANU - I DOTYCZY TO TAKZE WEJSCIA.
    #
    # Do 2026-09-26 podmiane dostawalo WYLACZNIE wyjscie, a wejscie jechalo
    # z cena zamkniecia swiecy - starsza o 71-82 minuty (pomiary w naglowku
    # sekcji "cena z CHWILI WYSYLKI"). Wszystkie trzy odrzucenia PostOnly
    # w historii produkcji to wejscia; oba wypelnienia mialy limit po stronie
    # pasywnej. Asymetria nie miala uzasadnienia: PostOnly odrzuca cene
    # przecinajaca spread niezaleznie od tego, czy wchodzimy, czy wychodzimy.
    #
    # PODMIANA IDZIE ZANIM POLICZYMY min_wartosc - inaczej sprawdzalibysmy
    # minimum na cenie, ktorej i tak nie wyslemy.
    #
    # KOLEJNOSC ZRODEL JEST KOLEJNOSCIA JAKOSCI, nie wygody:
    #   1. arkusz ksiegi EGZEKUCJI (`najlepsze`) - bid i ask, czyli wprost
    #      odpowiedz na pytanie "po ktorej stronie spreadu stanie zlecenie",
    #   2. ostatnia cena z tej samej ksiegi (`pasmo.odniesienie`) - przyblizenie
    #      o bledzie rzedu spreadu. Zmierzone na zleceniu 54: `lastPrice` lezalo
    #      11 tickow NAD askiem, wiec to przyblizenie zawodzi wlasnie wtedy, gdy
    #      rynek sie rusza. Zostaje jako zapasowe, bo awaria JEDNEGO endpointu
    #      nie ma zatrzymywac handlu.
    #
    # CZEGO TU NIE MA: decyzji, co robic, gdy nie ma ANI JEDNEGO z tych zrodel.
    # Ta funkcja nie wie, czy wolajacy w ogole pytal gieldy (`czy_zamykalna`
    # i `proba_egzekucji` nie pytaja i nie musza) - rozstrzyga to
    # `gielda_exec.zloz_plan`, ktory jako jedyny zna wynik odczytow.
    if najlepsze is not None and najlepsze.kompletne:
        rynkowa = cena_makera(najlepsze.pasywna(kupno=kupno), f.krok_ceny,
                              kupno=kupno)
        zrodlo = "arkusz"
    elif pasmo is not None:
        rynkowa = cena_makera(pasmo.odniesienie, f.krok_ceny, kupno=kupno)
        zrodlo = "cena_odniesienia"
    else:
        rynkowa = None
        zrodlo = "plan"
    # Krok ceny wiekszy od samej ceny to regula bez sensu dla tego instrumentu;
    # cena zero lub ujemna nie jest lepsza od planowej, a o wartosci zlecenia
    # wypowiada sie ponizej minimum wartosci.
    if rynkowa is not None and rynkowa > 0:
        c = rynkowa
    else:
        # Odrzucona cena rynkowa NIE JEST zrodlem wyslanej ceny. Zapisanie tu
        # "arkusz" mowiloby, ze arkusz zadzialal, a wyslalibysmy cene z planu.
        zrodlo = "plan"
    if slad is not None:
        slad["zrodlo_ceny"] = zrodlo
        slad["cena_rynkowa"] = None if rynkowa is None else str(rynkowa)

    if i <= 0:
        return i, c, (f"{rola}: ilość {ilosc} po zaokrągleniu do kroku "
                      f"{f.krok_ilosci} wynosi zero")
    if f.min_ilosc is not None and i < f.min_ilosc:
        return i, c, (f"{rola}: ilość {i} poniżej minimum giełdy {f.min_ilosc}")
    if i * c < f.min_wartosc:
        return i, c, (f"{rola}: wartość {i * c} poniżej minimum {f.min_wartosc}")
    return i, c, None


def czy_zamykalna(ilosc: Decimal, cena: Decimal, f: Filtry) -> bool:
    """Czy pozycję tej wielkości da się w ogóle zamknąć na tej giełdzie.

    NAJGORSZY SKUTEK WYPEŁNIENIA CZĘŚCIOWEGO. Zlecenie na 100 jednostek, które
    weszło w 3, zostawia pozycję poniżej minimum — nie do sprzedania. Nie jest
    to strata, dopóki rynek nie spadnie, i właśnie dlatego łatwo ją przeoczyć:
    nie boli, dopóki nie zaboli.
    """
    # Sprawdzamy sprzedaz, bo na spocie pozycja jest dluga i wyjsciem z niej
    # jest sprzedaz. Gdyby doszly pozycje krotkie, ta funkcja potrzebuje strony.
    _, _, powod = sprawdz_zlecenie(ilosc, cena, f, kupno=False, wyjscie=True)
    return powod is None
