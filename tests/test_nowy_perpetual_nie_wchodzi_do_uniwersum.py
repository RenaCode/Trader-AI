"""Nowy perpetual nie może wejść do uniwersum decyzyjnego przez DEFAULT kolumny.

CO BYŁO NIE TAK

`ref.instrument.tradeable` ma `DEFAULT true` (migracja 0020), a INSERT
w `funding.ensure_perp` tej kolumny NIE WYMIENIAŁ. Każdy nowy perpetual wchodził
więc do bazy z flagą uniwersum decyzyjnego WŁĄCZONĄ.

DLACZEGO TO DZIŚ NIE BOLI — I DLACZEGO TO NIE JEST ZABEZPIECZENIE

Od migracji 0020 nie powstał ani jeden taki wiersz: wszystkie perpetuale
w bazie są starsze i mają `tradeable = false` po tamtej migracji. Wystarczy
jednak jeden nowy instrument spotowy, którego aktywo nie ma jeszcze perpetuala
w bazie, żeby `ensure_perp` wiersz utworzył — a robi to potok danych, w nocy,
bez niczyjej decyzji.

CZYM BY TO GROZIŁO

Perpetual jest w tej bazie WYŁĄCZNIE źródłem stawki finansowania
(`meta.funding_from`) i nikt go nie obstawia — `active` jest dlatego `false`
i było podane jawnie od początku. Z `tradeable = true` wchodziłby do uniwersum
decyzyjnego, którym handluje pętla (`services/engine/uniwersum.py`) i po którym
liczy się odcisk cechy przekrojowej. Zwrot perpetuala jest niemal identyczny ze
zwrotem pary spotowej tego samego aktywa, więc policzony podwójnie przesuwałby
percentyle CAŁEGO zbioru — czyli zmieniałby wartość sygnału dla wszystkich
instrumentów, nie tylko dla jednego nowego.

DLACZEGO ATRAPA WYKONUJE PRAWDZIWY SQL

Cała poprawka to jedno słowo w liście kolumn, a jej sens zależy od DEFAULT-u
w bazie. Sprawdzenie przez `grep "tradeable" w źródle` przechodziłoby także dla
`tradeable = true`. Atrapa odtwarza więc schemat z `DEFAULT true` i wykonuje
prawdziwy INSERT z modułu — bo mierzona jest WARTOŚĆ W WIERSZU, a nie treść
napisu w kodzie.

Nic nie łączy się z produkcyjną bazą ani z giełdą.
"""
from __future__ import annotations

import json
import sqlite3

from services.ingest.funding import ensure_perp


def _na_sqlite(sql: str) -> str:
    """SQL z modułu w dialekcie sqlite. Tłumaczona jest SKŁADNIA, nie warunki."""
    s = " ".join(w.split("--")[0] for w in sql.splitlines())
    s = " ".join(s.split())
    s = s.replace("ref.instrument", "ref_instrument")
    # `meta || ?::jsonb` -> `json_patch`. Ta sama operacja: scalenie obiektu
    # w kolumnę JSON, z zachowaniem kluczy, których nie ma w nowym obiekcie.
    s = s.replace("meta = meta || %s::jsonb",
                  "meta = json_patch(COALESCE(meta, '{}'), ?)")
    return s.replace("%s", "?")


class Kursor:
    def __init__(self, db):
        self.db = db
        self._wiersz = None

    def execute(self, sql, params=None):
        wiersze = self.db.execute(_na_sqlite(sql), tuple(params or ())).fetchall()
        self._wiersz = dict(wiersze[0]) if wiersze else None

    def fetchone(self):
        return self._wiersz


def _baza():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    # `tradeable INTEGER NOT NULL DEFAULT 1` — TO JEST SEDNO TEGO TESTU.
    # Schemat bez DEFAULT-u sprawiłby, że test przechodzi niezależnie od
    # poprawki, bo nie byłoby czego zaniedbać.
    db.executescript("""
        CREATE TABLE ref_instrument (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            venue_id  INTEGER NOT NULL,
            symbol    TEXT    NOT NULL,
            engine    TEXT    NOT NULL,
            base_ccy  TEXT,
            quote_ccy TEXT,
            kind      TEXT    NOT NULL,
            active    INTEGER NOT NULL DEFAULT 1,
            tradeable INTEGER NOT NULL DEFAULT 1,
            meta      TEXT,
            UNIQUE (venue_id, symbol)
        );
    """)
    return db


SPOT = {"id": 1, "venue_id": 1, "symbol": "ADAUSDC", "engine": "crypto",
        "base_ccy": "ADA", "quote_ccy": "USDC"}


