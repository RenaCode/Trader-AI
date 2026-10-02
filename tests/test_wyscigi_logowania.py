"""RÓWNOLEGŁE ŻĄDANIA NIE MOGĄ OMIJAĆ BRAMEK LOGOWANIA. Do 02.10.2026 omijały.

CO BYŁO ZŁE

Trzy bramki w `routers/auth.py` stały na wzorcu „przeczytaj, policz w Pythonie,
zapisz". Każda działała przy żądaniach po kolei i każda przepuszczała żądania
wysłane naraz — a zgadywanie i odtwarzanie przechwyconego kodu to z definicji
żądania wysłane naraz:

  1. `/login`, błędne hasło: `failed_logins = row + 1`. Pięć równoległych
     zgadnięć czytało zero i pięć razy zapisywało jedynkę — blokada konta
     nie zapadała nigdy.
  2. `/mfa`, kod TOTP: `last_step` przeczytany przed weryfikacją. Dwa żądania
     z tym samym kodem przechodziły oba i oba dostawały sesję, choć ochrona
     przed powtórzeniem (RFC 6238 §5.2) jest po to, żeby drugie odpadło.
  3. `/mfa`, kod zapasowy: SELECT „nieużyty", potem osobny UPDATE — ten sam
     jednorazowy kod otwierał dwie sesje.

Do tego dwie dziury w powiadomieniach `always_on`: alarm o nowym urządzeniu
nie istniał na ścieżce z drugim składnikiem, a włączenie i wyłączenie TOTP
(wprost wymienione w etykiecie `credential.changed`, migracja 0011) nie było
zgłaszane wcale.

JAK ATRAPA SYMULUJE RÓWNOLEGŁOŚĆ

Prawdziwy `fetch_one` oddaje sterowanie pętli zdarzeń na czas zapytania. Atrapa
z `test_licznik_prob_mfa.py` tego nie robi — korutyna bez `await` w środku nie
ustępuje — więc `asyncio.gather` wykonywałby żądania po kolei i test nie
zobaczyłby żadnego wyścigu. Tutaj każde zapytanie zaczyna się od
`asyncio.sleep(0)`, czyli dokładnie od tego punktu przełączenia, który ma
produkcja.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from services.api.routers import auth as modul
from services.api.security import backup_code_hash
from tests.test_licznik_prob_mfa import (
    HASLO, BazaLogowania, OdpowiedzBezCiasteczek, PudelkoJawne, dobry_kod, zadanie,
)

KOD_ZAPASOWY = "ab12-cd34-ef56"


class BazaRownolegla(BazaLogowania):
    """Ta sama atrapa, ale każde zapytanie ustępuje pętli jak prawdziwe I/O."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.kody_zapasowe = {backup_code_hash(KOD_ZAPASOWY): None}   # hash -> used_at

    async def fetch_one(self, sql, params=None):
        await asyncio.sleep(0)
        plaski = " ".join(sql.split())

        # Kod zapasowy — obie postaci, żeby test mierzył poprawkę, a nie atrapę:
        # stara (SELECT, potem UPDATE po id) i nowa (UPDATE ... RETURNING).
        if plaski.startswith("SELECT id FROM app.backup_code"):
            _, h = params
            return {"id": h} if h in self.kody_zapasowe and self.kody_zapasowe[h] is None else None
        if plaski.startswith("UPDATE app.backup_code"):
            _, h = params
            if h in self.kody_zapasowe and self.kody_zapasowe[h] is None:
                self.kody_zapasowe[h] = "zużyty"
                return {"id": h}
            return None
        return await super().fetch_one(sql, params)

    async def execute(self, sql, params=None):
        await asyncio.sleep(0)
        plaski = " ".join(sql.split())
        if plaski.startswith("UPDATE app.backup_code SET used_at = now() WHERE id"):
            self.kody_zapasowe[params[0]] = "zużyty"
            return
        return await super().execute(sql, params)


