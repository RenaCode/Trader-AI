"""Ręczny kill-switch z panelu (decyzja właściciela 2026-10-04).

Ta sama ścieżka co automat — wiersz w `risk.killswitch_event` bez
`released_at`, który silnik czyta w każdym cyklu — z hasłem, wpisem w audycie
i alarmem krytycznym (dzwonek + bramka). Na produkcji tego się NIE wyzwala;
pilnują tego te testy.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from services.api.routers import risk
from services.engine import wznowienie

UZYTKOWNIK = {"id": 7, "login": "wlasciciel", "role": "owner", "session_id": 3}


class Atrapa:
    def __init__(self, *, puls="demo", aktywny=False, haslo_ok=True):
        self.puls = puls
        self.aktywny = aktywny
        self.haslo_ok = haslo_ok
        self.wstawione = []
        self.audyt = []
        self.alarmy = []
        self.hasla = []

    async def fetch_one(self, sql, params=None):
        if "risk.engine_heartbeat" in sql:
            return {"mode": self.puls} if self.puls else None
        if "INSERT INTO risk.killswitch_event" in sql:
            assert "NOT EXISTS" in sql and "released_at IS NULL" in sql
            if self.aktywny:
                return None
            self.wstawione.append(params)
            return {"id": 501, "triggered_at": __import__("datetime").datetime(2026, 10, 4)}
        raise AssertionError(sql)

    async def fetch_all(self, sql, params=None):
        assert 'app."user"' in sql
        return [{"id": 7}, {"id": 8}]

    async def audit(self, akcja, **kw):
        self.audyt.append((akcja, kw))

    async def potwierdz_haslo(self, user, haslo, request, *, akcja, status, komunikat):
        self.hasla.append((haslo, akcja, status))
        if not self.haslo_ok:
            raise HTTPException(status, komunikat)

    async def powiadom(self, user_id, kod, tytul, tresc="", **kw):
        self.alarmy.append((user_id, kod, tytul, tresc, kw))


def wlacz(monkeypatch, atrapa, engine="crypto", powod="sprawdzam podejrzany ruch na rachunku",
          haslo="tajne"):
    for nazwa, f in (("fetch_one", atrapa.fetch_one), ("fetch_all", atrapa.fetch_all),
                     ("audit", atrapa.audit), ("potwierdz_haslo", atrapa.potwierdz_haslo),
                     ("powiadom_o_bezpieczenstwie", atrapa.powiadom)):
        monkeypatch.setattr(risk, nazwa, f)
    body = risk.RecznyKillSwitchBody(powod=powod, haslo=haslo)
    return asyncio.run(risk.wlacz_killswitch(engine, body, request=None, user=UZYTKOWNIK))


def test_zapisuje_TEN_SAM_wiersz_co_automat_z_tym_kto(monkeypatch):
    a = Atrapa()
    odp = wlacz(monkeypatch, a)
    assert odp["ok"] and odp["id"] == 501 and odp["mode"] == "demo"
    (engine, mode, reason, limit, kto, *_), = a.wstawione
    assert (engine, mode, limit, kto) == ("crypto", "demo", risk.LIMIT_RECZNY, 7)
    assert "wlasciciel" in reason and "podejrzany ruch" in reason


def test_tryb_bierze_z_pulsu_silnika(monkeypatch):
    a = Atrapa(puls="live")
    assert wlacz(monkeypatch, a)["mode"] == "live"
    assert a.wstawione[0][1] == "live"


def test_bez_pulsu_silnika_odmawia(monkeypatch):
    a = Atrapa(puls=None)
    with pytest.raises(HTTPException) as e:
        wlacz(monkeypatch, a)
    assert e.value.status_code == 409 and not a.wstawione and not a.alarmy


def test_wymaga_hasla_i_bez_niego_nic_nie_robi(monkeypatch):
    a = Atrapa(haslo_ok=False)
    with pytest.raises(HTTPException) as e:
        wlacz(monkeypatch, a)
    # 403, nie 401: front na 401 wylogowuje, a sesja JEST wazna.
    assert e.value.status_code == 403
    assert a.hasla == [("tajne", "risk.killswitch_manual", 403)]
    assert not a.wstawione and not a.audyt and not a.alarmy


def test_drugie_zatrzymanie_przy_aktywnym_to_409(monkeypatch):
    a = Atrapa(aktywny=True)
    with pytest.raises(HTTPException) as e:
        wlacz(monkeypatch, a)
    assert e.value.status_code == 409 and not a.audyt and not a.alarmy


def test_zostawia_slad_w_audycie_i_wysyla_alarm_krytyczny(monkeypatch):
    a = Atrapa()
    wlacz(monkeypatch, a)
    (akcja, kw), = a.audyt
    assert akcja == "risk.killswitch_manual" and kw["entity_id"] == "501"
    assert kw["user_id"] == 7
    # Do kazdego aktywnego odbiorcy - jak alarm dozoru; bramka tlumi duplikat.
    assert [u for u, *_ in a.alarmy] == [7, 8]
    _, kod, tytul, tresc, kw = a.alarmy[0]
    assert kod == "killswitch.triggered" and kw["severity"] == "critical"
    assert kw["engine"] == "crypto" and kw["data"]["event_id"] == 501
    assert "ręcznie" in tytul and "wlasciciel" in tresc


@pytest.mark.parametrize("pole,wartosc", [("powod", "krótko"), ("powod", ""), ("haslo", "")])
def test_powod_i_haslo_sa_wymagane(pole, wartosc):
    dane = {"powod": "sprawdzam podejrzany ruch", "haslo": "x", pole: wartosc}
    with pytest.raises(ValidationError):
        risk.RecznyKillSwitchBody(**dane)


def test_nieznany_silnik_odpada_walidacja():
    """`Silnik` to Literal - 422 zamiast bledu rzutowania enuma w bazie."""
    from typing import get_args
    assert set(get_args(risk.Silnik)) == {"crypto", "equity"}


def test_automat_wznowienia_NIE_zwalnia_recznego(capsys):
    """`limit_name = 'reczny'` nie ma wiersza w `risk.limit_def`, wiec
    `wznow` zostawia zdarzenie czlowiekowi - nie zwolni go po karencji."""
    zwolnione = []

    async def fetch_all(sql, params=None):
        if "FROM risk.killswitch_event" in sql:
            return [{"id": 501, "reason": "Ręczne zatrzymanie", "limit_name": risk.LIMIT_RECZNY,
                     "triggered_at": None, "wiek_min": 999}]
        return []

    async def fetch_one(sql, params=None):
        if "risk.limit_def" in sql:
            return None
        return {"equity": 1000, "peak": 1000, "day_ago": 1000, "nav": 1000,
                "nav_peak": 1000, "nav_day_ago": 1000}

    async def execute(sql, params=None):
        zwolnione.append(sql)

    async def notify(*a, **kw):
        raise AssertionError("brak powodu do powiadomienia")

    async def stan(*a, **kw):
        return {"equity": 1000}

    oryginalny = wznowienie.stan_konta
    wznowienie.stan_konta = stan
    try:
        asyncio.run(wznowienie.wznow(fetch_all, fetch_one, execute, notify, "crypto", "demo",
                                     dziennik=wznowienie.Dziennik()))
    finally:
        wznowienie.stan_konta = oryginalny
    assert not zwolnione
    assert "zostaje ręczne" in capsys.readouterr().out


def test_endpoint_chroniony_csrf_i_rola_wlasciciela():
    zrodlo = Path("services/api/routers/risk.py").read_text(encoding="utf-8")
    i = zrodlo.index('@router.post("/killswitch",')
    dekorator = zrodlo[i:zrodlo.index("async def wlacz_killswitch", i)]
    assert "require_csrf" in dekorator and "wymagaj_wlasciciela" in dekorator


def test_interfejs_ma_czym_wlaczyc_i_pyta_o_haslo():
    zrodlo = Path("web/src/components/Risk.jsx").read_text(encoding="utf-8")
    assert "`/api/engines/${engine}/risk/killswitch`" in zrodlo
    assert 'type="password"' in zrodlo and "haslo" in zrodlo
    assert "triggered_by" in zrodlo, "historia ma mówić, kto zatrzymał ręcznie"
