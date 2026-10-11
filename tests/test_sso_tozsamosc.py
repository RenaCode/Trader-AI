"""SSO: sesja z naglowka Authelii (`Remote-User`) - renacode-infra docs/sso-tozsamosc.md.

CO SIE TU DZIEJE

Przy `SSO_NAGLOWKI=1` (chart: `sso.enabled`) zadanie z `Remote-User` od
Authelii dostaje sesje aplikacji dla ISTNIEJACEGO konta o tym loginie - bez
hasla i bez TOTP aplikacji, bo 2FA wymusila juz Authelia. Bez flagi naglowek
nie znaczy NIC: router bez middleware'u Authelii przepuszcza `Remote-User`
od klienta (sprawdzone w probie infra), wiec zaufanie bez flagi = kazdy, kto
zna login wlasciciela, jest wlascicielem. Pierwszy test w sekcji bazodanowej
pilnuje dokladnie tego.

Testy bazodanowe ida przez cale API (TestClient + prawdziwy Postgres po
migracjach, jak w CI), bo to, co tu sie liczy - ciasteczka z zaleznosci,
`sso` w `app.session`, adres w `app.audit_log` - atrapa odtworzylaby sama.
"""
from __future__ import annotations

import base64
import os
import re
import secrets
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import HTTPException
from starlette.datastructures import Headers

from services.api import sso

KORZEN = Path(__file__).resolve().parents[1]
DB = os.environ.get("TEST_DB_URL")
WYLOGUJ = "https://auth.renacode.com/logout?rd=https://trader.renacode.com/"
HASLO = "haslo-awaryjne-2026"
ADRES = "203.0.113.7"


# ------------------------------------------------------------ bez bazy
def _zadanie(*pary: tuple[str, str]):
    return SimpleNamespace(headers=Headers(raw=[(k.lower().encode(), v.encode())
                                                for k, v in pary]))


def test_flaga_wylaczona_naglowek_nic_nie_znaczy(monkeypatch):
    monkeypatch.delenv("SSO_NAGLOWKI", raising=False)
    z = _zadanie(("Remote-User", "marcin"), ("Remote-Groups", "wlasciciel"))
    assert sso.login_z_naglowka(z) is None
    # Tylko dokladne "1" wlacza zaufanie - "true", "yes" itd. to wylaczone.
    for wartosc in ("true", "0", "", "yes"):
        monkeypatch.setenv("SSO_NAGLOWKI", wartosc)
        assert sso.login_z_naglowka(z) is None, wartosc


def test_flaga_wlaczona_poprawny_naglowek(monkeypatch):
    monkeypatch.setenv("SSO_NAGLOWKI", "1")
    z = _zadanie(("Remote-User", "marcin"), ("Remote-Groups", "rodzina, wlasciciel"))
    assert sso.login_z_naglowka(z) == "marcin"
    assert sso.login_z_naglowka(_zadanie()) is None


@pytest.mark.parametrize("pary,kod", [
    ([("Remote-User", "marcin"), ("Remote-User", "intruz"), ("Remote-Groups", "wlasciciel")], 400),
    ([("Remote-User", "Marcin"), ("Remote-Groups", "wlasciciel")], 400),
    ([("Remote-User", "a b"), ("Remote-Groups", "wlasciciel")], 400),
    ([("Remote-User", ""), ("Remote-Groups", "wlasciciel")], 400),
    ([("Remote-User", "marcin,root"), ("Remote-Groups", "wlasciciel")], 400),
    ([("Remote-User", "marcin")], 403),
    ([("Remote-User", "marcin"), ("Remote-Groups", "rodzina")], 403),
    ([("Remote-User", "marcin"), ("Remote-Groups", "wlasciciele")], 403),
    # Konto administracyjne Authelii nie wchodzi do Tradera (decyzja 2026-10-11).
    ([("Remote-User", "admin"), ("Remote-Groups", "admins")], 403),
    ([("Remote-User", "marcin"), ("Remote-Groups", "wlasciciel"), ("Remote-Groups", "x")], 400),
])
def test_odmowy_naglowka(monkeypatch, pary, kod):
    monkeypatch.setenv("SSO_NAGLOWKI", "1")
    with pytest.raises(HTTPException) as e:
        sso.login_z_naglowka(_zadanie(*pary))
    assert e.value.status_code == kod


def test_nie_czyta_wariantow_naglowka(monkeypatch):
    """Kontrakt, pkt 2: `Remote_User` i `X-Remote-User` nie sa tozsamoscia."""
    monkeypatch.setenv("SSO_NAGLOWKI", "1")
    for nazwa in ("Remote_User", "X-Remote-User"):
        assert sso.login_z_naglowka(
            _zadanie((nazwa, "marcin"), ("Remote-Groups", "wlasciciel"))) is None


