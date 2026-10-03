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

import asyncio
import ipaddress
import json
import socket
from datetime import datetime, time, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
import psycopg

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


# Jedyny komunikat, jaki uzytkownik dostaje o nieudanym webhooku. Rozne tresci
# dla "nie ma takiego hosta", "odmowa polaczenia", "zly certyfikat" i "404"
# zamienialy przycisk "Test kanalu" w skaner uslug TLS wewnatrz klastra:
# odpowiedz mowila, pod ktorym adresem cos slucha. Szczegol idzie do logu.
WEBHOOK_BLAD = "Webhook niedostępny albo odrzucił wiadomość"


class WebhookNiedozwolony(RuntimeError):
    """Adres webhooka prowadzi poza publiczny internet."""


async def _rozwiaz(host: str, port: int) -> list[str]:
    wyniki = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    # Adres IPv6 z zakresem (`fe80::1%eth0`) - `ip_address` nie zna sufiksu,
    # a link-local i tak odpada na `is_global`.
    return [w[4][0].split("%")[0] for w in wyniki]


async def adres_webhooka(url: str) -> tuple[str, str, str]:
    """(url_z_IP, naglowek_Host, nazwa_do_TLS) - po sprawdzeniu, ze KAZDY adres jest publiczny.

    SSRF. Do 2026-10-03 jedynym warunkiem bylo `https://`, wiec webhook na
    `https://kubernetes.default.svc` albo `https://10.43.0.1` wysylal z poda API
    zadanie do wnetrza klastra. Rozwiazujemy nazwe SAMI i odrzucamy, gdy
    ktorykolwiek z adresow nie jest `is_global` (prywatne, petla zwrotna,
    link-local, CGNAT, adresy uslug k3s 10.43/16 i podow 10.42/16).

    PRZYPIECIE ADRESU. Sprawdzenie nazwy, a potem polaczenie po NAZWIE,
    zostawia okno na DNS rebinding: drugie rozwiazanie moze dac 10.43.0.1.
    Dlatego laczymy sie z adresem, ktory wlasnie sprawdzilismy, a nazwe
    podajemy osobno w `Host` i jako `sni_hostname` - certyfikat jest wtedy
    nadal weryfikowany wobec nazwy, a nie wobec IP.
    """
    czesci = urlsplit(url)
    if czesci.scheme != "https" or not czesci.hostname:
        raise WebhookNiedozwolony("adres musi zaczynac sie od https:// i miec nazwe hosta")
    host = czesci.hostname
    port = czesci.port or 443
    try:
        adresy = {ipaddress.ip_address(a) for a in await _rozwiaz(host, port)}
    except (OSError, ValueError) as e:
        raise WebhookNiedozwolony(f"nazwa {host} sie nie rozwiazuje: {e}") from e
    if not adresy:
        raise WebhookNiedozwolony(f"nazwa {host} nie ma adresu")
    niepubliczne = sorted(str(a) for a in adresy if not a.is_global)
    if niepubliczne:
        raise WebhookNiedozwolony(f"{host} prowadzi do adresu niepublicznego: {niepubliczne}")
    ip = sorted(adresy, key=str)[0]
    ip_url = f"[{ip}]" if ip.version == 6 else str(ip)
    naglowek_host = host if czesci.port in (None, 443) else f"{host}:{czesci.port}"
    return (urlunsplit(("https", f"{ip_url}:{port}", czesci.path or "/",
                        czesci.query, "")), naglowek_host, host)


async def _send_webhook(config: dict, payload: dict) -> None:
    headers = {"Content-Type": "application/json"}
    if config.get("secret"):
        # Prosty wspoldzielony sekret w naglowku - odbiorca moze odrzucic
        # zadania, ktore nie pochodza od nas.
        headers["X-Trader-Secret"] = config["secret"]
    try:
        url, host, sni = await adres_webhooka(config["url"])
        headers["Host"] = host
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(url, json=payload, headers=headers,
                             extensions={"sni_hostname": sni})
        if r.status_code >= 400:
            raise RuntimeError(f"webhook zwrocil {r.status_code}")
    except Exception as e:                                    # noqa: BLE001
        print(f"[webhook] {config.get('url', '?')[:80]}: {type(e).__name__}: {e}",
              flush=True)
        raise RuntimeError(WEBHOOK_BLAD) from None


