"""Odcisk uniwersum — tożsamość zbioru, na którym liczy się cechy przekrojowe.

DLACZEGO TO ISTNIEJE

`rel_strength` udaje własność instrumentu, a jest własnością PARY
(instrument, uniwersum). Percentyl BTC wśród dziesięciu par to inna liczba niż
wśród dwudziestu czterech — ta sama nazwa cechy, dwa różne znaczenia.

Dopóki nazwa nie mówi, dla jakiego zbioru wartość policzono, zmiana uniwersum
zostawia w bazie wiersze, które WYGLĄDAJĄ na aktualne. Nie ma awarii, nie ma
pustego pola, jest liczba — tyle że opisująca inny świat. Model kalibruje na niej
wagę, a produkcja stosuje tę wagę do liczby o innym znaczeniu.

Dwie rzeczy z tego wynikły w tym projekcie:

  * migracja 0023 zmieniła uniwersum decyzyjne z par USDT na USDC i wartości
    sprzed niej zostały w bazie obok nowych, nieodróżnialne po nazwie;
  * przy porównywaniu uniwersum kandydującego przeliczenie tej cechy nadpisywało
    wartości produkcyjne — eksperyment nie dawał się przeprowadzić, nie psując
    tego, co handluje.

Odcisk w nazwie rozwiązuje oba naraz. Wartości policzone dla innego zbioru
przestają być widoczne, zamiast po cichu znaczyć co innego, a eksperyment pisze
pod własną nazwą i nie dotyka produkcji.

CZEGO ODCISK CELOWO NIE OBEJMUJE

Tego, które instrumenty były notowane w danej świecy. NEARUSDC wszedł na giełdę
w sierpniu 2024, więc wcześniej ranking szedł po dziewięciu parach zamiast
dziesięciu. To jest zmienność DOSTĘPNOŚCI wewnątrz jednego uniwersum, nie zmiana
uniwersum — obsługuje ją próg `MIN_UNIVERSE` przy liczeniu. Wciągnięcie jej do
odcisku dałoby nową tożsamość cechy przy każdym debiucie i zerwałoby ciągłość
historii bez żadnego zysku.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable

# Osiem znaków heksadecymalnych. Przy kilkudziesięciu wariantach uniwersum
# w całym życiu projektu prawdopodobieństwo kolizji jest pomijalne, a nazwa
# cechy zostaje czytelna w logach i w bazie.
DLUGOSC = 8


def odcisk(symbole: Iterable[str]) -> str:
    """Stabilny odcisk zbioru symboli. Niezależny od kolejności i wielkości liter.

    Niezależność od kolejności jest wymaganiem, nie wygodą: silnik i trener
    czytają uniwersum osobnymi zapytaniami. Gdyby odcisk zależał od kolejności
    wierszy, rozjazd sortowania po stronie bazy zamieniłby cechę w martwą —
    cicho, bo brak wartości wygląda tak samo jak brak sygnału.
    """
    czyste = sorted({s.strip().upper() for s in symbole if s and s.strip()})
    if not czyste:
        raise ValueError("odcisk pustego uniwersum nie ma sensu")
    surowe = ",".join(czyste).encode()
    return hashlib.sha256(surowe).hexdigest()[:DLUGOSC]


def nazwa_przekrojowa(nazwa_bazowa: str, symbole: Iterable[str]) -> str:
    """'rel_strength__4h' + uniwersum -> 'rel_strength__4h#a1b2c3d4'.

    Znak `#` jest wybrany tak, żeby `Horizon.role_of` — który tnie po `@`
    i po `__` — zwracał dalej samą rolę. Sygnały dostają więc `rel_strength`
    niezależnie od uniwersum i nie muszą o nim wiedzieć.
    """
    return f"{nazwa_bazowa}#{odcisk(symbole)}"