# ------------------------------------------------------------ chart
def _render(*ustaw: str) -> dict:
    argv = ["helm", "template", str(KORZEN / "charts" / "trader")]
    for u in ustaw:
        argv += ["--set", u]
    wynik = subprocess.run(argv, capture_output=True, text=True)
    assert wynik.returncode == 0, wynik.stderr
    for d in yaml.safe_load_all(wynik.stdout):
        if d and d["kind"] == "Deployment" and d["metadata"]["name"].endswith("api"):
            kontener = next(c for c in d["spec"]["template"]["spec"]["containers"]
                            if c["name"] == "api")
            return {e["name"]: e.get("value") for e in kontener["env"]}
    raise AssertionError("brak Deploymentu api w renderze")


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm niedostępny")
def test_chart_flaga_aplikacji_tylko_razem_z_middlewareami():
    """Jeden przelacznik: middleware'y SSO (test_chart_sso.py) i zaufanie do
    naglowka. Domyslnie zadnej zmiennej SSO w podzie."""
    bez = _render()
    assert not [k for k in bez if k.startswith("SSO_")]
    z = _render("sso.enabled=true")
    assert z["SSO_NAGLOWKI"] == "1"
    assert z["SSO_GRUPA"] == "wlasciciel"
    assert z["SSO_WYLOGUJ_URL"] == WYLOGUJ


def test_chart_flaga_pod_warunkiem_sso_statycznie():
    """To samo bez helma: zmienna stoi WEWNATRZ `if .Values.sso.enabled`."""
    tekst = (KORZEN / "charts/trader/templates/api.yaml").read_text(encoding="utf-8")
    blok = re.search(r"\{\{-? if \.Values\.sso\.enabled \}\}(.*?)\{\{-? end \}\}", tekst, re.S)
    assert blok and "SSO_NAGLOWKI" in blok.group(1)
    assert tekst.count("SSO_NAGLOWKI") == 1


# ------------------------------------------------------------ cale API + Postgres
bazodanowy = pytest.mark.skipif(not DB, reason="ustaw TEST_DB_URL na jednorazowa baze")


