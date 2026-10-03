"""Rewizja wartości cechy ma własną datę — `feat.feature_value.revised_at` (0070).

CO BYŁO ŹLE

`features.insert_values` nadpisywał wartość (ON CONFLICT DO UPDATE) i wpisywał
przy tym `ingested_at = znane_od(event_time)` — tę samą datę co przy pierwszym
zapisie. Wartość poprawiona tydzień po decyzji wyglądała więc w bazie jak znana
od zamknięcia świecy, a oba strażniki bitemporalne (`train.load_panel`
i `loop.sprawdz/kontrola_datowania`) przepuszczały ją z definicji. Rozjazd
„silnik decydował na X, trener uczy się na Y” był niemierzalny.

CO PILNUJĄ TE TESTY

1. Zapis: nadpisanie ustawia `revised_at = now()`, nowy wiersz zostawia NULL,
   przebieg bez zmiany wartości nie przesuwa daty, a `ingested_at` zostaje
   `znane_od` (inaczej trener wyrzuciłby każdą przeliczoną wstecz cechę).
2. Trener: wartość zrewidowana po chwili decyzji ZOSTAJE w panelu, ale jest
   policzona i raportowana — z tą samą tolerancją co straż `ingested_at`.
3. Silnik: kontrola startowa liczy rewizje z 7 dni tym samym progiem.
4. Na prawdziwym Postgresie (TEST_DB_URL): kolumna istnieje, jest NULL-owalna
   bez wartości domyślnej, a nadpisanie działa też w chunku SKOMPRESOWANYM.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.engine import train
from services.engine.horizons import H1H, H4H, H5M
from services.ingest import features
from services.ingest.features import znane_od

KORZEN = Path(__file__).resolve().parents[1]
MIGRACJA = KORZEN / "db" / "migrations" / "0070_rewizja_cechy_ma_wlasna_date.sql"
T = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)


def _sql_zapisu() -> str:
    class Kursor:
        def __init__(self):
            self.sql = []

        def executemany(self, sql, rows):
            self.sql.append(sql)

    k = Kursor()
    features.insert_values(k, [(1, 1, T, 1.0, znane_od(T, H4H))])
    assert len(k.sql) == 1
    return " ".join(k.sql[0].split())


# ------------------------------------------------------------------ zapis

def test_nadpisanie_ustawia_date_rewizji_a_nowy_wiersz_jej_nie_ma():
    """Na starym kodzie DO UPDATE nie dotykał żadnej daty poza `ingested_at`."""
    sql = _sql_zapisu()
    wstaw, aktualizuj = sql.split("ON CONFLICT", 1)
    assert "revised_at = now()" in aktualizuj, (
        "nadpisanie wartości cechy nie zostawia daty rewizji — strażniki znów "
        "nie odróżnią wartości znanej na czas od poprawionej później")
    assert "revised_at" not in wstaw, (
        "nowy wiersz dostaje datę rewizji — NULL ma znaczyć 'nigdy nie poprawiany'")


def test_ingested_at_przy_nadpisaniu_zostaje_znane_od_a_nie_now():
    """`ingested_at = now()` wyrzuciłoby z panelu każdą przeliczoną wstecz cechę."""
    aktualizuj = _sql_zapisu().split("ON CONFLICT", 1)[1]
    assert "ingested_at = EXCLUDED.ingested_at" in aktualizuj
    assert "ingested_at = now()" not in aktualizuj


def test_przebieg_bez_zmiany_wartosci_nie_przesuwa_rewizji():
    """Rewizja to ZMIANA wartości. Bez warunku każdy cykl potoku byłby rewizją."""
    aktualizuj = _sql_zapisu().split("ON CONFLICT", 1)[1]
    assert "WHERE feat.feature_value.value IS DISTINCT FROM EXCLUDED.value" in aktualizuj


# ------------------------------------------------------------------ trener

class _KursorPanelu:
    def __init__(self, wiersze):
        self.wiersze = wiersze
        self.sql = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return self.wiersze


def test_load_panel_niesie_licznik_rewizji_i_NIE_wyrzuca_wiersza():
    """Na starym kodzie wiersz nie miał pola `rewizje`, a zapytanie nie czytało
    `revised_at` w ogóle — rewizja była nie do odróżnienia."""
    kursor = _KursorPanelu([
        {"t": T, "feats": {"rsi": 50.0, "atr_pct": 1.0}, "rewizje": 1,
         "close": 100.0, "high": 101.0, "low": 99.0},
        {"t": T + timedelta(hours=1), "feats": {"rsi": 51.0}, "rewizje": 0,
         "close": 100.0, "high": 101.0, "low": 99.0},
    ])
    panel = train.load_panel(kursor, 1, H1H, T, T + timedelta(days=1), fp="x")
    assert [r["rewizje"] for r in panel] == [1, 0]
    assert len(panel) == 2, "wiersz z rewizją wypadł z panelu"
    sql = " ".join(kursor.sql.split())
    assert "fv.revised_at > fv.event_time + 2 * %(bar)s::interval" in sql, (
        "tolerancja rewizji w load_panel rozjechała się ze strażą ingested_at")
    assert "count(*) FILTER (WHERE f.po_decyzji) AS rewizje" in sql
    # Straż `ingested_at` stoi dalej — rewizja jej nie zastępuje.
    assert "fv.ingested_at <= fv.event_time + 2 * %(bar)s::interval" in sql


def test_raport_rewizji_liczy_odsetek_wartosci_cech():
    panele = {
        "A": [{"f": {"a": 1.0, "b": 2.0}, "rewizje": 1},
              {"f": {"a": 1.0, "b": 2.0}, "rewizje": 0}],
        # Panel spoza load_panel (atrapa, narzędzie) nie ma pola — to nie rewizja.
        "B": [{"f": {"a": 1.0}}],
    }
    assert train.rewizje_po_decyzji(panele) == (1, 5)


def test_trener_raportuje_rewizje_w_logu():
    zrodlo = (KORZEN / "services" / "engine" / "train.py").read_text()
    blok = zrodlo[zrodlo.index("def train("):]
    assert "rewizje_po_decyzji(panels)" in blok, (
        "trener nie raportuje rewizji po czasie decyzji")


@pytest.mark.parametrize("h", [H5M, H1H, H4H])
def test_predykat_rewizji_odroznia_poprawke_od_wartosci_znanej_na_czas(h):
    """Odtworzenie predykatu z SQL: tolerancja = straż `ingested_at` (2 świece)."""
    def po_decyzji(event_time, revised_at):
        return revised_at is not None and revised_at > event_time + 2 * timedelta(
            minutes=h.bar_minutes)

    assert not po_decyzji(T, None), "wiersz nigdy nie poprawiany liczony jako rewizja"
    # Poprawka w następnym cyklu potoku, zanim decyzja mogła zapaść — nie liczy się.
    assert not po_decyzji(T, znane_od(T, h))
    # Poprawka tydzień później (np. świeca poprawiona backfillem) — liczy się.
    assert po_decyzji(T, T + timedelta(days=7))


# ------------------------------------------------------------------ silnik

def test_kontrola_datowania_liczy_rewizje_tym_samym_progiem():
    """Na starym kodzie kontrola znała tylko `ingested_at`, czyli przepuszczała
    każdą rewizję z definicji."""
    zrodlo = (KORZEN / "services" / "engine" / "loop.py").read_text()
    blok = zrodlo[zrodlo.index("def kontrola_datowania"):
                  zrodlo.index('probuj("datowanie nie wyprzedza')]
    plaski = " ".join(blok.split())
    assert "fv.revised_at > fv.event_time + 2 * %s::interval" in plaski
    assert "fv.ingested_at < fv.event_time + %s::interval" in plaski, (
        "kontrola datowania zgubiła pierwotny warunek")
    # Rewizja to UWAGA, nie błąd: blokowanie startu za uprawnioną poprawkę
    # świecy nie naprawia niczego.
    assert 'r["zrewidowane"]' in blok and "UWAGA" in blok


def _zapytanie_kontroli_datowania() -> str:
    zrodlo = (KORZEN / "services" / "engine" / "loop.py").read_text()
    blok = zrodlo[zrodlo.index("def kontrola_datowania"):]
    zapytanie = blok[blok.index('"""') + 3:]
    return zapytanie[:zapytanie.index('"""')]


