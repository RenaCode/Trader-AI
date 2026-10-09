"""Zakladka RYZYKO: limity, ich wykorzystanie, naruszenia, kill-switch."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..auth import adres_klienta, audit, current_user, require_csrf, wymagaj_wlasciciela
from ..db import execute, fetch_all, fetch_one
from ..notifications import powiadom_o_bezpieczenstwie
from ..typy import Silnik, Tryb
from .auth import potwierdz_haslo, potwierdz_kod_totp, totp_wlaczone


router = APIRouter(prefix="/api/engines/{engine}/risk", tags=["risk"])


@router.get("/limits")
async def limits(engine: Silnik, mode: Tryb = Query("demo")):
    rows = await fetch_all(
        """
        SELECT ld.id, ld.name, ld.scope, ld.value, ld.unit, ld.action, ld.enabled,
               ld.wznowienie, ld.max_wznowien, i.symbol,
               lu.used, lu.utilization, lu.event_time
        FROM risk.limit_def ld
        LEFT JOIN ref.instrument i ON i.id = ld.instrument_id
        LEFT JOIN LATERAL (
            SELECT used, utilization, event_time FROM risk.limit_usage
            WHERE limit_def_id = ld.id ORDER BY event_time DESC LIMIT 1
        ) lu ON true
        WHERE ld.engine::text = %s AND ld.mode::text = %s
        ORDER BY ld.name
        """,
        (engine, mode),
    )
    return [
        {
            "id": r["id"], "name": r["name"], "scope": r["scope"], "symbol": r["symbol"],
            "threshold": float(r["value"]), "unit": r["unit"], "action": r["action"],
            "enabled": r["enabled"],
            # REGULA WZNOWIENIA jest czescia definicji limitu, nie szczegolem
            # wdrozenia. Bez niej interfejs pokazuje, ze system sie zatrzyma,
            # i milczy o tym, czy sam wroci - a to jest pierwsze pytanie, jakie
            # zadaje sie o autonomiczny bezpiecznik.
            "wznowienie": r["wznowienie"], "max_wznowien": r["max_wznowien"],
            # LIMIT NIGDY NIEZMIERZONY ODDAJEMY JAKO `null`, NIE JAKO ZERO.
            #
            # `0.0` i `r["utilization"] or 0.0` sprawialy, ze brak wiersza
            # w `risk.limit_usage` byl nie do odroznienia od pomiaru rownego
            # zeru. Skutek jest odwrotny do intuicji: gdy dozorca PRZESTANIE
            # pisac pomiary, wszystkie paski zjezdzaja do zera i zakladka
            # "Ryzyko" wyglada LEPIEJ niz przed awaria - awaria nadzoru maluje
            # sie na ekranie jako wzorowa dyscyplina.
            #
            # `as_of` i `enabled` zostaja: front na nich polega, a data pomiaru
            # jest jedynym swiadkiem tego, ze pomiar w ogole byl.
            "used": float(r["used"]) if r["used"] is not None else None,
            "utilization": float(r["utilization"]) if r["utilization"] is not None else None,
            "as_of": r["event_time"].isoformat() if r["event_time"] else None,
        }
        for r in rows
    ]


@router.get("/violations")
async def violations(engine: Silnik, limit: int = Query(30, le=200)):
    rows = await fetch_all(
        """
        SELECT v.id, v.occurred_at, v.observed, v.threshold, v.action_taken,
               ld.name AS limit_name, ld.unit, v.decision_id
        FROM risk.violation v
        JOIN risk.limit_def ld ON ld.id = v.limit_def_id
        WHERE v.engine::text = %s ORDER BY v.occurred_at DESC LIMIT %s
        """,
        (engine, limit),
    )
    return [
        {
            "id": r["id"], "at": r["occurred_at"].isoformat(), "limit": r["limit_name"],
            "observed": float(r["observed"]), "threshold": float(r["threshold"]),
            "unit": r["unit"], "action": r["action_taken"], "decision_id": r["decision_id"],
        }
        for r in rows
    ]


@router.get("/killswitch")
async def killswitch(engine: Silnik, limit: int = Query(10, le=100)):
    rows = await fetch_all(
        """
        SELECT k.id, k.triggered_at, k.reason, k.limit_name, k.released_at,
               k.released_auto, k.release_note, u.login AS released_by,
               w.login AS triggered_by
        FROM risk.killswitch_event k
        LEFT JOIN app.user u ON u.id = k.released_by
        LEFT JOIN app.user w ON w.id = k.triggered_by
        WHERE k.engine::text = %s ORDER BY k.triggered_at DESC LIMIT %s
        """,
        (engine, limit),
    )
    return [
        {
            "id": r["id"], "triggered_at": r["triggered_at"].isoformat(),
            "reason": r["reason"], "limit_name": r["limit_name"],
            "released_at": r["released_at"].isoformat() if r["released_at"] else None,
            "released_by": r["released_by"], "release_note": r["release_note"],
            # KTO ZWOLNIL: system czy czlowiek. Bez tego dziennik nie odroznia
            # zwolnienia przemyslanego od automatycznego, a to jest pierwsza
            # rzecz, o ktora sie pyta po serii zatrzyman.
            "released_auto": r["released_auto"],
            # KTO ZATRZYMAL: `null` = system (limit albo proba pozarowa),
            # login = reczny kill-switch z panelu (migracja 0092).
            "triggered_by": r["triggered_by"],
            "active": r["released_at"] is None,
        }
        for r in rows
    ]


# Nazwa w `limit_name` dla zatrzymania z panelu. Nie ma takiego limitu
# w `risk.limit_def`, a `wznowienie.wznow` przy aktywnym takim wierszu nie
# zwalnia ZADNEGO zdarzenia (reczne ma pierwszenstwo, audyt 2026-10-09 W-1).
# Proba pozarowa i likwidacja widza je jak kazde prawdziwe zatrzymanie.
# Ta sama wartosc stoi w `wznowienie.LIMIT_RECZNY` (silnik nie importuje
# routerow API) - zgodnosc pilnuje tests/test_reczny_killswitch.py.
LIMIT_RECZNY = "reczny"


class RecznyKillSwitchBody(BaseModel):
    # Powod z tego samego powodu co przy zwolnieniu: zostaje w bazie na
    # pytanie "dlaczego system przestal handlowac".
    powod: str = Field(min_length=10, max_length=500)
    # BEZ HASLA - I TO JEST CALA OCHRONA TEGO KIERUNKU (przeglad 2026-10-04).
    #
    # Do tej poprawki zatrzymanie wymagalo hasla, a `potwierdz_haslo` odmawia
    # przy zablokowanym koncie. Piec zlych logowan z dowolnego miejsca
    # blokowalo konto na 15 minut - i przez te 15 minut wlasciciel NIE MOGL
    # zatrzymac handlu z panelu, choc jego sesja byla wazna. Zatrzymanie
    # niczego nie ryzykuje: najgorsze, co zrobi przejeta sesja, to wstrzymanie
    # wejsc, ktore widac w alarmie i w audycie. Haslo chroni teraz kierunek,
    # ktory ryzykuje pieniadze - zwolnienie.


@router.post("/killswitch",
             dependencies=[Depends(require_csrf), Depends(wymagaj_wlasciciela)])
async def wlacz_killswitch(engine: Silnik, body: RecznyKillSwitchBody, request: Request,
                           user: dict = Depends(current_user)):
    """Reczny kill-switch: TA SAMA sciezka co automat (decyzja 2026-10-04).

    Wiersz w `risk.killswitch_event` bez `released_at` - dokladnie to, co
    zapisuje `RiskKernel._trigger_killswitch`. Silnik i dozorca czytaja go
    w kazdym cyklu (`killswitch_active`), wiec handel staje przy najblizszej
    ocenie, a wyjscia z pozycji dzialaja jak przy automacie. Zwolnienie tez
    jak przy automacie: `POST .../killswitch/{id}/release` z powodem.

    TRYB Z PULSU SILNIKA, nie z formularza. `risk.engine_heartbeat.mode`
    zapisuje silnik przy kazdym cyklu, wiec zatrzymanie trafia w tryb, ktory
    naprawde handluje. Formularz z wyborem trybu pozwalalby zatrzymac `live`,
    gdy handluje `demo` - i odwrotnie - z poczuciem, ze sie udalo.

    RECZNE ZATRZYMANIE JEST MOZLIWE ZAWSZE (audyt 2026-10-09, W-1). Do tej
    poprawki `INSERT ... WHERE NOT EXISTS` sprawdzal KAZDE aktywne zdarzenie,
    wiec przy zadzialanym automacie (`max_daily_loss`, `max_drawdown`) endpoint
    oddawal 409, a przycisk w panelu znikal. Automat ma regule wznowienia
    i po karencji sam wracal do handlu - wlasciciel, ktory chcial trzymac
    silnik zatrzymany do przegladu, nie mial jak. Teraz recznym zatrzymaniem
    jest WLASNY wiersz `limit_name = 'reczny'`, obok automatycznego:
    `wznowienie.wznow` nie zwalnia niczego, dopoki taki wiersz jest aktywny,
    a silnik stoi, dopoki aktywny jest choc jeden.

    JEDNO AKTYWNE RECZNE. Drugie klikniecie przy aktywnym recznym nie dopisuje
    wiersza (do zwolnienia bylyby dwa) i nie jest bledem - zatrzymanie, ktore
    juz trzyma, to wynik, o ktory proszono: 200 z id istniejacego. Wyscig dwoch
    rownoleglych klikniec (READ COMMITTED, bez indeksu unikalnego) daje dwa
    reczne wiersze - bezpiecznie: silnik stoi, dopoki oba nie zostana zwolnione.

    BEZ HASLA I BEZ SPRAWDZANIA BLOKADY KONTA - uzasadnienie przy
    `RecznyKillSwitchBody`. Zostaje CSRF, rola wlasciciela, audyt i alarm.

    ALARM przez `notify` (kod `killswitch.triggered`, krytyczny), czyli do
    dzwonka i do bramki RenaCode. W tle - nieudana wysylka nie moze cofnac ani
    opoznic zatrzymania. Ten sam wiersz w dzwonku sluzy dozorowi
    (`sprawdz_ryzyko`) za pierwsze zgloszenie, wiec nie wysle drugiego
    "Kill-switch zadzialal", tylko przypomnienia w swoim rytmie.
    """
    puls = await fetch_one(
        "SELECT mode::text AS mode FROM risk.engine_heartbeat WHERE engine::text = %s",
        (engine,))
    if not puls:
        raise HTTPException(409, "Silnik nie zgłosił jeszcze pulsu — nie wiadomo, "
                                 "w jakim trybie handluje, więc nie ma czego zatrzymać")
    tryb = puls["mode"]
    powod = body.powod.strip()
    reason = f"Ręczne zatrzymanie z panelu ({user['login']}): {powod}"

    z = await fetch_one(
        "INSERT INTO risk.killswitch_event (engine, mode, reason, limit_name, triggered_by)"
        " SELECT %s, %s, %s, %s, %s"
        "  WHERE NOT EXISTS (SELECT 1 FROM risk.killswitch_event"
        "                     WHERE engine::text = %s AND mode::text = %s"
        "                       AND limit_name = %s AND released_at IS NULL)"
        " RETURNING id, triggered_at",
        (engine, tryb, reason, LIMIT_RECZNY, user["id"], engine, tryb, LIMIT_RECZNY))
    if not z:
        juz = await fetch_one(
            "SELECT id, triggered_at FROM risk.killswitch_event"
            " WHERE engine::text = %s AND mode::text = %s AND limit_name = %s"
            "   AND released_at IS NULL ORDER BY triggered_at LIMIT 1",
            (engine, tryb, LIMIT_RECZNY))
        if juz:
            return {"ok": True, "id": juz["id"], "mode": tryb, "juz_aktywne": True,
                    "triggered_at": juz["triggered_at"].isoformat(),
                    "message": "Ręczne zatrzymanie już trzyma silnik — nic nie "
                               "zmieniono. Zwolnienie: zakładka Ryzyko."}
        # Zwolnione miedzy INSERT a SELECT - rzadkie, ale wtedy stanu nie znamy.
        raise HTTPException(409, "Stan kill-switcha zmienił się w trakcie — "
                                 "odśwież zakładkę i spróbuj ponownie.")

    await audit("risk.killswitch_manual", user_id=user["id"],
                entity="risk.killswitch_event", entity_id=str(z["id"]),
                after={"engine": engine, "mode": tryb, "reason": reason},
                request=request)

    for u in await fetch_all('SELECT id FROM app."user" WHERE is_active'):
        await powiadom_o_bezpieczenstwie(
            u["id"], "killswitch.triggered",
            f"Kill-switch włączony ręcznie ({engine})",
            f"Handel zatrzymany z panelu ({tryb}), konto {user['login']}.\n\n"
            f"Powód: {powod}\n\nSilnik NIE WEJDZIE w żadną pozycję do czasu "
            f"ręcznego zwolnienia (zakładka Ryzyko). Automatyczne wznowienie "
            f"jest wstrzymane, dopóki to zatrzymanie trwa.",
            engine=engine, severity="critical",
            data={"event_id": z["id"], "reczny": True, "kto": user["login"],
                  "mode": tryb})

    return {"ok": True, "id": z["id"], "mode": tryb,
            "triggered_at": z["triggered_at"].isoformat(),
            "message": "Kill-switch włączony. Silnik przestanie otwierać pozycje "
                       "przy najbliższym cyklu decyzyjnym."}


class ZwolnienieBody(BaseModel):
    # POWOD JEST WYMAGANY, i to jest cala tresc tego pola.
    #
    # Kill-switch nie wygasa sam, bo automatyczne wznowienie po obsunieciu to
    # najprostsza droga do powtorzenia tej samej straty. Zwolnienie jednym
    # klignieciem, bez podania powodu, jest tym samym wznowieniem - tyle ze
    # z dodatkowym krokiem, ktory daje zludzenie namyslu.
    #
    # Minimalna dlugosc nie sprawdza, czy ktos pomyslal. Sprawdza, czy w ogole
    # cokolwiek napisal - i zostawia to w bazie na potem, gdy ktos bedzie pytal,
    # dlaczego system wrocil do handlu.
    note: str = Field(min_length=10, max_length=500)
    # HASLO, BO ZWOLNIENIE WZNAWIA HANDEL (przeglad 2026-10-04). Do tej poprawki
    # wystarczal CSRF i rola wlasciciela, czyli przejeta sesja mogla zwolnic
    # KAZDY kill-switch - takze automatyczny `max_drawdown`, ktory wlasnie
    # zatrzymal strate. `potwierdz_haslo` liczy pomylki razem z logowaniem
    # i blokuje konto po MAX_FAILED.
    haslo: str = Field(min_length=1, max_length=1024)
    # KOD DRUGIEGO SKLADNIKA, gdy konto ma TOTP (przeglad 2026-10-04, T-12 c).
    # Zwolnienie jest kierunkiem, ktory ryzykuje pieniadze - samo haslo
    # (uzyte ponownie, wyludzone) nie moze go otwierac na koncie, ktore ma
    # drugi skladnik. 6 cyfr TOTP albo kod zapasowy.
    kod: str | None = Field(default=None, max_length=64)


@router.post("/killswitch/{event_id}/release",
             dependencies=[Depends(require_csrf), Depends(wymagaj_wlasciciela)])
async def zwolnij_killswitch(engine: Silnik, event_id: int, body: ZwolnienieBody,
                             request: Request, user: dict = Depends(current_user)):
    """Reczne zwolnienie kill-switcha.

    DLACZEGO TO ISTNIEJE DOPIERO TERAZ

    Kolumny `released_at`, `released_by` i `release_note` sa w schemacie od
    migracji 0007. Sciezki zapisu nie bylo: router ryzyka mial wylacznie GET,
    a zaden inny modul nie pisal do tej tabeli. Kill-switch byl wiec drzwiami
    JEDNOKIERUNKOWYMI - dzialal, zatrzymywal silnik i nie dawalo sie go zwolnic
    inaczej niz recznym SQL-em na produkcyjnej bazie.

    Jednoczesnie powiadomienie o zadzialaniu mowilo wprost "Zwolnienie: zakladka
    Ryzyko w interfejsie", a interfejs pokazywal komunikat "Odblokowanie wymaga
    recznego potwierdzenia" bez zadnej kontrolki. Instrukcja odsylala do rzeczy,
    ktorej nie bylo.

    HASLO PRZED CZYMKOLWIEK INNYM: odpowiedz 404/409 nie moze mowic
    zgadujacemu, ktore zdarzenia istnieja. 403, nie 401 - front na 401
    wylogowuje, a sesja jest wazna.
    """
    await potwierdz_haslo(user, body.haslo, request, akcja="risk.killswitch_released",
                          status=403, komunikat="Hasło jest nieprawidłowe")
    if await totp_wlaczone(user["id"]):
        await potwierdz_kod_totp(user, body.kod, request,
                                 akcja="risk.killswitch_released.kod", status=403,
                                 komunikat="Kod weryfikacji dwuetapowej jest wymagany "
                                           "albo nieprawidłowy")

    z = await fetch_one(
        "SELECT id, engine::text AS engine, reason, released_at"
        " FROM risk.killswitch_event WHERE id = %s", (event_id,))
    if not z:
        raise HTTPException(404, "Nie ma takiego zdarzenia kill-switcha")
    if z["engine"] != engine:
        raise HTTPException(404, "Zdarzenie należy do innego silnika")
    if z["released_at"]:
        # 409, nie 200: powtorzone zwolnienie znaczy, ze ktos widzi inny stan
        # niz baza. Ciche potwierdzenie zostawiloby go w tym przekonaniu.
        raise HTTPException(409, "To zdarzenie zostało już zwolnione")

    await execute(
        "UPDATE risk.killswitch_event"
        " SET released_at = now(), released_by = %s, release_note = %s"
        " WHERE id = %s AND released_at IS NULL",
        (user["id"], body.note.strip(), event_id))

    await audit("risk.killswitch_released", user_id=user["id"],
                entity="risk.killswitch_event", entity_id=str(event_id),
                before={"reason": z["reason"], "released_at": None},
                after={"release_note": body.note.strip()}, request=request)

    # ALARM O ZWOLNIENIU (przeglad 2026-10-04, T-6). Do tej poprawki slad byl
    # wylacznie w `app.audit`, do ktorego nikt nie zaglada - przejeta sesja
    # z haslem zwalniala `max_drawdown` po cichu. `critical`, wiec przez bramke
    # na telefon; w tle, jak przy zatrzymaniu, zeby martwy kanal nie wydluzal
    # odpowiedzi.
    # Za Traefikiem i nginx `request.client` to pod web, nie klient (audyt
    # 2026-10-09, N-1) - ta sama funkcja co audyt i sesje.
    ip = (adres_klienta(request) if request is not None else None) or "?"
    notatka = body.note.strip()
    for u in await fetch_all('SELECT id FROM app."user" WHERE is_active'):
        await powiadom_o_bezpieczenstwie(
            u["id"], "killswitch.released",
            f"Kill-switch ZWOLNIONY ręcznie ({engine})",
            f"Zwolnił: {user['login']} (IP {ip}).\n\nNotatka: {notatka}\n\n"
            f"Pierwotny powód zatrzymania: {z['reason']}\n\n"
            f"Silnik wraca do handlu przy najbliższym cyklu.",
            engine=engine, severity="critical",
            data={"event_id": event_id, "kto": user["login"], "reczny": True,
                  "powod_pierwotny": z["reason"], "notatka": notatka})

    return {"ok": True, "message": "Kill-switch zwolniony. Silnik wróci do handlu "
                                   "przy najbliższym cyklu decyzyjnym."}


@router.get("/dozorca")
async def dozorca(engine: Silnik):
    """Puls dozorcy ryzyka — procesu, który ocenia limity NIEZALEŻNIE od silnika.

    Bez tego widoku autonomia jest niewidoczna: system podejmuje decyzje sam,
    a interfejs pokazuje wyłącznie ich skutki. Martwy dozorca wygląda dokładnie
    jak dozorca, który nie ma co zgłaszać — różnicę widać tylko w wieku pulsu.
    """
    rows = await fetch_all(
        "SELECT nazwa, last_beat_at, detail,"
        "       round(extract(epoch from (now() - last_beat_at))) AS sekund_temu"
        " FROM risk.dozorca_heartbeat WHERE nazwa LIKE %s ORDER BY nazwa",
        (f"%:{engine}:%",))
    return [
        {
            "nazwa": r["nazwa"],
            "last_beat_at": r["last_beat_at"].isoformat(),
            "sekund_temu": int(r["sekund_temu"]),
            # Prog trzykrotnie wiekszy od interwalu (60 s): jeden pominiety cykl
            # zdarza sie przy wdrozeniu i nie jest awaria, trzy pod rzad juz sa.
            "zyje": int(r["sekund_temu"]) < 180,
            "detail": r["detail"],
        }
        for r in rows
    ]
