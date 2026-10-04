"""Zaleznosci uwierzytelniania: odczyt sesji z ciasteczka, ochrona endpointow, CSRF.

Model sesyjny (nie JWT) jest tu celowy: sesje da sie natychmiast uniewaznic po
stronie serwera, co dla systemu obracajacego pieniedzmi jest wazniejsze niz
bezstanowosc. JWT bez listy odwolan nie da sie wylogowac przed wygasnieciem.
"""
from __future__ import annotations

import hmac
import ipaddress
from datetime import datetime, timezone

from fastapi import Cookie, Depends, Header, HTTPException, Request, Response
from psycopg.types.json import Jsonb

from .db import fetch_one, execute
from .security import (SESSION_COOKIE, SESSION_IDLE, SESSION_TOUCH_EVERY,
                       session_token_hash)

# `__Host-` jak SESSION_COOKIE (security.py); front czyta te nazwe w utils/api.js.
CSRF_COOKIE = "__Host-trader_csrf"
CSRF_HEADER = "x-csrf-token"


async def current_user(
    request: Request,
    trader_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> dict:
    """Zwraca uzytkownika albo 401. Uzywane jako Depends na chronionych routerach."""
    if not trader_session:
        raise HTTPException(status_code=401, detail="Wymagane zalogowanie")

    row = await fetch_one(
        """
        SELECT s.id AS session_id, s.expires_at, s.mfa_satisfied, s.last_seen_at,
               u.id, u.login, u.role, u.is_active
        FROM app.session s
        JOIN app.user u ON u.id = s.user_id
        WHERE s.token_hash = %s AND s.revoked_at IS NULL
        """,
        (session_token_hash(trader_session),),
    )
    if not row:
        raise HTTPException(status_code=401, detail="Sesja nieznana lub uniewazniona")
    teraz = datetime.now(timezone.utc)
    if row["expires_at"] <= teraz:
        raise HTTPException(status_code=401, detail="Sesja wygasla")
    # BEZCZYNNOSC. Sesja nieuzywana dluzej niz `SESSION_IDLE` jest
    # uniewazniana W BAZIE, a nie tylko odrzucana: odrzucenie bez zapisu
    # zostawialoby ja na liscie aktywnych sesji w profilu i dawalo sie
    # "ozywic", gdyby ktos kiedys wydluzyl limit.
    if row["last_seen_at"] is not None and row["last_seen_at"] <= teraz - SESSION_IDLE:
        await execute("UPDATE app.session SET revoked_at = now()"
                      " WHERE id = %s AND revoked_at IS NULL", (row["session_id"],))
        raise HTTPException(status_code=401, detail="Sesja wygasla z powodu bezczynnosci")
    if not row["is_active"]:
        raise HTTPException(status_code=403, detail="Konto nieaktywne")
    if not row["mfa_satisfied"]:
        raise HTTPException(status_code=401, detail="Drugi skladnik niepotwierdzony")

    if row["last_seen_at"] is None or row["last_seen_at"] <= teraz - SESSION_TOUCH_EVERY:
        await execute("UPDATE app.session SET last_seen_at = now() WHERE id = %s",
                      (row["session_id"],))

    request.state.user_id = row["id"]
    request.state.session_id = row["session_id"]
    return row


async def wymagaj_wlasciciela(user: dict = Depends(current_user)) -> dict:
    """Operacje zmieniajace stan sa dla roli `owner`, nie dla kazdej waznej sesji.

    DLACZEGO TO ISTNIEJE, SKORO KONTA `viewer` DZIS NIE MA

    Rola jest w schemacie od migracji 0007 (`CHECK (role IN ('owner','viewer'))`),
    jest pobierana w `current_user`, zwracana przez `/auth/me` i pokazywana
    w `Profile` - tak, jakby cos znaczyla. Nie znaczyla nic: `grep -rn "role"
    services/api/` dawal same odczyty do odpowiedzi, ani jednego porownania.
    Ochrona byla deklarowana raz, jako `Depends(current_user)` na calym
    routerze, a `current_user` sprawdza wylacznie waznosc sesji.

    Konta `viewer` faktycznie dzis nie da sie zalozyc - `scripts/create_admin.py`
    wpisuje na sztywno `'owner'`, a zaden endpoint nie tworzy uzytkownikow.
    Luka jest wiec UTAJONA, a nie czynna: zadziala w dniu, w ktorym ktos doda
    drugie konto recznym SQL-em, i wtedy nic o tym nie powie. Tamtego dnia
    `viewer` zwolnilby kill-switcha (`risk.py`), podmienil klucz Anthropica
    (`settings.py`), usunal klucz API gieldy (`credentials.py`) i zapisal
    przeplyw kapitalu (`kapital.py`).

    ODCZYTY ZOSTAJA OTWARTE. Rola `viewer` ma znaczyc "widzi wszystko, nie
    rusza niczego" - zalozenie jej na caly router zamienialoby ja w konto bez
    dostepu, czyli w cos, czego nikt nie zalozy.

    403, nie 401: sesja JEST wazna, odmawiamy samego dzialania. 401 poza
    logowaniem wyrzuca uzytkownika na ekran logowania i zabiera mu komunikat
    sprzed oczu - patrz `tests/test_401_nie_wylogowuje.py`.
    """
    if user["role"] != "owner":
        raise HTTPException(
            status_code=403,
            detail="Ta operacja wymaga roli właściciela. Konto ma rolę "
                   f"„{user['role']}” — może czytać, nie może zmieniać.")
    return user


async def require_csrf(
    request: Request,
    x_csrf_token: str | None = Header(default=None, alias=CSRF_HEADER),
    trader_csrf: str | None = Cookie(default=None, alias=CSRF_COOKIE),
) -> None:
    """Double-submit cookie dla metod zmieniajacych stan.

    Ciasteczko sesji ma SameSite=Strict, co samo w sobie blokuje wiekszosc CSRF,
    ale to jest druga, niezalezna warstwa - przegladarki roznie traktuja Strict
    przy nawigacjach, a koszt tej kontroli jest zerowy.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    # `compare_digest`, nie `!=`: zwykle porownanie konczy sie na pierwszej
    # roznicy, wiec czas odpowiedzi mowi, ile znakow tokenu bylo trafionych.
    if (not trader_csrf or not x_csrf_token
            or not hmac.compare_digest(trader_csrf.encode(), x_csrf_token.encode())):
        raise HTTPException(status_code=403, detail="Nieprawidlowy token CSRF")


def set_session_cookies(response: Response, token: str, csrf: str, max_age: int) -> None:
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=max_age, httponly=True, secure=True, samesite="strict", path="/",
    )
    # CSRF celowo BEZ httponly - front musi go odczytac, zeby odeslac w naglowku.
    # Sam w sobie nie daje dostepu; jest wazny tylko w parze z ciasteczkiem sesji.
    response.set_cookie(
        CSRF_COOKIE, csrf,
        max_age=max_age, httponly=False, secure=True, samesite="strict", path="/",
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", samesite="strict", secure=True)
    response.delete_cookie(CSRF_COOKIE, path="/", samesite="strict", secure=True)


def adres_klienta(request) -> str | None:
    """Adres klienta - z X-Forwarded-For za Traefikiem - albo None, gdy to nie adres.

    JEDNO miejsce dla audytu, sesji i wykrywania nowego urzadzenia. Do
    2026-10-03 kazde z trzech rozbieralo naglowek samo i wkladalo pierwszy
    element prosto do kolumny `inet`. Pod w klastrze wysylajacy w XFF smieci
    (`x-forwarded-for: abc`) wywracal tym logowanie bledem 500, bo psycopg
    oddawal `InvalidTextRepresentation` z INSERT-a do `app.session`.

    Smieci daja None, nie wyjatek: brak adresu w audycie jest gorszy od adresu,
    ale lepszy od logowania, ktore nie dziala. `ipaddress` normalizuje tez
    zapis (np. IPv6), wiec porownanie "znanego urzadzenia" nie zalezy od tego,
    jak posrednik sformatowal adres.
    """
    fwd = request.headers.get("x-forwarded-for")
    kandydat = (fwd.split(",")[0].strip() if fwd
                else (request.client.host if request.client else None))
    if not kandydat:
        return None
    try:
        return str(ipaddress.ip_address(kandydat))
    except ValueError:
        return None


async def audit(action: str, *, user_id: int | None = None, entity: str | None = None,
                entity_id: str | None = None, before=None, after=None,
                request: Request | None = None) -> None:
    ip = None
    ua = None
    if request is not None:
        ip = adres_klienta(request)
        ua = request.headers.get("user-agent")
    # Goly dict psycopg odrzuca przy WIAZANIU PARAMETRU, a wolajacy zdazyl juz
    # zapisac swoja zmiane (pula chodzi z autocommit=True) - uzytkownik dostawal
    # 500 po dokonanej zmianie, bez sladu w audycie. `Jsonb` jak w puls.py
    # i dozorca.py. None zostaje None, zeby w bazie byl NULL (brak stanu),
    # a nie jsonowy `null` (stan pusty) - to dwie rozne rzeczy przy czytaniu.
    await execute(
        "INSERT INTO app.audit_log (actor_user_id, action, entity, entity_id, before, after, ip, user_agent)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (user_id, action, entity, entity_id,
         None if before is None else Jsonb(before),
         None if after is None else Jsonb(after), ip, ua),
    )


# LISTA SESJI AKTYWNYCH - wspolna dla `/auth/sessions` i `/account/sessions`.
# Bezczynna dluzej niz SESSION_IDLE jest martwa (`current_user` odrzuci ja przy
# nastepnym zadaniu), ale do tego zadania lezala w bazie z `revoked_at IS NULL`
# i lista pokazywala ja jako aktywna (audyt Fable U-11). Parametry:
# (biezaca sesja, uzytkownik, prog bezczynnosci w sekundach).
SQL_SESJE_AKTYWNE = (
    "SELECT id, created_at, expires_at, ip, user_agent, (id = %s) AS current"
    " FROM app.session"
    " WHERE user_id = %s AND revoked_at IS NULL AND expires_at > now()"
    "   AND coalesce(last_seen_at, created_at) > now() - make_interval(secs => %s)"
    " ORDER BY created_at DESC")
SESSION_IDLE_S = int(SESSION_IDLE.total_seconds())


Protected = Depends(current_user)
