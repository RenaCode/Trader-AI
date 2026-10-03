"""Audyt bezpieczenstwa z 03.10.2026 (S1-S6, S8-S11): kazda luka ma tu swojego straznika.

CO BYŁO ZŁE — W JEDNYM MIEJSCU, BO LUKI SKŁADAŁY SIĘ W ŁAŃCUCH

  S1  Nieistniejący login: zawsze 401. Istniejący po pięciu złych hasłach: 429.
      Pięć prób mówiło, czy konto istnieje.
  S9  Nieistniejący login: `sleep(0.25)` zamiast Argon2 — inny czas odpowiedzi.
      Argon2 liczony w pętli zdarzeń, czyli każda próba wstrzymywała całe API.
  S2  Śmieci w X-Forwarded-For lądowały w kolumnie `inet` → logowanie 500.
  S3  `/password`, `/totp/disable`, `revoke-others` sprawdzały hasło bez
      licznika i bez audytu — przejęta sesja zgadywała bez końca.
  S4  `/totp/disable` samym hasłem. Z S3: sesja → hasło → MFA wyłączone.
  S6  `/totp/setup` bez hasła — przypięcie konta do cudzej aplikacji.
  S5  Powiadomienie w ścieżce żądania: martwy kanał = ~35 s logowania.
  S8  Sesja 12 h bez limitu bezczynności.
  S10 Webhook: tylko `https://`, adresy klastra osiągalne, różne komunikaty
      błędów skanowały usługi TLS w środku.
  S11 CSRF porównywany przez `!=`.

Atrapa bazy jest tu MAŁA i stanowa: trzyma konto i sesję, stosuje instrukcje,
które ścieżki wysyłają, a na nieznaną instrukcję się wywraca — przepuszczenie
jej po cichu zamieniłoby „kod nie podbija licznika" w „test nie zauważył".
"""
from __future__ import annotations

import asyncio
import re
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pyotp
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from services.api import auth as modul_auth
from services.api import notifications as modul_powiadomien
from services.api import security
from services.api.routers import account as modul_konta
from services.api.routers import auth as modul
from services.api.security import backup_code_hash, hash_password

HASLO = "haslo-wlasciciela-2026"
SEKRET = pyotp.random_base32()
KOD_ZAPASOWY = "ab12-cd34-ef56"


def teraz():
    return datetime.now(timezone.utc)


def zadanie(xff: str | None = None):
    naglowki = {"x-forwarded-for": xff} if xff is not None else {}
    return SimpleNamespace(headers=naglowki, client=SimpleNamespace(host="203.0.113.9"),
                           method="POST", state=SimpleNamespace())


class Odpowiedz:
    def set_cookie(self, *a, **k):
        pass


class PudelkoJawne:
    def encrypt(self, v):
        return v.encode()

    def decrypt(self, v):
        return bytes(v).decode()


