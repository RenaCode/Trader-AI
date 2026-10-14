"""Zaleznosci uwierzytelniania: odczyt sesji z ciasteczka, ochrona endpointow, CSRF.

Model sesyjny (nie JWT) jest tu celowy: sesje da sie natychmiast uniewaznic po
stronie serwera, co dla systemu obracajacego pieniedzmi jest wazniejsze niz
bezstanowosc. JWT bez listy odwolan nie da sie wylogowac przed wygasnieciem.
"""
from __future__ import annotations

import hmac
import ipaddress
import secrets
from datetime import datetime, timezone

from fastapi import Cookie, Depends, Header, HTTPException, Request, Response
from psycopg.types.json import Jsonb

from . import sso
from .db import fetch_one, execute
from .security import (SESSION_COOKIE, SESSION_IDLE, SESSION_TOUCH_EVERY, SESSION_TTL,
                       new_session_token, session_expiry, session_token_hash)

# `__Host-` jak SESSION_COOKIE (security.py); front czyta te nazwe w utils/api.js.
CSRF_COOKIE = "__Host-trader_csrf"
CSRF_HEADER = "x-csrf-token"


SQL_SESJA_Z_TOKENU = """
    SELECT s.id AS session_id, s.expires_at, s.mfa_satisfied, s.last_seen_at, s.sso,
           u.id, u.login, u.role, u.is_active
    FROM app.session s
    JOIN app.user u ON u.id = s.user_id
    WHERE s.token_hash = %s AND s.revoked_at IS NULL
"""


