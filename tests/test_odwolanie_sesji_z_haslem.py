"""Odwołanie pojedynczej sesji wymaga hasła (przegląd 2026-10-04, T-12 d).

Bez hasła przejęta sesja mogła w kółko wylogowywać właściciela, a on nie mógł
jej odwołać inaczej niż `revoke-others`, które hasła i tak wymagało.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from services.api.routers import account

UZYTKOWNIK = {"id": 7, "login": "wlasciciel", "role": "owner", "session_id": 3}


def _odwolaj(monkeypatch, haslo_ok: bool):
    wolania, zapisy = [], []

    async def potwierdz(user, haslo, request, *, akcja, status, komunikat):
        wolania.append((haslo, akcja, status))
        if not haslo_ok:
            raise HTTPException(status, komunikat)

    async def fetch_one(sql, params=None):
        return {"?column?": 1}

    async def execute(sql, params=None):
        zapisy.append(sql)

    async def audit(*a, **kw):
        pass

    for nazwa, f in (("potwierdz_haslo", potwierdz), ("fetch_one", fetch_one),
                     ("execute", execute), ("audit", audit)):
        monkeypatch.setattr(account, nazwa, f)
    body = account.RevokeBody(password="tajne")
    wynik = asyncio.run(account.revoke_session(9, body, request=None, user=UZYTKOWNIK))
    return wynik, wolania, zapisy


def test_bez_hasla_walidacja_odmawia():
    with pytest.raises(ValidationError):
        account.RevokeBody(password="")


def test_zle_haslo_403_i_sesja_zostaje(monkeypatch):
    with pytest.raises(HTTPException) as e:
        _odwolaj(monkeypatch, haslo_ok=False)
    assert e.value.status_code == 403


def test_dobre_haslo_odwoluje(monkeypatch):
    wynik, wolania, zapisy = _odwolaj(monkeypatch, haslo_ok=True)
    assert wynik["ok"] and wolania == [("tajne", "account.session_revoked", 403)]
    assert any("revoked_at = now()" in z for z in zapisy)


def test_front_wysyla_haslo_przy_pojedynczym_odwolaniu():
    zrodlo = Path("web/src/components/Profile.jsx").read_text(encoding="utf-8")
    assert "del(`/api/account/sessions/${id}`, { password })" in zrodlo
