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
"""
from __future__ import annotations

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


def do_kroku_w_dol(wartosc: Decimal, krok: Decimal) -> Decimal:
    """Zaokrąglenie W DÓŁ do wielokrotności kroku.

    Kierunek nie jest kwestią gustu. W górę dałoby pozycję większą, niż
    zatwierdził risk kernel — a przekroczenie limitu przez zaokrąglenie jest
    najcichszym możliwym sposobem jego złamania.
    """
    if krok <= 0:
        raise ValueError("krok musi byc dodatni")
    return (wartosc / krok).to_integral_value(rounding=ROUND_DOWN) * krok


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
    return w_dol + krok


def sprawdz_zlecenie(ilosc: Decimal, cena: Decimal, f: Filtry, *,
                     kupno: bool, wyjscie: bool) -> tuple[Decimal, Decimal, str | None]:
    """Zwraca (ilosc, cena, powod_odmowy). `powod_odmowy is None` znaczy: wolno.

    `kupno` i `wyjscie` SĄ ROZDZIELONE CELOWO. Kusi, żeby wyprowadzić jedno
    z drugiego — „wyjście to sprzedaż" — ale to prawda tylko dla pozycji
    długiej. Dla krótkiej wyjściem jest kupno, więc sklejenie tych dwóch pojęć
    przyciągałoby cenę limitu w złą stronę: o krok drożej przy każdym zamknięciu
    shorta. Koszt mały, systematyczny i niewidoczny.

    `wyjscie` nie zmienia reguł — zmienia WAGĘ odmowy, i dlatego wchodzi
    do komunikatu. Odmowa wyjścia zostawia otwartą pozycję, więc musi być
    czytelna jako zdarzenie krytyczne już w treści, a nie dopiero w kodzie,
    który ją odbiera.
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
