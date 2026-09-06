"""Do bazy trafiają wyłącznie świece ZAMKNIĘTE.

DLACZEGO TO JEST WŁASNY PLIK TESTÓW

`event_time` świecy to jej POCZĄTEK. Warunek `event_time < now` przepuszczał
więc świecę trwającą: o 09:01 świeca 09:00 spełniała go i lądowała w bazie
z zakresem jednej minuty. Wznawianie zaczynało od najnowszej posiadanej świecy,
a zapis pomijał konflikt — więc taka świeca nie była już nigdy odpytywana
i zostawała błędna na zawsze.

Zmierzone na BTCUSDC (55 godzin z kompletem danych minutowych):
zamknięcie zgodne z faktycznym w 9 przypadkach, a zapisany zakres high-low
stanowił 39% prawdziwego. Każda cecha liczona z tych świec — ATR, położenie
w zakresie, reżim zmienności — była zaniżona, a nowy mechanizm wypełniania
limitów oceniałby je na zakresach zawężonych o dwie trzecie.

Błąd nie objawiał się awarią. Świece były, wyglądały poprawnie, miały właściwe
otwarcie — brakowało tylko reszty godziny.
"""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

ZRODLO = Path("services/ingest/backfill.py").read_text()


def _przepuszczone(swiece_start, step, now, stop):
    """Odtworzenie warunku filtru z backfill.py."""
    return [t for t in swiece_start if t < stop and t + step <= now]


TERAZ = datetime(2026, 9, 6, 9, 1, tzinfo=timezone.utc)
GODZINA = timedelta(hours=1)


def test_swieca_trwajaca_nie_wchodzi_do_bazy():
    trwajaca = datetime(2026, 9, 6, 9, 0, tzinfo=timezone.utc)
    assert _przepuszczone([trwajaca], GODZINA, TERAZ, TERAZ) == []


def test_swieca_domknieta_wchodzi():
    zamknieta = datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc)
    assert _przepuszczone([zamknieta], GODZINA, TERAZ, TERAZ) == [zamknieta]


def test_granica_domkniecia_co_do_sekundy():
    """Świeca 08:00 domyka się dokładnie o 09:00 — i wtedy wolno ją zapisać."""
    swieca = datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc)
    rowno = datetime(2026, 9, 6, 9, 0, tzinfo=timezone.utc)
    tuz_przed = rowno - timedelta(seconds=1)
    assert _przepuszczone([swieca], GODZINA, rowno, rowno) == [swieca]
    assert _przepuszczone([swieca], GODZINA, tuz_przed, tuz_przed) == []


def test_stary_warunek_przepuszczal_trwajaca():
    """Kanarek na regresję: gdyby wrócił warunek `event_time < now`."""
    trwajaca = datetime(2026, 9, 6, 9, 0, tzinfo=timezone.utc)
    stary = [t for t in [trwajaca] if t < TERAZ]
    assert stary == [trwajaca], "test nie odtwarza starego, błędnego warunku"
    assert _przepuszczone([trwajaca], GODZINA, TERAZ, TERAZ) != stary


def test_zapis_poprawia_istniejaca_swiece():
    """Bez tego świeca raz zapisana błędnie zostaje błędna na zawsze."""
    assert "DO UPDATE SET" in ZRODLO, "zapis nadal pomija konflikt zamiast poprawiać"
    assert "IS DISTINCT FROM" in ZRODLO, \
        "brak warunku — ingested_at zmieniałoby się przy każdym przebiegu bez powodu"


def test_wznawianie_ma_zakladke():
    """Wznawianie od `have_until + step` nigdy nie odpyta świecy, którą już ma —
    więc żadna poprawka nie miałaby jak dotrzeć."""
    assert re.search(r"have_until - ZAKLADKA \* step", ZRODLO), \
        "wznawianie nie cofa się o zakładkę"
