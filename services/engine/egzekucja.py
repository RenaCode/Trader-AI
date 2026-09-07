"""Wypełnienie częściowe: co robi produkcja, gdy weszła połowa zlecenia.

DLACZEGO TO JEST NAJTRUDNIEJSZY PUNKT CAŁEJ ŚCIEŻKI

Symulacja zna dwa stany: zlecenie weszło albo nie weszło. Giełda zna trzeci
i to on jest najczęstszy przy większych zleceniach z limitem. Żadna z dwóch
oczywistych odpowiedzi nie jest dobra:

  * potraktować częściowe jak PEŁNE — pozycja w dzienniku jest większa niż
    faktyczna, więc risk kernel blokuje wejścia, których nic nie blokuje,
    a przy wyjściu sprzedajemy ilość, której nie mamy,
  * potraktować częściowe jak BRAK — trzymamy pozycję, o której system nie wie.
    To jest dokładnie pozycja-widmo, tyle że stworzona przez nas samych.

Zostaje trzecia: PRZYJĄĆ TO, CO WESZŁO, I ANULOWAĆ RESZTĘ. Pozycja jest wtedy
mniejsza od zaplanowanej, ale prawdziwa — a dziennik zgadza się z giełdą.

CZEGO TO NIE ROZWIĄZUJE, I TRZEBA TO POWIEDZIEĆ WPROST

Symulacja nadal wypełnia wszystko w całości. Każde wypełnienie częściowe jest
więc ROZJAZDEM MIĘDZY BACKTESTEM A PRODUKCJĄ — mierzalnym, ale realnym.
Zmyślanie ułamka wypełnienia w symulacji byłoby gorsze: z samych świec OHLC nie
da się go wyprowadzić bez założeń o kolejce w arkuszu, a założenie przebrane za
pomiar jest najgorszym rodzajem liczby. Dlatego ułamek jest ZAPISYWANY przy
każdym zleceniu i ma być zmierzony, zanim ktokolwiek uzna backtest za wiążący
przy tej wielkości pozycji.

PRZYPADEK, KTÓREGO NIE WOLNO PRZEOCZYĆ: wypełnienie częściowe poniżej minimum
giełdy daje pozycję NIE DO ZAMKNIĘCIA. Nie boli, dopóki rynek nie spadnie.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from services.engine.zlecenia import Filtry, czy_zamykalna


@dataclass(frozen=True)
class Rozstrzygniecie:
    """Co zrobić ze zleceniem, którego świeca dobiegła końca."""
    rodzaj: str            # 'pelne' | 'czesciowe' | 'brak' | 'nie_do_zamkniecia'
    ilosc: Decimal         # ile faktycznie przyjmujemy do pozycji
    anuluj_reszte: bool
    ulamek: Decimal        # ile z zaplanowanego weszlo — 0..1
    powod: str


def co_z_wypelnieniem(wypelniono: Decimal, planowano: Decimal, cena: Decimal,
                      f: Filtry) -> Rozstrzygniecie:
    """Decyzja o wypełnieniu. CZYSTA FUNKCJA — bez giełdy i bez bazy.

    Rozdzielenie decyzji od jej wykonania jest tu warunkiem sprawdzalności:
    inaczej test tej logiki wymagałby giełdy, a giełda w teście to już nie jest
    test tej logiki.
    """
    if planowano <= 0:
        raise ValueError("planowana ilosc musi byc dodatnia")
    ulamek = (wypelniono / planowano) if wypelniono > 0 else Decimal(0)

    if wypelniono <= 0:
        return Rozstrzygniecie("brak", Decimal(0), True, Decimal(0),
                               "zlecenie nie weszło — plan wygasa")

    if wypelniono >= planowano:
        return Rozstrzygniecie("pelne", planowano, False, Decimal(1),
                               "zlecenie weszło w całości")

    if not czy_zamykalna(wypelniono, cena, f):
        # NIE ODRZUCAMY TEGO WYPELNIENIA. Ono juz sie stalo - aktywo jest nasze
        # niezaleznie od tego, co zapiszemy. Zapisanie go jako "brak" byloby
        # stworzeniem pozycji-widma wlasna reka.
        return Rozstrzygniecie(
            "nie_do_zamkniecia", wypelniono, True, ulamek,
            f"weszło {wypelniono} z {planowano}, ale tej ilości NIE DA SIĘ "
            f"sprzedać na tej giełdzie — pozycja jest nasza i pozostaje "
            f"nierozliczalna, dopóki nie zostanie dokupiona do minimum")

    return Rozstrzygniecie(
        "czesciowe", wypelniono, True, ulamek,
        f"weszło {wypelniono} z {planowano} ({ulamek:.1%}) — przyjmujemy to, "
        f"co weszło, resztę anulujemy; symulacja wypełniłaby całość, więc "
        f"to jest zmierzony rozjazd backtestu z produkcją")


def rozjazd_z_symulacja(rozstrzygniecia: list[Rozstrzygniecie]) -> dict:
    """Ile backtest zawyża wielkość pozycji względem tego, co naprawdę wchodzi.

    Symulacja przyjmuje ułamek 1.0 dla każdego zlecenia, które dotknęło limitu.
    Średnia z ułamków faktycznych jest więc BEZPOŚREDNIĄ miarą tego zawyżenia —
    i jedyną uczciwą drogą do poprawienia symulacji, bo bierze się z pomiaru,
    a nie z założenia o kolejce w arkuszu.
    """
    weszly = [r for r in rozstrzygniecia if r.rodzaj != "brak"]
    if not weszly:
        return {"n": 0, "sredni_ulamek": None, "czesciowych": 0,
                "nie_do_zamkniecia": 0}
    return {
        "n": len(weszly),
        "sredni_ulamek": sum(r.ulamek for r in weszly) / len(weszly),
        "czesciowych": sum(1 for r in weszly if r.rodzaj == "czesciowe"),
        "nie_do_zamkniecia": sum(1 for r in weszly if r.rodzaj == "nie_do_zamkniecia"),
    }
