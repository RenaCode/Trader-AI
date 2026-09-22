"""Zakladka STAN: krzywa kapitalu, pozycje, ekspozycja, heartbeat silnika."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from services.engine.horizons import DEFAULT_BAR
from ..db import fetch_all, fetch_one

# Silnik i tryb to w bazie enumy (`ref.engine`, `ref.trade_mode`). psycopg
# wysyla `str` jako `unknown`, wiec literal rzutuje dopiero Postgres - przy
# nieznanej wartosci konczylo sie to InvalidTextRepresentation, czyli 500
# ("Internal Server Error") na zwykla literowke w adresie. Domkniecie listy
# TUTAJ sprawia, ze FastAPI odmawia 422 ZANIM powstanie jakiekolwiek zapytanie.
Silnik = Literal["crypto", "equity"]
Tryb = Literal["demo", "live"]

router = APIRouter(prefix="/api/engines/{engine}", tags=["overview"])


@router.get("/overview")
async def overview(engine: Silnik, mode: Tryb = Query("demo")):
    latest = await fetch_one(
        """
        SELECT equity, cash, unrealized, realized_cum, exposure, event_time
        FROM exec.equity_point
        WHERE engine::text = %s AND mode::text = %s
        ORDER BY event_time DESC LIMIT 1
        """,
        (engine, mode),
    )

    # Punkt odniesienia sprzed 24h - do policzenia zmiany dziennej.
    day_ago = await fetch_one(
        """
        SELECT equity FROM exec.equity_point
        WHERE engine::text = %s AND mode::text = %s AND event_time <= now() - interval '24 hours'
        ORDER BY event_time DESC LIMIT 1
        """,
        (engine, mode),
    )

    first = await fetch_one(
        """
        SELECT equity FROM exec.equity_point
        WHERE engine::text = %s AND mode::text = %s
        ORDER BY event_time ASC LIMIT 1
        """,
        (engine, mode),
    )

    heartbeat = await fetch_one(
        "SELECT status, last_beat_at, detail FROM risk.engine_heartbeat WHERE engine::text = %s",
        (engine,),
    )

    state = await fetch_one(
        # Jawny filtr po horyzoncie: od migracji 0025 stanow jest wiecej niz jeden
        # na silnik, a dashboard pokazuje ten, ktorym silnik handluje.
        "SELECT status, reason, updated_at FROM learn.learning_state"
        " WHERE engine::text = %s AND bar = %s",
        (engine, DEFAULT_BAR),
    )

    equity = float(latest["equity"]) if latest else 0.0
    start = float(first["equity"]) if first else 0.0
    prev = float(day_ago["equity"]) if day_ago else start

    return {
        "engine": engine,
        "mode": mode,
        "equity": equity,
        "cash": float(latest["cash"]) if latest else 0.0,
        "unrealized": float(latest["unrealized"]) if latest else 0.0,
        "realized_cum": float(latest["realized_cum"]) if latest else 0.0,
        "exposure": float(latest["exposure"]) if latest else 0.0,
        "change_24h_pct": ((equity - prev) / prev * 100) if prev else 0.0,
        "change_total_pct": ((equity - start) / start * 100) if start else 0.0,
        "as_of": latest["event_time"] if latest else None,
        "heartbeat": heartbeat,
        "learning_status": state["status"] if state else "warmup",
        "learning_reason": state["reason"] if state else None,
    }


@router.get("/equity-curve")
async def equity_curve(engine: Silnik, mode: Tryb = Query("demo"), days: int = Query(30, le=365)):
    rows = await fetch_all(
        """
        SELECT event_time, equity, exposure
        FROM exec.equity_point
        WHERE engine::text = %s AND mode::text = %s AND event_time >= now() - make_interval(days => %s)
        ORDER BY event_time
        """,
        (engine, mode, days),
    )
    return [
        {"t": r["event_time"].isoformat(), "equity": float(r["equity"]), "exposure": float(r["exposure"])}
        for r in rows
    ]


@router.get("/positions")
async def positions(engine: Silnik, mode: Tryb = Query("demo")):
    rows = await fetch_all(
        """
        SELECT p.id, i.symbol, p.side, p.qty, p.avg_price, p.opened_at,
               p.realized_pnl, p.fees_paid, p.closed_at
        FROM exec.position p
        JOIN ref.instrument i ON i.id = p.instrument_id
        WHERE p.engine::text = %s AND p.mode::text = %s AND p.closed_at IS NULL
        ORDER BY p.opened_at DESC
        """,
        (engine, mode),
    )
    return [
        {
            "id": r["id"], "symbol": r["symbol"], "side": r["side"],
            "qty": float(r["qty"]), "avg_price": float(r["avg_price"]),
            "opened_at": r["opened_at"].isoformat(),
            "realized_pnl": float(r["realized_pnl"]),
        }
        for r in rows
    ]
