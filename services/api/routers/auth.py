"""Logowanie, sesje, TOTP.

Zgodnie z OWASP Session Management / Password Storage:
  - Argon2id 64 MiB / t=3 / p=4 (powyzej minimum 19 MiB / t=2 / p=1)
  - identyfikator sesji 256 bitow z CSPRNG, w bazie wylacznie SHA-256
  - NOWA sesja przy kazdym logowaniu i przy zmianie uprawnien (session fixation)
  - ciasteczko HttpOnly + Secure + SameSite=Strict
  - blokada konta po serii nieudanych prob, z rosnacym opoznieniem
"""
from __future__ import annotations

import asyncio
import base64
import io
import secrets
from datetime import datetime, timedelta, timezone

import qrcode
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..auth import (
    CSRF_COOKIE, audit, clear_session_cookies, current_user, require_csrf,
    set_session_cookies,
)
from ..db import execute, fetch_all, fetch_one
from ..notifications import powiadom_o_bezpieczenstwie
from ..security import (
    SESSION_TTL, backup_code_hash, hash_password, new_backup_codes, new_session_token,
    new_totp_secret, session_expiry, totp_uri, verify_password, verify_totp,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

MAX_FAILED = 5
LOCKOUT = timedelta(minutes=15)


class LoginBody(BaseModel):
    login: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=512)


class MfaBody(BaseModel):
    challenge: str = Field(min_length=10, max_length=256)
    code: str = Field(min_length=6, max_length=32)


class TotpConfirmBody(BaseModel):
    code: str = Field(min_length=6, max_length=10)


class PasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=12, max_length=512)


async def _start_session(user_id: int, mfa_ok: bool, request: Request,
                         response: Response) -> None:
    token, token_hash = new_session_token()
    csrf = secrets.token_urlsafe(32)
    fwd = request.headers.get("x-forwarded-for")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)

    await execute(
        "INSERT INTO app.session (user_id, token_hash, expires_at, mfa_satisfied, ip, user_agent)"
        " VALUES (%s,%s,%s,%s,%s,%s)",
        (user_id, token_hash, session_expiry(), mfa_ok, ip, request.headers.get("user-agent")),
    )
    set_session_cookies(response, token, csrf, int(SESSION_TTL.total_seconds()))


@router.get("/me")
async def me(user: dict = Depends(current_user)):
    totp = await fetch_one(
        "SELECT confirmed_at IS NOT NULL AS enabled FROM app.totp_secret WHERE user_id = %s",
        (user["id"],),
    )
    return {
        "login": user["login"],
        "role": user["role"],
        "totp_enabled": bool(totp and totp["enabled"]),
        "session_expires_at": user["expires_at"].isoformat(),
    }


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    row = await fetch_one(
        "SELECT id, login, password_hash, is_active, failed_logins, locked_until"
        " FROM app.user WHERE login = %s",
        (body.login.strip(),),
    )

    # Stala odpowiedz niezaleznie od tego, czy login istnieje - inaczej czas
    # odpowiedzi i tresc bledu pozwalaja wyliczyc liste kont.
    generic = HTTPException(status_code=401, detail="Nieprawidlowy login lub haslo")

    if not row:
        # Symulujemy koszt weryfikacji hasla, zeby czas odpowiedzi nie zdradzal
        # nieistniejacego konta.
        await asyncio.sleep(0.25)
        raise generic

    now = datetime.now(timezone.utc)
    if row["locked_until"] and row["locked_until"] > now:
        left = int((row["locked_until"] - now).total_seconds())
        raise HTTPException(status_code=429,
                            detail=f"Konto zablokowane po nieudanych probach. Sprobuj za {left} s.")

    ok, rehash = verify_password(row["password_hash"], body.password)
    if not ok:
        failed = (row["failed_logins"] or 0) + 1
        lock = now + LOCKOUT if failed >= MAX_FAILED else None
        await execute(
            "UPDATE app.user SET failed_logins = %s, locked_until = %s WHERE id = %s",
            (failed, lock, row["id"]),
        )
        await audit("auth.login_failed", user_id=row["id"], request=request)
        raise generic

    if not row["is_active"]:
        raise HTTPException(status_code=403, detail="Konto nieaktywne")

    if rehash:
        await execute("UPDATE app.user SET password_hash = %s WHERE id = %s", (rehash, row["id"]))

    await execute(
        "UPDATE app.user SET failed_logins = 0, locked_until = NULL, last_login_at = now()"
        " WHERE id = %s", (row["id"],),
    )

    totp = await fetch_one(
        "SELECT secret_enc FROM app.totp_secret WHERE user_id = %s AND confirmed_at IS NOT NULL",
        (row["id"],),
    )

    if totp:
        # Drugi skladnik wlaczony: sesja powstaje dopiero po podaniu kodu.
        # Do tego czasu wydajemy krotkotrwaly bilet, ktory sam w sobie nic nie otwiera.
        challenge, challenge_hash = new_session_token()
        await execute(
            "INSERT INTO app.session (user_id, token_hash, expires_at, mfa_satisfied)"
            " VALUES (%s,%s,%s,false)",
            (row["id"], challenge_hash, now + timedelta(minutes=5)),
        )
        await audit("auth.password_ok_mfa_pending", user_id=row["id"], request=request)
        return {"mfa_required": True, "challenge": challenge}

    await _start_session(row["id"], True, request, response)
    await audit("auth.login", user_id=row["id"], request=request)
    await _zglos_nowe_urzadzenie(row["id"], request)
    return {"mfa_required": False, "login": row["login"]}


