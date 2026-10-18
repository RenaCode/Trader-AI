"""Tory zamkniete wynikiem hipotezy - JEDNO miejsce, ktore o tym wie.

Tor = (silnik, swieca, cennik). Zamkniecie toru to wynik negatywny zapisany
w rejestrze hipotez (renacode-infra: docs/projekty/trader/hipotezy.md), a nie
awaria: kod i dane zostaja, trener tygodniowy liczy dalej (seria H-4, dryf),
a silnik demo moze dalej grac kandydatem - wylacznie jako pomiar egzekucji
(jak E-1: wynik finansowy NIE jest wynikiem). Z tego wynikaja trzy skutki
w kodzie i zaden wiecej:

1. odmowa startu silnika na tym torze z `--mode live` (`loop.py`);
2. odmowa `train.py --zatwierdz-promocje` na tym torze - wersja `active`
   to jedyna, ktora wolno wpuscic na live, a tor zamkniety na live nie idzie;
3. dozor nie wysyla powiadomien, ktore o zamknietym torze sa juz szumem:
   przypomnienia o trwalym dryfie i "zmiana struktury czeka na zatwierdzenie"
   (zatwierdzic sie jej nie da - patrz pkt 2). Werdykt zostaje w logu.

Awaria trenera i zmiana stanu nauki dalej alarmuja: trener chodzi, a jego
padniecie zatrzymuje tez equity i serie H-4.

DLACZEGO NIE BLOKADA NOWYCH WEJSC NA DEMO. H-9 § STOP pozwala demo grac
dalej jako pomiar egzekucji, a dwie otwarte miary stoja na tych wejsciach:
H-10 (wypelnialnosc wejsc demo z >= 100 wejsc po wdrozeniu) i B-0 krok 1
(adapter Krakena wysyla PostOnly "na sygnalach silnika demo"). Wylaczenie
wejsc zamknelo by obie po cichu.

COFNIECIE: usuniecie wpisu z `ZAMKNIETE` (i wiersza decyzji w rejestrze).
Ponowne otwarcie toru H-9 dopuszcza wylacznie po Bramie 0 na innym cenniku
albo innej gieldzie, jako nowa hipoteza od zera - nie przez cofniecie tego
wpisu dla tego samego cennika.
"""
from __future__ import annotations

from typing import NamedTuple


class Zamkniecie(NamedTuple):
    hipoteza: str
    data: str
    powod: str

    def opis(self) -> str:
        return (f"tor zamknięty po {self.hipoteza} ({self.data}): {self.powod}")


# Klucz (engine, bar, kind) - dokladnie te wartosci, ktore dostaja `loop.py`
# i `train.py` z chartu. Perpetuale (`perp`, B-0 / H-12) i equity (`stock`)
# NIE sa tu objete: H-9 rozstrzyga wylacznie cennik `spot_eu`.
ZAMKNIETE: dict[tuple[str, str, str], Zamkniecie] = {
    ("crypto", "4h", "spot_eu"): Zamkniecie(
        hipoteza="H-9", data="2026-10-18",
        powod="ensemble 4h na spot_eu bez przewagi przy prowizji 0,50%/rundę "
              "(−79…−86 pb/rundę po dopłacie na 5/5 przesunięć, DSR 0,0000); "
              "demo wyłącznie jako pomiar egzekucji, bez promocji i bez live"),
}


def zamkniecie(engine: str, bar: str, kind: str | None) -> Zamkniecie | None:
    """Zamkniecie toru albo None. CZYSTA FUNKCJA.

    `kind=None` (wolajacy, ktory cennika nie zna - np. dozor wywolany bez
    `--kind`) NIE trafia w zaden wpis: brak wiedzy o cenniku nie moze
    wyciszac powiadomien toru, ktory byc moze jest otwarty (H-12 na `perp`
    dzieli z tym torem silnik i swiece).
    """
    if kind is None:
        return None
    return ZAMKNIETE.get((engine, bar, kind))
