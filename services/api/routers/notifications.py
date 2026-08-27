"""API powiadomien i ustawien konta."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..auth import audit, current_user, require_csrf
from ..db import execute, fetch_all, fetch_one
from ..notifications import notify
from ..security import SecretBox

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


class ChannelBody(BaseModel):
    kind: str = Field(pattern="^(telegram|webhook)$")
    label: str = Field(min_length=1, max_length=64)
    bot_token: str | None = Field(default=None, max_length=200)
    chat_id: str | None = Field(default=None, max_length=64)
    url: str | None = Field(default=None, max_length=500)
    secret: str | None = Field(default=None, max_length=200)


class PreferenceBody(BaseModel):
    event_code: str
    enabled: bool
    channels: list[str]
    cooldown_s: int | None = Field(default=None, ge=0, le=86400)


class QuietBody(BaseModel):
    quiet_from: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    quiet_to: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    timezone: str = Field(default="Europe/Warsaw", max_length=64)
    daily_digest: bool = True
    digest_at: str = Field(default="08:00", pattern=r"^\d{2}:\d{2}$")


# ------------------------------------------------------------------ dzwonek
@router.get("")
async def list_notifications(unread_only: bool = Query(False), limit: int = Query(50, le=200),
                             user: dict = Depends(current_user)):
    rows = await fetch_all(
        """
        SELECT n.id, n.event_code, n.severity, n.title, n.body, n.data, n.engine,
               n.created_at, n.read_at, e.label AS event_label, e.category
        FROM notify.notification n
        JOIN notify.event_type e ON e.code = n.event_code
        WHERE n.user_id = %s AND (NOT %s OR n.read_at IS NULL)
        ORDER BY n.created_at DESC LIMIT %s
        """,
        (user["id"], unread_only, limit),
    )
    return [
        {
            "id": r["id"], "event_code": r["event_code"], "event_label": r["event_label"],
            "category": r["category"], "severity": r["severity"], "title": r["title"],
            "body": r["body"], "data": r["data"], "engine": r["engine"],
            "created_at": r["created_at"].isoformat(),
            "read": r["read_at"] is not None,
        }
        for r in rows
    ]


@router.get("/unread-count")
async def unread_count(user: dict = Depends(current_user)):
    row = await fetch_one(
        "SELECT count(*) AS n, count(*) FILTER (WHERE severity = 'critical') AS krytyczne"
        " FROM notify.notification WHERE user_id = %s AND read_at IS NULL", (user["id"],))
    return {"unread": row["n"], "critical": row["krytyczne"]}


@router.post("/read", dependencies=[Depends(require_csrf)])
async def mark_read(ids: list[int] | None = None, user: dict = Depends(current_user)):
    if ids:
        await execute(
            "UPDATE notify.notification SET read_at = now()"
            " WHERE user_id = %s AND id = ANY(%s) AND read_at IS NULL", (user["id"], ids))
    else:
        await execute("UPDATE notify.notification SET read_at = now()"
                      " WHERE user_id = %s AND read_at IS NULL", (user["id"],))
    return {"ok": True}


# ------------------------------------------------------------------ kanaly
@router.get("/channels")
async def list_channels(user: dict = Depends(current_user)):
    rows = await fetch_all(
        "SELECT id, kind::text AS kind, label, hint, enabled, verified_at, last_error,"
        " last_sent_at, created_at"
        " FROM notify.channel WHERE user_id = %s ORDER BY kind, label", (user["id"],))
    return [
        {
            "id": r["id"], "kind": r["kind"], "label": r["label"], "hint": r["hint"],
            "enabled": r["enabled"],
            "verified": r["verified_at"] is not None,
            "last_error": r["last_error"],
            "last_sent_at": r["last_sent_at"].isoformat() if r["last_sent_at"] else None,
        }
        for r in rows
    ]


@router.post("/channels", dependencies=[Depends(require_csrf)])
async def add_channel(body: ChannelBody, request: Request, user: dict = Depends(current_user)):
    if body.kind == "telegram":
        if not body.bot_token or not body.chat_id:
            raise HTTPException(400, "Telegram wymaga tokenu bota i identyfikatora czatu")
        config = {"bot_token": body.bot_token.strip(), "chat_id": body.chat_id.strip()}
        hint = "czat " + body.chat_id.strip()[-6:]
    else:
        if not body.url or not body.url.startswith("https://"):
            raise HTTPException(400, "Webhook wymaga adresu zaczynającego się od https://")
        config = {"url": body.url.strip(), "secret": (body.secret or "").strip()}
        hint = body.url.split("/")[2][:40]

    await execute(
        "INSERT INTO notify.channel (user_id, kind, label, config_enc, hint)"
        " VALUES (%s,%s,%s,%s,%s)"
        " ON CONFLICT (user_id, kind, label) DO UPDATE"
        "   SET config_enc = EXCLUDED.config_enc, hint = EXCLUDED.hint,"
        "       verified_at = NULL, last_error = NULL",
        (user["id"], body.kind, body.label.strip(),
         SecretBox().encrypt(json.dumps(config)), hint),
    )
    await audit("notify.channel_added", user_id=user["id"], entity="notify.channel",
                entity_id=body.label, request=request)
    return {"ok": True}


@router.post("/channels/{channel_id}/test", dependencies=[Depends(require_csrf)])
async def test_channel(channel_id: int, user: dict = Depends(current_user)):
    """Wysyla wiadomosc testowa. Sukces oznacza kanal zweryfikowany."""
    from ..notifications import _send_telegram, _send_webhook

    ch = await fetch_one(
        "SELECT id, kind::text AS kind, config_enc, label FROM notify.channel"
        " WHERE id = %s AND user_id = %s",
        (channel_id, user["id"]))
    if not ch:
        raise HTTPException(404, "Nie ma takiego kanału")
    if ch["kind"] == "inapp":
        return {"ok": True, "message": "Kanał w aplikacji nie wymaga weryfikacji."}

    config = json.loads(SecretBox().decrypt(bytes(ch["config_enc"])))
    try:
        if ch["kind"] == "telegram":
            await _send_telegram(config, "Trader AI — test kanału",
                                 "Jeśli to widzisz, powiadomienia działają.", "info")
        else:
            await _send_webhook(config, {"event": "channel.test", "severity": "info",
                                         "title": "Trader AI — test kanału"})
    except Exception as e:                                     # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"[:400]
        await execute("UPDATE notify.channel SET last_error = %s, verified_at = NULL"
                      " WHERE id = %s", (msg, channel_id))
        return {"ok": False, "message": msg}

    await execute("UPDATE notify.channel SET verified_at = now(), last_error = NULL,"
                  " last_sent_at = now() WHERE id = %s", (channel_id,))
    return {"ok": True, "message": "Wiadomość testowa wysłana."}


@router.delete("/channels/{channel_id}", dependencies=[Depends(require_csrf)])
async def delete_channel(channel_id: int, request: Request, user: dict = Depends(current_user)):
    row = await fetch_one("SELECT kind::text AS kind, label FROM notify.channel"
                          " WHERE id = %s AND user_id = %s",
                          (channel_id, user["id"]))
    if not row:
        raise HTTPException(404, "Nie ma takiego kanału")
    if row["kind"] == "inapp":
        raise HTTPException(400, "Kanału w aplikacji nie można usunąć")
    await execute("DELETE FROM notify.channel WHERE id = %s", (channel_id,))
    await audit("notify.channel_removed", user_id=user["id"], entity_id=row["label"],
                request=request)
    return {"ok": True}


# ------------------------------------------------------------------ preferencje
@router.get("/preferences")
async def preferences(user: dict = Depends(current_user)):
    rows = await fetch_all(
        """
        SELECT e.code, e.label, e.description, e.category, e.default_severity,
               e.always_on, e.default_cooldown_s,
               COALESCE(p.enabled, true) AS enabled,
               COALESCE(p.channels, '{inapp}')::text[] AS channels,
               p.cooldown_s
        FROM notify.event_type e
        LEFT JOIN notify.preference p ON p.event_code = e.code AND p.user_id = %s
        ORDER BY e.category, e.code
        """,
        (user["id"],),
    )
    return [
        {
            "code": r["code"], "label": r["label"], "description": r["description"],
            "category": r["category"], "severity": r["default_severity"],
            "always_on": r["always_on"],
            "enabled": r["always_on"] or r["enabled"],
            "channels": list(r["channels"]),
            "cooldown_s": r["cooldown_s"] if r["cooldown_s"] is not None else r["default_cooldown_s"],
        }
        for r in rows
    ]


@router.put("/preferences", dependencies=[Depends(require_csrf)])
async def set_preference(body: PreferenceBody, user: dict = Depends(current_user)):
    et = await fetch_one("SELECT always_on FROM notify.event_type WHERE code = %s",
                         (body.event_code,))
    if not et:
        raise HTTPException(404, "Nieznany typ zdarzenia")
    if et["always_on"] and not body.enabled:
        raise HTTPException(
            400,
            "Tego alertu nie można wyłączyć. Kill-switch albo martwy silnik, o których "
            "nie wiesz, są groźniejsze niż ich brak — dają złudzenie kontroli.",
        )
    await execute(
        "INSERT INTO notify.preference (user_id, event_code, enabled, channels, cooldown_s)"
        " VALUES (%s,%s,%s,%s,%s)"
        " ON CONFLICT (user_id, event_code) DO UPDATE"
        "   SET enabled = EXCLUDED.enabled, channels = EXCLUDED.channels,"
        "       cooldown_s = EXCLUDED.cooldown_s",
        (user["id"], body.event_code, body.enabled, body.channels, body.cooldown_s),
    )
    return {"ok": True}


@router.get("/quiet")
async def get_quiet(user: dict = Depends(current_user)):
    row = await fetch_one(
        "SELECT quiet_from, quiet_to, timezone, daily_digest, digest_at"
        " FROM notify.settings WHERE user_id = %s", (user["id"],))
    if not row:
        return {"quiet_from": None, "quiet_to": None, "timezone": "Europe/Warsaw",
                "daily_digest": True, "digest_at": "08:00"}
    return {
        "quiet_from": row["quiet_from"].strftime("%H:%M") if row["quiet_from"] else None,
        "quiet_to": row["quiet_to"].strftime("%H:%M") if row["quiet_to"] else None,
        "timezone": row["timezone"], "daily_digest": row["daily_digest"],
        "digest_at": row["digest_at"].strftime("%H:%M"),
    }


@router.put("/quiet", dependencies=[Depends(require_csrf)])
async def set_quiet(body: QuietBody, user: dict = Depends(current_user)):
    await execute(
        "INSERT INTO notify.settings (user_id, quiet_from, quiet_to, timezone, daily_digest, digest_at)"
        " VALUES (%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (user_id) DO UPDATE SET quiet_from = EXCLUDED.quiet_from,"
        "   quiet_to = EXCLUDED.quiet_to, timezone = EXCLUDED.timezone,"
        "   daily_digest = EXCLUDED.daily_digest, digest_at = EXCLUDED.digest_at",
        (user["id"], body.quiet_from, body.quiet_to, body.timezone,
         body.daily_digest, body.digest_at),
    )
    return {"ok": True}


@router.post("/selftest", dependencies=[Depends(require_csrf)])
async def selftest(user: dict = Depends(current_user)):
    """Wysyla powiadomienie przez pelna sciezke - z regulami, wyciszeniem i kanalami."""
    nid = await notify(
        user["id"], "postmortem.ready",
        "Test powiadomień",
        "To powiadomienie przeszło pełną ścieżkę: reguły, godziny ciszy i wszystkie "
        "skonfigurowane kanały.",
        data={"test": True},
    )
    return {"ok": nid is not None,
            "message": "Wysłane." if nid else "Wyciszone przez ustawienia lub częstotliwość."}
