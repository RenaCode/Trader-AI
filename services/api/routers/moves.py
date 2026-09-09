"""Zakladki RUCHY i ZRODLA.

"Ruchy" odpowiadaja na "co system zrobil i dlaczego" oraz "co planuje".
Rozbicie na wklady (decision_contribution) sprawia, ze "dlaczego" jest odczytane
z bazy, a nie zgadniete.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..db import fetch_all, fetch_one

router = APIRouter(prefix="/api/engines/{engine}", tags=["moves"])


@router.get("/decisions")
async def decisions(engine: str, limit: int = Query(50, le=500), action: str | None = None):
    rows = await fetch_all(
        """
        SELECT d.id, d.decided_at, d.action, d.confidence, d.regime,
               d.risk_verdict, d.risk_reason, d.rationale, d.target_qty, d.target_price,
               d.latency_ms, i.symbol,
               m.name AS model_name, mv.version AS model_version
        FROM journal.decision d
        JOIN ref.instrument i ON i.id = d.instrument_id
        LEFT JOIN learn.model_version mv ON mv.id = d.model_version_id
        LEFT JOIN learn.model m ON m.id = mv.model_id
        WHERE d.engine = %s AND (%s::text IS NULL OR d.action::text = %s)
        ORDER BY d.decided_at DESC LIMIT %s
        """,
        (engine, action, action, limit),
    )
    return [
        {
            "id": r["id"], "at": r["decided_at"].isoformat(), "symbol": r["symbol"],
            "action": r["action"], "confidence": r["confidence"], "regime": r["regime"],
            "risk_verdict": r["risk_verdict"], "risk_reason": r["risk_reason"],
            "rationale": r["rationale"], "latency_ms": r["latency_ms"],
            "target_qty": float(r["target_qty"]) if r["target_qty"] is not None else None,
            "target_price": float(r["target_price"]) if r["target_price"] is not None else None,
            "model": (f'{r["model_name"]} v{r["model_version"]}' if r["model_name"] else None),
        }
        for r in rows
    ]


@router.get("/decisions/{decision_id}")
async def decision_detail(engine: str, decision_id: int):
    """Pelne "dlaczego": wklad kazdej cechy, sygnalu i newsa w te decyzje."""
    head = await fetch_one(
        """
        SELECT d.id, d.decided_at, d.action, d.confidence, d.regime, d.risk_verdict,
               d.risk_reason, d.rationale, d.feature_snapshot, i.symbol,
               -- Ile sygnalow stalo za wynikiem. Sam wynik tego nie zdradza:
               -- combine() renormalizuje go po wadze faktycznie obecnej, wiec
               -- +0,34 z trzech sygnalow wyglada tak samo jak +0,34 z osmiu.
               -- NULL = decyzja sprzed migracji 0039, nie "pelne pokrycie".
               d.pokrycie_sygnalow
        FROM journal.decision d
        JOIN ref.instrument i ON i.id = d.instrument_id
        WHERE d.id = %s AND d.engine = %s
        """,
        (decision_id, engine),
    )
    if not head:
        raise HTTPException(404, "Nie ma takiej decyzji")

    contributions = await fetch_all(
        """
        SELECT kind, ref_id, label, value, weight, contribution
        FROM journal.decision_contribution
        WHERE decision_id = %s
        ORDER BY abs(contribution) DESC
        """,
        (decision_id,),
    )

    # Artykuly, ktore wspoltworzyly te decyzje - z linkami do zrodel.
    news = await fetch_all(
        """
        SELECT a.id, a.title, a.url, f.name AS source, a.event_time,
               an.direction, an.magnitude, an.novelty, an.confidence, dc.contribution
        FROM journal.decision_contribution dc
        JOIN news.article a ON a.id = dc.ref_id AND dc.kind = 'news'
        JOIN news.feed f ON f.id = a.feed_id
        LEFT JOIN news.article_analysis an ON an.article_id = a.id
        WHERE dc.decision_id = %s
        ORDER BY abs(dc.contribution) DESC
        """,
        (decision_id,),
    )

    return {
        "id": head["id"], "at": head["decided_at"].isoformat(), "symbol": head["symbol"],
        "action": head["action"], "confidence": head["confidence"], "regime": head["regime"],
        "risk_verdict": head["risk_verdict"], "risk_reason": head["risk_reason"],
        "rationale": head["rationale"], "feature_snapshot": head["feature_snapshot"],
        "contributions": [
            {"kind": c["kind"], "ref_id": c["ref_id"], "label": c["label"],
             "value": c["value"], "weight": c["weight"], "contribution": c["contribution"]}
            for c in contributions
        ],
        "news": [
            {"id": n["id"], "title": n["title"], "url": n["url"], "source": n["source"],
             "at": n["event_time"].isoformat(), "direction": n["direction"],
             "magnitude": n["magnitude"], "novelty": n["novelty"],
             "contribution": n["contribution"]}
            for n in news
        ],
    }


@router.get("/planned")
async def planned(engine: str):
    """Co system PLANUJE: warunki wejscia i poziomy triggerow."""
    rows = await fetch_all(
        """
        SELECT po.id, po.side, po.qty, po.order_type, po.limit_price, po.trigger,
               po.valid_until, po.created_at, i.symbol,
               d.rationale, d.confidence, d.id AS decision_id
        FROM journal.planned_order po
        JOIN ref.instrument i ON i.id = po.instrument_id
        JOIN journal.decision d ON d.id = po.decision_id
        WHERE d.engine = %s AND po.status = 'pending'
        ORDER BY po.created_at DESC
        """,
        (engine,),
    )
    return [
        {
            "id": r["id"], "decision_id": r["decision_id"], "symbol": r["symbol"],
            "side": r["side"], "qty": float(r["qty"]), "order_type": r["order_type"],
            "limit_price": float(r["limit_price"]) if r["limit_price"] is not None else None,
            "trigger": r["trigger"], "confidence": r["confidence"],
            "rationale": r["rationale"],
            "valid_until": r["valid_until"].isoformat() if r["valid_until"] else None,
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


@router.get("/trades")
async def trades(engine: str, mode: str = Query("demo"), limit: int = Query(50, le=500)):
    rows = await fetch_all(
        """
        SELECT p.id, i.symbol, p.side, p.qty, p.avg_price, p.opened_at, p.closed_at,
               p.realized_pnl, p.fees_paid, p.open_decision_id
        FROM exec.position p
        JOIN ref.instrument i ON i.id = p.instrument_id
        WHERE p.engine = %s AND p.mode = %s AND p.closed_at IS NOT NULL
        ORDER BY p.closed_at DESC LIMIT %s
        """,
        (engine, mode, limit),
    )
    return [
        {
            "id": r["id"], "symbol": r["symbol"], "side": r["side"],
            "qty": float(r["qty"]), "avg_price": float(r["avg_price"]),
            "opened_at": r["opened_at"].isoformat(),
            "closed_at": r["closed_at"].isoformat(),
            "realized_pnl": float(r["realized_pnl"]),
            "fees_paid": float(r["fees_paid"]),
            "decision_id": r["open_decision_id"],
        }
        for r in rows
    ]