class Baza:
    def __init__(self, *, hash_hasla: str | None = None, totp: bool = False):
        self.konto = {"id": 7, "login": "wlasciciel",
                      "password_hash": hash_hasla or hash_password(HASLO),
                      "is_active": True, "failed_logins": 0, "locked_until": None}
        self.sesja = {"id": 11, "revoked_at": None, "last_seen_at": teraz(),
                      "expires_at": teraz() + timedelta(hours=1)}
        self.totp = {"secret_enc": SEKRET.encode(), "last_step": None} if totp else None
        self.kody = {backup_code_hash(KOD_ZAPASOWY): None}
        self.sesje_wstawione: list[tuple] = []
        self.totp_usuniety = False
        self.sekret_zapisany = False
        self.odswiezenia = 0

    async def fetch_one(self, sql, params=None):
        q = " ".join(sql.split())
        if "FROM app.user WHERE login" in q:
            return dict(self.konto) if params[0] == self.konto["login"] else None
        if q.startswith("SELECT password_hash, locked_until FROM app.user WHERE id"):
            return {k: self.konto[k] for k in ("password_hash", "locked_until")}
        if q.startswith("UPDATE app.user SET failed_logins = failed_logins + 1"):
            prog, okno, _ = params
            self.konto["failed_logins"] += 1
            if self.konto["failed_logins"] >= prog:
                self.konto["locked_until"] = teraz() + okno
            return {"failed_logins": self.konto["failed_logins"]}
        if q.startswith("SELECT secret_enc, last_step FROM app.totp_secret"):
            return dict(self.totp) if self.totp else None
        if q.startswith("SELECT confirmed_at FROM app.totp_secret"):
            return None
        if q.startswith("SELECT secret_enc FROM app.totp_secret"):
            return {"secret_enc": self.totp["secret_enc"]} if self.totp else None
        if "FROM app.audit_log" in q:
            return {"?column?": 1}
        if q.startswith("UPDATE app.totp_secret SET last_step"):
            krok, _, granica = params
            if self.totp["last_step"] is not None and self.totp["last_step"] >= granica:
                return None
            self.totp["last_step"] = krok
            return {"user_id": 7}
        if q.startswith("UPDATE app.backup_code SET used_at"):
            _, h = params
            if h in self.kody and self.kody[h] is None:
                self.kody[h] = "zuzyty"
                return {"id": 1}
            return None
        if "FROM app.session s JOIN app.user u" in q:
            if self.sesja["revoked_at"] is not None:
                return None
            return {"session_id": self.sesja["id"], "expires_at": self.sesja["expires_at"],
                    "mfa_satisfied": True, "last_seen_at": self.sesja["last_seen_at"],
                    "id": 7, "login": "wlasciciel", "role": "owner", "is_active": True}
        raise AssertionError("zapytanie bez odpowiedzi w atrapie:\n" + q)

    async def execute(self, sql, params=None):
        q = " ".join(sql.split())
        if q.startswith("UPDATE app.user SET failed_logins = failed_logins + 1"):
            await self.fetch_one(sql, params)
            return
        if q.startswith("UPDATE app.session SET revoked_at = now() WHERE id"):
            if params[0] == self.sesja["id"]:
                self.sesja["revoked_at"] = teraz()
            return
        if q.startswith("UPDATE app.session SET revoked_at = now() WHERE user_id"):
            return
        if q.startswith("UPDATE app.session SET last_seen_at"):
            self.sesja["last_seen_at"] = teraz()
            self.odswiezenia += 1
            return
        if q.startswith("INSERT INTO app.session"):
            self.sesje_wstawione.append(tuple(params))
            return
        if q.startswith("UPDATE app.user SET password_hash"):
            self.konto["password_hash"] = params[0]
            return
        if q.startswith("UPDATE app.user SET failed_logins = 0"):
            self.konto["failed_logins"], self.konto["locked_until"] = 0, None
            return
        if q.startswith("DELETE FROM app.totp_secret"):
            self.totp_usuniety = True
            return
        if q.startswith("DELETE FROM app.backup_code"):
            return
        if q.startswith("INSERT INTO app.totp_secret"):
            self.sekret_zapisany = True
            return
        raise AssertionError("instrukcja bez obslugi w atrapie:\n" + q)


@pytest.fixture
def baza(monkeypatch):
    def zbuduj(**kw):
        b = Baza(**kw)
        b.audyt, b.powiadomienia = [], []
        for m in (modul, modul_konta, modul_auth):
            monkeypatch.setattr(m, "fetch_one", b.fetch_one)
            monkeypatch.setattr(m, "execute", b.execute)

        async def audyt(akcja, **k):
            b.audyt.append((akcja, k.get("entity")))

        async def powiadom(user_id, kod, tytul, tresc="", **k):
            b.powiadomienia.append(kod)

        for m in (modul, modul_konta):
            monkeypatch.setattr(m, "audit", audyt)
        monkeypatch.setattr(modul, "powiadom_o_bezpieczenstwie", powiadom)
        monkeypatch.setattr("services.api.security.SecretBox", PudelkoJawne)
        modul._fantomy.clear()
        return b

    return zbuduj


def status(korutyna) -> tuple[int, str]:
    try:
        asyncio.run(korutyna)
        return 200, ""
    except HTTPException as e:
        return e.status_code, str(e.detail)


def zaloguj(login, haslo="zle-haslo"):
    return status(modul.login(modul.LoginBody(login=login, password=haslo),
                              zadanie(), Odpowiedz()))


UZYTKOWNIK = {"id": 7, "session_id": 11, "login": "wlasciciel", "role": "owner"}


