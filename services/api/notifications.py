"""Wysylka powiadomien: reguly, wyciszenie, ograniczenie czestotliwosci, kanaly.

Jedno wejscie dla calego systemu: `notify(...)`. Kazde zdarzenie najpierw laduje
w tabeli powiadomien (zrodlo prawdy), a dopiero potem sa proby dostarczenia -
dzieki temu awaria bramki nie gubi zdarzenia, tylko jego doreczenie.

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


# CallMeBot: darmowa bramka do WhatsAppa. Rejestracja polega na wyslaniu jednej
# wiadomosci na ich numer; w odpowiedzi przychodzi klucz przypisany DO TEGO
# NUMERU. Wysylka to jedno GET.
#
# DLACZEGO NIE OFICJALNE API META. Poza 24-godzinnym oknem od ostatniej
# wiadomosci uzytkownika Meta przepuszcza WYLACZNIE zatwierdzone szablony ze
# zmiennymi. Alert dziala wiec podczas testu - bo test robi sie zaraz po
# napisaniu do bota - i przestaje dzialac dobe pozniej. Cicho. Dla systemu,
# ktorego jedynym zadaniem jest odezwac sie, gdy cos jest nie tak, to jest
# najgorszy mozliwy tryb awarii.
#
# Cena za to: CallMeBot jest usluga nieoficjalna i ma wlasne ograniczenie tempa.
# Dlatego kazde niepowodzenie ladu je w notify.delivery ze statusem 'failed'
# i trescia bledu - kanal, ktory przestal dzialac, ma byc widoczny w interfejsie,
# a nie domyslany z ciszy.
WHATSAPP_URL = "https://api.callmebot.com/whatsapp.php"


async def _send_whatsapp(config: dict, title: str, body: str, severity: str) -> None:
    icon = {"critical": "\U0001F534", "warning": "\U0001F7E0", "info": "\U0001F535"}[severity]
    text = f"{icon} {title}\n\n{body}" if body else f"{icon} {title}"
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.get(WHATSAPP_URL, params={
            "phone": config["telefon"],
            "apikey": config["apikey"],
            "text": text[:900],
        })
        # CallMeBot odpowiada 200 takze wtedy, gdy odmawia - powod siedzi
        # w tresci. Sam kod odpowiedzi nie wystarcza wiec za potwierdzenie.
        tresc = r.text[:300]
        if r.status_code != 200:
            raise RuntimeError(f"CallMeBot zwrocil {r.status_code}: {tresc}")
        nisko = tresc.lower()
        if "apikey" in nisko and ("invalid" in nisko or "not" in nisko):
            raise RuntimeError(f"CallMeBot odrzucil klucz: {tresc}")
        if "error" in nisko:
            raise RuntimeError(f"CallMeBot: {tresc}")


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


async def powiadom_o_bezpieczenstwie(user_id: int, event_code: str, title: str,
                                     body: str = "", **kw) -> None:
    """Powiadomienie, ktore NIE MOZE przerwac operacji, o ktorej powiadamia.

    Zdarzenia bezpieczenstwa - nowe logowanie, zmiana klucza, zmiana hasla - sa
    zgłaszane W TRAKCIE obslugi zadania uzytkownika. Wyjatek z kanalu
    powiadomien (niedostepny WhatsApp, blad webhooka, zerwane polaczenie) nie
    moze wywrocic logowania ani zapisu klucza.

    Mechanizm ostrzegania, ktory potrafi zablokowac chroniona operacje, jest
    gorszy niz jego brak: zamienia awarie kanalu powiadomien w awarie systemu.

    Blad jest polykany, ale nie milczaco - trafia do dziennika procesu.
    """
    try:
        await notify(user_id, event_code, title, body, **kw)
    except Exception as e:                                    # noqa: BLE001
        print(f"[powiadomienie] {event_code} nie zostało wysłane: "
              f"{type(e).__name__}: {e}", flush=True)


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
        wanted = {"inapp", "whatsapp", "webhook"}

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
            if ch["kind"] == "whatsapp":
                await _send_whatsapp(config, title, body, sev)
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
