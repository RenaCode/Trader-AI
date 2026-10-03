"""Drobne poprawki API z audytu Fable (U-11, 2026-10-03).

1. FastAPI wystawiał `/docs` i `/openapi.json` - pełna mapa API dla każdego,
   kto dojdzie do procesu (z zewnątrz zasłaniał to nginx, nie aplikacja).
2. `/api/health` jest publiczny i oddawał `{"migrations": 72}` - wersję schematu
   anonimowemu odwiedzającemu.
3. Eksport CSV (cała historia transakcji) szedł bez śladu w `app.audit_log`
   i dla każdej ważnej sesji, nie tylko właściciela.
4. Sesja bezczynna > 30 min była listowana jako aktywna, dopóki nikt jej nie
   użył (unieważnienie zachodzi dopiero przy następnym żądaniu).
"""
from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path

import pytest

KORZEN = Path(__file__).resolve().parents[1]


def test_brak_docs_i_openapi():
    from services.api.main import app

    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None


def test_health_nie_zdradza_liczby_migracji(monkeypatch):
    from services.api import main

    async def fetch_one(sql, params=None):
        return {"n": 72, "ok": 1}

    monkeypatch.setattr(main, "fetch_one", fetch_one)
    odp = asyncio.run(main.health())
    assert odp == {"status": "ok"}


def test_panel_nie_czyta_liczby_migracji():
    tekst = (KORZEN / "web/src/App.jsx").read_text(encoding="utf-8")
    assert "health.data.migrations" not in tekst


@pytest.mark.parametrize("nazwa", ["wypelnienia", "pozycje"])
def test_eksport_tylko_wlasciciel_i_z_audytem(monkeypatch, nazwa):
    from services.api.auth import wymagaj_wlasciciela
    from services.api.routers import eksport

    funkcja = getattr(eksport, nazwa)
    domyslne = [p.default for p in inspect.signature(funkcja).parameters.values()]
    assert any(getattr(d, "dependency", None) is wymagaj_wlasciciela
               for d in domyslne), f"{nazwa}: eksport bez wymagaj_wlasciciela"

    zapisane = []

    async def audit(akcja, **kw):
        zapisane.append((akcja, kw))

    async def fetch_all(sql, params=None):
        return []

    monkeypatch.setattr(eksport, "audit", audit)
    monkeypatch.setattr(eksport, "fetch_all", fetch_all)
    asyncio.run(funkcja(engine="crypto", request=None, user={"id": 7},
                        mode="demo", od=None, do=None, wariant="csv"))
    assert [a for a, _ in zapisane] == [f"eksport.{nazwa}"]
    assert zapisane[0][1]["user_id"] == 7
    assert zapisane[0][1]["after"]["wierszy"] == 0


@pytest.mark.parametrize("modul", ["account", "auth"])
def test_lista_sesji_pomija_bezczynne(monkeypatch, modul):
    import importlib

    m = importlib.import_module(f"services.api.routers.{modul}")
    zapytania = []

    async def fetch_all(sql, params=None):
        zapytania.append((sql, params))
        return []

    monkeypatch.setattr(m, "fetch_all", fetch_all)
    asyncio.run(m.sessions(user={"id": 1, "session_id": 2}))
    sql, params = zapytania[0]
    assert "coalesce(last_seen_at, created_at)" in sql
    assert 1800 in params, "próg bezczynności nie z SESSION_IDLE"


DB = os.environ.get("TEST_DB_URL")


@pytest.mark.skipif(not DB, reason="ustaw TEST_DB_URL na jednorazowa baze")
def test_zapytanie_sesji_przechodzi_przez_postgresa():
    import psycopg

    from services.api.auth import SQL_SESJE_AKTYWNE

    with psycopg.connect(DB) as c:
        assert c.execute(SQL_SESJE_AKTYWNE, (2, 1, 1800)).fetchall() == []