@pytest.fixture
def konta(monkeypatch):
    """Konta z losowym loginem; po tescie znikaja razem z audytem."""
    import psycopg

    from services.api.security import SecretBox, hash_password

    monkeypatch.setenv("DATABASE_URL", DB)
    monkeypatch.setenv("SECRETS_KEY", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setenv("SSO_WYLOGUJ_URL", WYLOGUJ)
    monkeypatch.delenv("SSO_GRUPA", raising=False)
    utworzone: list[int] = []

    def zaloz(*, totp: bool = False, aktywne: bool = True, zablokowane: bool = False) -> dict:
        login = f"sso-{uuid.uuid4().hex[:10]}"
        with psycopg.connect(DB, autocommit=True) as c:
            uid = c.execute(
                'INSERT INTO app."user" (login, password_hash, is_active, locked_until)'
                " VALUES (%s, %s, %s, CASE WHEN %s THEN now() + interval '10 minutes' END)"
                " RETURNING id",
                (login, hash_password(HASLO), aktywne, zablokowane)).fetchone()[0]
            if totp:
                c.execute("INSERT INTO app.totp_secret (user_id, secret_enc, confirmed_at)"
                          " VALUES (%s, %s, now())",
                          (uid, SecretBox().encrypt("JBSWY3DPEHPK3PXP")))
        utworzone.append(uid)
        return {"id": uid, "login": login}

    yield zaloz
    with psycopg.connect(DB, autocommit=True) as c:
        for uid in utworzone:
            c.execute("DELETE FROM app.audit_log WHERE actor_user_id = %s", (uid,))
            c.execute('DELETE FROM app."user" WHERE id = %s', (uid,))
        c.execute("DELETE FROM app.audit_log WHERE action = 'auth.sso_brak_konta'"
                  " AND entity_id LIKE 'sso-%%'")


def _klient():
    from fastapi.testclient import TestClient

    from services.api.main import app
    # https: ciasteczka `__Host-` sa `Secure`, po http klient by ich nie odeslal.
    return TestClient(app, base_url="https://testserver")


def _authelia(login: str, grupy: str = "wlasciciel") -> dict:
    return {"Remote-User": login, "Remote-Groups": grupy,
            "Remote-Email": "contact@renacode.com", "X-Forwarded-For": ADRES}


def _sesje(uid: int) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(DB, row_factory=dict_row) as c:
        return c.execute("SELECT id, sso, mfa_satisfied, revoked_at, host(ip) AS ip"
                         " FROM app.session WHERE user_id = %s ORDER BY id", (uid,)).fetchall()


def _audyt(uid: int) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(DB, row_factory=dict_row) as c:
        return c.execute("SELECT action, host(ip) AS ip, before, after FROM app.audit_log"
                         " WHERE actor_user_id = %s ORDER BY id", (uid,)).fetchall()


@bazodanowy
def test_flaga_wylaczona_podrobiony_remote_user_nic_nie_daje(konta, monkeypatch):
    monkeypatch.delenv("SSO_NAGLOWKI", raising=False)
    wl = konta(totp=True)
    with _klient() as k:
        for sciezka in ("/api/auth/me", "/api/engines", "/api/auth/sessions"):
            r = k.get(sciezka, headers=_authelia(wl["login"]))
            assert r.status_code == 401, (sciezka, r.text)
            assert "set-cookie" not in r.headers, sciezka
    assert _sesje(wl["id"]) == []


@bazodanowy
def test_sesja_dla_istniejacego_loginu_bez_hasla_i_bez_totp(konta, monkeypatch):
    monkeypatch.setenv("SSO_NAGLOWKI", "1")
    wl = konta(totp=True)                      # konto MA TOTP w aplikacji
    with _klient() as k:
        r = k.get("/api/auth/me", headers=_authelia(wl["login"]))
        assert r.status_code == 200, r.text
        assert r.json()["login"] == wl["login"] and r.json()["sso"] is True
        assert k.cookies.get("__Host-trader_session") and k.cookies.get("__Host-trader_csrf")
        # Kolejne zadania ida ta sama sesja - nie nowa przy kazdym.
        assert k.get("/api/auth/me", headers=_authelia(wl["login"])).status_code == 200
        assert k.get("/api/engines", headers=_authelia(wl["login"])).status_code == 200
    sesje = _sesje(wl["id"])
    assert len(sesje) == 1
    assert sesje[0]["sso"] is True and sesje[0]["mfa_satisfied"] is True
    assert sesje[0]["ip"] == ADRES
    # Audyt: kto (actor_user_id) i skad (adres z `adres_klienta`), bez danych
    # wrazliwych - zadnych naglowkow, maili ani tokenow w before/after.
    wpis = [a for a in _audyt(wl["id"]) if a["action"] == "auth.sso_login"]
    assert len(wpis) == 1 and wpis[0]["ip"] == ADRES
    assert wpis[0]["before"] is None and wpis[0]["after"] is None


@bazodanowy
def test_nieznany_login_403_z_komunikatem_i_bez_konta(konta, monkeypatch):
    import psycopg

    monkeypatch.setenv("SSO_NAGLOWKI", "1")
    konta()
    obcy = f"sso-{uuid.uuid4().hex[:10]}"
    with _klient() as k:
        r = k.get("/api/auth/me", headers=_authelia(obcy))
    assert r.status_code == 403
    assert obcy in r.json()["detail"] and "Brak konta" in r.json()["detail"]
    assert "set-cookie" not in r.headers
    with psycopg.connect(DB) as c:
        assert c.execute('SELECT count(*) FROM app."user" WHERE login = %s',
                         (obcy,)).fetchone()[0] == 0


@bazodanowy
@pytest.mark.parametrize("grupy", ["", "rodzina", "rodzina,goscie", "admins"])
def test_bez_grupy_403(konta, monkeypatch, grupy):
    monkeypatch.setenv("SSO_NAGLOWKI", "1")
    wl = konta()
    with _klient() as k:
        r = k.get("/api/auth/me", headers=_authelia(wl["login"], grupy))
    assert r.status_code == 403 and "wlasciciel" in r.json()["detail"]
    assert _sesje(wl["id"]) == []


@bazodanowy
def test_podwojny_remote_user_400(konta, monkeypatch):
    monkeypatch.setenv("SSO_NAGLOWKI", "1")
    wl = konta()
    with _klient() as k:
        r = k.get("/api/auth/me", headers=[("Remote-User", wl["login"]),
                                           ("Remote-User", "intruz"),
                                           ("Remote-Groups", "wlasciciel")])
    assert r.status_code == 400
    assert _sesje(wl["id"]) == []


@bazodanowy
def test_konto_nieaktywne_i_zablokowane(konta, monkeypatch):
    monkeypatch.setenv("SSO_NAGLOWKI", "1")
    nieaktywne, zablokowane = konta(aktywne=False), konta(zablokowane=True)
    with _klient() as k:
        assert k.get("/api/auth/me", headers=_authelia(nieaktywne["login"])).status_code == 403
        assert k.get("/api/auth/me", headers=_authelia(zablokowane["login"])).status_code == 429
    assert _sesje(nieaktywne["id"]) == [] and _sesje(zablokowane["id"]) == []


@bazodanowy
def test_flaga_bez_naglowka_zwykle_logowanie(konta, monkeypatch):
    """Droga awaryjna (`kubectl port-forward`, Authelia lezy): haslo aplikacji."""
    monkeypatch.setenv("SSO_NAGLOWKI", "1")
    wl = konta()
    with _klient() as k:
        assert k.get("/api/auth/me").status_code == 401
        r = k.post("/api/auth/login", json={"login": wl["login"], "password": HASLO})
        assert r.status_code == 200 and r.json()["mfa_required"] is False
        me = k.get("/api/auth/me")
        assert me.status_code == 200 and me.json()["sso"] is False
        # Wylogowanie bez naglowka nie odsyla na portal - nie ma czego tam konczyc.
        r = k.post("/api/auth/logout",
                   headers={"X-CSRF-Token": k.cookies.get("__Host-trader_csrf")})
        assert r.status_code == 200 and "wyloguj_url" not in r.json()
    assert [s["sso"] for s in _sesje(wl["id"])] == [False]


@bazodanowy
def test_zmiana_remote_user_daje_sesje_nowego_i_uniewaznia_stara(konta, monkeypatch):
    monkeypatch.setenv("SSO_NAGLOWKI", "1")
    a, b = konta(), konta()
    with _klient() as k:
        assert k.get("/api/auth/me", headers=_authelia(a["login"])).json()["login"] == a["login"]
        r = k.get("/api/auth/me", headers=_authelia(b["login"]))
        assert r.status_code == 200 and r.json()["login"] == b["login"]
    assert [s["revoked_at"] is not None for s in _sesje(a["id"])] == [True]
    assert [s["revoked_at"] is None for s in _sesje(b["id"])] == [True]


@bazodanowy
def test_wylogowanie_odsyla_na_portal(konta, monkeypatch):
    monkeypatch.setenv("SSO_NAGLOWKI", "1")
    wl = konta()
    with _klient() as k:
        assert k.get("/api/auth/me", headers=_authelia(wl["login"])).status_code == 200
        r = k.post("/api/auth/logout", headers={
            **_authelia(wl["login"]), "X-CSRF-Token": k.cookies.get("__Host-trader_csrf")})
    assert r.status_code == 200, r.text
    assert r.json()["wyloguj_url"] == WYLOGUJ
    assert all(s["revoked_at"] is not None for s in _sesje(wl["id"]))


@bazodanowy
def test_sciezki_bez_sesji_z_podrobionym_naglowkiem_bez_skutku(konta, monkeypatch):
    """Sondy i formularz logowania nie przechodza przez `current_user` - naglowek
    nie zaklada tam sesji ani nie zastepuje hasla."""
    monkeypatch.setenv("SSO_NAGLOWKI", "1")
    wl = konta()
    with _klient() as k:
        for sciezka in ("/api/zywy", "/api/health"):
            r = k.get(sciezka, headers=_authelia(wl["login"]))
            assert r.status_code == 200 and "set-cookie" not in r.headers, sciezka
        r = k.post("/api/auth/login", json={"login": wl["login"], "password": "zle-haslo"},
                   headers=_authelia(wl["login"]))
        assert r.status_code == 401
    assert _sesje(wl["id"]) == []


@bazodanowy
def test_sesja_sso_nie_jest_swiezym_uwierzytelnieniem(konta, monkeypatch):
    """Czynnosci wrazliwe nadal pytaja o haslo (i TOTP) APLIKACJI - kontrakt, pkt 4."""
    monkeypatch.setenv("SSO_NAGLOWKI", "1")
    wl = konta(totp=True)
    with _klient() as k:
        assert k.get("/api/auth/me", headers=_authelia(wl["login"])).status_code == 200
        naglowki = {**_authelia(wl["login"]),
                    "X-CSRF-Token": k.cookies.get("__Host-trader_csrf")}
        r = k.post("/api/auth/password", headers=naglowki, json={
            "current_password": "nie-to-haslo", "new_password": secrets.token_urlsafe(16)})
        assert r.status_code == 401
        # Dobre haslo bez kodu TOTP aplikacji - tez odmowa.
        r = k.post("/api/auth/password", headers=naglowki, json={
            "current_password": HASLO, "new_password": secrets.token_urlsafe(16)})
        assert r.status_code == 401 and "Kod" in r.json()["detail"]
    akcje = [a["action"] for a in _audyt(wl["id"])]
    assert "auth.password_changed" not in akcje
    assert akcje.count("auth.reauth_failed") == 2
