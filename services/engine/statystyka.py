"""Statystyka wspolna dla trenera i diagnostyki.

Wydzielona, bo obie strony jej potrzebuja, a wzajemny import trenera
i diagnostyki tworzyl cykl.
"""
from __future__ import annotations

import math
import statistics


def rangi(xs: list[float]) -> list[float]:
    """Rangi z usrednianiem remisow."""
    posortowane = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(posortowane):
        j = i
        while j + 1 < len(posortowane) and xs[posortowane[j + 1]] == xs[posortowane[i]]:
            j += 1
        srednia = (i + j) / 2 + 1
        for k in range(i, j + 1):
            out[posortowane[k]] = srednia
        i = j + 1
    return out


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Korelacja rangowa. None, gdy proba za mala albo ktoras kolumna stala.

    Rangowa, a nie liniowa, bo interesuje nas UPORZADKOWANIE sytuacji od
    najlepszej do najgorszej, a nie skala - wyniki sygnalow sa i tak przycinane
    do zakresu -1..1, wiec ich skala jest umowna.

    Zwraca None zamiast liczby, gdy nie ma czego liczyc. Liczba bez znaczenia
    jest gorsza niz jej brak, bo wyglada jak wynik.
    """
    if len(xs) < 30:
        return None
    rx, ry = rangi(xs), rangi(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    licz = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return licz / (sx * sy) if sx > 0 and sy > 0 else None
