"""Zakladka ZRODLA: z jakich stron system korzysta, co z nich wyciagnal."""
from __future__ import annotations

from fastapi import APIRouter, Query

from services.ingest.llm import limit_dobowy_usd, limit_wywolan_dobowy

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
            # STAN KANALU - jedna odpowiedz na pytanie "czy to dziala", liczona
            # TUTAJ, a nie w komponencie.
            #
            # `last_status` opisuje OSTATNI ODCZYT, a worker newsow pyta
            # wylacznie kanaly `WHERE enabled` (news_worker.py). Kanal
            # wylaczony trzyma wiec zamrozone `ok` z chwili sprzed wylaczenia
            # i wyglada dokladnie jak kanal pracujacy - tyle ze z odczytem
            # sprzed trzech dni. Stary sukces nie jest dowodem pracy, a wiersz
            # bez odczytu w ogole nie jest wierszem ze statusem `ok`.
            #
            # Surowe `last_status` ZOSTAJE: to jedyny slad tego, jak kanal
            # konczyl prace, i diagnostyka po wylaczeniu z niego korzysta.
            # Zmienia sie tylko to, ktore pole odpowiada na pytanie o stan.
            "stan": ("wylaczony" if not r["enabled"]
                     else "nigdy_nieodpytany" if r["last_polled_at"] is None
                     else r["last_status"] or "nieznany"),
            "articles_seen": r["articles_seen"], "articles_kept": r["articles_kept"],
            "last_24h": r["last_24h"],
            # Ile z pobranych artykulow przeszlo filtr istotnosci. ~95% to szum,
            # wiec niski odsetek jest oczekiwany, nie alarmujacy.
            "keep_rate": (r["articles_kept"] / r["articles_seen"]) if r["articles_seen"] else None,
        }
        for r in rows
    ]


@router.get("/api-spend")
async def api_spend(dni: int = Query(30, le=365)):
    """Wydatki na model - z podzialem na przepustki i z limitem dobowym.

    Koszt jest tu widoczny nie dla ciekawostki. Analiza newsow to jedyny element
    systemu, ktory wydaje pieniadze poza gielda, i jedyny, ktory moze zaczac je
    wydawac w petli (np. gdy kanal zmieni identyfikatory wpisow i deduplikacja
    przestanie dzialac). Kwota widoczna w interfejsie jest tansza niz kwota
    odkryta na rachunku.
    """
    dobowe = await fetch_all(
        """
        SELECT date_trunc('day', event_time)::date AS dzien,
               purpose,
               count(*)                 AS wywolania,
               sum(input_tokens)        AS tokeny_wej,
               sum(output_tokens)       AS tokeny_wyj,
               round(sum(cost_usd), 4)  AS koszt
        FROM news.api_spend
        WHERE event_time >= now() - make_interval(days => %s)
        GROUP BY 1, 2 ORDER BY 1 DESC, 2
        """,
        (dni,),
    )
    dzis = await fetch_all(
        "SELECT COALESCE(sum(cost_usd), 0) AS koszt, count(*) AS wywolania"
        " FROM news.api_spend WHERE event_time >= date_trunc('day', now())")
    laczne = await fetch_all(
        "SELECT COALESCE(sum(cost_usd), 0) AS koszt FROM news.api_spend"
        " WHERE event_time >= now() - make_interval(days => %s)", (dni,))

    # Ten sam odczyt co w potoku analizy. Wlasna kopia `os.environ.get`
    # rozjechala by sie z workerem przy pierwszej zmianie nazwy zmiennej,
    # a rozjazd objawilby sie tym, ze UI pokazuje inny limit niz ten,
    # ktory faktycznie zatrzymuje wydatki.
    #
    # OBA LIMITY, bo worker ma OD POPRAWKI DWIE BRAMKI (`news_worker.py:412-417`
    # przerywa przy `usd >= DAILY_LIMIT_USD` ALBO `wywolania >=
    # DZIENNY_LIMIT_WYWOLAN`), a zakladka pokazywala wylacznie kwotowa.
    # To nie jest szczegol: bramka kwotowa nie zadzialala ANI RAZU. 11.09.2026
    # potok stanal calkowicie przy $0,114 z $5 - zatrzymal go DARMOWY prog
    # Gemini, liczony w ZAPYTANIACH - a karta pokazywala przy tym
    # "wykorzystanie 2% · w normie". Limit, ktory naprawde zatrzymal analize,
    # nie byl na ekranie w ogole.
    limit = limit_dobowy_usd()
    limit_wywolan = limit_wywolan_dobowy()
    wydano_dzis = float(dzis[0]["koszt"]) if dzis else 0.0
    wywolan_dzis = dzis[0]["wywolania"] if dzis else 0
    return {
        "limit_dobowy_usd": limit,
        "limit_dobowy_wywolan": limit_wywolan,
        "dzis": {
            "koszt_usd": round(wydano_dzis, 4),
            "wywolania": wywolan_dzis,
            "wykorzystanie": round(wydano_dzis / limit, 4) if limit else None,
            # Licznik bez mianownika nie mowi nic o tym, jak blisko progu jest
            # potok - a to jest ta bramka, ktora zatrzymuje go naprawde.
            "wykorzystanie_wywolan": (round(wywolan_dzis / limit_wywolan, 4)
                                      if limit_wywolan else None),
        },
        "okres_dni": dni,
        "koszt_okresu_usd": round(float(laczne[0]["koszt"]), 4) if laczne else 0.0,
        "dobowe": [
            {"dzien": r["dzien"].isoformat(), "przepustka": r["purpose"],
             "wywolania": r["wywolania"], "tokeny_wej": int(r["tokeny_wej"] or 0),
             "tokeny_wyj": int(r["tokeny_wyj"] or 0), "koszt_usd": float(r["koszt"])}
            for r in dobowe
        ],
    }


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