async def _zglos_nowe_urzadzenie(user_id: int, request: Request) -> None:
    """Powiadomienie o logowaniu z pary (adres, przegladarka), ktorej nie bylo.

    DEFINICJA "NOWEGO URZADZENIA" JEST TAKA, NA JAKA POZWALAJA DANE

    Dziennik audytu zapisuje `ip` i `user_agent` przy kazdym zdarzeniu. Nowe
    urzadzenie to para, ktora nie wystapila przy zadnym WCZESNIEJSZYM udanym
    logowaniu tego uzytkownika. To nie jest odcisk przegladarki ani rozpoznanie
    sprzetu - i nie udaje, ze jest.

    Skutki tej definicji, ktore trzeba znac: zmiana adresu IP u tego samego
    operatora wywola powiadomienie, a logowanie z tego samego adresu i tej samej
    przegladarki przez kogos innego - nie. Przy systemie trzymajacym klucze API
    gieldy falszywy alarm jest tanszy niz przeoczenie.

    Typ zdarzenia `auth.new_login` jest oznaczony jako `always_on`, wiec
    uzytkownik nie moze go wylaczyc. Tak zaprojektowano to w migracji 0007
    i nikt tego nigdy nie podlaczyl.
    """
    fwd = request.headers.get("x-forwarded-for")
    ip = (fwd.split(",")[0].strip() if fwd
          else (request.client.host if request.client else None))
    ua = request.headers.get("user-agent")

    # `< now()` odcina wpis wlasnie zapisany przez audit() - inaczej kazde
    # logowanie widzialoby samo siebie jako wczesniejsze i nic by nie zglaszalo.
    znane = await fetch_one(
        "SELECT 1 FROM app.audit_log WHERE actor_user_id = %s AND action = 'auth.login'"
        "   AND ip IS NOT DISTINCT FROM %s::inet AND user_agent IS NOT DISTINCT FROM %s"
        "   AND created_at < now() - interval '1 second' LIMIT 1",
        (user_id, ip, ua))
    if znane:
        return
    await powiadom_o_bezpieczenstwie(
        user_id, "auth.new_login", "Logowanie z nowego urządzenia",
        f"Adres: {ip or 'nieznany'}\nPrzeglądarka: {(ua or 'nieznana')[:200]}\n\n"
        f"Jeśli to nie Ty — zmień hasło i usuń klucze API giełdy.",
        severity="warning")


