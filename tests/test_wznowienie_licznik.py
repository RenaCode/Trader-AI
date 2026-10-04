"""Alarmy automatu wznowienia: raz na zdarzenie i zwolnienie przez bramkę.

PRZEGLĄD 2026-10-04 (T-3, T-4, T-6)

1. „Kill-switch wraca w kółko" szło przy KAŻDYM przebiegu dozorcy (co 60 s),
   dopóki licznik dobowy nie opadł. Komentarz zakładał tłumienie w `notify()`,
   ale `killswitch.triggered` jest `always_on`, a `always_on` omija cooldown —
   do ~1440 alarmów krytycznych na dobę. Teraz alarm idzie raz na zdarzenie:
   znacznikiem jest samo powiadomienie (`data.event_id` + `rodzaj='licznik'`).
2. Automatyczne zwolnienie szło jako `killswitch.triggered`/`warning`, czyli
   tylko do dzwonka. Handel wracał bez człowieka, a właściciel po alarmie
   o zatrzymaniu uważał, że stoi. Teraz `killswitch.released`/`critical`.
"""
from __future__ import annotations

import asyncio

from services.engine import wznowienie


class Baza:
    """Jedno aktywne zatrzymanie `max_daily_loss` z regułą `po_ustaniu`."""

    def __init__(self, *, wznowien_24h: int, max_wznowien: int = 3,
                 kapital: float = 100_000.0):
        self.wznowien_24h = wznowien_24h
        self.max_wznowien = max_wznowien
        self.kapital = kapital
        self.powiadomienia: list[dict] = []
        self.zwolnione: list = []

    async def fetch_all(self, sql, params=None):
        if "risk.killswitch_event" in sql and "released_at IS NULL" in sql:
            if self.zwolnione:
                return []
            return [{"id": 77, "reason": "Strata dzienna 3,2% > 3%",
                     "limit_name": "max_daily_loss", "triggered_at": None,
                     "wiek_min": 120.0}]
        if 'app."user"' in sql:
            return [{"id": 1}]
        raise AssertionError(f"nieoczekiwane zapytanie: {sql}")

    async def fetch_one(self, sql, params=None):
        if "notify.notification" in sql:
            assert "data->>'event_id' = %s" in sql and "'licznik'" in sql, sql
            eid, = params
            return {"jest": 1} if any(
                p["kod"] == "killswitch.triggered"
                and str(p["data"].get("event_id")) == eid
                and p["data"].get("rodzaj") == "licznik"
                for p in self.powiadomienia) else None
        if "risk.limit_def" in sql:
            return {"value": 3, "wznowienie": "po_ustaniu",
                    "max_wznowien": self.max_wznowien}
        if "released_auto" in sql:
            return {"n": self.wznowien_24h}
        return {"equity": self.kapital, "nav": 1.0, "peak": self.kapital,
                "day_ago": self.kapital, "szczyt": 1.0, "wplaty_netto": self.kapital,
                "event_time": None}

    async def execute(self, sql, params=None):
        if "released_at = now()" in sql:
            self.zwolnione.append(params)

    async def notify(self, user_id, kod, tytul, tresc="", **kw):
        self.powiadomienia.append({"kod": kod, "tytul": tytul, "tresc": tresc,
                                   "severity": kw.get("severity"),
                                   "data": kw.get("data") or {}})


def _wznow(baza, **kw):
    return asyncio.run(wznowienie.wznow(baza.fetch_all, baza.fetch_one, baza.execute,
                                        baza.notify, "crypto", "demo",
                                        dziennik=wznowienie.Dziennik(), **kw))


def test_licznik_wyczerpany_DWA_przebiegi_daja_JEDEN_alarm():
    baza = Baza(wznowien_24h=3)
    _wznow(baza)
    _wznow(baza)
    assert len(baza.powiadomienia) == 1, baza.powiadomienia
    (p,) = baza.powiadomienia
    assert p["kod"] == "killswitch.triggered" and p["severity"] == "critical"
    assert p["data"]["rodzaj"] == "licznik" and p["data"]["event_id"] == 77
    assert not baza.zwolnione


def test_licznik_w_trybie_suchym_niczego_nie_wysyla():
    baza = Baza(wznowien_24h=3)
    _wznow(baza, sucho=True)
    assert baza.powiadomienia == []


def test_zwolnienie_automatyczne_to_killswitch_released_KRYTYCZNY():
    baza = Baza(wznowien_24h=1)
    _wznow(baza)
    assert len(baza.zwolnione) == 1
    (p,) = baza.powiadomienia
    assert p["kod"] == "killswitch.released" and p["severity"] == "critical"
    assert "AUTOMATYCZNIE" in p["tytul"] and "max_daily_loss" in p["tytul"]
    assert p["data"]["reczny"] is False and p["data"]["event_id"] == 77
    assert p["data"]["wznowien_24h"] == 2
    assert "najwyżej 3" in p["tresc"]
