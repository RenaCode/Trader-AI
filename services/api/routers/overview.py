"""Zakladka STAN: krzywa kapitalu, pozycje, ekspozycja, heartbeat silnika."""
from __future__ import annotations

from fastapi import APIRouter, Query

from services.engine.horizons import DEFAULT_BAR
from ..db import fetch_all, fetch_one
from ..typy import Silnik, Tryb


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

    # BRAK PUNKTU TO `null`, NIE ZERO.
    #
    # Podstawienie 0.0 za brak wiersza w `exec.equity_point` sprawialo, ze
    # silnik bez ani jednego pomiaru wygladal na rachunek WYZEROWANY - kapital
    # 0,00, ekspozycja 0,00, gotowka 0,00 - i do tego ze wzrostem "+0,00%"
    # w tonie zysku. Dwa rozne stany swiata ("nie wiemy") i ("wiemy, ze zero")
    # trafialy na ten sam ekran jako ta sama liczba, a gorszy z nich byl tym,
    # ktorego nie dalo sie zobaczyc.
    equity = float(latest["equity"]) if latest else None
    start = float(first["equity"]) if first else None

    # ZMIANA 24H MA BYC ZMIANA 24-GODZINNA.
    #
    # `prev = ... if day_ago else start` robilo z braku punktu sprzed doby
    # odniesienie do PIERWSZEGO punktu w ogole. Silnik dzialajacy szesc godzin
    # pokazywal w kaflu "Zmiana 24h" zmiane od startu - liczbe prawdziwa,
    # podpisana nieprawdziwym okresem. Bez punktu starszego niz doba ta zmiana
    # nie istnieje i tak ma byc podana.
    prev = float(day_ago["equity"]) if day_ago else None

    return {
        "engine": engine,
        "mode": mode,
        "equity": equity,
        "cash": float(latest["cash"]) if latest else None,
        "unrealized": float(latest["unrealized"]) if latest else None,
        "realized_cum": float(latest["realized_cum"]) if latest else None,
        "exposure": float(latest["exposure"]) if latest else None,
        "change_24h_pct": ((equity - prev) / prev * 100) if equity is not None and prev else None,
        "change_total_pct": ((equity - start) / start * 100) if equity is not None and start else None,
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