async def current_user(
    request: Request,
    # Domyslne None tylko dla wolajacych wprost (testy); FastAPI wstrzykuje
    # odpowiedz po samym typie.
    response: Response = None,
    trader_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> dict:
    """Zwraca uzytkownika albo 401. Uzywane jako Depends na chronionych routerach.

    Przy `SSO_NAGLOWKI=1` i obecnym `Remote-User` (services/api/sso.py) sesja
    aplikacji musi nalezec do TEGO loginu. Brak sesji, sesja martwa albo
    cudza = nowa sesja `sso` bez hasla i bez TOTP aplikacji (Authelia wymusila
    2FA, zanim zadanie tu doszlo). Bez naglowka - sesja z hasla jak dotad,
    takze przy wlaczonej fladze: to jest droga awaryjna przez `kubectl
    port-forward`, gdy Authelia lezy. Sesja `sso` bez naglowka - 401.
    """
    sso_login = sso.login_z_naglowka(request)
    if sso_login is None:
        row = await _uzytkownik_z_sesji(request, trader_session)
        if row.get("sso"):
            # Sesja `sso` nie przeszla hasla ani TOTP aplikacji - jej jedynym
            # dowodem jest naglowek Authelii. Bez niego (port-forward, flaga
            # wylaczona po wycofaniu SSO) to token na okaziciela: skopiowane
            # ciasteczko nie moze ominac hasla i TOTP drogi awaryjnej.
            raise HTTPException(status_code=401,
                                detail="Sesja SSO jest wazna tylko przez portal logowania")
        return row
    if trader_session:
        try:
            row = await _uzytkownik_z_sesji(request, trader_session)
        except HTTPException as e:
            if e.status_code != 401:
                raise
        else:
            if row["login"] == sso_login:
                return _rola_wedlug_grupy(row, request)
            # W tej przegladarce Authelia ma juz kogo innego. Sesja poprzedniej
            # osoby nie ma prawa przezyc w ciasteczku obok - uniewazniona, nie
            # tylko pominieta.
            await execute("UPDATE app.session SET revoked_at = now()"
                          " WHERE id = %s AND revoked_at IS NULL", (row["session_id"],))
            await audit("auth.sso_zmiana_uzytkownika", user_id=row["id"], request=request)
    return _rola_wedlug_grupy(await _sesja_sso(sso_login, request, response), request)


def _rola_wedlug_grupy(row: dict, request: Request) -> dict:
    """Sesja wpuszczona tylko przez grupe podgladu: rola `viewer` niezaleznie
    od bazy (sso.py, `tylko_podglad`). Kopia, nie zmiana `row`."""
    if row.get("role") != "viewer" and sso.tylko_podglad(request):
        return {**row, "role": "viewer"}
    return row


async def _uzytkownik_z_sesji(request: Request, trader_session: str | None) -> dict:
    if not trader_session:
        raise HTTPException(status_code=401, detail="Wymagane zalogowanie")

    row = await fetch_one(SQL_SESJA_Z_TOKENU, (session_token_hash(trader_session),))
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


async def _sesja_sso(login: str, request: Request, response: Response) -> dict:
    """Nowa sesja `sso` dla ISTNIEJACEGO konta o loginie == `Remote-User`.

    Bez zakladania kont w locie (kontrakt, pkt 3): konto niesie role, klucze
    gield i kanaly powiadomien, wiec brak dopasowania to 403 z komunikatem,
    a nie puste konto `owner`.

    BLOKADA KONTA OBOWIAZUJE TAKZE TUTAJ. Piec zlych hasel przy ponownym
    potwierdzeniu (`potwierdz_haslo`) blokuje konto i uniewaznia sesje,
    z ktorej zgadywano. Bez tej kontroli nastepne zadanie z naglowkiem
    zakladaloby od razu nowa sesje i blokada nie odcinalaby niczego.

    Ciasteczka ida przez `response` zaleznosci - FastAPI dokleja je do
    odpowiedzi endpointu. Pierwsze zadanie panelu to `GET /api/auth/me`, wiec
    sesja i token CSRF sa na miejscu, zanim cokolwiek zostanie zapisane.
    """
    konto = await fetch_one(
        "SELECT id, is_active, locked_until FROM app.user WHERE login = %s", (login,))
    if not konto:
        await audit("auth.sso_brak_konta", entity="user", entity_id=login, request=request)
        raise HTTPException(
            status_code=403,
            detail=f"Brak konta w Traderze dla użytkownika „{login}” — poproś "
                   "administratora o konto z tym samym loginem co w Authelii.")
    if not konto["is_active"]:
        raise HTTPException(status_code=403, detail="Konto nieaktywne")
    teraz = datetime.now(timezone.utc)
    if konto["locked_until"] and konto["locked_until"] > teraz:
        left = int((konto["locked_until"] - teraz).total_seconds())
        raise HTTPException(status_code=429,
                            detail=f"Konto zablokowane po nieudanych probach. Sprobuj za {left} s.")

    token, token_hash = new_session_token()
    csrf = secrets.token_urlsafe(32)
    await execute(
        "INSERT INTO app.session (user_id, token_hash, expires_at, mfa_satisfied, sso, ip, user_agent)"
        " VALUES (%s,%s,%s,true,true,%s,%s)",
        (konto["id"], token_hash, session_expiry(), adres_klienta(request),
         request.headers.get("user-agent")))
    set_session_cookies(response, token, csrf, int(SESSION_TTL.total_seconds()))
    await audit("auth.sso_login", user_id=konto["id"], request=request)
    # Alarm `always_on` o nowym urzadzeniu takze dla SSO - inaczej wlaczenie
    # SSO wylaczaloby go po cichu (ta sama wada co kiedys przy TOTP, patrz
    # `routers/auth.py:/mfa`). Oslona jak tam: blad wykrywania nie wywraca
    # logowania. Import leniwy: routers.auth importuje ten modul.
    from .routers.auth import _zglos_nowe_urzadzenie
    try:
        await _zglos_nowe_urzadzenie(konto["id"], request)
    except Exception as e:                                    # noqa: BLE001
        print(f"[powiadomienie] wykrywanie nowego urządzenia zawiodło: "
              f"{type(e).__name__}: {e}", flush=True)

    row = await fetch_one(SQL_SESJA_Z_TOKENU, (token_hash,))
    request.state.user_id = row["id"]
    request.state.session_id = row["session_id"]
    return row


async def wymagaj_wlasciciela(user: dict = Depends(current_user)) -> dict:
    """Operacje zmieniajace stan sa dla roli `owner`, nie dla kazdej waznej sesji.

    DLACZEGO TO ISTNIEJE

    Rola jest w schemacie od migracji 0007 (`CHECK (role IN ('owner','viewer'))`),
    jest pobierana w `current_user`, zwracana przez `/auth/me` i pokazywana
    w `Profile` - tak, jakby cos znaczyla. Nie znaczyla nic: `grep -rn "role"
    services/api/` dawal same odczyty do odpowiedzi, ani jednego porownania.
    Ochrona byla deklarowana raz, jako `Depends(current_user)` na calym
    routerze, a `current_user` sprawdza wylacznie waznosc sesji.

    Do 14.10 konta `viewer` nie dalo sie zalozyc - `scripts/create_admin.py`
    wpisywal na sztywno `'owner'`. Od 14.10 `create_admin.py --rola viewer`
    zaklada je naprawde (podglad dla rodziny przez SSO), wiec bez tej funkcji
    luka bylaby CZYNNA:
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
