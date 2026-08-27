"""Ustawienia konta: profil, sesje, dziennik aktywnosci.

Zgodnie z praktyka opisana w badaniach UX ustawien konta:
  - zmiana danych wrazliwych wymaga ponownego potwierdzenia tozsamosci
  - kazda sekcja zapisuje sie osobno i daje wlasna informacje zwrotna
  - uzytkownik widzi swoje aktywne sesje i moze je uniewaznic
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..auth import audit, current_user, require_csrf
from ..db import execute, fetch_all, fetch_one
from ..security import verify_password

router = APIRouter(prefix="/api/account", tags=["account"])


class ProfileBody(BaseModel):
    display_name: str | None = Field(default=None, max_length=64)
    timezone: str = Field(default="Europe/Warsaw", max_length=64)
    locale: str = Field(default="pl", pattern="^(pl|en)$")


class RevokeBody(BaseModel):
    password: str = Field(min_length=1, max_length=512)


@router.get("/profile")
async def profile(user: dict = Depends(current_user)):
    row = await fetch_one(
        "SELECT login, display_name, timezone, locale, role, created_at, last_login_at"
        " FROM app.user WHERE id = %s", (user["id"],))
    totp = await fetch_one(
        "SELECT confirmed_at FROM app.totp_secret WHERE user_id = %s", (user["id"],))
    backup = await fetch_one(
        "SELECT count(*) FILTER (WHERE used_at IS NULL) AS pozostalo, count(*) AS razem"
        " FROM app.backup_code WHERE user_id = %s", (user["id"],))
    return {
        "login": row["login"], "display_name": row["display_name"],
        "timezone": row["timezone"], "locale": row["locale"], "role": row["role"],
        "created_at": row["created_at"].isoformat(),
        "last_login_at": row["last_login_at"].isoformat() if row["last_login_at"] else None,
        "totp_enabled": bool(totp and totp["confirmed_at"]),
        "backup_codes_left": backup["pozostalo"] if backup else 0,
        "backup_codes_total": backup["razem"] if backup else 0,
    }


@router.put("/profile", dependencies=[Depends(require_csrf)])
async def update_profile(body: ProfileBody, request: Request,
                         user: dict = Depends(current_user)):
    await execute(
        "UPDATE app.user SET display_name = %s, timezone = %s, locale = %s WHERE id = %s",
        (body.display_name, body.timezone, body.locale, user["id"]))
    await audit("account.profile_updated", user_id=user["id"], request=request)
    return {"ok": True}


@router.get("/sessions")
async def sessions(user: dict = Depends(current_user)):
    rows = await fetch_all(
        "SELECT id, created_at, expires_at, ip, user_agent, (id = %s) AS current"
        " FROM app.session"
        " WHERE user_id = %s AND revoked_at IS NULL AND expires_at > now()"
        " ORDER BY created_at DESC",
        (user["session_id"], user["id"]))
    return [
        {
            "id": r["id"], "created_at": r["created_at"].isoformat(),
            "expires_at": r["expires_at"].isoformat(),
            "ip": str(r["ip"]) if r["ip"] else None,
            "user_agent": r["user_agent"], "current": r["current"],
        }
        for r in rows
    ]


@router.delete("/sessions/{session_id}", dependencies=[Depends(require_csrf)])
async def revoke_session(session_id: int, request: Request,
                         user: dict = Depends(current_user)):
    if session_id == user["session_id"]:
        raise HTTPException(400, "Bieżącej sesji nie unieważniaj tutaj — użyj wylogowania")
    row = await fetch_one("SELECT 1 FROM app.session WHERE id = %s AND user_id = %s",
                          (session_id, user["id"]))
    if not row:
        raise HTTPException(404, "Nie ma takiej sesji")
    await execute("UPDATE app.session SET revoked_at = now() WHERE id = %s", (session_id,))
    await audit("account.session_revoked", user_id=user["id"], entity_id=str(session_id),
                request=request)
    return {"ok": True}


@router.post("/sessions/revoke-others", dependencies=[Depends(require_csrf)])
async def revoke_others(body: RevokeBody, request: Request,
                        user: dict = Depends(current_user)):
    """Wylogowanie ze wszystkich innych urzadzen.

    Wymaga hasla, bo to operacja obronna - uzywana wtedy, gdy podejrzewasz,
    ze ktos ma dostep do konta. Przejety sesyjny cookie nie powinien wystarczyc
    do jej wykonania ANI do zablokowania prawowitego wlasciciela.
    """
    row = await fetch_one("SELECT password_hash FROM app.user WHERE id = %s", (user["id"],))
    ok, _ = verify_password(row["password_hash"], body.password)
    if not ok:
        raise HTTPException(401, "Hasło jest nieprawidłowe")

    await execute(
        "UPDATE app.session SET revoked_at = now()"
        " WHERE user_id = %s AND id <> %s AND revoked_at IS NULL",
        (user["id"], user["session_id"]))
    await audit("account.sessions_revoked_all", user_id=user["id"], request=request)
    return {"ok": True}


@router.get("/activity")
async def activity(limit: int = Query(50, le=200), user: dict = Depends(current_user)):
    """Dziennik aktywnosci konta - z tabeli audytu, tylko wpisy tego uzytkownika."""
    rows = await fetch_all(
        "SELECT id, at, action, entity, entity_id, ip, user_agent"
        " FROM app.audit_log WHERE actor_user_id = %s ORDER BY at DESC LIMIT %s",
        (user["id"], limit))

    LABELS = {
        "auth.login": "Zalogowano",
        "auth.login_failed": "Nieudana próba logowania",
        "auth.logout": "Wylogowano",
        "auth.mfa": "Potwierdzono drugi składnik",
        "auth.mfa_failed": "Błędny kod drugiego składnika",
        "auth.mfa_backup_code": "Użyto kodu zapasowego",
        "auth.password_changed": "Zmieniono hasło",
        "auth.password_ok_mfa_pending": "Hasło poprawne, oczekiwanie na kod",
        "auth.totp_enabled": "Włączono weryfikację dwuetapową",
        "auth.totp_disabled": "Wyłączono weryfikację dwuetapową",
        "auth.totp_setup_started": "Rozpoczęto konfigurację 2FA",
        "credential.create": "Dodano klucz API giełdy",
        "credential.delete": "Usunięto klucz API giełdy",
        "credential.test": "Sprawdzono połączenie z giełdą",
        "secret.set": "Zapisano klucz usługi",
        "secret.delete": "Usunięto klucz usługi",
        "wallet.add": "Dodano portfel",
        "wallet.delete": "Usunięto portfel",
        "account.profile_updated": "Zaktualizowano profil",
        "account.session_revoked": "Unieważniono sesję",
        "account.sessions_revoked_all": "Wylogowano wszystkie inne urządzenia",
        "notify.channel_added": "Dodano kanał powiadomień",
        "notify.channel_removed": "Usunięto kanał powiadomień",
    }
    return [
        {
            "id": r["id"], "at": r["at"].isoformat(), "action": r["action"],
            "label": LABELS.get(r["action"], r["action"]),
            "entity_id": r["entity_id"],
            "ip": str(r["ip"]) if r["ip"] else None,
            "user_agent": r["user_agent"],
            "suspicious": r["action"] in ("auth.login_failed", "auth.mfa_failed"),
        }
        for r in rows
    ]
