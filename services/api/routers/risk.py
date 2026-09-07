"""Zakladka RYZYKO: limity, ich wykorzystanie, naruszenia, kill-switch."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..auth import audit, current_user, require_csrf
from ..db import execute, fetch_all, fetch_one

router = APIRouter(prefix="/api/engines/{engine}/risk", tags=["risk"])


@router.get("/limits")
async def limits(engine: str, mode: str = Query("demo")):
    rows = await fetch_all(
        """
        SELECT ld.id, ld.name, ld.scope, ld.value, ld.unit, ld.action, ld.enabled,
               i.symbol,
               lu.used, lu.utilization, lu.event_time
        FROM risk.limit_def ld
        LEFT JOIN ref.instrument i ON i.id = ld.instrument_id
        LEFT JOIN LATERAL (
            SELECT used, utilization, event_time FROM risk.limit_usage
            WHERE limit_def_id = ld.id ORDER BY event_time DESC LIMIT 1
        ) lu ON true
        WHERE ld.engine = %s AND ld.mode = %s
        ORDER BY ld.name
        """,
        (engine, mode),
    )
    return [
        {
            "id": r["id"], "name": r["name"], "scope": r["scope"], "symbol": r["symbol"],
            "threshold": float(r["value"]), "unit": r["unit"], "action": r["action"],
            "enabled": r["enabled"],
            "used": float(r["used"]) if r["used"] is not None else 0.0,
            "utilization": r["utilization"] or 0.0,
            "as_of": r["event_time"].isoformat() if r["event_time"] else None,
        }
        for r in rows
    ]


@router.get("/violations")
async def violations(engine: str, limit: int = Query(30, le=200)):
    rows = await fetch_all(
        """
        SELECT v.id, v.occurred_at, v.observed, v.threshold, v.action_taken,
               ld.name AS limit_name, ld.unit, v.decision_id
        FROM risk.violation v
        JOIN risk.limit_def ld ON ld.id = v.limit_def_id
        WHERE v.engine = %s ORDER BY v.occurred_at DESC LIMIT %s
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
async def killswitch(engine: str, limit: int = Query(10, le=100)):
    rows = await fetch_all(
        """
        SELECT k.id, k.triggered_at, k.reason, k.released_at, k.release_note, u.login AS released_by
        FROM risk.killswitch_event k
        LEFT JOIN app.user u ON u.id = k.released_by
        WHERE k.engine = %s ORDER BY k.triggered_at DESC LIMIT %s
        """,
        (engine, limit),
    )
    return [
        {
            "id": r["id"], "triggered_at": r["triggered_at"].isoformat(),
            "reason": r["reason"],
            "released_at": r["released_at"].isoformat() if r["released_at"] else None,
            "released_by": r["released_by"], "release_note": r["release_note"],
            # Kill-switch wymaga RECZNEGO odblokowania - automatyczne wznowienie
            # po drawdownie to najprostsza droga do powtorzenia tej samej straty.
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
async def zwolnij_killswitch(engine: str, event_id: int, body: ZwolnienieBody,
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