def test_kontrola_datowania_ma_trzy_parametry():
    """Kolejność parametrów: tolerancja datowania, tolerancja rewizji, świeca."""
    zapytanie = _zapytanie_kontroli_datowania()
    assert zapytanie.count("%s") == 3
    assert zapytanie.index("ingested_at") < zapytanie.index("revised_at") \
        < zapytanie.index("params->>'bar'")


# ------------------------------------------------------------------ migracja

def test_migracja_jest_bezpieczna_dla_dzialajacej_bazy():
    tresc = MIGRACJA.read_text()
    kod = "\n".join(l for l in tresc.splitlines() if not l.lstrip().startswith("--"))
    assert "lock_timeout" in kod, "ALTER na hypertable bez lock_timeout"
    assert "ADD COLUMN revised_at timestamptz;" in " ".join(kod.split()), (
        "kolumna z DEFAULT/NOT NULL przepisywałaby albo blokowała skompresowane chunki")
    assert "CREATE INDEX" not in kod.upper()


TEST_DB_URL = os.environ.get("TEST_DB_URL")
wymaga_bazy = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="ustaw TEST_DB_URL na jednorazową bazę z zastosowanymi migracjami")


def _polacz():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(TEST_DB_URL, row_factory=dict_row)


def _zasiej(cur, h, t0, n):
    cur.execute("SELECT id FROM ref.venue LIMIT 1")
    venue = cur.fetchone()["id"]
    sym = f"REW{uuid.uuid4().hex[:8]}/USDC"
    cur.execute(
        "INSERT INTO ref.instrument (venue_id, symbol, engine, quote_ccy, kind,"
        " active, tradeable) VALUES (%s,%s,'crypto','USDC','spot',true,true)"
        " RETURNING id", (venue, sym))
    iid = cur.fetchone()["id"]
    cur.execute(
        "INSERT INTO feat.feature_def (name, params, code_ref, active)"
        " VALUES (%s, %s::jsonb, 'test', true) RETURNING id",
        (f"rsi__{h.bar}_{sym}", '{"role": "rsi", "bar": "' + h.bar + '"}'))
    fid = cur.fetchone()["id"]
    for i in range(n):
        t = t0 + i * timedelta(minutes=h.bar_minutes)
        cur.execute(
            "INSERT INTO market.candle (instrument_id, timeframe, event_time,"
            " open, high, low, close, volume) VALUES (%s,%s,%s,100,101,99,100,1)",
            (iid, h.source_timeframe, t))
    return iid, fid