@router.post("/mfa")
async def mfa(body: MfaBody, request: Request, response: Response):
    from ..security import session_token_hash, SecretBox

    pending = await fetch_one(
        "SELECT s.id, s.user_id, s.expires_at, t.secret_enc, t.last_step"
        " FROM app.session s JOIN app.totp_secret t ON t.user_id = s.user_id"
        " WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.mfa_satisfied = false",
        (session_token_hash(body.challenge),),
    )
    if not pending or pending["expires_at"] <= datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Bilet wygasl - zaloguj sie ponownie")

    secret = SecretBox().decrypt(bytes(pending["secret_enc"]))
    code = body.code.strip().replace(" ", "").replace("-", "")

    # Krok czasowy, na ktorym kod przeszedl. Zapisujemy go, zeby ten sam kod
    # nie dal sie uzyc drugi raz w oknie tolerancji zegara (RFC 6238 §5.2).
    step = verify_totp(secret, code, last_step=pending["last_step"])
    accepted = step is not None
    used_backup = False
    if not accepted:
        # Kod zapasowy - jednorazowy, oznaczany jako zuzyty w tej samej transakcji.
        bc = await fetch_one(
            "SELECT id FROM app.backup_code WHERE user_id = %s AND code_hash = %s AND used_at IS NULL",
            (pending["user_id"], backup_code_hash(body.code)),
        )
        if bc:
            await execute("UPDATE app.backup_code SET used_at = now() WHERE id = %s", (bc["id"],))
            accepted = True
            used_backup = True

    if not accepted:
        await audit("auth.mfa_failed", user_id=pending["user_id"], request=request)
        raise HTTPException(status_code=401, detail="Nieprawidlowy kod")

    if step is not None:
        # Zapis PRZED utworzeniem sesji: gdyby kolejny krok zawiodl, kod i tak
        # ma pozostac zuzyty. Odwrotna kolejnosc zostawialaby okno na powtorzenie.
        await execute(
            "UPDATE app.totp_secret SET last_step = %s WHERE user_id = %s",
            (step, pending["user_id"]))

    # Bilet zuzyty. Sesja powstaje NOWA - to jest zabezpieczenie przed session fixation.
    await execute("UPDATE app.session SET revoked_at = now() WHERE id = %s", (pending["id"],))
    await _start_session(pending["user_id"], True, request, response)
    await audit("auth.mfa_backup_code" if used_backup else "auth.mfa",
                user_id=pending["user_id"], request=request)
    return {"ok": True}


@router.post("/logout", dependencies=[Depends(require_csrf)])
async def logout(request: Request, response: Response, user: dict = Depends(current_user)):
    await execute("UPDATE app.session SET revoked_at = now() WHERE id = %s", (user["session_id"],))
    clear_session_cookies(response)
    await audit("auth.logout", user_id=user["id"], request=request)
    return {"ok": True}


@router.post("/password", dependencies=[Depends(require_csrf)])
async def change_password(body: PasswordBody, request: Request,
                          user: dict = Depends(current_user)):
    row = await fetch_one("SELECT password_hash FROM app.user WHERE id = %s", (user["id"],))
    ok, _ = verify_password(row["password_hash"], body.current_password)
    if not ok:
        raise HTTPException(status_code=401, detail="Biezace haslo jest nieprawidlowe")

    await execute("UPDATE app.user SET password_hash = %s WHERE id = %s",
                  (hash_password(body.new_password), user["id"]))
    # Zmiana hasla uniewaznia WSZYSTKIE pozostale sesje - jesli ktos przejal konto,
    # traci dostep w tym momencie.
    await execute(
        "UPDATE app.session SET revoked_at = now() WHERE user_id = %s AND id <> %s AND revoked_at IS NULL",
        (user["id"], user["session_id"]),
    )
    await audit("auth.password_changed", user_id=user["id"], request=request)
    # `credential.changed` obejmuje "zmiane kluczy LUB hasla" - patrz etykieta
    # typu zdarzenia w migracji 0007. Zawsze wlaczone, bo zmiana hasla bez
    # wiedzy wlasciciela to pierwszy krok przejecia konta.
    await powiadom_o_bezpieczenstwie(
        user["id"], "credential.changed", "Hasło zostało zmienione",
        "Jeśli to nie Ty — konto jest przejęte. Wszystkie sesje zostały "
        "unieważnione, ale klucze API giełdy nadal działają.",
        severity="warning")
    return {"ok": True}


