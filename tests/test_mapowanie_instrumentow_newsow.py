"""Symbol z analizy LLM -> instrumenty o tym samym aktywie bazowym (poprawka C).

Do 2026-10-05 `zapisz_analize` wiązała artykuł wyłącznie z symbolem, który
model wypisał dosłownie. Model wybiera zwykle parę najbardziej znaną (ETHUSDT),
a uniwersum decyzyjne stoi od 0023 na parach USDC — więc cecha `news` pary,
którą handlujemy, dostawała mniej, niż wiedzieliśmy.

Zmiana działa TYLKO DO PRZODU: `news.article_instrument` nie ma `ingested_at`,
więc powiązania dopisane wstecz weszłyby do świec, w których tej wiedzy
jeszcze nie było (docs/hipotezy.md).
"""
from __future__ import annotations

import os

import pytest

from services.ingest.news_worker import (
    SQL_INSTRUMENTY, aktywo_bazowe, mapa_instrumentow, zapisz_analize)

# Kolumny dokładnie z SQL_INSTRUMENTY. `base_ccy` puste tak, jak w parach z 0018.
WIERSZE = [
    {"id": 2, "symbol": "ETHUSDT", "engine": "crypto", "base_ccy": "ETH", "quote_ccy": "USDT"},
    {"id": 12, "symbol": "ETHUSDC", "engine": "crypto", "base_ccy": None, "quote_ccy": "USDC"},
    {"id": 11, "symbol": "BTCUSDC", "engine": "crypto", "base_ccy": None, "quote_ccy": "USDC"},
    {"id": 30, "symbol": "AAPL", "engine": "equity", "base_ccy": None, "quote_ccy": "USD"},
]


def test_aktywo_bazowe_z_kolumny_albo_z_symbolu():
    assert [aktywo_bazowe(w) for w in WIERSZE] == ["ETH", "ETH", "BTC", "AAPL"]


def test_symbol_mapuje_sie_na_wszystkie_pary_tego_aktywa():
    mapa = mapa_instrumentow(WIERSZE)
    assert mapa["ETHUSDT"] == (2, 12)
    assert mapa["ETHUSDC"] == (2, 12)
    assert mapa["BTCUSDC"] == (11,)
    assert mapa["AAPL"] == (30,)
    # Klucze to dalej symbole - lista w prompcie się nie zmienia.
    assert sorted(mapa) == ["AAPL", "BTCUSDC", "ETHUSDC", "ETHUSDT"]


def test_to_samo_aktywo_w_INNYM_silniku_sie_nie_skleja():
    mapa = mapa_instrumentow(WIERSZE + [
        {"id": 40, "symbol": "ETH", "engine": "equity", "base_ccy": None, "quote_ccy": "USD"}])
    assert mapa["ETHUSDT"] == (2, 12)
    assert mapa["ETH"] == (40,)


class _Kursor:
    def __init__(self):
        self.powiazania = []

    def execute(self, sql, params=None):
        if sql.startswith("INSERT INTO news.article_instrument"):
            self.powiazania.append(params[1])


def _zapisz(symbole):
    cur = _Kursor()
    data = {"direction": "bullish", "magnitude": 0.5, "horizon": "1d",
            "confidence": 0.7, "novelty": 0.4, "reasoning": "-",
            "instruments": symbole}
    zapisz_analize(cur, 1, "model", data, {"input": 1, "output": 1}, 0.0,
                   None, mapa_instrumentow(WIERSZE))
    return cur.powiazania


def test_ETHUSDT_z_analizy_wiaze_artykul_takze_z_ETHUSDC():
    assert _zapisz(["ETHUSDT"]) == [2, 12]


def test_oba_symbole_tego_aktywa_nie_dubluja_powiazan():
    """`compute_news` SUMUJE po `news.article_instrument` — podwójny wiersz
    to podwójna waga jednego zdarzenia."""
    assert _zapisz(["ETHUSDT", "ethusdc"]) == [2, 12]


def test_nieznany_symbol_nie_wiaze_niczego():
    assert _zapisz(["DOGEUSDT"]) == []
    assert _zapisz([]) == []


DB = os.environ.get("TEST_DB_URL")


@pytest.mark.skipif(not DB, reason="ustaw TEST_DB_URL na jednorazowa baze")
def test_zapytanie_NA_POSTGRESIE_daje_kolumny_dla_mapy():
    """Atrapa wyżej podaje `engine`, `base_ccy` i `quote_ccy` sama; ten test
    sprawdza, że SQL_INSTRUMENTY naprawdę je pobiera — także rzutowanie enuma
    `ref.engine` na tekst — i że para bez `base_ccy` łączy się z parą USDT."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(DB, row_factory=dict_row) as c:
        eth = c.execute("SELECT id, venue_id FROM ref.instrument"
                        " WHERE symbol = 'ETHUSDT'").fetchone()
        assert eth, "seed 0008 nie ma ETHUSDT"
        nowy = c.execute(
            "INSERT INTO ref.instrument (venue_id, symbol, engine, quote_ccy, kind)"
            " VALUES (%s, 'ETHUSDCTEST', 'crypto', 'USDCTEST', 'spot') RETURNING id",
            (eth["venue_id"],)).fetchone()["id"]
        mapa = mapa_instrumentow(c.execute(SQL_INSTRUMENTY).fetchall())
        c.rollback()
    assert mapa["ETHUSDT"] == tuple(sorted((eth["id"], nowy)))
    assert mapa["ETHUSDCTEST"] == mapa["ETHUSDT"]
