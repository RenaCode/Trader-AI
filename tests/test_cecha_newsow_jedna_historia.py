"""Jedna historia wnosi do `news_sent` JEDEN wkład, nie tyle, ile razy ją pobrano.

CO SIĘ DZIAŁO

Kanały typu „live updates" to relacja na żywo: ten sam URL wraca przy każdym
pobraniu z dopisaną treścią. `content_hash` słusznie widzi za każdym razem co
innego (0 duplikatów po haszu), więc w `news.article` powstaje osobny wiersz —
a wszystkie dziedziczą `event_time` PIERWSZEJ publikacji, bo tyle podaje RSS.

Zmierzone na produkcji 2026-09-24: 112 nadmiarowych wierszy na 2210 artykułów,
77 powtórzonych URL-i, wszystkie z jednego kanału (CoinDesk). Rekordzista —
relacja z 2026-09-15 06:37:24 — ma DZIEWIĘĆ wierszy pobranych od 06:39 do 18:49
tego samego dnia, każdy z innym tytułem i tym samym `event_time`.

`compute_news` sumowało po wierszach, więc sentyment jednej historii wchodził do
każdej świecy 24-godzinnego okna dziewięć razy. Przy 27 artykułach uznanych
w całej bazie za istotne taka dziewiątka to znaczna część próbki: cecha mierzyła
nie „co się dzieje na rynku", tylko „jak często redakcja dopisuje akapit".

DLACZEGO NIE KASUJEMY MIGAWEK

Relacja o 18:49 mówiła co innego niż o 06:39 — to nie są duplikaty do
wyrzucenia, tylko kolejne stany tej samej historii. Zostają w bazie, a wybór
„która obowiązuje w chwili T" zapada przy liczeniu cechy: najświeższa ZNANA
wtedy, czyli `DISTINCT ON (a.url) ... ORDER BY a.url, an.ingested_at DESC`,
nadal pod filtrem `an.ingested_at <= b.t`.

POMIAR NA PRAWDZIWEJ BAZIE (Postgres 17, odtworzony kształt produkcyjny:
dziewięć migawek jednej relacji + jedna niezależna historia, świeca po
wszystkich migawkach):

    stare zapytanie (sumuje migawki)   score = 10.00   analiz = 10
    nowe  zapytanie (DISTINCT ON url)  score =  2.00   analiz =  2

CZYM TEN PLIK JEST, A CZYM NIE

To STRAŻNIK KONSTRUKCJI, nie wykonanie SQL-a — pakiet nie ma bazy, a atrapa
udająca `DISTINCT ON` sprawdzałaby własną implementację, nie produkcyjną.
Dlatego liczby wyżej pochodzą z prawdziwego Postgresa, a tu pilnujemy, żeby
konstrukcja, która je dała, nie zniknęła z zapytania niezauważona.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.ingest import features


class _Kursor:
    def __init__(self):
        self.sql = ""

    def execute(self, sql, params=None):
        plaski = " ".join(sql.split())
        if plaski.startswith("SELECT b.t"):
            self.sql = plaski

    def fetchall(self):
        return []

    def executemany(self, sql, rows):
        pass


@pytest.fixture
def sql_compute_news() -> str:
    h = features.HORIZONS["4h"]
    cur = _Kursor()
    teraz = datetime(2026, 9, 15, 20, tzinfo=timezone.utc)
    features.compute_news(cur, instrument_id=1, h=h, feat_id=7,
                          since=teraz - timedelta(days=1), until=teraz)
    assert cur.sql, "nie przechwycono zapytania `compute_news`"
    return cur.sql


def test_zapytanie_liczy_jeden_wklad_na_URL(sql_compute_news):
    """Sedno: bez tego jedna relacja wchodzi tyle razy, ile razy ją pobrano."""
    assert "DISTINCT ON (a.url)" in sql_compute_news, (
        "zniknęło odróżnianie historii od jej kolejnych migawek — sentyment "
        "jednej relacji „live updates” znów policzy się dziewięć razy")


def test_wybierana_jest_NAJSWIEZSZA_migawka(sql_compute_news):
    """`DISTINCT ON` bez tego `ORDER BY` oddaje migawkę PRZYPADKOWĄ.

    To nie jest kosmetyka: bez jawnego porządku Postgres może zwrócić dowolny
    wiersz z grupy, więc cecha zależałaby od planu zapytania. Relacja z 06:39
    i z 18:49 mówią co innego.
    """
    assert "ORDER BY a.url, an.ingested_at DESC" in sql_compute_news, (
        "wybór migawki przestał być deterministyczny — cecha zaczyna zależeć "
        "od planu zapytania zamiast od tego, co było wiadomo w chwili T")


def test_granica_bitemporalna_ZOSTAJE(sql_compute_news):
    """Kanarek na nadgorliwość: odsiewanie migawek nie może znieść filtru,
    który pilnuje, że wybieramy wyłącznie spośród ZNANYCH w chwili T."""
    assert "an.ingested_at <= b.t" in sql_compute_news, (
        "zniknął filtr bitemporalny — cecha zaczęła widzieć analizy, których "
        "w chwili liczenia świecy jeszcze nie było")


def test_okno_24h_ZOSTAJE(sql_compute_news):
    assert "interval '24 hours'" in sql_compute_news, (
        "zniknęło okno 24 h — cecha przestała opisywać to, co deklaruje jej opis")
