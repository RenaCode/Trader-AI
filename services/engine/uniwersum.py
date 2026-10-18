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


# WYKLUCZENIE ZE WZGLEDU NA SPREAD (audyt Fable F-1.5, 2026-10-03).
#
# Wlasna ksiega, 7 dni do 03.10: NEARUSDC srednio 21,2 pb, DOGE 7,6, reszta
# <= 6 pb. NEAR dal 4 rundy gieldowe i -218 USD, czyli 59% straty gieldowej -
# spread placony na rundzie byl wiekszy niz cala zmierzona przewaga strategii.
#
# CO TO WYKLUCZA, A CZEGO NIE. Wykluczony instrument nie dostaje NOWEGO wejscia
# (petla: hold z powodem `szeroki_spread`). Wyjscie idzie dalej - blokada
# wyjscia trzymalaby ryzyko, ktorego ta regula chce sie pozbyc. Warunek
# uniwersum decyzyjnego wyzej (`WARUNEK_UNIWERSUM_DECYZYJNEGO`) ZOSTAJE BEZ
# ZMIAN: z niego liczy sie `odcisk()`, a dynamiczna zmiana zbioru zmienilaby
# nazwe cechy `rel_strength` - sygnal zniknalby bez sladu do czasu backfillu.
#
# SREDNIA, NIE MEDIANA - z tego samego powodu co w `spread.py`: koszt placi
# kazda transakcja, wiec liczy sie wartosc oczekiwana.
PROG_SPREADU_PB = 15.0
OKNO_SPREADU_DNI = 7
# Ponizej tylu swiec z arkuszem srednia mowi o chwili pomiaru, nie o rynku -
# a brak pomiaru to nie powod do wykluczenia. 12 swiec 4h to dwie doby.
MIN_SWIEC_SPREADU = 12

_SQL_SREDNIE_SPREADY = """
SELECT fv.instrument_id, avg(fv.value) AS srednia, count(*) AS n
  FROM feat.feature_value fv
  JOIN feat.feature_def fd ON fd.id = fv.feature_def_id
 WHERE fd.active
   AND fd.params->>'role' = 'book_spread_bps'
   AND fd.params->>'bar'  = %(bar)s
   AND fv.instrument_id = ANY(%(ids)s)
   AND fv.event_time >= %(od)s AND fv.event_time < %(do)s
   AND fv.ingested_at <= %(do)s
   AND fv.value IS NOT NULL
 GROUP BY fv.instrument_id
"""


def srednie_spready(cur, instrument_ids: list[int], bar: str,
                    teraz) -> dict[int, tuple[float, int]]:
    """Sredni `book_spread_bps` z ostatnich OKNO_SPREADU_DNI dni: id -> (pb, swiec)."""
    from datetime import timedelta

    if not instrument_ids:
        return {}
    cur.execute(_SQL_SREDNIE_SPREADY, {
        "bar": bar, "ids": list(instrument_ids),
        "od": teraz - timedelta(days=OKNO_SPREADU_DNI), "do": teraz})
    wynik = {}
    for w in cur.fetchall():
        w = w if isinstance(w, dict) else dict(zip(("instrument_id", "srednia", "n"), w))
        wynik[w["instrument_id"]] = (float(w["srednia"]), int(w["n"]))
    return wynik


def za_szeroki_spread(srednie: dict[int, tuple[float, int]],
                      prog: float = PROG_SPREADU_PB,
                      min_swiec: int = MIN_SWIEC_SPREADU) -> dict[int, float]:
    """Instrumenty do wykluczenia z wejsc: id -> sredni spread. CZYSTA FUNKCJA."""
    return {i: sr for i, (sr, n) in srednie.items() if n >= min_swiec and sr > prog}