@pytest.fixture
def baza(monkeypatch):
    def zbuduj(**kw):
        b = BazaRownolegla(**kw)
        monkeypatch.setattr(modul, "fetch_one", b.fetch_one)
        monkeypatch.setattr(modul, "execute", b.execute)

        async def cisza(*a, **k):
            return None

        monkeypatch.setattr(modul, "audit", cisza)
        monkeypatch.setattr("services.api.security.SecretBox", PudelkoJawne)
        b.powiadomienia = []

        async def zapisz(user_id, kod, tytul, tresc="", **k):
            b.powiadomienia.append((kod, tytul))

        monkeypatch.setattr(modul, "powiadom_o_bezpieczenstwie", zapisz)
        return b

    return zbuduj


async def _status(korutyna) -> int:
    try:
        await korutyna
        return 200
    except HTTPException as e:
        return e.status_code


def _mfa(kod: str):
    return modul.mfa(modul.MfaBody(challenge="x" * 32, code=kod),
                     zadanie(), OdpowiedzBezCiasteczek())


async def _naraz(*korutyny):
    return await asyncio.gather(*(_status(k) for k in korutyny))


# ============================================== 1. licznik błędnych haseł

def test_rownolegle_bledne_hasla_ZAKLADAJA_blokade(baza):
    """Pięć zgadnięć naraz to pięć prób, nie jedna."""
    b = baza()

    wyniki = asyncio.run(_naraz(*(
        modul.login(modul.LoginBody(login="wlasciciel", password=f"zle-{i}"),
                    zadanie(), OdpowiedzBezCiasteczek())
        for i in range(modul.MAX_FAILED))))

    assert wyniki == [401] * modul.MAX_FAILED
    assert b.konto["failed_logins"] == modul.MAX_FAILED, (
        f"{modul.MAX_FAILED} równoległych błędnych haseł podbiło licznik do "
        f"{b.konto['failed_logins']} — odczyt i zapis w Pythonie gubią próby")
    assert b.konto["locked_until"] is not None, (
        "seria błędnych haseł wysłana naraz nie zablokowała konta")


def test_poprawne_haslo_po_blednych_dalej_dziala(baza):
    """Kanarek: atomowy licznik nie może zamurować drogi właścicielowi."""
    b = baza()
    asyncio.run(_status(modul.login(
        modul.LoginBody(login="wlasciciel", password="zle"),
        zadanie(), OdpowiedzBezCiasteczek())))
    wynik = asyncio.run(modul.login(
        modul.LoginBody(login="wlasciciel", password=HASLO),
        zadanie(), OdpowiedzBezCiasteczek()))
    assert wynik["mfa_required"] is True
    assert b.konto["failed_logins"] == 1


# ============================================== 2. ten sam kod TOTP dwa razy

def test_ten_sam_kod_TOTP_wyslany_naraz_otwiera_JEDNA_sesje(baza):
    b = baza()
    kod = dobry_kod()

    wyniki = asyncio.run(_naraz(_mfa(kod), _mfa(kod)))

    assert sorted(wyniki) == [200, 401], wyniki
    assert b.sesje_zalozone == 1, (
        f"jeden kod TOTP otworzył {b.sesje_zalozone} sesje — ochrona przed "
        f"powtórzeniem nie działa przy odtworzeniu kodu od razu")


# ============================================== 3. kod zapasowy dwa razy

def test_kod_zapasowy_wyslany_naraz_otwiera_JEDNA_sesje(baza):
    b = baza()

    wyniki = asyncio.run(_naraz(_mfa(KOD_ZAPASOWY), _mfa(KOD_ZAPASOWY)))

    assert sorted(wyniki) == [200, 401], wyniki
    assert b.sesje_zalozone == 1, (
        f"jednorazowy kod zapasowy otworzył {b.sesje_zalozone} sesje")
    assert b.kody_zapasowe[backup_code_hash(KOD_ZAPASOWY)] is not None


def test_kod_zapasowy_dalej_dziala_raz(baza):
    """Kanarek: atomowe zużycie nie może odrzucać kodu w ogóle."""
    b = baza()
    assert asyncio.run(_status(_mfa(KOD_ZAPASOWY))) == 200
    assert b.sesje_zalozone == 1
    # Nowy bilet, żeby odmowa wynikała z ZUŻYTEGO KODU, a nie ze zużytego biletu.
    b.bilet["revoked_at"] = None
    assert asyncio.run(_status(_mfa(KOD_ZAPASOWY))) == 401
    assert b.sesje_zalozone == 1


# ============================================== 4. powiadomienia always_on

