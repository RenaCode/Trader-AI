"""DRUGI SKŁADNIK MA LICZYĆ PRÓBY. Do 23.09.2026 nie liczył żadnych.

CO BYŁO ZŁE

Cała ochrona przed zgadywaniem siedziała w `/api/auth/login`: `failed_logins`,
`locked_until`, blokada po pięciu próbach (migracja 0007). `/api/auth/mfa` nie
miał ani licznika, ani opóźnienia, ani unieważnienia biletu po serii błędów —
a w całym API nie ma ogranicznika tempa.

Ścieżka, którą to otwierało, zamykała się w pętlę:

  1. atakujący zna hasło (wyciek, phishing) i woła `/login`;
  2. udane hasło ZEROWAŁO `failed_logins` — czyli jedyny licznik w systemie
     nigdy nie rósł u kogoś, kto hasło zna;
  3. bilet ważny 5 minut przyjmował dowolną liczbę błędnych kodów;
  4. sześć cyfr plus okno tolerancji zegara (`TOTP_WINDOW = 1`) to TRZY ważne
     kody naraz, więc kilka tysięcy żądań na bilet daje ~1%;
  5. po biletcie — nowy bilet, od punktu 1, bez końca.

Poprawka stawia trzy rzeczy naraz i każda z nich ma tu własnego strażnika:
licznik na koncie, unieważnienie biletu po `MAX_MFA_NA_BILET` błędach oraz
zerowanie licznika dopiero po PEŁNYM zalogowaniu, a nie po samym haśle.

CZEGO PILNUJE ATRAPA

`BazaLogowania` nie oddaje gotowych odpowiedzi: trzyma stan biletu i konta
i STOSUJE do niego to, co instrukcja faktycznie mówi. Instrukcja, której nie
rozumie, kończy się błędem — ciche przepuszczenie zamieniłoby „kod nie podbija
licznika" w „test nie zauważył, że nie podbija". Wiersz oddany na `SELECT` jest
dodatkowo przycinany do kolumn, które to zapytanie NAPRAWDĘ pobiera
(`sprawdz_kolumny`), bo atrapa podająca kolumnę spoza `SELECT`-a mierzy
wyłącznie samą siebie.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pyotp
import pytest

from services.api.routers import auth as modul
from services.api.security import TOTP_STEP, hash_password
from tests.test_dane_bez_zer import sprawdz_kolumny

SEKRET = pyotp.random_base32()
HASLO = "haslo-wlasciciela-2026"
UZYTKOWNIK = 7


def teraz() -> datetime:
    return datetime.now(timezone.utc)


def dobry_kod() -> str:
    return pyotp.TOTP(SEKRET).now()


def zly_kod() -> str:
    """Kod na pewno inny od bieżącego i od sąsiednich kroków okna tolerancji."""
    krok = int(teraz().timestamp()) // TOTP_STEP
    sasiedzi = {pyotp.TOTP(SEKRET).at((krok + d) * TOTP_STEP) for d in (-1, 0, 1)}
    for n in range(1000000):
        kandydat = f"{n:06d}"
        if kandydat not in sasiedzi:
            return kandydat
    raise AssertionError("nie da się")


def zadanie():
    """Minimalne `Request` — `audit` jest podmieniony, więc czyta to tylko
    `_start_session`."""
    return SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.9"))


class OdpowiedzBezCiasteczek:
    """`Response` w zakresie, którego używa `set_session_cookies`."""

    def __init__(self):
        self.ciasteczka = {}

    def set_cookie(self, nazwa, wartosc, **_):
        self.ciasteczka[nazwa] = wartosc


class PudelkoJawne:
    """`SecretBox` bez klucza — testujemy przepływ sterowania, nie szyfrowanie."""

    def encrypt(self, v):
        return v.encode()

    def decrypt(self, v):
        return bytes(v).decode()


class BazaLogowania:
    """Stan konta i biletu plus wykonanie instrukcji, które ścieżka logowania wysyła."""

    def __init__(self, *, failed_logins: int = 0, locked_until=None,
                 mfa_failed: int = 0, bilet_wazny_s: int = 300,
                 hash_hasla: str | None = None):
        self.konto = {
            "id": UZYTKOWNIK, "login": "wlasciciel",
            "password_hash": hash_hasla if hash_hasla is not None else hash_password(HASLO),
            "is_active": True, "failed_logins": failed_logins,
            "locked_until": locked_until, "last_login_at": None,
        }
        self.bilet = {
            "id": 1, "user_id": UZYTKOWNIK, "mfa_failed": mfa_failed,
            "revoked_at": None, "mfa_satisfied": False,
            "expires_at": teraz() + timedelta(seconds=bilet_wazny_s),
        }
        self.last_step = None
        self.sesje_zalozone = 0
        self.bilety_wydane = 0
        self.sql: list[str] = []

    # ----------------------------------------------------------------- odczyt
    async def fetch_one(self, sql, params=None):
        plaski = " ".join(sql.split())
        self.sql.append(plaski)

        if "FROM app.user WHERE login" in plaski:
            return self._przytnij(plaski, dict(self.konto))

        if "FROM app.session s JOIN app.totp_secret t" in plaski:
            if self.bilet["revoked_at"] is not None or self.bilet["mfa_satisfied"]:
                return None
            wiersz = {
                "id": self.bilet["id"], "user_id": self.bilet["user_id"],
                "expires_at": self.bilet["expires_at"], "secret_enc": SEKRET.encode(),
                "last_step": self.last_step, "locked_until": self.konto["locked_until"],
                "mfa_failed": self.bilet["mfa_failed"],
            }
            return self._przytnij(plaski, wiersz)

        if "FROM app.totp_secret WHERE user_id" in plaski:
            return self._przytnij(plaski, {"secret_enc": SEKRET.encode(),
                                           "confirmed_at": teraz()})

        if "FROM app.backup_code" in plaski or plaski.startswith("UPDATE app.backup_code"):
            return None          # to konto nie ma kodów zapasowych

        if plaski.startswith("UPDATE app.totp_secret SET last_step"):
            # Zużycie kroku jest warunkowe (`last_step < krok`) i zwraca wiersz
            # tylko wtedy, gdy TO żądanie przestawiło krok.
            krok, _, granica = params
            if self.last_step is not None and self.last_step >= granica:
                return None
            self.last_step = krok
            return {"user_id": UZYTKOWNIK}

        if "FROM app.audit_log" in plaski:
            return {"?column?": 1}   # urządzenie znane — bez powiadomienia

        # REZERWACJA PRÓBY (przegląd 2026-10-04): podbicie tylko na koncie
        # niezablokowanym, zwraca wiersz albo nic - jak warunkowy UPDATE.
        if plaski.startswith("UPDATE app.user SET failed_logins = failed_logins + 1"):
            assert "RETURNING failed_logins" in plaski, plaski
            prog, okno = params[0], params[1]
            if "locked_until <= now()" in plaski and self.konto["locked_until"] \
                    and self.konto["locked_until"] > teraz():
                return None
            self.konto["failed_logins"] += 1
            if self.konto["failed_logins"] >= prog:
                self.konto["locked_until"] = teraz() + okno
            return {"failed_logins": self.konto["failed_logins"]}

        if plaski.startswith("UPDATE app.session SET mfa_failed = mfa_failed + 1"):
            assert "revoked_at IS NULL" in plaski and "RETURNING" in plaski, plaski
            if self.bilet["revoked_at"] is not None:
                return None
            self.bilet["mfa_failed"] += 1
            if self.bilet["mfa_failed"] >= params[0]:
                self.bilet["revoked_at"] = teraz()
            return {"mfa_failed": self.bilet["mfa_failed"]}

        if plaski.startswith("SELECT locked_until FROM app.user WHERE id"):
            return {"locked_until": self.konto["locked_until"]}

        raise AssertionError("zapytanie bez podłożonej odpowiedzi:\n" + plaski)

    def _przytnij(self, sql: str, wiersz: dict) -> dict:
        """Wiersz zawężony do kolumn, które to zapytanie NAPRAWDĘ pobiera.

        Prawdziwa baza nie odda kolumny spoza `SELECT`-a, więc atrapa też nie
        może — inaczej kod czytający `row["locked_until"]` z zapytania, które
        `locked_until` pomija, przeszedłby test i wywrócił się na produkcji.
        """
        lista = sql.split(" FROM ")[0]
        wybrane = {k: v for k, v in wiersz.items()
                   if re.search(rf"\b{re.escape(k)}\b", lista)}
        sprawdz_kolumny(sql, [wybrane])
        return wybrane

    # ------------------------------------------------------------------ zapis
    async def execute(self, sql, params=None):
        plaski = " ".join(sql.split())
        self.sql.append(plaski)
        p = list(params or ())

        if plaski.startswith("INSERT INTO app.session"):
            # Dwa różne INSERT-y pod tym samym początkiem. Bilet ma
            # `mfa_satisfied = false` i nie ma adresu; sesja właściwa ma jedno
            # i drugie. Rozróżnienie jest tu konieczne, bo cała wada polega na
            # tym, ILE biletów da się wystawić na jedno hasło.
            if "mfa_satisfied) VALUES (%s,%s,%s,false)" in plaski:
                self.bilet = {"id": self.bilet["id"] + 1, "user_id": UZYTKOWNIK,
                              "mfa_failed": 0, "revoked_at": None,
                              "mfa_satisfied": False,
                              "expires_at": teraz() + timedelta(minutes=5)}
                self.bilety_wydane += 1
            else:
                self.sesje_zalozone += 1
            return

        if "UPDATE app.session SET mfa_failed = mfa_failed + 1" in plaski:
            prog = p[0]
            self.bilet["mfa_failed"] += 1
            if self.bilet["mfa_failed"] >= prog:
                self.bilet["revoked_at"] = teraz()
            return

        if "UPDATE app.session SET revoked_at = now()" in plaski:
            self.bilet["revoked_at"] = teraz()
            return

        if "UPDATE app.user SET failed_logins = failed_logins + 1" in plaski:
            prog, okno = p[0], p[1]
            self.konto["failed_logins"] += 1
            if self.konto["failed_logins"] >= prog:
                self.konto["locked_until"] = teraz() + okno
            return

        if plaski.startswith("UPDATE app.user SET failed_logins = greatest(failed_logins - 1, 0)"):
            zalozyla, _ = p
            self.konto["failed_logins"] = max(self.konto["failed_logins"] - 1, 0)
            if zalozyla:
                self.konto["locked_until"] = None
            return

        if "UPDATE app.user SET failed_logins = %s, locked_until = %s" in plaski:
            self.konto["failed_logins"], self.konto["locked_until"] = p[0], p[1]
            return

        if "UPDATE app.user SET failed_logins = 0" in plaski:
            self.konto["failed_logins"] = 0
            self.konto["locked_until"] = None
            self.konto["last_login_at"] = teraz()
            return

        if "UPDATE app.totp_secret SET last_step" in plaski:
            self.last_step = p[0]
            return

        if "UPDATE app.user SET password_hash" in plaski:
            self.konto["password_hash"] = p[0]
            return

        raise AssertionError("instrukcja bez obsługi w atrapie:\n" + plaski)


@pytest.fixture
def baza(monkeypatch):
    def zbuduj(**kw):
        b = BazaLogowania(**kw)
        monkeypatch.setattr(modul, "fetch_one", b.fetch_one)
        monkeypatch.setattr(modul, "execute", b.execute)

        async def cisza(*a, **k):
            return None

        monkeypatch.setattr(modul, "audit", cisza)
        monkeypatch.setattr("services.api.security.SecretBox", PudelkoJawne)
        return b

    return zbuduj


def mfa(kod: str):
    return modul.mfa(modul.MfaBody(challenge="x" * 32, code=kod),
                     zadanie(), OdpowiedzBezCiasteczek())


def podaj_kod(kod: str):
    """Zwraca kod HTTP odmowy albo 200 przy sukcesie."""
    from fastapi import HTTPException
    try:
        asyncio.run(mfa(kod))
        return 200
    except HTTPException as e:
        return e.status_code


# ======================================= 1. błędny kod podbija licznik na koncie

def test_bledny_kod_MFA_podbija_licznik_prob_na_koncie(baza):
    """Sedno wady: nieudany drugi składnik nie zostawiał ŻADNEGO śladu, na
    którym mogłaby się oprzeć jakakolwiek bramka."""
    b = baza()

    assert podaj_kod(zly_kod()) == 401

    assert b.konto["failed_logins"] == 1, (
        "błędny kod drugiego składnika nie podbił licznika prób — konta nic "
        "nie broni przed zgadywaniem, bo nie ma czego zliczyć")


def zaloguj(haslo: str = HASLO):
    return asyncio.run(modul.login(
        modul.LoginBody(login="wlasciciel", password=haslo),
        zadanie(), OdpowiedzBezCiasteczek()))


def test_konto_blokuje_sie_MIMO_wymieniania_biletow(baza):
    """Scenariusz z audytu w całości, a nie jego pierwszy krok.

    Sama bramka biletu niczego nie kosztuje atakującego, który zna hasło:
    zużyty bilet wymienia się na nowy jednym żądaniem. Serię kończy dopiero
    licznik NA KONCIE — i tylko dlatego, że hasło przestało go zerować.
    Te dwie poprawki działają wyłącznie razem.
    """
    b = baza()
    kody = 0

    while b.konto["locked_until"] is None and b.bilety_wydane < 10:
        zaloguj()
        for _ in range(modul.MAX_MFA_NA_BILET):
            if podaj_kod(zly_kod()) == 429:
                break
            kody += 1

    assert b.konto["locked_until"] is not None, (
        f"{kody} błędnych kodów na {b.bilety_wydane} biletach i konto nadal "
        f"otwarte — wymiana biletu omija każdą bramkę, która stoi tylko na nim")
    assert b.konto["locked_until"] > teraz()
    assert kody <= modul.MAX_FAILED, (
        f"blokada zapadła dopiero po {kody} kodach, a próg wynosi "
        f"{modul.MAX_FAILED} — licznik gdzieś się gubi")


# ======================================= 2. bilet ginie po kilku błędach

def test_bilet_jest_uniewazniany_po_serii_bledow(baza):
    """Druga, niezależna bramka. Blokada konta zapada po piątej próbie i jest
    wspólna dla wszystkich biletów; TEN bilet ma się zamknąć po trzeciej,
    zanim licznik konta w ogóle dojdzie do progu."""
    b = baza()

    for _ in range(modul.MAX_MFA_NA_BILET):
        podaj_kod(zly_kod())

    assert b.bilet["revoked_at"] is not None, (
        f"bilet przeżył {modul.MAX_MFA_NA_BILET} błędnych kodów — ścieżka "
        f"zgadywania nie zamyka się sama, tylko czeka na blokadę konta")
    assert b.konto["locked_until"] is None, (
        "ten test ma pokazywać bramkę BILETU, a konto zdążyło się zablokować — "
        "progi rozjechały się i test przestał mierzyć to, co miał")


def test_uniewazniony_bilet_nie_przyjmuje_juz_PRAWIDLOWEGO_kodu(baza):
    """Unieważnienie, które nie zamyka drogi, jest tylko wpisem w bazie."""
    b = baza()

    for _ in range(modul.MAX_MFA_NA_BILET):
        podaj_kod(zly_kod())

    assert podaj_kod(dobry_kod()) == 401, (
        "zamknięty bilet nadal otwiera sesję")
    assert b.sesje_zalozone == 0


# ======================================= 3. hasło nie zeruje licznika

def test_udane_haslo_NIE_ZERUJE_licznika_gdy_zostal_drugi_skladnik(baza):
    """To jest pętla, która czyniła blokadę konta nieosiągalną.

    Kto zna hasło, mógł wyzerować licznik w dowolnej chwili — wystarczyło
    zalogować się jeszcze raz. Logowanie kończy się na drugim składniku, więc
    i licznik ma się zerować dopiero tam.
    """
    b = baza(failed_logins=4)

    wynik = asyncio.run(modul.login(
        modul.LoginBody(login="wlasciciel", password=HASLO),
        zadanie(), OdpowiedzBezCiasteczek()))

    assert wynik["mfa_required"] is True
    assert b.konto["failed_logins"] == 4, (
        f"samo hasło skasowało licznik prób do {b.konto['failed_logins']} — "
        f"atakujący z wykradzionym hasłem może zerować go w nieskończoność, "
        f"więc blokada po {modul.MAX_FAILED} próbach nigdy nie zapadnie")


def test_zablokowane_konto_nie_przyjmuje_kodu_z_wczesniej_wydanego_biletu(baza):
    """Blokada sprawdzana wyłącznie w `/login` zatrzymuje wydawanie nowych
    biletów, ale biletowi już wydanemu pozwala dokończyć serię prób."""
    b = baza(locked_until=teraz() + timedelta(minutes=10))

    assert podaj_kod(dobry_kod()) == 429, (
        "bilet wydany przed blokadą działa mimo zablokowanego konta")
    assert b.sesje_zalozone == 0


# ======================================= 4. kanarki: poprawna ścieżka żyje

def test_prawidlowy_kod_dalej_otwiera_sesje(baza):
    """Bez tego wszystkie testy wyżej przechodziłyby również dla drzwi
    zamurowanych na głucho."""
    b = baza()

    assert podaj_kod(dobry_kod()) == 200
    assert b.sesje_zalozone == 1
    assert b.bilet["revoked_at"] is not None, "bilet po użyciu ma być zużyty"


def test_udany_drugi_skladnik_ZERUJE_licznik_prob(baza):
    """Druga strona poprawki: pełne zalogowanie ma wymazać wcześniejsze pomyłki
    właściciela. Licznik, który rośnie i nigdy nie maleje, zablokuje w końcu
    konto komuś, kto po prostu kilka razy się pomylił w ciągu miesiąca."""
    b = baza(failed_logins=3)

    assert podaj_kod(dobry_kod()) == 200
    assert b.konto["failed_logins"] == 0
    assert b.konto["last_login_at"] is not None, (
        "czas ostatniego logowania zapisuje się przy PEŁNYM zalogowaniu")


def test_kolumna_licznika_JEST_w_migracjach():
    """Atrapa nie zna schematu tej tabeli, więc sama nie wywróci się na
    kolumnie, której w bazie nie ma — a prawdziwy `UPDATE` tak, i to przy
    KAŻDYM błędnym kodzie, zamieniając 401 w 500. Schemat czytamy z migracji,
    nie przepisujemy go tutaj (patrz `tests/atrapa_bazy.py`)."""
    from tests.atrapa_bazy import KOLUMNY

    assert "mfa_failed" in KOLUMNY.get("app.session", set()), (
        "żadna migracja nie dodaje `app.session.mfa_failed`, a kod liczy na niej "
        "próby drugiego składnika")


def test_pomylka_przed_prawidlowym_kodem_nie_zamyka_drogi(baza):
    """Jedna literówka to nie atak. Bramka, która karze za pierwszą pomyłkę,
    zostanie wyłączona po tygodniu."""
    b = baza()

    assert podaj_kod(zly_kod()) == 401
    assert podaj_kod(dobry_kod()) == 200
    assert b.sesje_zalozone == 1