# ============================================== S1: brak enumeracji loginow
def test_nieistniejacy_login_blokuje_sie_tak_samo_jak_istniejacy(baza):
    baza()
    istniejacy = [zaloguj("wlasciciel") for _ in range(modul.MAX_FAILED + 1)]
    nieistniejacy = [zaloguj("nikt-taki") for _ in range(modul.MAX_FAILED + 1)]

    def bez_liczb(odp):
        return odp[0], re.sub(r"\d+", "N", odp[1])

    assert [bez_liczb(o) for o in istniejacy] == [bez_liczb(o) for o in nieistniejacy], (
        "seria złych haseł daje inne odpowiedzi dla loginu istniejącego "
        f"i nieistniejącego — da się wyliczyć konta:\n{istniejacy}\n{nieistniejacy}")
    assert istniejacy[-1][0] == 429, "kanarek: konto istniejące MA się zablokować"


def test_licznik_fantomow_ma_gorny_limit(baza, monkeypatch):
    baza()
    monkeypatch.setattr(modul, "_FANTOMY_MAX", 3)
    for i in range(10):
        modul._fantom_bledne_haslo(f"l{i}", teraz())
    assert len(modul._fantomy) == 3


# ============================================== S9: Argon2 na atrapie, poza petla
def test_nieistniejacy_login_weryfikuje_atrape_argon2_poza_petla(baza, monkeypatch):
    baza()
    wywolania = []
    prawdziwe = security.verify_password

    def szpieg(stored, haslo):
        wywolania.append((stored, threading.current_thread() is threading.main_thread()))
        return prawdziwe(stored, haslo)

    monkeypatch.setattr(security, "verify_password", szpieg)
    assert zaloguj("nikt-taki")[0] == 401
    assert zaloguj("wlasciciel")[0] == 401

    assert len(wywolania) == 2, "nieistniejący login nie liczy Argon2 — inny czas odpowiedzi"
    assert wywolania[0][0].startswith("$argon2id$"), "atrapa nie jest hashem Argon2id"
    assert not any(w[1] for w in wywolania), "Argon2 liczony w wątku pętli zdarzeń"


# ============================================== S2: XFF walidowany
@pytest.mark.parametrize("xff,oczekiwany", [
    ("198.51.100.7, 10.42.0.1", "198.51.100.7"),
    ("2001:DB8::0:1", "2001:db8::1"),
    ("abc", None),
    ("1.2.3.4.5", None),
    ("", "203.0.113.9"),
])
def test_adres_klienta(xff, oczekiwany):
    assert modul_auth.adres_klienta(zadanie(xff)) == oczekiwany


def test_smieci_w_xff_nie_wywracaja_logowania(baza):
    b = baza(hash_hasla=hash_password(HASLO))
    odp = asyncio.run(modul.login(modul.LoginBody(login="wlasciciel", password=HASLO),
                                  zadanie("<script>"), Odpowiedz()))
    assert odp["mfa_required"] is False
    assert b.sesje_wstawione and b.sesje_wstawione[0][4] is None, (
        "śmieci z X-Forwarded-For poszły do kolumny inet")


# ============================================== S3: limit prob przy ponownym hasle
def _haslo(sciezka: str, haslo: str):
    if sciezka == "password":
        return modul.change_password(
            modul.PasswordBody(current_password=haslo, new_password="nowe-haslo-12345"),
            zadanie(), UZYTKOWNIK)
    if sciezka == "totp_disable":
        return modul.totp_disable(
            modul.TotpDisableBody(current_password=haslo, code="000000"),
            zadanie(), UZYTKOWNIK)
    if sciezka == "totp_setup":
        return modul.totp_setup(modul.TotpSetupBody(current_password=haslo),
                                zadanie(), UZYTKOWNIK)
    return modul_konta.revoke_others(modul_konta.RevokeBody(password=haslo),
                                     zadanie(), UZYTKOWNIK)


SCIEZKI = ["password", "totp_disable", "totp_setup", "revoke_others"]


@pytest.mark.parametrize("sciezka", SCIEZKI)
def test_bledne_haslo_w_sesji_podbija_licznik_i_audyt(baza, sciezka):
    b = baza(totp=True)
    kod, _ = status(_haslo(sciezka, "zle"))
    assert kod in (401, 403)
    assert b.konto["failed_logins"] == 1, f"{sciezka}: złe hasło nie podbiło licznika prób"
    assert ("auth.reauth_failed", f"auth.{sciezka}" if sciezka != "revoke_others"
            else "account.revoke_others") in b.audyt, f"{sciezka}: brak śladu w audycie"


