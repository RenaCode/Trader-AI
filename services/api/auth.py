"""Zaleznosci uwierzytelniania: odczyt sesji z ciasteczka, ochrona endpointow, CSRF.

Model sesyjny (nie JWT) jest tu celowy: sesje da sie natychmiast uniewaznic po
stronie serwera, co dla systemu obracajacego pieniedzmi jest wazniejsze niz
bezstanowosc. JWT bez listy odwolan nie da sie wylogowac przed wygasnieciem.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Cookie, Depends, Header, HTTPException, Request, Response
from psycopg.types.json import Jsonb

from .db import fetch_one, execute
from .security import SESSION_COOKIE, session_token_hash

CSRF_COOKIE = "trader_csrf"
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
        SELECT s.id AS session_id, s.expires_at, s.mfa_satisfied,
               u.id, u.login, u.role, u.is_active
        FROM app.session s
        JOIN app.user u ON u.id = s.user_id
        WHERE s.token_hash = %s AND s.revoked_at IS NULL
        """,
        (session_token_hash(trader_session),),
    )
    if not row:
        raise HTTPException(status_code=401, detail="Sesja nieznana lub uniewazniona")
    if row["expires_at"] <= datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Sesja wygasla")
    if not row["is_active"]:
        raise HTTPException(status_code=403, detail="Konto nieaktywne")
    if not row["mfa_satisfied"]:
        raise HTTPException(status_code=401, detail="Drugi skladnik niepotwierdzony")

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
    if not trader_csrf or not x_csrf_token or trader_csrf != x_csrf_token:
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


async def audit(action: str, *, user_id: int | None = None, entity: str | None = None,
                entity_id: str | None = None, before=None, after=None,
                request: Request | None = None) -> None:
    ip = None
    ua = None
    if request is not None:
        # Za Traefikiem prawdziwy adres jest w X-Forwarded-For.
        fwd = request.headers.get("x-forwarded-for")
        ip = (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None))
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


Protected = Depends(current_user)
