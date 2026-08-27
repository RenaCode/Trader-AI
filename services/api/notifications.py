"""Wysylka powiadomien: reguly, wyciszenie, ograniczenie czestotliwosci, kanaly.

Jedno wejscie dla calego systemu: `notify(...)`. Kazde zdarzenie najpierw laduje
w tabeli powiadomien (zrodlo prawdy), a dopiero potem sa proby dostarczenia -
dzieki temu awaria Telegrama nie gubi zdarzenia, tylko jego doreczenie.

Dwie reguly, ktorych nie da sie obejsc ustawieniami:
  - zdarzenie `always_on` dochodzi zawsze, takze w godzinach ciszy
  - kill-switch i martwy silnik nie maja czasu wyciszenia
Alert krytyczny, ktory nie dotarl, jest gorszy niz jego brak, bo daje zludzenie
kontroli.
"""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from .db import execute, fetch_all, fetch_one
from .security import SecretBox


def _in_quiet_hours(now_local: time, start: time | None, end: time | None) -> bool:
    if not start or not end:
        return False
    if start <= end:
        return start <= now_local < end
    # Zakres przechodzacy przez polnoc, np. 23:00-07:00.
    return now_local >= start or now_local < end


async def _send_telegram(config: dict, title: str, body: str, severity: str) -> None:
    icon = {"critical": "\U0001F534", "warning": "\U0001F7E0", "info": "\U0001F535"}[severity]
    text = f"{icon} *{title}*\n\n{body}" if body else f"{icon} *{title}*"
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post(
            f"https://api.telegram.org/bot{config['bot_token']}/sendMessage",
            json={"chat_id": config["chat_id"], "text": text[:4000],
                  "parse_mode": "Markdown", "disable_web_page_preview": True},
        )
        if r.status_code != 200:
            raise RuntimeError(f"Telegram zwrocil {r.status_code}: {r.text[:200]}")


async def _send_webhook(config: dict, payload: dict) -> None:
    headers = {"Content-Type": "application/json"}
    if config.get("secret"):
        # Prosty wspoldzielony sekret w naglowku - odbiorca moze odrzucic
        # zadania, ktore nie pochodza od nas.
        headers["X-Trader-Secret"] = config["secret"]
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post(config["url"], json=payload, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"Webhook zwrocil {r.status_code}")


async def notify(user_id: int, event_code: str, title: str, body: str = "",
                 *, data: dict | None = None, engine: str | None = None,
                 severity: str | None = None) -> int | None:
    """Rejestruje zdarzenie i probuje je dostarczyc. Zwraca id powiadomienia."""
    et = await fetch_one(
        "SELECT code, default_severity, always_on, default_cooldown_s, label"
        " FROM notify.event_type WHERE code = %s", (event_code,))
    if not et:
        raise ValueError(f"nieznany typ zdarzenia: {event_code}")

    sev = severity or et["default_severity"]

    # channels::text[] jest konieczne: psycopg nie zna typu notify.channel_kind[]
    # i bez rzutowania zwraca go jako tekst '{inapp}', ktory set() rozbija na
    # pojedyncze znaki - kanaly nigdy by sie nie dopasowaly.
    pref = await fetch_one(
        "SELECT enabled, channels::text[] AS channels, cooldown_s FROM notify.preference"
        " WHERE user_id = %s AND event_code = %s", (user_id, event_code))
    enabled = et["always_on"] or (pref["enabled"] if pref else True)
    if not enabled:
        return None

    cooldown = (pref["cooldown_s"] if pref and pref["cooldown_s"] is not None
                else et["default_cooldown_s"])
    if cooldown and not et["always_on"]:
        recent = await fetch_one(
            "SELECT 1 FROM notify.notification WHERE user_id = %s AND event_code = %s"
            " AND created_at > now() - make_interval(secs => %s) LIMIT 1",
            (user_id, event_code, cooldown))
        if recent:
            # Cisza z powodu czestotliwosci jest normalnym dzialaniem, nie bledem -
            # nie zapisujemy jej jako zdarzenia, zeby nie zasmiecac dzwonka.
            return None

    row = await fetch_one(
        "INSERT INTO notify.notification (user_id, event_code, severity, title, body, data, engine)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (user_id, event_code, sev, title, body, json.dumps(data or {}), engine))
    nid = row["id"]

    # --- godziny ciszy -------------------------------------------------------
    settings = await fetch_one(
        "SELECT quiet_from, quiet_to, timezone FROM notify.settings WHERE user_id = %s",
        (user_id,))
    quiet = False
    if settings and not et["always_on"] and sev != "critical":
        try:
            local = datetime.now(ZoneInfo(settings["timezone"])).time()
        except Exception:                                     # noqa: BLE001
            local = datetime.now(timezone.utc).time()
        quiet = _in_quiet_hours(local, settings["quiet_from"], settings["quiet_to"])

    wanted = set(pref["channels"] if pref else ["inapp"])
    if et["always_on"]:
        # Zdarzenie krytyczne idzie wszystkimi skonfigurowanymi kanalami,
        # niezaleznie od preferencji dla tego typu.
        wanted = {"inapp", "telegram", "webhook"}

    channels = await fetch_all(
        "SELECT id, kind::text AS kind, config_enc, label FROM notify.channel"
        " WHERE user_id = %s AND enabled AND kind::text = ANY(%s)",
        (user_id, sorted(wanted)))

    box = SecretBox()
    for ch in channels:
        if ch["kind"] == "inapp":
            await execute(
                "INSERT INTO notify.delivery (notification_id, channel_id, kind, status, sent_at)"
                " VALUES (%s,%s,'inapp','sent', now())", (nid, ch["id"]))
            continue

        if quiet:
            await execute(
                "INSERT INTO notify.delivery (notification_id, channel_id, kind, status)"
                " VALUES (%s,%s,%s,'suppressed_quiet')", (nid, ch["id"], ch["kind"]))
            continue

        try:
            config = json.loads(box.decrypt(bytes(ch["config_enc"]))) if ch["config_enc"] else {}
            if ch["kind"] == "telegram":
                await _send_telegram(config, title, body, sev)
            else:
                await _send_webhook(config, {
                    "event": event_code, "severity": sev, "title": title,
                    "body": body, "engine": engine, "data": data or {},
                    "at": datetime.now(timezone.utc).isoformat(),
                })
            await execute(
                "INSERT INTO notify.delivery (notification_id, channel_id, kind, status, attempts, sent_at)"
                " VALUES (%s,%s,%s,'sent',1, now())", (nid, ch["id"], ch["kind"]))
            await execute("UPDATE notify.channel SET last_sent_at = now(), last_error = NULL"
                          " WHERE id = %s", (ch["id"],))
        except Exception as e:                                # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"[:400]
            await execute(
                "INSERT INTO notify.delivery (notification_id, channel_id, kind, status, attempts, error)"
                " VALUES (%s,%s,%s,'failed',1,%s)", (nid, ch["id"], ch["kind"], msg))
            await execute("UPDATE notify.channel SET last_error = %s WHERE id = %s",
                          (msg, ch["id"]))

    return nid
