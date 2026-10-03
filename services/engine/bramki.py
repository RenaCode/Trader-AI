"""Progi wejscia i wyjscia: JEDEN ZAMIAR, TYLE POPULACJI, ILE SKAL WYNIKU.

`ENTER_PCTL = 0.88` znaczy "handluj w 12% najsilniejszych sytuacji". Do
2026-09-27 ten percentyl liczyl sie na JEDNEJ liscie `abs(score)`, do ktorej
wpadaly wszystkie rezimy razem, i zwracal JEDNA liczbe. Wagi sa jednak brane
per rezim (`weights.get(regime)`), wiec skala wyniku zalezy od rezimu - a jedna
liczba nie moze wyrazac jednego percentyla na kilku skalach.

ZMIERZONE NA PRODUKCJI (crypto/demo, 2026-09-08..2026-09-27, czyli od wdrozenia
renormalizacji pokrycia wag; wynik odtworzony jako
`sum(contribution) / sum(abs(weight))` po `journal.decision_contribution`,
zgodnie z `signals.combine`):

    rezim       decyzji  sredni |score|  p88 rezimu  % ponad progiem 0,2798
    range           646         0,0710       0,1573                     7,0
    high_vol        306         0,2118       0,4304                    34,0
    trend_up        175         0,1503       0,3187                    18,9
    news_shock        3         0,0064       0,0095                     0,0

Zamiar 12% nie byl dotrzymany w ZADNYM rezimie: 7,0% w konsolidacji przy 34,0%
w wysokiej zmiennosci. To jest wada kalibracji sama w sobie i to ona jest tu
naprawiana. NIE jest to natomiast lekarstwo na zwiazek czestotliwosci handlu
z Sharpe'em w `range`: pomiar z 2026-09-27 pokazal, ze w oknach `range`
o 80 transakcjach skala wyniku jest identyczna jak w oknach o 27 (iloraz
zmiennosci 0,8266 wobec 0,8278), a trafnosc sie nie pogarsza - czyli prog
wejscia nie jest tam mechanizmem i obiecywac tego nie wolno.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from services.engine.signals import REGIMES

# MALA PROBA: WEJSCIA ODPADAJA, WYJSCIA NIGDY.
#
# Percentyl policzony na garstce obserwacji nie jest percentylem - przy 50
# probkach ponad progiem p88 stoi szesc z nich. Dawny bezpiecznik oddawal w tym
# miejscu stala 0,25, czyli liczbe dobrana pod rozklad wynikow ze swiecy
# 5-minutowej, sprzed zmiany normalizacji. Rozstrzygamy inaczej i ASYMETRYCZNIE:
#
#   * WEJSCIE w rezimie bez kalibracji NIE PRZYSLUGUJE (`wejscie()` -> None).
#     Nie ma tu "bezpiecznej" liczby zastepczej: prog wspolny jest wlasnie ta
#     liczba, o ktorej zmierzylismy, ze w `high_vol` przepuszcza 34% zamiast
#     12%, a im rzadszy rezim, tym bardziej jego skala odstaje od wspolnej -
#     zastepnik bylby wiec najluzniejszy dokladnie tam, gdzie dowodow jest
#     najmniej. Kierunek bledu wybieramy swiadomie: brak wejsc to koszt
#     utraconej okazji, widoczny w liczniku i odwracalny; wejscie po progu
#     o nieznanej czestotliwosci to pozycja, prowizja, poslizg i obsuniecie,
#     ktorych odwrocic nie da sie. W fazie demonstracyjnej, ktorej celem jest
#     POMIAR, pierwsze jest tansze.
#   * WYJSCIE przysluguje zawsze (`wyjscie()` oddaje prog zapasowy). Pozycja
#     otwarta w rezimie skalibrowanym moze dozyc rezimu nieskalibrowanego
#     i musi dac sie zamknac. Wyjscie i tak zachodzi takze na zmianie znaku
#     wyniku, wiec pozycji nie da sie tu uwiezic.
#
# Rezim bez kalibracji nie moze przy tym zniknac po cichu: `prob` trzyma liczby
# probek, `powod` nazywa, skad prog pochodzi, a petla decyzyjna zapisuje
# `powod_holdu = 'rezim_bez_progu'` (migracja 0067).
MIN_PROB_PROGU = 50

# Zalozenia poczatkowe: obowiazuja, dopoki nie ma wytrenowanego modelu.
# Trzymane TUTAJ, a nie osobno w `loop.py` i `train.py`, bo dwie kopie tej samej
# liczby to dwie liczby, ktore moga sie rozjechac.
PROG_WEJSCIA_POCZATKOWY = 0.25
PROG_WYJSCIA_POCZATKOWY = 0.12
# Wyjscie przy spadku sily sygnalu do tej czesci progu wejscia. LICZONE PER
# REZIM, z progu wejscia TEGO rezimu - inaczej wejscie i wyjscie mierzylyby sie
# na dwoch skalach.
EXIT_RATIO = 0.45


@dataclass(frozen=True)
class Bramki:
    """Progi dla jednego okna albo jednej wersji modelu, per rezim.

    Klasa, nie para liczb, bo odpowiedz "w tym rezimie nie ma progu wejscia"
    musi byc wyrazalna. Krotka `(enter, exit)` wymuszala podstawienie jakiejs
    liczby i przez to kazdy brak kalibracji wygladal jak kalibracja.
    """

    per_rezim: dict[str, tuple[float, float]]
    wyjscie_zapasowe: float = PROG_WYJSCIA_POCZATKOWY
    # ILE OBSERWACJI STOI ZA PROGIEM. Prog bez tej liczby jest nieczytelny:
    # 0,43 z 306 probek i 0,43 z 9 probek to dwie rozne rzeczy, a wygladaja
    # identycznie. Na oknie treningowym to liczba PROBEK rezimu, w wersji
    # modelu - liczba OKIEN, ktore daly prog dla tego rezimu (`enter_threshold_windows`).
    prob: dict[str, int] = field(default_factory=dict)
    powod: str = "per_rezim"

    def wejscie(self, rezim: str | None) -> float | None:
        """Prog wejscia albo None, czyli "w tym rezimie nie wchodzimy"."""
        para = self.per_rezim.get(rezim or "")
        return para[0] if para else None

    def wyjscie(self, rezim: str | None) -> float:
        """Prog wyjscia. ZAWSZE liczba: wyjsc musi byc wolno."""
        para = self.per_rezim.get(rezim or "")
        return para[1] if para else self.wyjscie_zapasowe

    def opis(self) -> str:
        """Jedna linia do logu - Z LICZBA PROBEK, bo bez niej prog jest goly."""
        if not self.per_rezim:
            return (f"BRAK progów wejścia ({self.powod}); "
                    f"wyjście {self.wyjscie_zapasowe:.4f}")
        czesci = [f"{r}={p[0]:.4f}" + (f" (n={self.prob[r]})" if r in self.prob else "")
                  for r, p in sorted(self.per_rezim.items())]
        brak = [r for r in REGIMES if r not in self.per_rezim]
        return ", ".join(czesci) + (
            f"; BEZ PROGU WEJŚCIA: {', '.join(brak)}" if brak else "")


def percentyl(proby: list[float], pctl: float) -> float:
    """Ten sam estymator, ktory stal w `score_threshold` przed zmiana.

    Indeksowanie zostaje bit w bit (`int(len * pctl)` z obcieciem do ostatniego
    elementu), zeby jedyna roznica miedzy "przed" i "po" byla POPULACJA, a nie
    sposob liczenia percentyla.
    """
    posortowane = sorted(proby)
    return posortowane[min(len(posortowane) - 1, int(len(posortowane) * pctl))]


def bramki_z_prob(proby: dict[str, list[float]], pctl: float,
                  per_rezim: bool = True) -> Bramki:
    """Progi z rozkladow `abs(score)` zebranych OSOBNO dla kazdego rezimu.

    `per_rezim=False` odtwarza arytmetyke sprzed 2026-09-27: jeden percentyl ze
    zlaczonej proby, ten sam dla wszystkich rezimow. Wariant istnieje po to,
    zeby dalo sie zmierzyc ROZNICE na tych samych oknach - a nie po to, zeby
    bylo do czego wrocic po cichu; `train.py` wymaga dla niego `--sucho`.
    """
    wszystkie = [v for lista in proby.values() for v in lista]
    # Prog zapasowy WYJSCIA liczy sie ze zlaczonej proby - jest tym, czym byl
    # dotad prog wspolny. Dotyczy wylacznie rezimow bez wlasnej kalibracji
    # i wylacznie wyjsc.
    if len(wszystkie) >= MIN_PROB_PROGU:
        zapas = max(percentyl(wszystkie, pctl), 1e-4) * EXIT_RATIO
    else:
        zapas = PROG_WYJSCIA_POCZATKOWY
    liczby = {r: len(proby.get(r) or ()) for r in REGIMES}

    if not per_rezim:
        if len(wszystkie) < MIN_PROB_PROGU:
            return Bramki({}, zapas, liczby,
                          powod="wspolny_prog_proba_ponizej_minimum")
        wejscie = max(percentyl(wszystkie, pctl), 1e-4)
        return Bramki({r: (wejscie, wejscie * EXIT_RATIO) for r in REGIMES}, zapas,
                      liczby, powod="wspolny_prog_wszystkie_rezimy")

    per: dict[str, tuple[float, float]] = {}
    for rezim in REGIMES:
        lista = proby.get(rezim) or []
        if len(lista) < MIN_PROB_PROGU:
            continue
        wejscie = max(percentyl(lista, pctl), 1e-4)
        per[rezim] = (wejscie, wejscie * EXIT_RATIO)
    return Bramki(per, zapas, liczby)


def bramki_z_modelu(params: dict | None) -> Bramki:
    """Progi z `learn.model_version.params` - JEDEN czytnik dla petli i symulacji.

    WERSJA MODELU SPRZED ZMIANY NIE MA PROGOW PER REZIM. Traktujemy to inaczej
    niz "rezim bez probek": to nie brak kalibracji, tylko starszy format zapisu,
    a odmowa wejsc we WSZYSTKICH rezimach zatrzymalaby handel w calosci do
    najblizszego udanego treningu - po cichu, bo silnik bez wejsc wyglada tak
    samo jak silnik na spokojnym rynku. Taka wersja dostaje wiec dawne
    zachowanie (jeden prog dla wszystkich rezimow) i `powod`, ktory kaze to
    powiedziec w logu.
    """
    p = params or {}
    per_rezim = p.get("enter_threshold_per_regime") or {}
    wspolny = float(p.get("enter_threshold", PROG_WEJSCIA_POCZATKOWY))
    zapas = float(p.get("exit_threshold", round(wspolny * EXIT_RATIO, 6)))
    zapisane_proby = p.get("enter_threshold_windows") or {}
    prob = {r: int(n) for r, n in zapisane_proby.items()}
    if not per_rezim:
        powod = ("zalozenia_poczatkowe" if "enter_threshold" not in p
                 else "wersja_modelu_bez_progow_per_rezim")
        wszedzie = {r: (wspolny, zapas) for r in REGIMES}
        return Bramki(wszedzie, zapas, prob, powod=powod)
    # Prog wyjscia liczymy z progu wejscia TEGO rezimu, a nie czytamy osobno:
    # dwa zapisane pola moglyby sie rozjechac, a `EXIT_RATIO` jest zalozeniem
    # projektu, nie wynikiem treningu.
    per = {r: (float(prog), round(float(prog) * EXIT_RATIO, 6))
           for r, prog in per_rezim.items()}
    return Bramki(per, zapas, prob)


def bramki_z_pary(wejscie: float, wyjscie: float) -> Bramki:
    """Dwie liczby podane z zewnatrz jako progi dla WSZYSTKICH rezimow.

    Dla wolaczy, ktorzy sami wybieraja progi (pomiar wariantu, test jednostkowy
    liczacy koszt albo limity). Nazwa mowi wprost, ze rezimu tu nie ma - bo
    `dict[str, tuple]` tego nie mowil i wlasnie dlatego prog wspolny przetrwal
    w tym kodzie tak dlugo.
    """
    return Bramki({r: (wejscie, wyjscie) for r in REGIMES}, wyjscie,
                  powod="para_progow_od_wolajacego")