@wymaga_bazy
def test_PRAWDZIWA_BAZA_kolumna_nullowalna_bez_domyslnej():
    with _polacz() as conn:
        r = conn.execute(
            "SELECT is_nullable, column_default, data_type"
            " FROM information_schema.columns WHERE table_schema='feat'"
            " AND table_name='feature_value' AND column_name='revised_at'").fetchone()
    assert r == {"is_nullable": "YES", "column_default": None,
                 "data_type": "timestamp with time zone"}


@wymaga_bazy
def test_PRAWDZIWA_BAZA_zapis_rewizja_i_panel_trenera():
    h = H1H
    t0 = T
    with _polacz() as conn:
        cur = conn.cursor()
        iid, fid = _zasiej(cur, h, t0, 3)
        wiersze = [(fid, iid, t0 + i * timedelta(hours=1), float(i),
                    znane_od(t0 + i * timedelta(hours=1), h)) for i in range(3)]
        features.insert_values(cur, wiersze)
        features.insert_values(cur, wiersze)          # przebieg bez zmian
        cur.execute("SELECT count(*) AS n FROM feat.feature_value"
                    " WHERE feature_def_id=%s AND revised_at IS NOT NULL", (fid,))
        assert cur.fetchone()["n"] == 0, "nowy albo niezmieniony wiersz ma datę rewizji"

        # Poprawka świecy t0: inna wartość, ta sama data `znane_od`.
        features.insert_values(cur, [(fid, iid, t0, 42.0, znane_od(t0, h))])
        cur.execute("SELECT value, ingested_at, revised_at FROM feat.feature_value"
                    " WHERE feature_def_id=%s AND event_time=%s", (fid, t0))
        r = cur.fetchone()
        assert r["value"] == 42.0
        assert r["ingested_at"] == znane_od(t0, h), "ingested_at przesunięte przy poprawce"
        assert r["revised_at"] is not None and r["revised_at"] > t0 + 2 * timedelta(hours=1)

        panel = train.load_panel(cur, iid, h, t0, t0 + timedelta(hours=3), fp="x")
        assert [p["rewizje"] for p in panel] == [1, 0, 0]
        assert panel[0]["f"]["rsi"] == 42.0, "trener ma uczyć się na wartości poprawionej"
        conn.rollback()


