"""Tory zamkniete wynikiem hipotezy - JEDNO miejsce, ktore o tym wie.

Tor = (silnik, swieca, cennik). Zamkniecie toru to wynik negatywny zapisany
w rejestrze hipotez (renacode-infra: docs/projekty/trader/hipotezy.md), a nie
awaria: kod i dane zostaja, trener tygodniowy liczy dalej (seria H-4, dryf),
a silnik demo chodzi dalej, ale NIE OTWIERA nowych pozycji. Z tego wynikaja
cztery skutki w kodzie i zaden wiecej:

1. odmowa startu silnika na tym torze z `--mode live` (`loop.py`);
2. odmowa `train.py --zatwierdz-promocje` na tym torze - wersja `active`
   to jedyna, ktora wolno wpuscic na live, a tor zamkniety na live nie idzie;
3. dozor nie wysyla powiadomien, ktore o zamknietym torze sa juz szumem:
   przypomnienia o trwalym dryfie i "zmiana struktury czeka na zatwierdzenie"
   (zatwierdzic sie jej nie da - patrz pkt 2). Werdykt zostaje w logu;
4. BLOKADA NOWYCH WEJSC NA DEMO (od 2026-10-18, B-0 = STOP): `loop.run_once`
   zamienia `enter_long`/`enter_short` na `hold` z powodem `tor_zamkniety`
   (migracja 0099). Wyjscia ida bez zmian, wiec otwarte pozycje domykaja sie
   same na sygnale wyjscia; puls (`risk.engine_heartbeat`) bije dalej, a
   zatrzymane wejscia widac w liczniku `tor_zamkniety` i w linii
   "ZATRZYMANE WEJŚCIA" podsumowania cyklu. Blokada stoi PO wstrzymaniu
   ryzykiem - proba pozarowa kill-switcha (`proba_killswitcha`) rozpoznaje
   ogniwo 3 po `wstrzymanie_ryzykiem` i nie moze go stracic na tym torze.

   Wczesniej demo wchodzilo dalej jako pomiar egzekucji dla H-10 i B-0
   krok 1 (adapter Krakena na sygnalach demo). Decyzja wlasciciela
   2026-10-18 zamknela caly tor crypto - spot i perp - jako wynik negatywny,
   a pomiaru Krakena nie uruchomiono, wiec obu powodow juz nie ma.

Awaria trenera i zmiana stanu nauki dalej alarmuja: trener chodzi, a jego
padniecie zatrzymuje tez equity i serie H-4.

COFNIECIE: usuniecie wpisu z `ZAMKNIETE` (i wiersza decyzji w rejestrze).
Ponowne otwarcie toru dopuszcza wylacznie Brama 0 na innym cenniku albo innej
gieldzie, jako nowa hipoteza od zera - nie przez cofniecie tego wpisu dla tego
samego cennika.
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
# i `train.py` z chartu. Spot (`spot_eu`, H-9) i perpetuale (`perp`, B-0)
# zamkniete; equity (`stock`) i crypto 1h NIE sa tu objete.
ZAMKNIETE: dict[tuple[str, str, str], Zamkniecie] = {
    ("crypto", "4h", "spot_eu"): Zamkniecie(
        hipoteza="H-9", data="2026-10-18",
        powod="ensemble 4h na spot_eu bez przewagi przy prowizji 0,50%/rundę "
              "(−79…−86 pb/rundę po dopłacie na 5/5 przesunięć, DSR 0,0000); "
              "bez promocji, bez live i bez nowych wejść na demo"),
    ("crypto", "4h", "perp"): Zamkniecie(
        hipoteza="B-0", data="2026-10-18",
        powod="Brama 0 = STOP: jedyny zapas kosztowy (perp MiFID Kraken, 6 pb) "
              "nie wystarcza na przewagę, której spot nie pokazał (H-9); koniec "
              "toru crypto jako wynik negatywny, pomiar Krakena nie uruchomiony"),
}


def zamkniecie(engine: str, bar: str, kind: str | None) -> Zamkniecie | None:
    """Zamkniecie toru albo None. CZYSTA FUNKCJA.

    `kind=None` (wolajacy, ktory cennika nie zna - np. dozor wywolany bez
    `--kind`) NIE trafia w zaden wpis: brak wiedzy o cenniku nie moze
    wyciszac powiadomien ani blokowac wejsc toru, ktory byc moze jest otwarty.
    """
    if kind is None:
        return None
    return ZAMKNIETE.get((engine, bar, kind))
