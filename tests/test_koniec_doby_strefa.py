"""Zamknięcia dobowe nie zależą od strefy sesji bazy (przegląd 2026-10-04, T-18).

`_koniec_doby` sprawdzało `.hour == 0` na znacznikach z bazy. W strefie innej
niż UTC te same świece dawały 0 zamknięć dobowych zamiast 60, a filtr trendu
po cichu przechodził na gotówkę.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from services.engine.baseline import _koniec_doby, _zamkniecia_dobowe

BAR = 240


def _swiece(strefa) -> list[dict]:
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    return [{"t": (start + timedelta(minutes=BAR * i)).astimezone(strefa),
             "price": 100.0 + i} for i in range(6 * 60)]


def test_te_same_swiece_w_UTC_i_Warszawie_daja_TE_SAME_zamkniecia():
    do = datetime(2026, 10, 1, tzinfo=timezone.utc)
    utc = _zamkniecia_dobowe(_swiece(timezone.utc), do, BAR)
    waw = _zamkniecia_dobowe(_swiece(ZoneInfo("Europe/Warsaw")), do, BAR)
    assert len(utc) == 60
    assert waw == utc


def test_naiwny_znacznik_traktowany_jak_UTC():
    assert _koniec_doby(datetime(2026, 10, 3, 20, 0), BAR)
    assert not _koniec_doby(datetime(2026, 10, 3, 16, 0), BAR)
    # 22:00 w Warszawie (CEST) = 20:00 UTC, świeca 4h kończy się o północy UTC.
    assert _koniec_doby(datetime(2026, 10, 3, 22, 0, tzinfo=ZoneInfo("Europe/Warsaw")), BAR)
