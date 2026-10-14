"""ROLA `viewer` MA COŚ ZNACZYĆ — albo nie ma jej być w interfejsie.

CO BYŁO ZŁE

Ochrona API jest deklarowana RAZ, w `main.py:52-57`:

    for r in _protected:
        app.include_router(r, dependencies=[Depends(current_user)])

a `current_user` sprawdza wyłącznie ważność sesji, `expires_at`, `is_active`
i `mfa_satisfied`. `row["role"]` jest pobierany (`auth.py:32`) i zwracany do
JSON-a, ale nigdy z niczym nie porównany: `grep -rn "role" services/api/`
dawał same odczyty do odpowiedzi.

Tymczasem migracja 0007 dopuszcza `CHECK (role IN ('owner','viewer'))`,
a `/auth/me` i `Profile` pokazują rolę tak, jakby niosła uprawnienia.

UCZCIWE ZASTRZEŻENIE, KTÓRE TU POWTARZAMY

Do 14.10 takiego konta nie dało się założyć — `scripts/create_admin.py`
wpisywał na sztywno `'owner'`. Od 14.10 `create_admin.py --rola viewer`
zakłada je naprawdę (podgląd dla rodziny przez SSO), więc bez tego strażnika
luka byłaby CZYNNA: `viewer` zwolniłby kill-switcha,
podmienił klucz Anthropica, usunął klucz API giełdy i zapisał przepływ
kapitału.

Dlatego strażnik jest STATYCZNY i idzie po WSZYSTKICH endpointach czterech
routerów. Test sprawdzający jeden endpoint zestarzałby się przy pierwszym
nowym — a to właśnie „nowy endpoint bez tej linijki" jest tu jedynym realnym
sposobem, żeby luka wróciła.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from services.api.auth import wymagaj_wlasciciela

KORZEN = Path(__file__).resolve().parents[1]
ROUTERY = KORZEN / "services/api/routers"

# Wyjątki, nie lista chronionych (przegląd Trader #27, 14.10): odkąd konto
# `viewer` da się założyć, nowy router z zapisem ma być chroniony z urzędu,
# a nie dopiero po dopisaniu go tutaj. `auth` i `account`: każdy użytkownik
# obsługuje WŁASNE konto — hasło, drugi składnik, własne sesje.
# `notifications`: preferencje i kanały są per użytkownik (`user_id` w każdym
# zapytaniu) i nie ruszają ani pieniędzy, ani kluczy.
WLASNE_KONTO = {"__init__.py", "auth.py", "account.py", "notifications.py"}
CHRONIONE = tuple(sorted(p.name for p in ROUTERY.glob("*.py") if p.name not in WLASNE_KONTO))

DEKORATOR = re.compile(
    r"@router\.(post|put|delete|patch)\((.*?)\)\s*\nasync def (\w+)", re.S)


def endpointy(plik: Path):
    tresc = plik.read_text(encoding="utf-8")
    for m in DEKORATOR.finditer(tresc):
        yield m.group(1), m.group(3), m.group(2)


# ======================================= zależność istnieje i naprawdę odmawia

def test_viewer_dostaje_403_a_nie_przechodzi():
    with pytest.raises(HTTPException) as e:
        asyncio.run(wymagaj_wlasciciela({"role": "viewer", "login": "obserwator"}))

    assert e.value.status_code == 403, (
        "odmowa musi być 403: sesja JEST ważna, odmawiamy samego działania. "
        "401 wyrzuciłoby użytkownika na ekran logowania i zabrało mu komunikat "
        "sprzed oczu (patrz test_401_nie_wylogowuje.py)")
    assert "właściciel" in e.value.detail.lower()


def test_owner_przechodzi():
    """Kanarek: bramka, która nie przepuszcza nikogo, przeszłaby test wyżej."""
    user = {"role": "owner", "login": "wlasciciel"}
    assert asyncio.run(wymagaj_wlasciciela(user)) is user


def test_rola_spoza_katalogu_tez_odpada():
    """Domyślnie ZAMKNIĘTE. Gdyby ktoś dopisał rolę do `CHECK`-a w bazie
    i zapomniał o API, nowa rola ma nie dostać uprawnień w prezencie."""
    with pytest.raises(HTTPException):
        asyncio.run(wymagaj_wlasciciela({"role": "audytor", "login": "x"}))


# ======================================= i jest NA KAŻDYM zmieniającym stan

def test_kazdy_endpoint_zmieniajacy_stan_wymaga_wlasciciela():
    braki = []
    for nazwa in CHRONIONE:
        for metoda, funkcja, dekorator in endpointy(ROUTERY / nazwa):
            if "wymagaj_wlasciciela" not in dekorator:
                braki.append(f"{nazwa}:{funkcja} ({metoda.upper()})")

    assert not braki, (
        "te operacje zmieniają stan systemu, a sprawdzają wyłącznie ważność "
        f"sesji — konto `viewer` wykonałoby każdą z nich: {braki}")


def test_ten_straznik_naprawde_cos_sprawdza():
    """Strażnik po pustej liście przechodzi zawsze i nie twierdzi niczego —
    to jest wada, na którą ten projekt już raz się nadział (`smoke.spec.mjs`
    i pętla po pustej tablicy wykresów)."""
    znalezione = [f"{n}:{f}" for n in CHRONIONE for _, f, _ in endpointy(ROUTERY / n)]

    assert len(znalezione) >= 10, (
        f"wyrażenie dopasowało tylko {len(znalezione)} endpointów "
        f"({znalezione}) — strażnik wyżej przestał pokrywać to, co miał")


def test_odczyty_zostaja_otwarte_dla_viewera():
    """Rola `viewer` ma znaczyć „widzi wszystko, nie rusza niczego". Założenie
    zależności na cały router zamieniłoby ją w konto bez dostępu — czyli
    w rolę, której nikt nigdy nie nada."""
    main = (KORZEN / "services/api/main.py").read_text(encoding="utf-8")

    assert "wymagaj_wlasciciela" not in main, (
        "rola właściciela jest wymagana na poziomie całych routerów, więc "
        "`viewer` nie zobaczy nawet zakładki Ryzyko")

    for nazwa in CHRONIONE:
        tresc = (ROUTERY / nazwa).read_text(encoding="utf-8")
        for m in re.finditer(r"@router\.get\((.*?)\)\s*\nasync def", tresc, re.S):
            assert "wymagaj_wlasciciela" not in m.group(1), (
                f"{nazwa}: odczyt wymaga roli właściciela")


# ======================================= alarmy tylko do kont, które mogą działać

ODBIORCY = re.compile(r"""SELECT id FROM app\.\\?"user\\?" WHERE is_active(?! AND role = 'owner')""")


def test_alarmy_nie_ida_do_podgladu():
    """Konto `viewer` niczego nie zwolni ani nie poprawi, więc alarmy
    operacyjne (dozór, kill-switch, kapitał) idą tylko do kont `owner`
    (przegląd Trader #27). Inaczej podgląd dla rodziny dostawałby każdy
    alarm, na który nie może zareagować."""
    braki = []
    for plik in (KORZEN / "services").rglob("*.py"):
        for nr, linia in enumerate(plik.read_text(encoding="utf-8").splitlines(), 1):
            if ODBIORCY.search(linia):
                braki.append(f"{plik.relative_to(KORZEN)}:{nr}")
    assert not braki, f"odbiorcy alarmów bez filtra roli: {braki}"


def test_wzorzec_odbiorcow_cos_lapie():
    """Kanarek dla wyrażenia wyżej: stare zapytanie musi zostać złapane."""
    assert ODBIORCY.search("""fetch_all('SELECT id FROM app."user" WHERE is_active')""")
    assert not ODBIORCY.search(
        """fetch_all("SELECT id FROM app.\\"user\\" WHERE is_active AND role = 'owner'")""")
