"""Finansowanie i pozycjonowanie ponawiają Z LICZNIKIEM — tak jak świece od 23.09.

`backfill._fetch_range` dostał licznik prób 23.09.2026
(tests/test_ponawianie_i_limit_cyklu_historii.py). Dwie bliźniacze pętle
w tym samym cyklu `history` — `funding._fetch_range` i
`pozycjonowanie._pobierz_okno` — zostały przy `sleep(5); continue` bez końca.
Okno serwisowe giełdy (5xx, limit zapytań: `retryable=True`) kręciło nimi co
pięć sekund, cykl się nie kończył, puls nie bił, a licznik awarii nie miał
czego liczyć.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from services.exchanges.base import ExchangeError
from services.ingest import backfill, funding, pozycjonowanie


@pytest.fixture
def bez_czekania(monkeypatch):
    oryginalny = asyncio.sleep

    async def natychmiast(_ile, *a, **kw):
        return await oryginalny(0)

    monkeypatch.setattr(asyncio, "sleep", natychmiast)


class _GieldaWOknieSerwisowym:
    def __init__(self, retryable: bool = True, ozdrowiej_po: int | None = None):
        self.retryable = retryable
        self.ozdrowiej_po = ozdrowiej_po
        self.zapytan = 0

    async def _odpowiedz(self):
        self.zapytan += 1
        if self.ozdrowiej_po is not None and self.zapytan > self.ozdrowiej_po:
            return []
        raise ExchangeError("503 Service Unavailable", retryable=self.retryable)

    async def fetch_funding(self, symbol, start, end):
        return await self._odpowiedz()

    async def fetch_positioning(self, symbol, start, end):
        return await self._odpowiedz()


def _okno():
    teraz = datetime.now(timezone.utc)
    return teraz - timedelta(days=2), teraz


def _funding(g):
    start, teraz = _okno()

    async def bieg():
        async with asyncio.timeout(5):
            return await funding._fetch_range(g, None, None, 1, "BTCUSDT",
                                              start, teraz, teraz)
    return asyncio.run(bieg())


def _pozycjonowanie(g):
    start, teraz = _okno()

    async def bieg():
        async with asyncio.timeout(5):
            return await pozycjonowanie._pobierz_okno(g, None, None, 1, "BTCUSDT",
                                                      start, teraz, teraz)
    return asyncio.run(bieg())


@pytest.mark.parametrize("pobierz", [_funding, _pozycjonowanie],
                         ids=["finansowanie", "pozycjonowanie"])
def test_ponawianie_MA_KONIEC(bez_czekania, pobierz):
    g = _GieldaWOknieSerwisowym()
    with pytest.raises(ExchangeError) as e:
        pobierz(g)
    assert "prób" in str(e.value)
    assert g.zapytan == backfill.PROBY_PONOWIEN + 1


@pytest.mark.parametrize("pobierz", [_funding, _pozycjonowanie],
                         ids=["finansowanie", "pozycjonowanie"])
def test_blad_nieponawialny_leci_od_razu(bez_czekania, pobierz):
    g = _GieldaWOknieSerwisowym(retryable=False)
    with pytest.raises(ExchangeError):
        pobierz(g)
    assert g.zapytan == 1


@pytest.mark.parametrize("pobierz", [_funding, _pozycjonowanie],
                         ids=["finansowanie", "pozycjonowanie"])
def test_krotka_przerwa_przechodzi(bez_czekania, pobierz):
    """Kanarek: kilka błędów pod rząd, potem odpowiedź — cykl ma się udać."""
    g = _GieldaWOknieSerwisowym(ozdrowiej_po=3)
    assert pobierz(g) == 0
