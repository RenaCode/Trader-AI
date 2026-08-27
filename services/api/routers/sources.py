"""Zakladka ZRODLA: z jakich stron system korzysta, co z nich wyciagnal."""
from __future__ import annotations

from fastapi import APIRouter, Query

from ..db import fetch_all

router = APIRouter(prefix="/api", tags=["sources"])


@router.get("/feeds")
async def feeds(engine: str | None = None):
    rows = await fetch_all(
        """
        SELECT f.id, f.name, f.url, f.engine, f.enabled, f.poll_interval_s,
               f.last_polled_at, f.last_status, f.last_error,
               f.articles_seen, f.articles_kept,
               (SELECT count(*) FROM news.article a
                WHERE a.feed_id = f.id AND a.ingested_at >= now() - interval '24 hours') AS last_24h
        FROM news.feed f
        WHERE (%s::text IS NULL OR f.engine::text = %s OR f.engine IS NULL)
        ORDER BY f.engine NULLS LAST, f.name
        """,
        (engine, engine),
    )
    return [
        {
            "id": r["id"], "name": r["name"], "url": r["url"], "engine": r["engine"],
            "enabled": r["enabled"], "poll_interval_s": r["poll_interval_s"],
            "last_polled_at": r["last_polled_at"].isoformat() if r["last_polled_at"] else None,
            "last_status": r["last_status"], "last_error": r["last_error"],
            "articles_seen": r["articles_seen"], "articles_kept": r["articles_kept"],
            "last_24h": r["last_24h"],
            # Ile z pobranych artykulow przeszlo filtr istotnosci. ~95% to szum,
            # wiec niski odsetek jest oczekiwany, nie alarmujacy.
            "keep_rate": (r["articles_kept"] / r["articles_seen"]) if r["articles_seen"] else None,
        }
        for r in rows
    ]


@router.get("/news")
async def news(engine: str | None = None, limit: int = Query(30, le=200)):
    rows = await fetch_all(
        """
        SELECT a.id, a.title, a.url, a.event_time, a.ingested_at, f.name AS source,
               an.direction, an.magnitude, an.confidence, an.novelty, an.horizon,
               an.reasoning, an.model,
               array_remove(array_agg(DISTINCT i.symbol), NULL) AS symbols
        FROM news.article a
        JOIN news.feed f ON f.id = a.feed_id
        LEFT JOIN news.article_analysis an ON an.article_id = a.id
        LEFT JOIN news.article_instrument ai ON ai.article_id = a.id
        LEFT JOIN ref.instrument i ON i.id = ai.instrument_id
        WHERE (%s::text IS NULL OR f.engine::text = %s OR f.engine IS NULL)
        GROUP BY a.id, f.name, an.direction, an.magnitude, an.confidence,
                 an.novelty, an.horizon, an.reasoning, an.model
        ORDER BY a.event_time DESC LIMIT %s
        """,
        (engine, engine, limit),
    )
    return [
        {
            "id": r["id"], "title": r["title"], "url": r["url"], "source": r["source"],
            "at": r["event_time"].isoformat(),
            # Roznica miedzy tymi dwoma to opoznienie, z jakim system poznal newsa.
            # Decyzje wczesniejsze niz ingested_at nie mogly go widziec.
            "ingested_at": r["ingested_at"].isoformat(),
            "lag_s": int((r["ingested_at"] - r["event_time"]).total_seconds()),
            "direction": r["direction"], "magnitude": r["magnitude"],
            "confidence": r["confidence"], "novelty": r["novelty"],
            "horizon": r["horizon"], "reasoning": r["reasoning"], "model": r["model"],
            "symbols": r["symbols"] or [],
        }
        for r in rows
    ]
