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

PASMO NIE JEST JEDNAK ODPOWIEDZIĄ NA PYTANIE WYJŚCIA. Wejściu wystarczy, że
zła cena nie pójdzie na giełdę — pozycji wtedy po prostu nie ma. Wyjście musi
się WYPEŁNIĆ, bo inaczej pozycja zostaje otwarta wbrew decyzji, a zlecenia
idą jako `PostOnly`: cena zbyt daleko od rynku leży bezczynnie, a cena
przecinająca spread jest odrzucana. Dlatego wyjście nie dostaje granicy
tolerancji, tylko cenę z księgi docelowej — patrz `cena_makera`.
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
        return None not in (self.krok_ilosci, self.krok_ceny, self.min_wartosc)


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


def sprawdz_zlecenie(ilosc: Decimal, cena: Decimal, f: Filtry, *,
                     kupno: bool, wyjscie: bool,
                     cena_odniesienia: Decimal | None = None,
                     atr_pct: float | None = None,
                     bary: float = BARY_ZYCIA_PLANU) -> tuple[Decimal, Decimal, str | None]:
    """Zwraca (ilosc, cena, powod_odmowy). `powod_odmowy is None` znaczy: wolno.

    `kupno` i `wyjscie` SĄ ROZDZIELONE CELOWO. Kusi, żeby wyprowadzić jedno
    z drugiego — „wyjście to sprzedaż" — ale to prawda tylko dla pozycji
    długiej. Dla krótkiej wyjściem jest kupno, więc sklejenie tych dwóch pojęć
    przyciągałoby cenę limitu w złą stronę: o krok drożej przy każdym zamknięciu
    shorta. Koszt mały, systematyczny i niewidoczny.

    `wyjscie` nie zmienia reguł giełdy — zmienia WAGĘ odmowy, i dlatego wchodzi
    do komunikatu. Odmowa wyjścia zostawia otwartą pozycję, więc musi być
    czytelna jako zdarzenie krytyczne już w treści, a nie dopiero w kodzie,
    który ją odbiera.

    PRZY CENIE ODNIESIENIA `wyjscie` zmienia coś więcej — samą reakcję, i to
    jest jedyne miejsce w tym module, w którym tak jest. Pasmo i odniesienie
    służą tu do dwóch różnych rzeczy:

      * WEJŚCIE poza pasmem → ODMOWA. Kosztuje okazję, czyli nic, czego byśmy
        już nie stracili, gdyby ceny nie było w ogóle. Pasmo jest tu OBRONĄ
        PRZED ABSURDALNĄ CENĄ i niczym więcej.
      * WYJŚCIE → CENA Z KSIĘGI DOCELOWEJ, o jeden krok po naszej stronie
        rynku (`cena_makera`). Odmowa zostawiłaby otwartą pozycję wbrew
        decyzji — dokładnie ten skutek, przed którym ostrzega docstring modułu —
        więc wyjściu nie jest potrzebna granica tolerancji, tylko cena, po
        której da się dziś WYJŚĆ.

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

    DLACZEGO WYJŚCIE DOSTAJE TĘ CENĘ ZAWSZE, A NIE DOPIERO POZA PASMEM. Pasmo
    mierzy, czy cena jest SENSOWNA, a nie czy jest WYKONALNA; wyjście wewnątrz
    pasma potrafi stać osiem procent od rynku i leżeć dokładnie tak samo.
    Cena limitu wyjścia nie niesie zresztą żadnej intencji poza „wyjść":
    `loop.zaplanuj` dostaje dla wejścia i wyjścia tę samą `mark_price` z księgi
    GLOBALNEJ sprzed maks. dwóch świec. Skoro znamy ostatnią cenę z księgi,
    w której zlecenie naprawdę wyląduje, to ona jest lepszą odpowiedzią na
    pytanie „po ile da się teraz wyjść".

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
    if pasmo is not None:
        if not wyjscie:
            if pasmo.poza:
                return i, c, f"{rola}: {pasmo.opis()}"
        else:
            # Podmiana ceny ZANIM policzymy min_wartosc - inaczej sprawdzalibysmy
            # minimum na cenie, ktorej i tak nie wyslemy.
            rynkowa = cena_makera(pasmo.odniesienie, f.krok_ceny, kupno=kupno)
            # Krok ceny wiekszy od samej ceny to regula bez sensu dla tego
            # instrumentu; cena zero lub ujemna nie jest lepsza od planowej,
            # a o wartosci zlecenia wypowiada sie ponizej minimum wartosci.
            if rynkowa > 0:
                c = rynkowa

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