@pytest.mark.parametrize("sciezka", SCIEZKI)
def test_seria_blednych_hasel_uniewaznia_sesje_i_blokuje(baza, sciezka):
    b = baza(totp=True)
    for _ in range(modul.MAX_FAILED):
        status(_haslo(sciezka, "zle"))
    assert b.sesja["revoked_at"] is not None, f"{sciezka}: sesja zgadująca przeżyła serię"
    assert status(_haslo(sciezka, HASLO))[0] == 429, (
        f"{sciezka}: zablokowane konto nadal przyjmuje próby hasła")


# ============================================== S4: wylaczenie TOTP haslem I kodem
def test_wylaczenie_TOTP_wymaga_pola_kodu():
    with pytest.raises(ValidationError):
        modul.TotpDisableBody(current_password=HASLO)


def test_wylaczenie_TOTP_samym_haslem_nie_przechodzi(baza):
    b = baza(totp=True)
    kod, _ = status(modul.totp_disable(
        modul.TotpDisableBody(current_password=HASLO, code="000000"), zadanie(), UZYTKOWNIK))
    assert kod == 401 and not b.totp_usuniety
    assert b.konto["failed_logins"] == 1, "błędny kod przy wyłączaniu nie liczy się jako próba"


@pytest.mark.parametrize("kod", ["totp", "zapasowy"])
def test_wylaczenie_TOTP_dobrym_kodem(baza, kod):
    b = baza(totp=True)
    wartosc = pyotp.TOTP(SEKRET).now() if kod == "totp" else KOD_ZAPASOWY
    assert status(modul.totp_disable(
        modul.TotpDisableBody(current_password=HASLO, code=wartosc),
        zadanie(), UZYTKOWNIK))[0] == 200
    assert b.totp_usuniety
    assert b.powiadomienia == ["credential.changed"]


# ============================================== S6: setup TOTP wymaga hasla
def test_konfiguracja_TOTP_bez_hasla_nie_zapisuje_sekretu(baza):
    b = baza()
    assert status(modul.totp_setup(modul.TotpSetupBody(current_password="zle"),
                                   zadanie(), UZYTKOWNIK))[0] == 401
    assert not b.sekret_zapisany
    assert status(modul.totp_setup(modul.TotpSetupBody(current_password=HASLO),
                                   zadanie(), UZYTKOWNIK))[0] == 200
    assert b.sekret_zapisany


# ============================================== S5: powiadomienie poza zadaniem
def test_powiadomienie_nie_czeka_na_kanal(monkeypatch):
    wyslane = []

    async def wolny_notify(*a, **k):
        await asyncio.sleep(0.5)
        wyslane.append(a[1])

    monkeypatch.setattr(modul_powiadomien, "notify", wolny_notify)

    async def scenariusz():
        start = asyncio.get_running_loop().time()
        await modul_powiadomien.powiadom_o_bezpieczenstwie(1, "auth.new_login", "t")
        czas = asyncio.get_running_loop().time() - start
        await asyncio.gather(*modul_powiadomien._w_tle)
        return czas

    czas = asyncio.run(scenariusz())
    assert czas < 0.1, f"żądanie czekało {czas:.2f} s na kanał powiadomień"
    assert wyslane == ["auth.new_login"], "powiadomienie w tle w ogóle nie poszło"


def test_blad_w_tle_nie_wycieka(monkeypatch, capsys):
    async def zly_notify(*a, **k):
        raise RuntimeError("kanal martwy")

    monkeypatch.setattr(modul_powiadomien, "notify", zly_notify)

    async def scenariusz():
        await modul_powiadomien.powiadom_o_bezpieczenstwie(1, "auth.new_login", "t")
        await asyncio.gather(*modul_powiadomien._w_tle)

    asyncio.run(scenariusz())
    assert "kanal martwy" in capsys.readouterr().out


# ============================================== S8: limit bezczynnosci
def _sesja(b):
    return status(modul_auth.current_user(zadanie(), trader_session="token"))


