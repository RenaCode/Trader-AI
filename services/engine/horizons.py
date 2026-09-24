"""Definicje horyzontow decyzyjnych.

Pierwsza iteracja projektu miala rozmiar swiecy (5 minut) zaszyty w trzech
miejscach naraz: w liczeniu cech, w panelu trenera i w petli decyzyjnej. Zmiana
horyzontu wymagalaby wtedy skopiowania calego potoku, a dwie kopie rozjechalyby
sie przy pierwszej poprawce - i porownanie "5 min vs 1 h" przestaloby byc
porownaniem tej samej strategii na dwoch horyzontach.

Dlatego horyzont jest tu jednym obiektem, a sygnaly czytaja ROLE cech
("zwrot krotki", "zwrot dlugi"), nie ich nazwy. Ta sama funkcja sygnalu dziala
na kazdym horyzoncie; zmienia sie tylko to, ile swiec liczy sie jako "krotko".

NAZEWNICTWO CECH: <rola>__<swieca>, np. `rsi__1h`. Separator jest podwojnym
podkresleniem, bo `@` jest juz zajete przez `feat.as_of()` na numer wersji
(zwraca `nazwa@wersja`) - pojedynczy separator by sie z tym zderzyl.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Dlugosc swiecy w minutach wedlug nazwy timeframe'u w `market.candle`.
#
# JEDNO ZRODLO, bo czytelnicy sa w dwoch modulach: potok cech przelicza nim
# rozgrzewke na liczbe swiec zrodlowych, a dozor - wiek danych na "ile swiec
# zwloki". Druga kopia rozjechalaby sie przy pierwszym nowym timeframie,
# a rozjazd bylby CICHY: obie strony nadal zwracaja liczbe, tylko inna.
MINUT_TIMEFRAME = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}


@dataclass(frozen=True)
class Horizon:
    bar: str                     # '5m' | '1h'
    bar_minutes: int
    source_timeframe: str        # z jakiego timeframe'u w market.candle budujemy swiece
    aggregate: bool              # czy trzeba agregowac (5m z 1m), czy czytamy wprost

    rsi_period: int
    atr_period: int
    ema_fast: int
    ema_slow: int
    ret_short: int               # w swiecach
    ret_mid: int
    ret_long: int
    range_window: int
    volume_lookback: int
    warmup: int                  # ile swiec trzeba, zanim cokolwiek ma sens

    # --- walk-forward ---
    train_days: int
    test_days: int
    step_days: int
    min_trades: int              # laczna liczba transakcji (wszystkie instrumenty)
                                 # ponizej ktorej okno nie niesie informacji
    max_staleness_min: int       # dopuszczalny wiek cechy przy odczycie point-in-time

    # Etykiety okien zwrotow - trafiaja do opisu cechy w UI, zeby dalo sie
    # przeczytac, ze "zwrot krotki" na 1 h znaczy co innego niz na 5 min.
    labels: dict = field(default_factory=dict)

    def name(self, role: str) -> str:
        return f"{role}__{self.bar}"

    @property
    def swiec_zrodlowych_na_swiece(self) -> int:
        """Ile swiec `source_timeframe` sklada sie na JEDNA swiece horyzontu.

        Wyliczane, a nie zapisane osobnym polem, zeby nie dalo sie ustawic
        wartosci niezgodnej z `source_timeframe` i `bar_minutes`.
        """
        zrodlo = MINUT_TIMEFRAME.get(self.source_timeframe)
        if zrodlo is None:
            raise ValueError(
                f"nieznany timeframe zrodlowy: {self.source_timeframe}")
        return max(1, self.bar_minutes // zrodlo)

    @staticmethod
    def role_of(feature_name: str) -> str:
        """'rsi__1h' -> 'rsi'. Odporne na sufiks wersji z feat.as_of()."""
        return feature_name.split("@")[0].split("__")[0]


# 5 minut: horyzont pierwszej iteracji. Zachowany, zeby dalo sie odtworzyc
# wynik, ktory wyszedl ujemny - to jest dowod, a nie smiec do skasowania.
H5M = Horizon(
    bar="5m", bar_minutes=5, source_timeframe="1m", aggregate=True,
    rsi_period=14, atr_period=14, ema_fast=20, ema_slow=50,
    ret_short=12, ret_mid=48, ret_long=288,
    range_window=288, volume_lookback=288, warmup=300,
    train_days=14, test_days=2, step_days=2, min_trades=5, max_staleness_min=90,
    labels={"ret_short": "1 h", "ret_mid": "4 h", "ret_long": "24 h",
            "range": "24 h", "volume": "24 h"},
)

# ---------------------------------------------------------------------------
# DLACZEGO DLUZSZA SWIECA - liczby, nie przeczucie.
#
# Runda kosztuje 0,190 % (prowizja 0,055 % x2 + poslizg 0,04 % x2). Ponizej
# mediana bezwzglednego zwrotu ze swiecy i koszt rundy wyrazony jako procent
# tego ruchu, zmierzone na 4 latach danych Bybita (2022-08 .. 2026-08):
#
#   swieca      BTC          ETH          SOL
#     5 min   0,05 % / 379%  0,07 % / 291%  0,08 % / 236%
#     1 h     0,20 % /  97%  0,26 % /  73%  0,42 % /  45%
#     4 h     0,40 % /  48%  0,53 % /  36%  0,85 % /  22%
#     1 d     1,18 % /  16%  1,58 % /  12%  2,52 % /   8%
#
# Na swiecy 5-minutowej koszt przekraczal caly typowy ruch od trzech do czterech
# razy. Zaden uklad wag tego nie odwroci - i dokladnie to pokazal pierwszy
# walk-forward (przewaga brutto -0,004 % na transakcje, czyli zero).
#
# Swieca dobowa ma najkorzystniejszy stosunek kosztu do ruchu, ale przez 4 lata
# daje tylko 1 460 obserwacji - za malo na uczciwy walk-forward z sensowna
# liczba okien. 4 godziny to punkt, w ktorym koszt schodzi do 22-48 % ruchu,
# a wciaz zostaje 8 760 swiec. Dlatego to on jest horyzontem domyslnym.
#
# Zastrzezenie, ktorego ta tabela NIE zdejmuje: korzystniejszy stosunek kosztu
# do ruchu nie tworzy przewagi. On tylko przestaje ja z gory wykluczac.
# ---------------------------------------------------------------------------

# 1 godzina: horyzont posredni, trzymany do porownania.
H1H = Horizon(
    bar="1h", bar_minutes=60, source_timeframe="1h", aggregate=False,
    rsi_period=14, atr_period=14, ema_fast=24, ema_slow=72,
    ret_short=4, ret_mid=24, ret_long=168,
    range_window=168, volume_lookback=168, warmup=200,
    train_days=90, test_days=14, step_days=14, min_trades=8, max_staleness_min=180,
    labels={"ret_short": "4 h", "ret_mid": "24 h", "ret_long": "7 dni",
            "range": "7 dni", "volume": "7 dni"},
)

# 4 godziny: horyzont domyslny drugiej iteracji.
# Okna sa dlugie celowo - 180 dni treningu to 1 080 swiec, czyli podobna liczba
# obserwacji co 14 dni na swiecach 5-minutowych, przy kosztach nizszych o rzad
# wielkosci wzgledem amplitudy.
H4H = Horizon(
    bar="4h", bar_minutes=240, source_timeframe="1h", aggregate=True,
    rsi_period=14, atr_period=14, ema_fast=20, ema_slow=50,
    ret_short=6, ret_mid=42, ret_long=180,
    range_window=42, volume_lookback=180, warmup=200,
    train_days=180, test_days=30, step_days=30, min_trades=12, max_staleness_min=600,
    labels={"ret_short": "24 h", "ret_mid": "7 dni", "ret_long": "30 dni",
            "range": "7 dni", "volume": "30 dni"},
)

HORIZONS = {h.bar: h for h in (H5M, H1H, H4H)}
DEFAULT_BAR = "4h"


def get(bar: str) -> Horizon:
    if bar not in HORIZONS:
        raise ValueError(f"nieznany horyzont: {bar} (dostepne: {', '.join(HORIZONS)})")
    return HORIZONS[bar]
