"""Zakladka RYZYKO: limity, ich wykorzystanie, naruszenia, kill-switch."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..auth import audit, current_user, require_csrf
from ..db import execute, fetch_all, fetch_one

# Patrz overview.py: wartosc zamknieta tutaj, zeby nieznany silnik konczyl sie
# odmowa 422, a nie bledem rzutowania enuma w Postgresie (500).
Silnik = Literal["crypto", "equity"]
Tryb = Literal["demo", "live"]

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
            "used": float(r["used"]) if r["used"] is not None else 0.0,
            "utilization": r["utilization"] or 0.0,
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
               k.released_auto, k.release_note, u.login AS released_by
        FROM risk.killswitch_event k
        LEFT JOIN app.user u ON u.id = k.released_by
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
            "active": r["released_at"] is None,
        }
        for r in rows
    ]


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


@router.post("/killswitch/{event_id}/release", dependencies=[Depends(require_csrf)])
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
    """
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
