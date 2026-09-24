"""`news_sent` nie ma zapisywać 0.0 tam, gdzie nie ma ŻADNEJ analizy.

CO TO KOSZTOWAŁO

Zapytanie `compute_news` idzie `FROM generate_series(...)` z samymi
`LEFT JOIN`-ami, więc KAŻDA świeca okna dostaje wiersz, a `COALESCE(..., 0)`
zamieniał brak danych w twarde zero. Nie było progu na liczbę analiz ani żadnego
rozróżnienia „brak danych" od „wiadomości wyważone".

Przez 13 dni postoju analizy (09.09–22.09.2026) artykuły były zapisywane, ale
`news.article_analysis` pozostawało puste. `compute_news` wypisało
`news_sent__4h = 0.0` dla każdej świecy i każdego instrumentu. Trener zobaczył
pełny, nie-NULL szereg wyglądający jak „rynek bez newsów" i skalibrował na nim
wagę cechy.

Gdyby wiersza nie było, cecha wypadłaby z decyzji JAWNIE — tak właśnie zachował
się `funding_z` i to BYŁO WIDOCZNE (opisane w `compute_funding_z`). Ta sama wada
została zamknięta po stronie API commitem `01d9bf1` („brak pomiaru przestaje byc
zwracany jako pomiar rowny zeru"); tutaj została otwarta.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.ingest import features


class _Kursor:
    """Tyle bazy, ile dotyka `compute_news` — i ANI KOLUMNY WIĘCEJ.

    Atrapa oddająca kolumnę, której prawdziwy `SELECT` nie pobiera, jest w tym
    projekcie znanym sposobem na zielony test przy złamanym kodzie: zdarzyło się
    to przy `nadrob_zaleglosci` i kosztowało 106 porzuconych artykułów. Dlatego
    ta atrapa SPRAWDZA, że zapytanie naprawdę pobiera `count(an.id)` — bez tego
    `analiz` w wierszach niżej byłoby wartością zmyśloną przez test.
    """

    def __init__(self, swiece: list[tuple[datetime, float, int]]):
        self.swiece = swiece
        self.zapisane: list[tuple] = []
        self.sql = ""

    def execute(self, sql, params=None):
        plaski = " ".join(sql.split())
        if plaski.startswith("SELECT b.t"):
            self.sql = plaski
            assert "AS analiz" in plaski and "count(" in plaski, (
                "zapytanie `compute_news` nie pobiera liczby analiz — kod "
                "czytający `r['analiz']` opierałby się na kolumnie, której "
                "produkcja mu nie daje")
        else:
            # `insert_values` - zapamiętujemy, co naprawdę poszło do bazy.
            self.zapisane.append((plaski, params))

    def fetchall(self):
        return [{"t": t, "score": score, "analiz": analiz}
                for t, score, analiz in self.swiece]

    def fetchone(self):
        return None


@pytest.fixture
def horyzont():
    return features.HORIZONS["4h"]


def _okno(ile: int, minuty: int) -> list[datetime]:
    baza = datetime(2026, 9, 15, tzinfo=timezone.utc)
    return [baza + timedelta(minutes=minuty * i) for i in range(ile)]


def _policz(monkeypatch, swiece, horyzont):
    """Uruchamia `compute_news` i oddaje wiersze, które poszły do `insert_values`."""
    zapisane: list[list] = []

    def _przechwyc(cur, rows):
        zapisane.append(list(rows))
        return len(rows)

    monkeypatch.setattr(features, "insert_values", _przechwyc)
    cur = _Kursor(swiece)
    features.compute_news(cur, instrument_id=1, h=horyzont, feat_id=7,
                          since=swiece[0][0], until=swiece[-1][0] + timedelta(days=1))
    return zapisane[0], cur


def test_SWIECA_BEZ_ANI_JEDNEJ_ANALIZY_nie_dostaje_wiersza(monkeypatch, horyzont):
    """13 dni ciszy zapisanych jako `news_sent__4h = 0.0` na każdej świecy."""
    czasy = _okno(3, horyzont.bar_minutes)
    wiersze, _ = _policz(monkeypatch,
                         [(czasy[0], 0.0, 0), (czasy[1], 0.0, 0), (czasy[2], 0.0, 0)],
                         horyzont)

    assert wiersze == [], (
        "brak danych zapisał się jako pomiar równy zeru — trener zobaczy pełny, "
        "nie-NULL szereg wyglądający jak „rynek bez newsów” i skalibruje na nim wagę")


def test_JEDNA_ANALIZA_NEUTRALNA_to_UCZCIWE_ZERO(monkeypatch, horyzont):
    """Próg jest JEDNA analiza, nie więcej — i to jest decyzja.

    Pytanie brzmi „czy w oknie 24 h było cokolwiek, co ta cecha mierzy", a nie
    „czy próbka jest duża". Zero policzone z jednej analizy o kierunku `neutral`
    JEST pomiarem; zero z zera analiz nim nie jest. Podniesienie progu wyżej
    kasowałoby prawdziwe pomiary z rzadkich okien.
    """
    czasy = _okno(2, horyzont.bar_minutes)
    wiersze, _ = _policz(monkeypatch,
                         [(czasy[0], 0.0, 1), (czasy[1], 0.0, 0)],
                         horyzont)

    assert len(wiersze) == 1
    assert wiersze[0][2] == czasy[0]
    assert wiersze[0][3] == 0.0


def test_SWIECE_Z_DANYMI_zapisuja_sie_bez_zmian(monkeypatch, horyzont):
    """Kanarek na nadgorliwość: poprawka ma usunąć wiersze BEZ pomiaru,
    a nie przerzedzić szereg tam, gdzie pomiar jest."""
    czasy = _okno(3, horyzont.bar_minutes)
    wiersze, _ = _policz(monkeypatch,
                         [(czasy[0], 0.42, 3), (czasy[1], -0.17, 1), (czasy[2], 0.0, 2)],
                         horyzont)

    assert [w[3] for w in wiersze] == [0.42, -0.17, 0.0], (
        "świeca z dwiema analizami, które się znoszą, ma zostać zapisana jako "
        "0.0 — to jest pomiar, a nie jego brak")


def test_ZAPYTANIE_POBIERA_LICZBE_ANALIZ(monkeypatch, horyzont):
    """Sedno, wyciągnięte na wierzch osobnym testem.

    Filtr w Pythonie jest wart tyle, ile kolumna, na której stoi. Gdyby
    `count(an.id)` zniknęło z SQL-a, testy wyżej dalej by przechodziły —
    atrapa podawałaby `analiz` z własnej pamięci, a produkcja dostawałaby
    `KeyError` albo, gorzej, wracała do zapisywania zer.
    """
    czasy = _okno(1, horyzont.bar_minutes)
    _, cur = _policz(monkeypatch, [(czasy[0], 0.0, 1)], horyzont)

    assert "AS analiz" in cur.sql and "count(" in cur.sql
    assert "COALESCE(sum(" in cur.sql, (
        "suma zniknęła z zapytania — cecha przestała być cechą")
