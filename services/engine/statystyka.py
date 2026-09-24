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


# Ponizej tego rozrzutu proba nie niesie informacji o zmiennosci i iloraz
# przestaje byc miara czegokolwiek. Nie jest to podloga PODSTAWIANA pod
# mianownik - to prog, ponizej ktorego oddajemy None.
#
# ROZNICA WOBEC `portfolio.MIN_SD` JEST CALA RZECZA. Tam 1e-4 wchodzi do
# mianownika przez `max(pstdev(rets), MIN_SD)`, wiec osiem identycznych zwrotow
# po 1% daje Sharpe 100 - i ta setka jest liczba wpisana, zeby nie bylo
# dzielenia przez zero, a nie przewaga stukrotna. Tutaj taka proba oddaje None.
MIN_ROZRZUT = 1e-12


def sharpe(zwroty: list[float]) -> float | None:
    """Sredni zwrot podzielony przez jego odchylenie. NA TRANSAKCJE, nie rocznie.

    NAZWA JEST MYLACA I TRZEBA TO POWIEDZIEC WPROST. „Sharpe" bez przymiotnika
    znaczy w finansach miare ANUALIZOWANA. Ta nie jest anualizowana i nie da sie
    jej porownac z zadna liczba z literatury: roznica to czynnik rzedu
    pierwiastka z liczby transakcji w roku, czyli kilkanascie razy. Prog
    produkcyjny 0,05 jest dobrany do TEJ miary - patrz komentarz w `train.py`.

    ODCHYLENIE PROBKOWE, NIE POPULACYJNE. `portfolio.metrics` liczy `pstdev`,
    czyli dzieli przez n zamiast przez n-1. Przy osmiu transakcjach zaniza
    mianownik o 6,5%, czyli zawyza Sharpe o tyle samo - w kazdym oknie i zawsze
    w te sama strone. Probka nie jest populacja; okno testowe jest wycinkiem.

    None, a nie liczba, gdy proba jest jednoelementowa albo stala. Jedna
    transakcja nie ma rozrzutu, a proba stala ma rozrzut zero - w obu wypadkach
    iloraz albo nie istnieje, albo powstaje wylacznie z tego, czym zatkano
    mianownik. To ta sama zasada, co w `przedzial_sredniej`: brak pomiaru musi
    wygladac inaczej niz pomiar.
    """
    if len(zwroty) < 2:
        return None
    sd = statistics.stdev(zwroty)
    if sd < MIN_ROZRZUT:
        return None
    return statistics.fmean(zwroty) / sd


def profit_factor(zwroty: list[float]) -> float | None:
    """Suma zyskow podzielona przez sume strat. None, gdy strat NIE BYLO.

    DLACZEGO None, A NIE ZERO. `portfolio.metrics` zwraca w tym miejscu 0.0,
    czyli okno bez ani jednej straty - najlepsze mozliwe - dostaje najgorsza
    mozliwa wartosc. Wchodzi potem do sredniej po oknach i ciagnie ja w dol,
    podczas gdy okno o wartosci 129 ciagnie ja w gore. Zaden z tych dwoch
    wplywow nie jest zamierzony i oba sa niewidoczne w wyniku.

    Zero oddajemy wylacznie wtedy, gdy zyskow nie bylo wcale - to jest
    prawdziwe zero, nie brak wartosci.
    """
    if not zwroty:
        return None
    zyski = sum(r for r in zwroty if r > 0)
    straty = -sum(r for r in zwroty if r < 0)
    if straty <= 0:
        return None
    return zyski / straty


def _kwantyl(posortowane: list[float], p: float) -> float | None:
    """Kwantyl z interpolacja liniowa. None, gdy proba za mala."""
    n = len(posortowane)
    if n < 4:
        return None
    poz = p * (n - 1)
    dol = int(poz)
    gora = min(dol + 1, n - 1)
    return posortowane[dol] + (posortowane[gora] - posortowane[dol]) * (poz - dol)


def rozklad(xs: list[float]) -> dict:
    """Opis rozkladu, a nie jedna liczba. CZYSTA FUNKCJA.

    DLACZEGO TO ISTNIEJE JAKO OSOBNA FUNKCJA

    Zeby nie dalo sie podac sredniej bez mediany i bez odsetka ujemnych.
    Raport walk-forward podawal `profit_factor 1,6419 (0 .. 129,2275)`: srednia
    z wielkosci nieograniczonej z gory, przy ktorej jedno okno na czterdziesci
    decyduje o calej liczbie. Mediana odpowiada na pytanie, ktore naprawde
    zadajemy - jak wyglada TYPOWE okno - a odsetek ujemnych na drugie:
    jak czesto jest zle.

    Kwartyle, a nie sam zakres. Zakres opisuje dwie skrajne obserwacje;
    przy rozkladzie skosnym to akurat te dwie, ktorych nie chcemy uogolniac.
    """
    n = len(xs)
    if n == 0:
        return {"n": 0, "srednia": None, "mediana": None, "q1": None, "q3": None,
                "najmniejszy": None, "najwiekszy": None,
                "ujemnych": 0, "udzial_ujemnych": None}
    s = sorted(xs)
    ujemnych = sum(1 for x in xs if x < 0)
    return {
        "n": n,
        "srednia": statistics.fmean(xs),
        "mediana": statistics.median(s),
        "q1": _kwantyl(s, 0.25),
        "q3": _kwantyl(s, 0.75),
        "najmniejszy": s[0],
        "najwiekszy": s[-1],
        "ujemnych": ujemnych,
        "udzial_ujemnych": ujemnych / n,
    }


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
