"""`dozor --co decyzje`: każda świeca dostaje decyzję dla każdego instrumentu.

CO BYŁO ZŁE (audyt Fable F-8.3, 2026-10-03)

Czujki wykrywały brak PULSU, nie brak SENSU. Silnik bije puls, gdy cykl się
udał — także wtedy, gdy nie podjął ani jednej decyzji. `swiezosc` pilnuje cech,
`odlozone` pilnuje świec odłożonych przez niepełny komplet sygnałów, ale żadna
nie pytała wprost: „czy dla tej świecy jest decyzja dla każdego instrumentu
uniwersum, skoro cechy przyszły?". 11 dni reżimu `range` bez cech cenowych
przeszło niezauważone.

Reguła: dwie kolejne domknięte świece z decyzjami dla mniej niż wszystkich
instrumentów uniwersum, PRZY świeżych cechach (potok cech tę świecę
wyprodukował) → `warning`. Brak cech to sprawa czujki `swiezosc` — tu cisza
z powodem w logu, żeby jedna awaria nie dawała dwóch alarmów.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from services.engine.dozor import (
    SWIEC_BEZ_DECYZJI, ZWLOKA_DECYZJI_MIN, ocen_decyzje_na_swiece,
    sprawdz_decyzje, swiece_do_oceny)

KORZEN = Path(__file__).resolve().parents[1]
TERAZ = datetime(2026, 10, 3, 14, 0, tzinfo=timezone.utc)
B = datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc)
H4 = timedelta(hours=4)


def _w(bar_start, decyzji, z_cechami):
    return {"bar_start": bar_start, "decyzji": decyzji, "z_cechami": z_cechami}


# --------------------------------------------------------------- czysta ocena
def test_komplet_decyzji_to_cisza():
    assert ocen_decyzje_na_swiece([_w(B, 10, 10), _w(B - H4, 10, 10)], 10) is None


def test_dwie_swiece_z_rzedu_bez_kompletu_przy_swiezych_cechach_ALARMUJA():
    w = ocen_decyzje_na_swiece([_w(B, 0, 10), _w(B - H4, 7, 10)], 10)
    assert w is not None
    assert w["swiece"] == [B, B - H4]
    assert "0/10" in w["opis"] and "7/10" in w["opis"]


def test_jedna_swieca_bez_kompletu_to_jeszcze_NIE_alarm():
    """Restart poda albo pojedynczy wywrócony cykl nie jest stanem."""
    assert SWIEC_BEZ_DECYZJI == 2
    assert ocen_decyzje_na_swiece([_w(B, 0, 10), _w(B - H4, 10, 10)], 10) is None


def test_brak_cech_to_NIE_ta_czujka():
    """Potok cech stoi -> alarmuje `swiezosc`. Dwa alarmy o jednej awarii
    uczą przewijać oba."""
    assert ocen_decyzje_na_swiece([_w(B, 0, 0), _w(B - H4, 0, 0)], 10) is None


def test_za_malo_swiec_do_oceny_to_cisza():
    assert ocen_decyzje_na_swiece([_w(B, 0, 10)], 10) is None


# --------------------------------------------------------------- które świece
def test_swieca_jest_oceniana_dopiero_po_zwloce_od_zamkniecia():
    """Decyzja dla B zapada po zamknięciu B (B+4h) z opóźnieniem potoku. Świeca,
    której okno decyzji jeszcze trwa w granicy zwłoki, nie może alarmować."""
    m = 240
    swiece = swiece_do_oceny(TERAZ, m, ZWLOKA_DECYZJI_MIN, 2)
    for s in swiece:
        assert s + timedelta(minutes=m + ZWLOKA_DECYZJI_MIN) <= TERAZ
    # najnowsza możliwa: każda późniejsza nie przeszła jeszcze zwłoki
    assert swiece[0] + H4 + timedelta(minutes=m + ZWLOKA_DECYZJI_MIN) > TERAZ
    assert swiece[0] - swiece[1] == H4


def test_zwloka_ponad_zmierzone_maksimum():
    """Przed F-4.1 decyzje zapadały do 88 min po świecy. Zwłoka mniejsza niż
    to maksimum dawałaby fałszywe alarmy przy pierwszej powolnej świecy."""
    assert ZWLOKA_DECYZJI_MIN >= 90


# --------------------------------------------------------------- przebieg
class Atrapa:
    def __init__(self, wiersze, uniwersum=10, zgloszone=False):
        self.wiersze = wiersze
        self.uniwersum = uniwersum
        self.zgloszone = zgloszone
        self.zapytania = []

    async def fetch_all(self, sql, params=None):
        self.zapytania.append(sql)
        if "risk.engine_heartbeat" in sql:
            return []
        if "FROM ref.instrument" in sql and "journal.decision" not in sql:
            return [{"id": i, "symbol": f"S{i}"} for i in range(self.uniwersum)]
        if "unnest" in sql:
            return self.wiersze
        if "NOT EXISTS" in sql:
            return [{"symbol": "NEARUSDC"}]
        if "notify.notification" in sql:
            return [{"x": 1}] if self.zgloszone else []
        if 'app."user"' in sql:
            return [{"id": 1}]
        raise AssertionError(sql)


def _uruchom(atrapa, **kw):
    wyslane = []

    async def notify(uid, kod, tytul, tresc, **reszta):
        wyslane.append({"kod": kod, "tytul": tytul, **reszta})

    kod = asyncio.run(sprawdz_decyzje(atrapa.fetch_all, notify, teraz=TERAZ, **kw))
    return kod, wyslane


def test_przebieg_zdrowy_zero_i_cisza(capsys):
    kod, wyslane = _uruchom(Atrapa([_w(B, 10, 10), _w(B - H4, 10, 10)]))
    assert (kod, wyslane) == (0, [])
    assert "komplet" in capsys.readouterr().out


def test_przebieg_z_brakami_wysyla_warning():
    kod, wyslane = _uruchom(Atrapa([_w(B, 9, 10), _w(B - H4, 9, 10)]))
    assert kod == 1
    assert [w["kod"] for w in wyslane] == ["engine.brak_decyzji"]
    assert wyslane[0]["severity"] == "warning"


def test_przebieg_zgloszony_w_ciagu_doby_nie_powtarza():
    kod, wyslane = _uruchom(Atrapa([_w(B, 9, 10), _w(B - H4, 9, 10)],
                                   zgloszone=True))
    assert (kod, wyslane) == (0, [])


def test_tryb_suchy_nie_wysyla():
    kod, wyslane = _uruchom(Atrapa([_w(B, 0, 10), _w(B - H4, 0, 10)]), sucho=True)
    assert kod == 1 and wyslane == []


# --------------------------------------------------------------- wpięcie
def test_typ_zdarzenia_w_migracji():
    sql = "\n".join(p.read_text(encoding="utf-8")
                    for p in sorted((KORZEN / "db/migrations").glob("*.sql")))
    assert "'engine.brak_decyzji'" in sql


def test_czujka_podlaczona_do_wyboru_co_i_do_zywotnosci():
    zrodlo = (KORZEN / "services/engine/dozor.py").read_text(encoding="utf-8")
    assert '"decyzje"' in zrodlo
    chart = (KORZEN / "charts/trader/templates/zywotnosc-cron.yaml").read_text(
        encoding="utf-8")
    assert "dozor.py --co decyzje" in chart


DB = os.environ.get("TEST_DB_URL")


@pytest.mark.skipif(not DB, reason="ustaw TEST_DB_URL na jednorazowa baze")
def test_zapytania_przechodza_przez_postgresa(monkeypatch):
    """Atrapa nie wykonuje SQL-a — ten test sprawdza, że Postgres go przyjmuje."""
    monkeypatch.setenv("DATABASE_URL", DB)
    from services.api import db as api_db

    async def przebieg():
        await api_db.open_pool()
        try:
            async def notify(*a, **kw):
                raise AssertionError("pusta baza nie powinna alarmować")
            return await sprawdz_decyzje(api_db.fetch_all, notify, sucho=True)
        finally:
            await api_db.close_pool()

    assert asyncio.run(przebieg()) == 0
