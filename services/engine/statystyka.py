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


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Przedzial ufnosci dla odsetka - metoda Wilsona.

    Nie zwykly przedzial normalny, bo ten przy malych probach i odsetkach
    blisko krancow potrafi wyjsc poza [0,1] i podaje falszywie waskie granice.
    Wilson nie ma tej wady i jest standardem przy n rzedu kilkudziesieciu.

    DLACZEGO TO JEST TU POTRZEBNE

    Przy 40 oknach przedzial ma szerokosc okolo 30 punktow procentowych. Wynik
    52,5% to [37,5%, 67,1%], a 62,5% to [47,0%, 75,8%] - przedzialy niemal sie
    pokrywaja. Bez tej informacji roznica dziesieciu punktow wyglada jak
    poprawa, a jest szumem.

    Cztery kolejne dobowe przebiegi, roznce sie wylacznie przesunieciem okna
    o jeden dzien, daly 52,5%, 57,5%, 60,0% i 62,5%. To jest skala wahania
    samej miary, nie zmiany strategii.
    """
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    srodek = (p + z * z / (2 * n)) / d
    polowa = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, srodek - polowa), min(1.0, srodek + polowa))


# Wartosci krytyczne rozkladu t Studenta, dwustronnie, poziom 95%, wedlug
# stopni swobody. Tablica, a NIE stale 1,96, bo proby, ktore ten projekt liczy,
# bywaja jednocyfrowe: przy trzech obserwacjach przedzial normalny jest ponad
# dwa razy za waski. Za waski przedzial nie jest drobna niescisloscia - mowi
# "zmierzone i rozne od zalozenia" o czyms, czego jeszcze nie widac.
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
       8: 2.306, 9: 2.262, 10: 2.228, 12: 2.179, 15: 2.131, 20: 2.086,
       25: 2.060, 30: 2.042, 40: 2.021, 60: 2.000, 120: 1.980}


def t95(df: int) -> float:
    """Wartosc krytyczna t dla podanej liczby stopni swobody.

    Miedzy wpisami tablicy bierzemy wartosc dla NAJBLIZSZEGO MNIEJSZEGO df,
    czyli zawsze ostrozniejsza (szersza) z dwoch. Interpolacja bylaby
    dokladniejsza i bez znaczenia: roznica miedzy 2,086 a 2,060 jest o rzad
    wielkosci mniejsza niz rozrzut, ktory tym mierzymy.

    MNIEJSZEGO, A NIE WIEKSZEGO - to nie jest ta sama strona. Wartosc krytyczna
    t MALEJE wraz z df, wiec siegniecie po WIEKSZY df daje wartosc MNIEJSZA,
    czyli przedzial WEZSZY: dokladnie odwrotnie niz mowilo uzasadnienie i
    dokladnie w strone, przed ktora ostrzega komentarz przy tablicy. Do
    2026-09-23 petla szla `for k in sorted(T95): if df <= k`, przez co df=11
    dostawalo 2,179 zamiast 2,201 (za wasko o 1,0%), df=13 -> 2,131 zamiast
    2,160, df=16 -> 2,086 zamiast 2,120 (najwiekszy blad, 1,6%).

    Skala bledu byla mala, kierunek nie: `poslizg.py` liczy przez
    `przedzial_sredniej` przedzial zmierzonego poslizgu i ALARMUJE, gdy
    zalozenie POSLIZG_TAKER wypada z przedzialu. Przy 17 wypelnieniach (df=16)
    przedzial byl 1,6% za waski, wiec zalozenie lezace tuz wewnatrz
    prawdziwego przedzialu wypadalo z policzonego - falszywy alarm "poslizg
    rozjechal sie z zalozeniem" i pokusa, zeby zmienic stala kosztu na
    podstawie szumu.

    Powyzej najwiekszego wpisu (df > 120) zostaje 1,980, a nie asymptotyczne
    1,96, z tego samego powodu: szersza z dwoch.
    """
    for k in sorted(T95, reverse=True):
        if df >= k:
            return T95[k]
    return T95[1]


def przedzial_sredniej(xs: list[float]) -> tuple[float, float] | None:
    """Przedzial ufnosci 95% dla SREDNIEJ z proby. None, gdy nie ma czego liczyc.

    None, a nie (x, x), przy jednej obserwacji. Przedzial o zerowej szerokosci
    wygladalby jak pomiar idealnie precyzyjny, a jedna obserwacja nie mowi
    o rozrzucie NIC. To ta sama zasada, przez ktora rozjazd_z_symulacja zwraca
    None zamiast zera: brak pomiaru musi wygladac inaczej niz pomiar.
    """
    n = len(xs)
    if n < 2:
        return None
    m = statistics.fmean(xs)
    polowa = t95(n - 1) * statistics.stdev(xs) / math.sqrt(n)
    return (m - polowa, m + polowa)