def test_logowanie_z_TOTP_zglasza_nowe_urzadzenie(baza, monkeypatch):
    """Alarm o nowym urządzeniu nie może znikać po włączeniu drugiego składnika."""
    b = baza()
    wywolania = []

    async def zglos(user_id, request):
        wywolania.append(user_id)

    monkeypatch.setattr(modul, "_zglos_nowe_urzadzenie", zglos)

    assert asyncio.run(_status(_mfa(dobry_kod()))) == 200
    assert wywolania == [b.konto["id"]], (
        "udany drugi składnik nie sprawdził, czy urządzenie jest nowe")


def test_wykrywanie_w_MFA_jest_oslonione(baza, monkeypatch):
    """Błąd wykrywania nie ma prawa wywrócić logowania — jak w `/login`."""
    b = baza()

    async def wybuch(user_id, request):
        raise RuntimeError("baza audytu niedostępna")

    monkeypatch.setattr(modul, "_zglos_nowe_urzadzenie", wybuch)
    assert asyncio.run(_status(_mfa(dobry_kod()))) == 200
    assert b.sesje_zalozone == 1


def test_znane_urzadzenie_uwzglednia_logowania_z_drugim_skladnikiem(monkeypatch):
    """Konto z TOTP kończy logowanie na `auth.mfa`, nie na `auth.login`.
    Zapytanie szukające wyłącznie `auth.login` uznawałoby każde urządzenie
    za nowe."""
    zapytania = []

    async def fetch_one(sql, params=None):
        zapytania.append(" ".join(sql.split()))
        return {"?column?": 1}

    monkeypatch.setattr(modul, "fetch_one", fetch_one)
    asyncio.run(modul._zglos_nowe_urzadzenie(7, zadanie()))
    assert zapytania and all(a in zapytania[0] for a in
                             ("'auth.login'", "'auth.mfa'", "'auth.mfa_backup_code'"))


def test_wylaczenie_TOTP_wymaga_tylko_hasla_i_jest_zglaszane(baza, monkeypatch):
    b = baza()

    async def fetch_one(sql, params=None):
        return {"password_hash": b.konto["password_hash"]}

    async def execute(sql, params=None):
        return None

    monkeypatch.setattr(modul, "fetch_one", fetch_one)
    monkeypatch.setattr(modul, "execute", execute)

    cialo = modul.TotpDisableBody(current_password=HASLO)
    wynik = asyncio.run(modul.totp_disable(cialo, zadanie(), {"id": 7}))

    assert wynik == {"ok": True}
    assert [k for k, _ in b.powiadomienia] == ["credential.changed"], (
        "wyłączenie drugiego składnika przeszło bez powiadomienia")


def test_wlaczenie_TOTP_jest_zglaszane(baza, monkeypatch):
    b = baza()

    async def fetch_one(sql, params=None):
        return {"secret_enc": b"x", "last_step": None}

    async def execute(sql, params=None):
        return None

    monkeypatch.setattr(modul, "fetch_one", fetch_one)
    monkeypatch.setattr(modul, "execute", execute)
    monkeypatch.setattr(modul, "verify_totp", lambda *a, **k: 1)

    wynik = asyncio.run(modul.totp_confirm(
        modul.TotpConfirmBody(code="123456"), zadanie(), {"id": 7}))

    assert wynik["ok"] is True
    assert [k for k, _ in b.powiadomienia] == ["credential.changed"], (
        "włączenie drugiego składnika przeszło bez powiadomienia")


def test_usuniecie_klucza_gieldy_jest_zglaszane(monkeypatch):
    from services.api.routers import credentials as kl

    zgloszone = []

    async def fetch_one(sql, params=None):
        return {"label": "demo-1"}

    async def nic(*a, **k):
        return None

    async def zapisz(user_id, kod, tytul, tresc="", **k):
        zgloszone.append((kod, tytul))

    monkeypatch.setattr(kl, "fetch_one", fetch_one)
    monkeypatch.setattr(kl, "execute", nic)
    monkeypatch.setattr(kl, "audit", nic)
    monkeypatch.setattr(kl, "powiadom_o_bezpieczenstwie", zapisz)

    asyncio.run(kl.delete_credential(3, zadanie(), {"id": 7}))
    assert zgloszone == [("credential.changed", "Usunięto klucz API giełdy")]
