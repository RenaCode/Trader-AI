"""Przepływ kapitału pod hasłem, z sufitem i z alarmem (przegląd 2026-10-04, T-7).

Kapitał crypto/demo liczy się z przepływów, więc wpis wpłaty podnosi każdy
limit liczony od kapitału. Do tej poprawki wystarczała sama sesja: wpłata
900 000 przy ~100 000 na rachunku dawała pozycje po ~50 000.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal as D
from typing import get_args

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from services.api import auth as modul_auth
from services.api import security
from services.api.routers import eksport, kapital

UZYTKOWNIK = {"id": 7, "login": "wlasciciel", "role": "owner", "session_id": 3}


class Atrapa:
    def __init__(self, *, haslo_ok=True, kapital_=D("99530"), wplaty=D("100000"),
                 punkt=True):
        self.haslo_ok = haslo_ok
        self.kapital = kapital_
        self.wplaty = wplaty
        self.punkt = punkt
        self.hasla = []
        self.zapisy = []
        self.alarmy = []

    async def fetch_one(self, sql, params=None):
        if "exec.equity_point" in sql:
            return {"nav": D("0.9953"), "equity": self.kapital} if self.punkt else None
        if "exec.cash_flow" in sql:
            return {"wplaty": self.wplaty}
        raise AssertionError(sql)

    async def fetch_all(self, sql, params=None):
        assert 'app."user"' in sql
        return [{"id": 7}]

    async def execute(self, sql, params=None):
        assert "INSERT INTO exec.cash_flow" in sql
        self.zapisy.append(params)

    async def audit(self, *a, **kw):
        pass

    async def potwierdz_haslo(self, user, haslo, request, *, akcja, status, komunikat):
        self.hasla.append((haslo, akcja, status))
        if not self.haslo_ok:
            raise HTTPException(status, komunikat)

    async def powiadom(self, user_id, kod, tytul, tresc="", **kw):
        self.alarmy.append((kod, tytul, tresc, kw))


def wplac(monkeypatch, a, amount, haslo="tajne", engine="crypto"):
    for nazwa, f in (("fetch_one", a.fetch_one), ("fetch_all", a.fetch_all),
                     ("execute", a.execute), ("audit", a.audit),
                     ("potwierdz_haslo", a.potwierdz_haslo),
                     ("powiadom_o_bezpieczenstwie", a.powiadom)):
        monkeypatch.setattr(kapital, nazwa, f)
    body = kapital.PrzeplywBody(amount=D(amount), note="zasilenie testowe", haslo=haslo)
    return asyncio.run(kapital.dodaj_przeplyw(engine, body, request=None, user=UZYTKOWNIK))


def test_bez_hasla_odmowa_walidacji():
    with pytest.raises(ValidationError):
        kapital.PrzeplywBody(amount=D("100"), note="zasilenie")
    with pytest.raises(ValidationError):
        kapital.PrzeplywBody(amount=D("100"), note="zasilenie", haslo="")


def test_zle_haslo_403_i_nic_nie_zapisuje(monkeypatch):
    a = Atrapa(haslo_ok=False)
    with pytest.raises(HTTPException) as e:
        wplac(monkeypatch, a, "50000")
    assert e.value.status_code == 403
    assert a.hasla == [("tajne", "kapital.przeplyw", 403)]
    assert not a.zapisy and not a.alarmy


def test_tryb_live_odrzucony():
    with pytest.raises(ValidationError):
        kapital.PrzeplywBody(amount=D("100"), note="zasilenie", haslo="x", mode="live")


@pytest.mark.parametrize("kwota", ["1e12", "-1e12", "1e15"])
def test_kwota_poza_zakresem_odrzucona(kwota):
    with pytest.raises(ValidationError):
        kapital.PrzeplywBody(amount=D(kwota), note="zasilenie", haslo="x")


def test_wplata_ponad_biezacy_kapital_400(monkeypatch):
    a = Atrapa()
    with pytest.raises(HTTPException) as e:
        wplac(monkeypatch, a, "900000")
    assert e.value.status_code == 400 and "99530" in e.value.detail
    assert not a.zapisy and not a.alarmy


def test_wplata_w_granicach_zapisana_i_ALARM_krytyczny(monkeypatch):
    a = Atrapa()
    odp = wplac(monkeypatch, a, "50000")
    assert odp["ok"] and len(a.zapisy) == 1
    (kod, tytul, tresc, kw), = a.alarmy
    assert kod == "kapital.przeplyw" and kw["severity"] == "critical"
    assert kw["data"]["kto"] == "wlasciciel" and kw["data"]["amount"] == 50000.0
    assert kw["data"]["wplaty_netto"] == 150000.0
    assert "Wpłata" in tytul and "wlasciciel" in tresc


def test_pierwsza_wplata_na_pustym_rachunku_bez_sufitu(monkeypatch):
    a = Atrapa(wplaty=D("0"), punkt=False)
    assert wplac(monkeypatch, a, "100000")["ok"]
    assert len(a.zapisy) == 1


def test_nieznany_silnik_to_422_a_nie_500():
    assert set(get_args(kapital.Silnik)) == {"crypto", "equity"}
    import inspect
    for f in (kapital.dodaj_przeplyw, kapital.przeplywy,
              eksport.wypelnienia, eksport.pozycje):
        assert inspect.signature(f).parameters["engine"].annotation in ("Silnik", kapital.Silnik), f
    assert inspect.signature(kapital.przeplywy).parameters["mode"].annotation in ("Tryb",)
    assert inspect.signature(eksport.wypelnienia).parameters["mode"].annotation in ("Tryb",)


def test_eksport_mode_spoza_listy_to_422():
    """`mode` szedł nieescapowany do `Content-Disposition`."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(eksport.router)
    app.dependency_overrides[modul_auth.wymagaj_wlasciciela] = lambda: UZYTKOWNIK
    with TestClient(app) as c:
        prefiks = eksport.router.prefix.replace("{engine}", "crypto")
        assert c.get(f"{prefiks}/wypelnienia", params={"mode": "../x"}).status_code == 422
        assert c.get(eksport.router.prefix.replace("{engine}", "foo")
                     + "/wypelnienia").status_code == 422


def test_ciasteczka_maja_prefiks_Host_Secure_Path_i_BEZ_Domain():
    from fastapi import Response
    assert security.SESSION_COOKIE == "__Host-trader_session"
    assert modul_auth.CSRF_COOKIE == "__Host-trader_csrf"
    r = Response()
    modul_auth.set_session_cookies(r, "t", "c", 60)
    naglowki = [v.decode() for k, v in r.raw_headers if k == b"set-cookie"]
    assert len(naglowki) == 2
    for n in naglowki:
        assert n.startswith("__Host-trader_")
        assert "Secure" in n and "Path=/" in n and "domain=" not in n.lower()


def test_front_czyta_ciasteczko_CSRF_pod_nowa_nazwa():
    from pathlib import Path
    zrodlo = Path("web/src/utils/api.js").read_text(encoding="utf-8")
    assert "__Host-trader_csrf=" in zrodlo