# ----------------------------------------------------------------- TOTP
@router.post("/totp/setup", dependencies=[Depends(require_csrf)])
async def totp_setup(request: Request, user: dict = Depends(current_user)):
    """Generuje sekret i kod QR. Sekret jest AKTYWNY dopiero po potwierdzeniu kodem."""
    from ..security import SecretBox

    existing = await fetch_one(
        "SELECT confirmed_at FROM app.totp_secret WHERE user_id = %s", (user["id"],))
    if existing and existing["confirmed_at"]:
        raise HTTPException(status_code=409, detail="Drugi skladnik jest juz wlaczony")

    secret = new_totp_secret()
    await execute(
        "INSERT INTO app.totp_secret (user_id, secret_enc) VALUES (%s,%s)"
        " ON CONFLICT (user_id) DO UPDATE SET secret_enc = EXCLUDED.secret_enc, confirmed_at = NULL",
        (user["id"], SecretBox().encrypt(secret)),
    )

    uri = totp_uri(secret, user["login"])
    buf = io.BytesIO()
    qrcode.make(uri).save(buf, format="PNG")
    await audit("auth.totp_setup_started", user_id=user["id"], request=request)
    return {
        "secret": secret,                     # do recznego wpisania w aplikacji
        "uri": uri,
        "qr_png_base64": base64.b64encode(buf.getvalue()).decode(),
    }


@router.post("/totp/confirm", dependencies=[Depends(require_csrf)])
async def totp_confirm(body: TotpConfirmBody, request: Request,
                       user: dict = Depends(current_user)):
    from ..security import SecretBox

    row = await fetch_one(
        "SELECT secret_enc, last_step FROM app.totp_secret WHERE user_id = %s", (user["id"],))
    if not row:
        raise HTTPException(status_code=404, detail="Najpierw uruchom konfiguracje")
    step = verify_totp(SecretBox().decrypt(bytes(row["secret_enc"])), body.code,
                       last_step=row["last_step"])
    if step is None:
        raise HTTPException(status_code=401, detail="Kod nie pasuje - sprawdz zegar urzadzenia")

    await execute("UPDATE app.totp_secret SET confirmed_at = now(), last_step = %s"
                  " WHERE user_id = %s", (step, user["id"]))
    await execute("DELETE FROM app.backup_code WHERE user_id = %s", (user["id"],))

    codes = new_backup_codes()
    for raw, h in codes:
        await execute("INSERT INTO app.backup_code (user_id, code_hash) VALUES (%s,%s)",
                      (user["id"], h))
    await audit("auth.totp_enabled", user_id=user["id"], request=request)
    # Kody pokazywane RAZ - w bazie sa tylko ich hashe.
    return {"ok": True, "backup_codes": [c for c, _ in codes]}


@router.post("/totp/disable", dependencies=[Depends(require_csrf)])
async def totp_disable(body: PasswordBody, request: Request,
                       user: dict = Depends(current_user)):
    row = await fetch_one("SELECT password_hash FROM app.user WHERE id = %s", (user["id"],))
    ok, _ = verify_password(row["password_hash"], body.current_password)
    if not ok:
        raise HTTPException(status_code=401, detail="Haslo jest nieprawidlowe")
    await execute("DELETE FROM app.totp_secret WHERE user_id = %s", (user["id"],))
    await execute("DELETE FROM app.backup_code WHERE user_id = %s", (user["id"],))
    await audit("auth.totp_disabled", user_id=user["id"], request=request)
    return {"ok": True}


@router.get("/sessions")
async def sessions(user: dict = Depends(current_user)):
    rows = await fetch_all(
        "SELECT id, created_at, expires_at, ip, user_agent, (id = %s) AS current"
        " FROM app.session WHERE user_id = %s AND revoked_at IS NULL AND expires_at > now()"
        " ORDER BY created_at DESC",
        (user["session_id"], user["id"]),
    )
    return [
        {"id": r["id"], "created_at": r["created_at"].isoformat(),
         "expires_at": r["expires_at"].isoformat(), "ip": str(r["ip"]) if r["ip"] else None,
         "user_agent": r["user_agent"], "current": r["current"]}
        for r in rows
    ]
