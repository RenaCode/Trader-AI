"""Instrument o średnim spreadzie z 7 dni > 15 pb nie dostaje NOWYCH wejść.

CO BYŁO ZŁE (audyt Fable F-1.5, 2026-10-03)

Własna księga, 7 dni: NEARUSDC 21,2 pb średnio, DOGE 7,6, reszta ≤ 6 pb.
NEAR: 4 rundy giełdowe, −218 USD = 59% straty giełdowej. Spread płacony na
rundę był większy niż cała zmierzona przewaga strategii.

CO ZNACZY „WYPADA Z UNIWERSUM DECYZYJNEGO" — I CZEGO NIE ZNACZY

* Nowe wejście odpada: decyzja `hold` z powodem `szeroki_spread`.
* WYJŚCIE IDZIE DALEJ. Pozycja otwarta przed wykluczeniem musi mieć jak wyjść;
  blokada wyjścia trzymałaby ryzyko, którego reguła chce się pozbyć.
* Odcisk uniwersum (`uniwersum.odcisk`) i przekrój `rel_strength` ZOSTAJĄ na
  pełnym zbiorze. Zmiana zbioru zmieniłaby nazwę cechy przekrojowej, a sygnał
  zniknąłby bez śladu do czasu backfillu (docstring `uniwersum.py`).
* Brak pomiaru to NIE wykluczenie: instrument z garstką świec arkusza zostaje.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.engine.uniwersum import (
    MIN_SWIEC_SPREADU, OKNO_SPREADU_DNI, PROG_SPREADU_PB, opis_wykluczen,
    za_szeroki_spread)

KORZEN = Path(__file__).resolve().parents[1]


def test_prog_i_okno_z_audytu():
    assert PROG_SPREADU_PB == 15.0
    assert OKNO_SPREADU_DNI == 7


def test_near_wypada_doge_zostaje():
    srednie = {1: (21.15, 41), 2: (7.63, 41), 3: (0.73, 41)}
    assert za_szeroki_spread(srednie) == {1: 21.15}


def test_brak_pomiaru_to_nie_wykluczenie():
    """Garstka świec mówi o chwili pomiaru, nie o rynku."""
    assert za_szeroki_spread({1: (40.0, MIN_SWIEC_SPREADU - 1)}) == {}


def test_prog_ostro_wiekszy():
    assert za_szeroki_spread({1: (PROG_SPREADU_PB, 41)}) == {}


def test_opis_podaje_symbol_i_spread():
    linie = opis_wykluczen({1: 21.15}, {1: "NEARUSDC"})
    assert any("NEARUSDC" in w and "21.15" in w for w in linie)
    assert opis_wykluczen({}, {}) == []


def test_trener_wypisuje_wykluczone():
    zrodlo = (KORZEN / "services/engine/train.py").read_text(encoding="utf-8")
    assert "opis_wykluczen(" in zrodlo


def test_migracja_dopuszcza_powod():
    sql = (KORZEN / "db/migrations/0075_powod_holdu_szeroki_spread.sql").read_text(
        encoding="utf-8")
    assert "'szeroki_spread'" in sql
    assert "lock_timeout" in sql


DB = os.environ.get("TEST_DB_URL")


@pytest.mark.skipif(not DB, reason="ustaw TEST_DB_URL na jednorazowa baze")
def test_srednie_spready_z_bazy():
    import psycopg
    from psycopg.rows import dict_row

    from services.engine.uniwersum import srednie_spready

    teraz = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    nazwa = f"book_spread_bps__4h_test_{uuid.uuid4().hex[:6]}"
    with psycopg.connect(DB, autocommit=True, row_factory=dict_row) as c:
        venue = c.execute("SELECT id FROM ref.venue WHERE engine = 'crypto'"
                          " ORDER BY id LIMIT 1").fetchone()["id"]
        ids = [c.execute(
            "INSERT INTO ref.instrument (venue_id, symbol, engine, quote_ccy, kind)"
            " VALUES (%s, %s, 'crypto', 'USDC', 'spot') RETURNING id",
            (venue, f"T{uuid.uuid4().hex[:6].upper()}USDC")).fetchone()["id"]
            for _ in range(2)]
        fd = c.execute(
            "INSERT INTO feat.feature_def (name, params, code_ref)"
            " VALUES (%s, '{\"role\": \"book_spread_bps\", \"bar\": \"4h\"}', 'test')"
            " RETURNING id", (nazwa,)).fetchone()["id"]
        try:
            for k in range(30):
                t = teraz - timedelta(hours=4 * (k + 1))
                c.execute("INSERT INTO feat.feature_value (feature_def_id,"
                          " instrument_id, event_time, value) VALUES"
                          " (%s,%s,%s,21.0), (%s,%s,%s,3.0)",
                          (fd, ids[0], t, fd, ids[1], t))
            # starsze niż okno: nie wchodzą do średniej
            c.execute("INSERT INTO feat.feature_value (feature_def_id,"
                      " instrument_id, event_time, value) VALUES (%s,%s,%s,900.0)",
                      (fd, ids[1], teraz - timedelta(days=OKNO_SPREADU_DNI + 1)))
            with c.cursor() as cur:
                # zegar teraźniejszy, nie pełna godzina: `ingested_at <= do`
                # odcina wiersze zapisane po chwili pomiaru (punkt w czasie)
                wynik = srednie_spready(cur, ids, "4h", datetime.now(timezone.utc))
            assert wynik[ids[0]] == (pytest.approx(21.0), 30)
            assert wynik[ids[1]] == (pytest.approx(3.0), 30)
            assert set(za_szeroki_spread(wynik)) == {ids[0]}
        finally:
            c.execute("DELETE FROM feat.feature_value WHERE feature_def_id = %s", (fd,))
            c.execute("DELETE FROM feat.feature_def WHERE id = %s", (fd,))
            c.execute("DELETE FROM ref.instrument WHERE id = ANY(%s)", (ids,))


# ------------------------------------------------------- bramka spójności (2026-10-18)
#
# Equity: `book_spread_bps` z feedu IEX (LLY 278 pb, MSFT 35 pb) wykluczał 18 z 20
# spółek, choć `spread.zmierz_spread` - ten sam pomiar w trenerze - odrzuca go
# bramką spójności. `wykluczenia_spreadowe` pyta tę samą bramkę.

class _Baza:
    """Atrapa rozstrzygająca po treści zapytania: wiersze bramki
    (`spread._SQL`) i średnie (`_SQL_SREDNIE_SPREADY`) to dwa różne pytania."""

    def __init__(self, obserwacje, srednie):
        self.obserwacje, self.srednie, self._w = obserwacje, srednie, []

    def execute(self, sql, params=None):
        self._w = self.obserwacje if "zakres_bps" in sql else self.srednie

    def fetchall(self):
        return list(self._w)


def _obserwacje(spready_i_zakresy: dict[int, tuple[float, float]], n=40):
    t0 = datetime(2026, 10, 10, tzinfo=timezone.utc)
    return [{"instrument_id": i, "event_time": t0 + timedelta(hours=k),
             "spread_bps": sp, "tradeable": True, "zakres_bps": zakres}
            for i, (sp, zakres) in spready_i_zakresy.items() for k in range(n)]


def _h():
    from services.engine.horizons import get as get_horizon
    return get_horizon("1h")


TERAZ_T = datetime(2026, 10, 12, tzinfo=timezone.utc)


def test_pomiar_ODRZUCONY_bramka_nie_wyklucza_nikogo():
    """Feed IEX: kwotowany spread większy niż cały zakres świecy (iloraz > 0,5)."""
    from services.engine.uniwersum import wykluczenia_spreadowe

    baza = _Baza(_obserwacje({1: (278.0, 120.0), 2: (35.0, 40.0), 3: (20.0, 30.0)}),
                 [{"instrument_id": 1, "srednia": 278.0, "n": 40},
                  {"instrument_id": 2, "srednia": 35.0, "n": 40},
                  {"instrument_id": 3, "srednia": 20.0, "n": 40}])
    wykluczone, powod = wykluczenia_spreadowe(baza, "equity", [1, 2, 3], _h(), TERAZ_T)

    assert wykluczone == {}
    assert powod and "ODRZUCONY" in powod


def test_KANAREK_te_same_dane_bez_bramki_wykluczaly_wszystkie_trzy():
    """Stara ścieżka (sama średnia) na tych samych danych - dowód, że test
    wyżej sprawdza bramkę, a nie np. próg świec."""
    srednie = {1: (278.0, 40), 2: (35.0, 40), 3: (20.0, 40)}
    assert set(za_szeroki_spread(srednie)) == {1, 2, 3}


def test_pomiar_PRZYJETY_wyklucza_DOKLADNIE_jak_wczesniej():
    """Krypto: iloraz ~0,03, bramka przechodzi - wynik identyczny ze starą
    regułą, także dla instrumentu z 12-29 świecami (poza `per_id` pomiaru)."""
    from services.engine.uniwersum import srednie_spready, wykluczenia_spreadowe

    srednie = [{"instrument_id": 1, "srednia": 21.15, "n": 41},
               {"instrument_id": 2, "srednia": 7.63, "n": 41},
               {"instrument_id": 3, "srednia": 0.73, "n": 41},
               {"instrument_id": 4, "srednia": 40.0, "n": 20}]
    baza = _Baza(_obserwacje({1: (21.15, 700.0), 2: (7.63, 250.0), 3: (0.73, 30.0)})
                 + _obserwacje({4: (40.0, 1300.0)}, n=20), srednie)
    wykluczone, powod = wykluczenia_spreadowe(baza, "crypto", [1, 2, 3, 4], _h(), TERAZ_T)

    assert powod is None
    assert wykluczone == za_szeroki_spread(srednie_spready(baza, [1, 2, 3, 4], "1h", TERAZ_T))
    assert wykluczone == {1: 21.15, 4: 40.0}


def test_petla_i_trener_wolaja_TE_SAMA_funkcje():
    for plik in ("services/engine/loop.py", "services/engine/train.py"):
        zrodlo = (KORZEN / plik).read_text(encoding="utf-8")
        assert "wykluczenia_spreadowe(" in zrodlo, plik
        assert "za_szeroki_spread(srednie_spready(" not in zrodlo, (
            f"{plik} liczy wykluczenia z pominięciem bramki spójności")
