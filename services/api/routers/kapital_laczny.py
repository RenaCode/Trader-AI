"""Kapitał ŁĄCZNY obu platform na jednej siatce czasu.

Krzywa per silnik żyje w `exec.equity_point` w rytmie własnego silnika
(krypto: co cykl, 24/7; equity: w sesji). Sumowanie „po wierszach" dałoby
sumę dwóch różnych chwil. Dlatego siatka jest wspólna (godzinowa), a wartość
silnika w punkcie siatki to jego OSTATNI znany kapitał nie później niż ten
punkt — czyli to, co naprawdę było wtedy na rachunku, także w nocy i w weekend,
gdy giełda tradycyjna stoi.

Silnik bez ani jednego punktu w oknie (dziś: equity, zatrzymany świadomie)
wchodzi jako brak — nie jako zero — żeby suma nie kłamała o spadku, gdy
druga platforma po prostu jeszcze nie ruszyła.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from ..db import fetch_all

router = APIRouter(prefix="/api/kapital", tags=["kapital"])


@router.get("/laczny")
async def laczny(days: int = Query(45, ge=1, le=365), mode: str = Query("demo")):
    rows = await fetch_all(
        """
        WITH siatka AS (
            SELECT generate_series(
                date_trunc('hour', now() - make_interval(days => %s)),
                date_trunc('hour', now()), interval '1 hour') AS t
        ),
        silniki AS (
            SELECT DISTINCT engine::text AS engine FROM exec.equity_point
            WHERE mode::text = %s
        )
        SELECT s.t, e.engine, p.equity
        FROM siatka s
        CROSS JOIN silniki e
        LEFT JOIN LATERAL (
            SELECT equity FROM exec.equity_point q
            WHERE q.engine::text = e.engine AND q.mode::text = %s AND q.event_time <= s.t
            ORDER BY q.event_time DESC LIMIT 1
        ) p ON true
        ORDER BY s.t, e.engine
        """,
        (days, mode, mode),
    )
    silniki = sorted({r["engine"] for r in rows})
    punkty: dict = {}
    for r in rows:
        p = punkty.setdefault(r["t"], {"t": r["t"].isoformat()})
        p[r["engine"]] = float(r["equity"]) if r["equity"] is not None else None
    seria = []
    for p in punkty.values():
        znane = [p[e] for e in silniki if p.get(e) is not None]
        p["razem"] = sum(znane) if znane else None
        seria.append(p)

    def zmiana(klucz: str) -> dict:
        wart = [p[klucz] for p in seria if p.get(klucz) is not None]
        if not wart:
            return {"teraz": None, "od_startu": None, "od_startu_pct": None,
                    "doba": None, "doba_pct": None}
        teraz, start = wart[-1], wart[0]
        # Punkt sprzed doby = 24 kroki siatki godzinowej wstecz (jeśli są).
        dobe_temu = wart[-25] if len(wart) >= 25 else start
        return {
            "teraz": teraz,
            "od_startu": teraz - start,
            "od_startu_pct": (teraz / start - 1) * 100 if start else None,
            "doba": teraz - dobe_temu,
            "doba_pct": (teraz / dobe_temu - 1) * 100 if dobe_temu else None,
        }

    return {
        "silniki": silniki,
        "punkty": seria,
        "zmiany": {**{e: zmiana(e) for e in silniki}, "razem": zmiana("razem")},
    }
