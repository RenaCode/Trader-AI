"""Zakladka RYZYKO: limity, ich wykorzystanie, naruszenia, kill-switch."""
from __future__ import annotations

from fastapi import APIRouter, Query

from ..db import fetch_all

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