def _wiersz(db, symbol: str) -> dict:
    return dict(db.execute("SELECT * FROM ref_instrument WHERE symbol = ?",
                           (symbol,)).fetchone())


def test_default_bazy_JEST_wlaczony_czyli_jest_co_zaniedbac():
    """Kanarek na sam test. Gdyby atrapa nie miała `DEFAULT 1`, wszystko niżej
    przechodziłoby także dla kodu sprzed poprawki."""
    db = _baza()
    db.execute("INSERT INTO ref_instrument (venue_id, symbol, engine, kind)"
               " VALUES (1,'X','crypto','spot')")
    assert _wiersz(db, "X")["tradeable"] == 1, (
        "atrapa nie odtwarza `DEFAULT true` z migracji 0020 — test mierzyłby "
        "wtedy własny schemat, nie poprawkę")


def test_nowy_perpetual_dostaje_tradeable_FALSE():
    """Sedno poprawki. Wiersz, nie napis w kodzie."""
    db = _baza()
    # Instrument spotowy, żeby UPDATE `meta` miał co zaktualizować.
    db.execute("INSERT INTO ref_instrument (id, venue_id, symbol, engine, kind,"
               " active, tradeable) VALUES (1,1,'ADAUSDC','crypto','spot',1,1)")
    perp_id, aktywo = ensure_perp(Kursor(db), SPOT)

    perp = _wiersz(db, "ADAUSDT.P")
    assert perp["id"] == perp_id
    assert perp["tradeable"] == 0, (
        "nowy perpetual wszedł do bazy z `tradeable = true` z DEFAULT-u — czyli "
        "do uniwersum decyzyjnego, którym silnik handluje i po którym liczy się "
        "odcisk cechy przekrojowej")
    assert perp["active"] == 0, (
        "perpetual jest wyłącznie źródłem stawki finansowania — `active` też "
        "musi zostać wyłączone, i to było podane jawnie od początku")
    assert perp["kind"] == "perp"
    assert aktywo == "ADAUSDT"


def test_spot_dostaje_wskazanie_zrodla_finansowania():
    """Kanarek odwrotny: `ensure_perp`, które nic nie robi, przeszłoby test wyżej.

    Funkcja ma DWA skutki i drugi jest tym, po co istnieje: wpisać instrumentowi
    spotowemu, z którego perpetuala bierze się jego stawka finansowania.
    """
    db = _baza()
    db.execute("INSERT INTO ref_instrument (id, venue_id, symbol, engine, kind,"
               " active, tradeable, meta) VALUES (1,1,'ADAUSDC','crypto','spot',"
               "1,1,'{\"cos\":1}')")
    perp_id, _ = ensure_perp(Kursor(db), SPOT)

    meta = json.loads(_wiersz(db, "ADAUSDC")["meta"])
    assert meta["funding_from"] == perp_id
    # Aktywo kwotowane inaczej niż perpetual — przybliżenie zapisane wprost.
    assert meta["funding_proxy"] == "ADAUSDT.P"
    assert meta["cos"] == 1, "UPDATE nadpisał `meta` zamiast ją scalić"


def test_istniejacy_perpetual_NIE_zmienia_flagi():
    """Zmiana uniwersum decyzyjnego należy do migracji, nie do potoku danych.

    Gałąź `DO UPDATE` dotyka wyłącznie `kind` — także po tej poprawce. Gdyby
    zaczęła ustawiać `tradeable`, nocny przebieg potoku cofałby ręczną decyzję
    o uniwersum, a wyglądałoby to na własną zmianę zdania przez bazę.
    """
    db = _baza()
    db.execute("INSERT INTO ref_instrument (id, venue_id, symbol, engine, kind,"
               " active, tradeable) VALUES (1,1,'ADAUSDC','crypto','spot',1,1)")
    db.execute("INSERT INTO ref_instrument (id, venue_id, symbol, engine, kind,"
               " active, tradeable) VALUES (9,1,'ADAUSDT.P','crypto','spot',1,1)")
    ensure_perp(Kursor(db), SPOT)
    perp = _wiersz(db, "ADAUSDT.P")
    assert perp["kind"] == "perp", "istniejący wiersz nie dostał poprawnego `kind`"
    assert perp["tradeable"] == 1, (
        "`DO UPDATE` przestawił `tradeable` istniejącemu instrumentowi — potok "
        "danych zmienia wtedy uniwersum decyzyjne bez niczyjej decyzji")
