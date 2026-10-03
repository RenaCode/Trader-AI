"""Uzgodnienie bez klucza giełdy kończy się kodem NIEWYKONALNE (2), nie zgodnością (0).

Do 2026-10-03 nad `return 0` stał komentarz „BRAK KLUCZA TO NIE JEST
ZGODNOŚĆ" — kod robił dokładnie to, przed czym ostrzegał. Job czujki kończył się
na zielono, a jedynym śladem była linia w logu (audyt 2026-10-03, G3).
"""
from __future__ import annotations

import asyncio
import sys

from services.engine import uzgodnienie


def test_brak_klucza_to_kod_niewykonalne(monkeypatch):
    import services.api.db as db
    import services.exchanges.mozliwosci as mozliwosci

    async def nic(*a, **k):
        return []

    async def bez_puli(*a, **k):
        return None

    monkeypatch.setattr(db, "open_pool", bez_puli)
    monkeypatch.setattr(db, "close_pool", bez_puli)
    monkeypatch.setattr(db, "fetch_all", nic)
    monkeypatch.setattr(mozliwosci, "wybierz_klucz", lambda wiersze, mode: (None, None))
    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(sys, "argv", ["uzgodnienie.py", "--kind", "spot_eu"])
    assert asyncio.run(uzgodnienie.main()) == 2
