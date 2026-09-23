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


# UNIWERSUM DECYZYJNE — JEDEN WARUNEK DLA WSZYSTKICH, KTÓRZY JE CZYTAJĄ.
#
# Pętla decyzyjna (loop.py), trener (train.py) i potok cech (features.py) pytały
# o ten zbiór trzema osobnymi, przepisanymi zapytaniami. Czwarte — w
# `diagnose_signals.py` — zgubiło `tradeable`, i to nie objawiło się awarią:
# odcisk policzony po „wszystkich aktywnych" nie pasował do żadnej definicji
# w `feat.feature_def`, więc `load_panel` nie zwracał ANI JEDNEGO wiersza
# `rel_strength`, `cross_section()` zwracał None, a tabela IC drukowała się bez
# tego jednego wiersza. Brak wiersza wygląda dokładnie tak samo jak sygnał,
# którego nikt nie policzył.
#
# `tradeable`, nie `active`: historię zbieramy szerzej, niż handlujemy (0020).
# Od migracji 0023 ten sam aktyw jest w bazie DWA RAZY — jako *USDT i jako
# *USDC — więc „wszystkie aktywne" to zbiór, w którym BTC liczy się podwójnie.
#
# WSPÓLNY JEST WARUNEK, A NIE CAŁE ZAPYTANIE — i to nie jest ustępstwo, tylko
# odpowiedź na to, co naprawdę grozi. Czytelnicy potrzebują RÓŻNYCH kolumn
# (pętla `kind` i `quote_ccy`, trener `quote_ccy`, potok cech samego `symbol`),
# więc jedna sztywna lista kolumn nie dałaby się użyć i każdy przepisałby
# zapytanie od nowa — dokładnie tak, jak było do 2026-09-23, gdy `uniwersum.py`
# obiecywało „jedno zapytanie dla wszystkich", a jego jedynymi importerami byli
# `diagnose_signals.py` i `diagnose_timing.py`. Rozjechać się może wyłącznie
# WARUNEK, bo to on wyznacza zbiór, z którego liczy się `odcisk()`.
#
# Czym to grozi: `loop.horizon_feature_ids` filtruje po odcisku ŚCIŚLE.
# Wystarczy, żeby kolejna zmiana definicji uniwersum trafiła do `loop.py`,
# a nie do `features.py`, żeby produkcja szukała `rel_strength#<nowy>` pod
# nazwą, której nikt nie zapisał. `signals.cross_section` zwraca wtedy None
# i sygnał znika BEZ ŚLADU. Że zmiany uniwersum w tym projekcie zachodzą,
# widać po dwóch osieroconych odciskach w `feat.feature_def` (`77c96da1`,
# `d446b80c`, oba z wartościami do 2026-09-07).
WARUNEK_UNIWERSUM_DECYZYJNEGO = (
    " WHERE engine = %s AND active AND tradeable AND kind <> 'perp'"
    " ORDER BY symbol")


def sql_uniwersum_decyzyjnego(kolumny: str = "id, symbol") -> str:
    """Zapytanie o uniwersum decyzyjne z WŁASNĄ listą kolumn.

    Warunek jest wspólny i niezmienny; wybór kolumn należy do wołającego, bo
    każdy czytelnik potrzebuje innych. `%s` w warunku oczekuje jednego
    parametru: `engine`.
    """
    return f"SELECT {kolumny} FROM ref.instrument{WARUNEK_UNIWERSUM_DECYZYJNEGO}"


SQL_UNIWERSUM_DECYZYJNE = sql_uniwersum_decyzyjnego()


def uniwersum_decyzyjne(cur, engine: str) -> list[dict]:
    """Instrumenty, którymi silnik faktycznie obstawia. Wiersze z `ref.instrument`."""
    cur.execute(SQL_UNIWERSUM_DECYZYJNE, (engine,))
    return cur.fetchall()