def test_sesja_bezczynna_jest_odrzucana_i_uniewazniana(baza):
    b = baza()
    b.sesja["last_seen_at"] = teraz() - security.SESSION_IDLE - timedelta(seconds=1)
    kod, tresc = _sesja(b)
    assert kod == 401 and "bezczynn" in tresc
    assert b.sesja["revoked_at"] is not None


def test_ruch_odswieza_sesje_najwyzej_raz_na_minute(baza):
    b = baza()
    b.sesja["last_seen_at"] = teraz() - timedelta(seconds=10)
    assert _sesja(b)[0] == 200 and b.odswiezenia == 0
    b.sesja["last_seen_at"] = teraz() - timedelta(minutes=5)
    assert _sesja(b)[0] == 200 and b.odswiezenia == 1


def test_kolumna_last_seen_at_jest_w_migracji():
    from pathlib import Path
    sql = Path("db/migrations/0069_bezczynnosc_sesji.sql").read_text()
    assert re.search(r"ALTER TABLE app\.session\s+ADD COLUMN last_seen_at timestamptz"
                     r" NOT NULL DEFAULT now\(\)", sql)


# ============================================== S10: webhook SSRF
def _dns(monkeypatch, adresy):
    async def rozwiaz(host, port):
        return adresy

    monkeypatch.setattr(modul_powiadomien, "_rozwiaz", rozwiaz)


@pytest.mark.parametrize("adresy", [["10.43.0.1"], ["127.0.0.1"], ["169.254.169.254"],
                                    ["93.184.216.34", "10.42.0.5"], ["::1"], ["100.64.0.1"]])
def test_webhook_do_adresu_niepublicznego_odrzucony(monkeypatch, adresy):
    _dns(monkeypatch, adresy)
    with pytest.raises(modul_powiadomien.WebhookNiedozwolony):
        asyncio.run(modul_powiadomien.adres_webhooka("https://kubernetes.default.svc/x"))


def test_webhook_przypiety_do_sprawdzonego_adresu(monkeypatch):
    _dns(monkeypatch, ["93.184.216.34"])
    url, host, sni = asyncio.run(
        modul_powiadomien.adres_webhooka("https://hooks.example.com:8443/a?b=1"))
    assert url == "https://93.184.216.34:8443/a?b=1"
    assert host == "hooks.example.com:8443" and sni == "hooks.example.com"


def test_webhook_bez_https_odrzucony():
    with pytest.raises(modul_powiadomien.WebhookNiedozwolony):
        asyncio.run(modul_powiadomien.adres_webhooka("http://example.com/"))


@pytest.mark.parametrize("przyczyna", ["zablokowany", "odmowa", "status"])
def test_webhook_jeden_komunikat_bledu(monkeypatch, przyczyna):
    import httpx

    if przyczyna == "zablokowany":
        _dns(monkeypatch, ["10.43.0.1"])
    else:
        _dns(monkeypatch, ["93.184.216.34"])

        def obsluga(request):
            if przyczyna == "odmowa":
                raise httpx.ConnectError("Connection refused")
            return httpx.Response(404)

        prawdziwy = httpx.AsyncClient
        monkeypatch.setattr(modul_powiadomien.httpx, "AsyncClient",
                            lambda **k: prawdziwy(transport=httpx.MockTransport(obsluga), **k))
    with pytest.raises(RuntimeError) as e:
        asyncio.run(modul_powiadomien._send_webhook({"url": "https://h.example.com/"}, {}))
    assert str(e.value) == modul_powiadomien.WEBHOOK_BLAD


# ============================================== S11: CSRF w stalym czasie
def test_csrf_porownywany_przez_compare_digest(monkeypatch):
    wywolania = []
    prawdziwe = modul_auth.hmac.compare_digest

    def szpieg(a, b):
        wywolania.append(1)
        return prawdziwe(a, b)

    monkeypatch.setattr(modul_auth.hmac, "compare_digest", szpieg)
    asyncio.run(modul_auth.require_csrf(zadanie(), x_csrf_token="abc", trader_csrf="abc"))
    assert wywolania, "token CSRF porównywany zwykłym `!=`"
    with pytest.raises(HTTPException) as e:
        asyncio.run(modul_auth.require_csrf(zadanie(), x_csrf_token="abd", trader_csrf="abc"))
    assert e.value.status_code == 403