def opis_wykluczen(wykluczone: dict[int, float],
                   symbole: dict[int, str]) -> list[str]:
    """Linie logu: ktore instrumenty i z jakim spreadem nie dostaja wejsc."""
    return [f"WYKLUCZONY Z WEJŚĆ: {symbole.get(i, i)} — średni spread "
            f"{sr:.2f} pb z {OKNO_SPREADU_DNI} dni > {PROG_SPREADU_PB:.0f} pb "
            f"(wyjścia bez zmian)" for i, sr in sorted(wykluczone.items(),
                                                     key=lambda t: -t[1])]


# BRAMKA SPOJNOSCI PRZED WYKLUCZENIEM (2026-10-18).
#
# `za_szeroki_spread` brala surowa srednia `book_spread_bps`, a dla silnika
# equity ta cecha pochodzi z feedu IEX (`/v2/stocks/quotes/latest`, jedna
# gielda z kilkunastu). `spread.zmierz_spread` od 2026-09-27 odrzuca taki pomiar
# bramka spojnosci (kwotowanie porownywalne z calym zakresem high-low swiecy
# nie jest cena, po ktorej ktos handluje) - trener go wiec NIE uzywal, a petla
# wykluczala nim 18 z 20 spolek (LLY 278 pb, MSFT 35 pb). Ta sama liczba byla
# jednoczesnie "niezmierzona" w karcie kosztu i "za duza" w petli.
#
# Teraz o tym, czy pomiar w ogole jest pomiarem, rozstrzyga TA SAMA bramka co
# w karcie kosztu, na tych samych 7 dniach. Wykluczenia wylacza WYLACZNIE jawne
# odrzucenie spojnoscia (`spojnosc > PROG_SPOJNOSCI`) - to jest twierdzenie
# "ten arkusz nie jest cena, po ktorej ktos handluje". Kazdy inny `bps is None`
# z `zmierz_spread` (mniej niz 3 instrumenty z 30 swiecami, za male pokrycie
# swiec) to "za malo danych na agregat uniwersum", a nie dowod falszu arkusza:
# dla crypto 4h wystarczy dziura ~2 dob w `book_snapshot` (29 swiec), a NEAR
# (18-21 pb) wracalby wtedy do wejsc. W tych przypadkach i przy pomiarze
# przyjetym regula zostaje DOKLADNIE ta sama co wczesniej (`za_szeroki_spread`
# na `srednie_spready`, prog swiec 12).
# `per_id` z pomiaru nie wystarcza: liczy tylko instrumenty z co najmniej
# `spread.MIN_SWIEC_NA_INSTRUMENT` (30) swiecami, wiec swiezy listing z 12-29
# swiecami po 21 pb przestalby byc wykluczany.
#
# Silnik i trener wolaja te funkcje, nie kazdy wlasna kopie - rozjazd
# "trener odrzuca, petla wyklucza" byl dokladnie ta wada.
def wykluczenia_spreadowe(cur, engine: str, instrument_ids: list[int], h,
                          teraz) -> tuple[dict[int, float], str | None]:
    """(wykluczone id -> sredni spread, powod braku wykluczen albo None).

    Powod jest napisem tylko wtedy, gdy pomiar odrzucono i wykluczen NIE MA
    z tego powodu - wolajacy ma go wypisac (raz, nie co cykl).
    """
    from datetime import timedelta

    from services.engine.spread import PROG_SPOJNOSCI, zmierz_spread

    if not instrument_ids:
        return {}, None
    pomiar = zmierz_spread(cur, engine, h.bar, h.source_timeframe,
                           h.bar_minutes, list(instrument_ids),
                           teraz - timedelta(days=OKNO_SPREADU_DNI), teraz)
    if pomiar.spojnosc is not None and pomiar.spojnosc > PROG_SPOJNOSCI:
        return {}, (f"wykluczenia spreadem WYŁĄCZONE — pomiar z "
                    f"{OKNO_SPREADU_DNI} dni odrzucony: {pomiar.powod}")
    return za_szeroki_spread(srednie_spready(cur, instrument_ids, h.bar, teraz)), None