@wymaga_bazy
def test_PRAWDZIWA_BAZA_nadpisanie_w_chunku_SKOMPRESOWANYM():
    """Prawie cała historia cech leży w chunkach skompresowanych (polityka 30 dni),
    a backfill poprawia właśnie historię."""
    h = H1H
    t0 = datetime(2020, 1, 6, tzinfo=timezone.utc)     # osobny, stary chunk
    with _polacz() as conn:
        cur = conn.cursor()
        iid, fid = _zasiej(cur, h, t0, 2)
        features.insert_values(cur, [(fid, iid, t0, 1.0, znane_od(t0, h))])
        cur.execute("SELECT c FROM show_chunks('feat.feature_value',"
                    " older_than => %s::timestamptz) c", (t0 + timedelta(days=8),))
        for (c,) in [tuple(r.values()) for r in cur.fetchall()]:
            cur.execute("SELECT compress_chunk(%s::regclass, true)", (c,))
        cur.execute("SELECT bool_and(is_compressed) AS ok FROM timescaledb_information.chunks"
                    " WHERE hypertable_schema='feat' AND hypertable_name='feature_value'"
                    " AND range_end <= %s", (t0 + timedelta(days=8),))
        assert cur.fetchone()["ok"], "test nie skompresował chunka — mierzyłby co innego"
        features.insert_values(cur, [(fid, iid, t0, 2.0, znane_od(t0, h))])
        cur.execute("SELECT value, revised_at FROM feat.feature_value"
                    " WHERE feature_def_id=%s AND event_time=%s", (fid, t0))
        r = cur.fetchone()
        assert r["value"] == 2.0 and r["revised_at"] is not None
        conn.rollback()


@wymaga_bazy
def test_PRAWDZIWA_BAZA_zapytanie_kontroli_startowej_silnika_sie_wykonuje():
    """`--sprawdz` bramkuje start silnika. Zapytanie z kolumną, której schemat
    nie ma, zatrzymałoby silnik — tu wykonujemy DOKŁADNIE tekst z loop.py."""
    h = H1H
    teraz = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    t0 = teraz - timedelta(days=2)
    with _polacz() as conn:
        cur = conn.cursor()
        iid, fid = _zasiej(cur, h, t0, 1)
        features.insert_values(cur, [(fid, iid, t0, 1.0, znane_od(t0, h))])
        features.insert_values(cur, [(fid, iid, t0, 2.0, znane_od(t0, h))])
        cur.execute(_zapytanie_kontroli_datowania(),
                    (f"{h.bar_minutes} minutes", f"{h.bar_minutes} minutes", h.bar))
        r = cur.fetchone()
        assert r["n"] == 0
        assert r["zrewidowane"] >= 1, "kontrola nie widzi rewizji po czasie decyzji"
        conn.rollback()
