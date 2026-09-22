"""Silnik przełączony na giełdę nie dziedziczy pozycji z rachunku papierowego.

13.09.2026 przełączenie `--egzekucja gielda` zastało w bazie pozycję otwartą na
papierze. Giełda jej nie miała: wyjście zostałoby odrzucone, uzgodnienie
zgłosiłoby pozycję-widmo i zatrzymało wejścia. Skończyło się zejściem całego
silnika z powrotem na papier i wyciszeniem uzgodnienia w demo — czyli utratą
jedynego pomiaru poślizgu, dla którego demo istnieje.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from services.engine.loop import zamknij_pozycje_papierowe
from tests.atrapa_bazy import Kursor

TERAZ = datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)

POZYCJA = {"id": 31, "engine": "crypto", "mode": "demo", "instrument_id": 5,
           "symbol": "ARBUSDC", "side": "long", "qty": D("6810.105875"),
           "avg_price": D("0.22007"), "open_decision_id": 900}
CENA = {"close": D("0.2150"), "wiek_min": 1.0}
# Dwa odczyty `zapisz_wyjscie`: skąd jest pozycja i id założonego zlecenia.
SKAD = {"engine": "crypto", "mode": "demo", "instrument_id": 5, "side": "long",
        "quote_ccy": "USDC"}
ID_ZLECENIA = {"id": 91}


class KursorSekwencyjny(Kursor):
    def fetchall(self):
        odp = self._wiersze.pop(0) if self._wiersze else []
        assert isinstance(odp, list), f"fetchall dostał odpowiedź dla fetchone: {odp!r}"
        return list(odp)


class Polaczenie:
    def __init__(self, cur):
        self._cur, self.zatwierdzen = cur, 0

    def cursor(self, row_factory=None):
        return self._cur

    def commit(self):
        self.zatwierdzen += 1


def test_pozycja_papierowa_jest_zamykana_z_JAWNA_decyzja_administracyjna():
    cur = KursorSekwencyjny([[POZYCJA], CENA, {"id": 777}, SKAD, ID_ZLECENIA])
    conn = Polaczenie(cur)

    zamkniete = zamknij_pozycje_papierowe(conn, "crypto", "demo", "spot_eu", now=TERAZ)

    assert [z["id"] for z in zamkniete] == [31]
    assert zamkniete[0]["decision_id"] == 777
    decyzje = [(s, p) for s, p in cur.sql if "INSERT INTO journal.decision " in s]
    assert len(decyzje) == 1, "każde zamknięcie ma zostawić DOKŁADNIE jedną decyzję"
    sql, params = decyzje[0]
    assert "'exit'" in sql
    assert "administracyjne" in sql, (
        "dziennik jest append-only i ma mówić prawdę: to nie jest decyzja modelu")
    assert "NIE jest decyzja modelu" in params[-1]
    wyjscia = [s for s, _ in cur.sql if "INSERT INTO exec.fill" in s]
    assert wyjscia, "zamknięcie ma przejść przez normalną księgowość wyjścia"
    assert conn.zatwierdzen == 1


def test_zapytanie_wybiera_WYLACZNIE_pozycje_z_rachunku_papierowego():
    """Pozycja otwarta NA GIEŁDZIE zostaje — giełda ją ma i to ona ją zamknie.
    Atrapa nie wykonuje WHERE, więc pilnujemy warunku w samym SQL-u."""
    cur = KursorSekwencyjny([[]])
    zamkniete = zamknij_pozycje_papierowe(Polaczenie(cur), "crypto", "demo", "spot_eu",
                                          now=TERAZ)
    assert zamkniete == []
    sql = next(s for s, _ in cur.sql if "FROM exec.position" in s)
    assert "raw->>'execution' = 'internal_paper'" in sql
    assert "o.decision_id = p.open_decision_id" in sql
    assert "closed_at IS NULL" in sql


def test_bez_ceny_NIE_zamyka_i_NIE_pozwala_wystartowac():
    """Wyjście po zerze byłoby gorsze niż pozycja-widmo — a cicho zostawiona
    pozycja to dokładnie stan z 13.09."""
    cur = KursorSekwencyjny([[POZYCJA], None])   # brak świecy minutowej
    with pytest.raises(RuntimeError):
        zamknij_pozycje_papierowe(Polaczenie(cur), "crypto", "demo", "spot_eu", now=TERAZ)
    assert not any("INSERT INTO journal.decision " in s for s, _ in cur.sql)
