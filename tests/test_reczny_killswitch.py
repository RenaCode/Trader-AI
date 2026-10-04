"""Ręczny kill-switch z panelu (decyzja właściciela 2026-10-04).

Ta sama ścieżka co automat — wiersz w `risk.killswitch_event` bez
`released_at`, który silnik czyta w każdym cyklu — z wpisem w audycie
i alarmem krytycznym (dzwonek + bramka). Na produkcji tego się NIE wyzwala;
pilnują tego te testy.

KIERUNEK OCHRONY (przegląd 2026-10-04): zatrzymanie BEZ hasła i bez blokady
konta (pięć złych logowań nie może odebrać właścicielowi hamulca), zwolnienie
Z hasłem (przejęta sesja nie może wznowić handlu po `max_drawdown`).
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from services.api.routers import risk
from services.engine import wznowienie

UZYTKOWNIK = {"id": 7, "login": "wlasciciel", "role": "owner", "session_id": 3}


class Atrapa:
    def __init__(self, *, puls="demo", aktywny=False, haslo_ok=True, blokada=False,
                 zdarzenie=None):
        self.puls = puls
        self.aktywny = aktywny
        self.haslo_ok = haslo_ok
        self.blokada = blokada
        self.zdarzenie = zdarzenie
        self.wstawione = []
        self.zwolnione = []
        self.audyt = []
        self.alarmy = []
        self.hasla = []

    async def fetch_one(self, sql, params=None):
        if "FROM risk.killswitch_event WHERE id" in sql:
            return self.zdarzenie
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

    async def execute(self, sql, params=None):
        assert "UPDATE risk.killswitch_event" in sql
        self.zwolnione.append(params)

    async def audit(self, akcja, **kw):
        self.audyt.append((akcja, kw))

    async def potwierdz_haslo(self, user, haslo, request, *, akcja, status, komunikat):
        """Jak `auth.potwierdz_haslo`: blokada konta odmawia PRZED sprawdzeniem."""
        self.hasla.append((haslo, akcja, status))
        if self.blokada:
            raise HTTPException(429, "Konto zablokowane")
        if not self.haslo_ok:
            raise HTTPException(status, komunikat)

    async def powiadom(self, user_id, kod, tytul, tresc="", **kw):
        self.alarmy.append((user_id, kod, tytul, tresc, kw))


def _podstaw(monkeypatch, atrapa):
    for nazwa, f in (("fetch_one", atrapa.fetch_one), ("fetch_all", atrapa.fetch_all),
                     ("execute", atrapa.execute),
                     ("audit", atrapa.audit), ("potwierdz_haslo", atrapa.potwierdz_haslo),
                     ("powiadom_o_bezpieczenstwie", atrapa.powiadom)):
        monkeypatch.setattr(risk, nazwa, f)


def wlacz(monkeypatch, atrapa, engine="crypto", powod="sprawdzam podejrzany ruch na rachunku"):
    _podstaw(monkeypatch, atrapa)
    body = risk.RecznyKillSwitchBody(powod=powod)
    return asyncio.run(risk.wlacz_killswitch(engine, body, request=None, user=UZYTKOWNIK))


AKTYWNE = {"id": 501, "engine": "crypto", "reason": "max_drawdown", "released_at": None}


def zwolnij(monkeypatch, atrapa, engine="crypto", haslo="tajne",
            note="obsunięcie wyjaśnione, dane świec poprawione"):
    _podstaw(monkeypatch, atrapa)
    body = risk.ZwolnienieBody(note=note, haslo=haslo)
    return asyncio.run(risk.zwolnij_killswitch(engine, 501, body, request=None,
                                               user=UZYTKOWNIK))


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


def test_zatrzymanie_NIE_pyta_o_haslo(monkeypatch):
    a = Atrapa(haslo_ok=False)
    assert wlacz(monkeypatch, a)["ok"]
    assert a.hasla == [] and a.wstawione


def test_zablokowane_konto_NIE_blokuje_zatrzymania(monkeypatch):
    """Pięć złych logowań z dowolnego miejsca blokuje konto na 15 minut.
    Do 2026-10-04 przez te 15 minut nie dało się zatrzymać handlu z panelu."""
    a = Atrapa(blokada=True)
    odp = wlacz(monkeypatch, a)
    assert odp["ok"] and a.wstawione and a.audyt and a.alarmy


def test_zwolnienie_WYMAGA_hasla_i_bez_niego_nic_nie_robi(monkeypatch):
    a = Atrapa(haslo_ok=False, zdarzenie=AKTYWNE)
    with pytest.raises(HTTPException) as e:
        zwolnij(monkeypatch, a)
    # 403, nie 401: front na 401 wylogowuje, a sesja JEST wazna.
    assert e.value.status_code == 403
    assert a.hasla == [("tajne", "risk.killswitch_released", 403)]
    assert not a.zwolnione and not a.audyt


def test_zwolnienie_przy_zablokowanym_koncie_odmawia(monkeypatch):
    a = Atrapa(blokada=True, zdarzenie=AKTYWNE)
    with pytest.raises(HTTPException) as e:
        zwolnij(monkeypatch, a)
    assert e.value.status_code == 429 and not a.zwolnione


def test_zwolnienie_z_haslem_zapisuje_kto_i_audyt(monkeypatch):
    a = Atrapa(zdarzenie=AKTYWNE)
    assert zwolnij(monkeypatch, a)["ok"]
    (kto, notatka, eid), = a.zwolnione
    assert (kto, eid) == (7, 501) and "obsunięcie" in notatka
    assert [akcja for akcja, _ in a.audyt] == ["risk.killswitch_released"]


def test_haslo_sprawdzane_ZANIM_odpowiedz_zdradzi_zdarzenie(monkeypatch):
    """404 dla nieistniejącego zdarzenia nie może iść przed hasłem."""
    a = Atrapa(haslo_ok=False, zdarzenie=None)
    with pytest.raises(HTTPException) as e:
        zwolnij(monkeypatch, a)
    assert e.value.status_code == 403


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


@pytest.mark.parametrize("wartosc", ["krótko", ""])
def test_powod_zatrzymania_jest_wymagany(wartosc):
    with pytest.raises(ValidationError):
        risk.RecznyKillSwitchBody(powod=wartosc)


@pytest.mark.parametrize("pole,wartosc", [("note", "krótko"), ("haslo", "")])
def test_powod_i_haslo_zwolnienia_sa_wymagane(pole, wartosc):
    dane = {"note": "dane świec poprawione w sha-abc", "haslo": "x", pole: wartosc}
    with pytest.raises(ValidationError):
        risk.ZwolnienieBody(**dane)


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


def _komponent(zrodlo: str, nazwa: str) -> str:
    i = zrodlo.index(f"function {nazwa}(")
    return zrodlo[i:re.search(r"\n(export default )?function ", zrodlo[i:]).start() + i]


def test_interfejs_zatrzymuje_bez_hasla_a_zwalnia_z_haslem():
    zrodlo = Path("web/src/components/Risk.jsx").read_text(encoding="utf-8")
    wlacz_ui = _komponent(zrodlo, "WlaczKillSwitch")
    zwolnij_ui = _komponent(zrodlo, "ZwolnijKillSwitch")
    assert "`/api/engines/${engine}/risk/killswitch`" in wlacz_ui
    assert 'type="password"' not in wlacz_ui and "haslo" not in wlacz_ui
    assert "/release`" in zwolnij_ui
    assert 'type="password"' in zwolnij_ui and "{ note: note.trim(), haslo }" in zwolnij_ui
    assert "triggered_by" in zrodlo, "historia ma mówić, kto zatrzymał ręcznie"