# Zadania wysylki w tle. `create_task` trzyma tylko SLABA referencje, wiec
# zadanie bez wpisu tutaj potrafi zostac zebrane przez GC w polowie wysylki -
# dokumentacja asyncio mowi o tym wprost.
_w_tle: set[asyncio.Task] = set()


async def _powiadom_bez_wyjatku(user_id: int, event_code: str, title: str,
                                body: str, kw: dict) -> None:
    try:
        await notify(user_id, event_code, title, body, **kw)
    except Exception as e:                                    # noqa: BLE001
        print(f"[powiadomienie] {event_code} nie zostało wysłane: "
              f"{type(e).__name__}: {e}", flush=True)


async def powiadom_o_bezpieczenstwie(user_id: int, event_code: str, title: str,
                                     body: str = "", **kw) -> None:
    """Powiadomienie, ktore NIE MOZE przerwac ani SPOWOLNIC operacji, o ktorej powiadamia.

    Zdarzenia bezpieczenstwa - nowe logowanie, zmiana klucza, zmiana hasla - sa
    zgłaszane W TRAKCIE obslugi zadania uzytkownika. Wyjatek z kanalu
    powiadomien (niedostepny WhatsApp, blad webhooka, zerwane polaczenie) nie
    moze wywrocic logowania ani zapisu klucza.

    Mechanizm ostrzegania, ktory potrafi zablokowac chroniona operacje, jest
    gorszy niz jego brak: zamienia awarie kanalu powiadomien w awarie systemu.

    POZA SCIEZKA ZADANIA. Do 2026-10-03 wysylka szla tu wprost (`await
    notify`), a kanaly maja timeouty 15 s (webhook) i 20 s (CallMeBot). Martwy
    kanal wydluzal logowanie i zmiane hasla do ok. 35 s - przy przejeciu konta,
    kiedy wlasciciel zmienia haslo w pospiechu, to jest najgorszy moment na
    zawieszony formularz. Teraz zadanie odpowiada od razu, a zapis i wysylka
    ida w tle na tej samej petli (pula bazy jest wspolna).

    Blad jest polykany, ale nie milczaco - trafia do dziennika procesu.
    """
    zadanie = asyncio.create_task(
        _powiadom_bez_wyjatku(user_id, event_code, title, body, kw))
    _w_tle.add(zadanie)
    zadanie.add_done_callback(_w_tle.discard)


async def _zapisz_zdarzenie(user_id: int, event_code: str, sev: str, title: str,
                            body: str, dane: dict,
                            engine: str | None) -> tuple[dict, str, dict, str | None]:
    """Zapis zdarzenia do tabeli. NIEZNANY KOMPONENT DEGRADUJE KOLUMNE, NIE ALERT.

    Zwraca (wiersz, tresc, dane, zapisany_engine). Trzy ostatnie moga sie roznic
    od tego, co przyszlo - wolajacy ma uzywac ICH, zeby kanaly wysylaly to samo,
    co stoi w bazie.

    PO CO TO ISTNIEJE

    `notify.notification.engine` ma typ `ref.engine`, czyli enum SILNIKOW
    HANDLOWYCH ('crypto','equity'). Nie ma w nim - i swiadomie miec nie bedzie,
    patrz migracje 0029, 0034 i 0035 - dozorcy ryzyka, workerow danych ani
    zadnego przyszlego procesu, ktory nie handluje. Kazdy z nich moze umrzec
    i o smierci kazdego z nich trzeba powiadomic.

    Do 2026-09-12 wlozenie takiej nazwy do `engine` konczylo caly `notify()`
    wyjatkiem `InvalidTextRepresentation` - czyli POWIADOMIENIE O AWARII GINELO
    Z POWODU LITEROWKI W SWOIM WLASNYM METADANYCH. Zmierzone: dozorca ryzyka
    lezal 887 minut, czujka poprawnie go wykryla, i nikt sie o tym nie dowiedzial.
    Detekcja dzialala, dostawa nie.

    DLACZEGO DEGRADACJA, A NIE TWARDY BLAD

    Bo to sa dwie rozne rzeczy: "kolumna nie zna tej nazwy" i "nie ma alarmu".
    Pierwsza jest usterka metadanych i da sie ja naprawic jutro; druga jest
    cisza w jedynym kanale, ktory mial sie odezwac. Alarm, ktory nie doszedl,
    jest gorszy niz brak czujki, bo tworzy zludzenie nadzoru - wiec z tych dwoch
    poswiecamy kolumne.

    TO NIE JEST POLKNIETY BLAD. Usterka zostaje GLOSNA w trzech miejscach naraz:
      * linia w logu procesu, z nazwa komponentu i typem zdarzenia;
      * `data->>'komponent'` w samym powiadomieniu, wiec widac ja w dzwonku
        i w ladunku webhooka;
      * dopisek w tresci, wiec dociera takze tam, gdzie czlowiek naprawde
        czyta - w WhatsAppie.
    Znika wylacznie jedna rzecz: wyjatek, ktory kasowal alarm.

    Wolamy bez sprawdzania z wyprzedzeniem (`pg_enum`), bo kazdy taki podglad
    jest DRUGA KOPIA definicji typu - rozjedzie sie przy pierwszym rozszerzeniu
    enuma i zrobi dokladnie te cisze, ktorej tu zapobiegamy. Baza jest jedynym
    zrodlem prawdy o tym, co ten typ przyjmuje, wiec pytamy ja wprost.

    Ponowienie jest bezpieczne, bo pula chodzi z `autocommit=True` (patrz
    `db.open_pool`): odrzucona instrukcja nie zostawia transakcji do posprzatania.
    Gdy poprawka tez padnie, wyjatek leci dalej - winowajca jest wtedy inny
    (np. `severity` spoza `notify.severity`) i zgadywanie go byloby udawaniem.
    """
    sql = ("INSERT INTO notify.notification"
           " (user_id, event_code, severity, title, body, data, engine)"
           " VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id")
    try:
        row = await fetch_one(sql, (user_id, event_code, sev, title, body,
                                    json.dumps(dane), engine))
        return row, body, dane, engine
    except psycopg.errors.InvalidTextRepresentation:
        if engine is None:
            # Kolumna `engine` nie moze byc tu winna - nie zgadujemy, co jest.
            raise
        print(f"[powiadomienie] {event_code}: „{engine}” nie jest wartoscia "
              f"enuma ref.engine (silniki handlowe). Alert IDZIE, nazwa "
              f"komponentu laduje w data->>'komponent' i w tresci; kolumna "
              f"`engine` zostaje pusta. Popraw wolajacego.", flush=True)
        dane = {**dane, "komponent": engine}
        body = f"{body}\n\nKomponent: {engine}" if body else f"Komponent: {engine}"
        row = await fetch_one(sql, (user_id, event_code, sev, title, body,
                                    json.dumps(dane), None))
        return row, body, dane, None


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

    # `body` i `data` moga wrocic wzbogacone o nazwe komponentu spoza enuma -
    # patrz `_zapisz_zdarzenie`. Od tej linii uzywamy ICH, zeby kanaly wysylaly
    # to samo, co stoi w dzwonku.
    row, body, dane, engine = await _zapisz_zdarzenie(
        user_id, event_code, sev, title, body, data or {}, engine)
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
                    "body": body, "engine": engine, "data": dane,
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
