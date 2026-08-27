#!/usr/bin/env python3
"""Skaner wiadomosci: RSS -> filtr istotnosci -> analiza -> sygnal.

Dwustopniowosc jest celowa. Okolo 95% artykulow w kanalach crypto to szum
(podsumowania, powtorki, teksty sponsorowane). Tani model odsiewa je za ulamek
kosztu, drogi analizuje wylacznie to, co przeszlo. Bez tego rachunek za API
rosnie kilkunastokrotnie bez zadnego zysku informacyjnego.

Dwa znaczniki czasu sa tu krytyczne:
  event_time  = publikacja artykulu
  ingested_at = moment, w ktorym MY skonczylismy analize
Decyzja podjeta przed `ingested_at` nie moze tego artykulu widziec - to jest
jedyna rzecz chroniaca warstwe uczenia przed uczeniem sie z przyszlosci.

Uzycie:
    DATABASE_URL=... ANTHROPIC_API_KEY=... python services/ingest/news_worker.py --once
    DATABASE_URL=... ANTHROPIC_API_KEY=... python services/ingest/news_worker.py   # petla
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import feedparser
import httpx
import psycopg
from anthropic import AsyncAnthropic
from psycopg.rows import dict_row

from services.api.security import SecretBox

ANALYSIS_MODEL = os.environ.get("ANALYSIS_MODEL", "claude-opus-5")
TRIAGE_MODEL = os.environ.get("TRIAGE_MODEL", "claude-haiku-4-5")

# Schemat wymuszony na modelu. Bez `strict` model potrafi dorzucic pole albo
# zwrocic liczbe jako tekst, a to konczy sie wyjatkiem przy zapisie do bazy.
ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "instruments": {"type": "array", "items": {"type": "string"}},
        "direction": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
        "magnitude": {"type": "number"},
        "horizon": {"type": "string", "enum": ["minutes", "hours", "days", "weeks"]},
        "confidence": {"type": "number"},
        "novelty": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": ["relevant", "instruments", "direction", "magnitude", "horizon",
                 "confidence", "novelty", "reasoning"],
    "additionalProperties": False,
}

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "worth_analyzing": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["worth_analyzing", "reason"],
    "additionalProperties": False,
}

# Prefiks stabilny miedzy wywolaniami -> trafia w cache promptow (~10x taniej
# przy powtarzalnych zapytaniach). Wszystko zmienne idzie PO nim.
TRIAGE_SYSTEM = """Jesteś filtrem istotności dla systemu tradingowego.

Oceniasz, czy nagłówek wiadomości może w ogóle wpłynąć na cenę instrumentu
finansowego w horyzoncie od minut do tygodni.

Odrzucaj: podsumowania tygodnia, materiały sponsorowane, poradniki, powtórzenia
tego samego zdarzenia, spekulacje bez nowego faktu, ceny podane bez kontekstu.

Przyjmuj: decyzje regulatorów i banków centralnych, wyniki spółek, przepływy
instytucjonalne, awarie i włamania, zmiany w protokołach, dane makro, wypowiedzi
osób realnie wpływających na rynek.

Bądź surowy. Fałszywe przyjęcie kosztuje analizę drogim modelem; fałszywe
odrzucenie kosztuje jeden sygnał wśród wielu."""

ANALYSIS_SYSTEM = """Analizujesz wiadomości rynkowe dla autonomicznego systemu
tradingowego. Twoja ocena wchodzi jako JEDNA z wielu cech do modelu, który sam
kalibruje jej wagę — nie podejmujesz decyzji o transakcji.

Wypełniaj pola według tych znaczeń:

- instruments: symbole, których wiadomość realnie dotyczy (np. BTCUSDT, ETHUSDT,
  SOLUSDT, SPY, AAPL). Pusta lista, jeśli żaden z nich.
- direction: kierunek spodziewanego wpływu na cenę.
- magnitude 0..1: siła oczekiwanego ruchu. 0.1 to szum, 0.9 to zdarzenie
  przestawiające rynek.
- horizon: w jakim czasie wpływ się zrealizuje.
- confidence 0..1: jak pewny jesteś SWOJEJ oceny, nie tego, że rynek zareaguje.
- novelty 0..1: ile z tego jest NOWĄ informacją, której rynek jeszcze nie wycenił.
  To najważniejsze i najczęściej mylone pole. Wiadomość o mocnym wydźwięku, ale
  powtarzająca coś znanego od tygodnia, ma novelty bliskie zeru — i wtedy jej
  wartość dla systemu jest bliska zeru niezależnie od magnitude.
- reasoning: dwa, trzy zdania po polsku. Powiedz wprost, czego NIE wiesz.

Nie zawyżaj pewności. System mierzy kalibrację twoich ocen i rozjazd między
deklarowaną a rzeczywistą trafnością obniża wagę tej cechy."""


def anthropic_key(conn) -> str:
    """Klucz z bazy (wpisany w interfejsie), a w razie jego braku ze srodowiska.

    Kolejnosc jest celowa: klucz podany przez uzytkownika w UI ma pierwszenstwo,
    bo to on jest wymieniany bez redeployu. Zmienna srodowiskowa zostaje jako
    zapasowa sciezka dla CI i uruchomien lokalnych.
    """
    row = conn.execute("SELECT value_enc FROM app.secret WHERE name = 'anthropic_api_key'").fetchone()
    if row:
        return SecretBox().decrypt(bytes(row[0]))
    env = os.environ.get("ANTHROPIC_API_KEY")
    if env:
        return env
    sys.exit(
        "Brak klucza Anthropic. Wpisz go w interfejsie (Ustawienia -> Klucz API Anthropic) "
        "albo ustaw ANTHROPIC_API_KEY."
    )


def content_hash(title: str, summary: str) -> bytes:
    return hashlib.sha256(f"{title}\n{summary}".encode()).digest()


async def fetch_feed(client: httpx.AsyncClient, url: str) -> tuple[list, str]:
    """Zwraca (wpisy, status). Blad kanalu nie moze zatrzymac calego cyklu."""
    try:
        r = await client.get(url, follow_redirects=True,
                             headers={"User-Agent": "Trader-AI/0.1 (+https://trader.renacode.com)"})
        if r.status_code != 200:
            return [], f"http_{r.status_code}"
        parsed = feedparser.parse(r.content)
        return parsed.entries, "ok"
    except httpx.TimeoutException:
        return [], "timeout"
    except Exception as e:                                   # noqa: BLE001
        return [], f"blad: {type(e).__name__}"


def entry_time(entry) -> datetime:
    for key in ("published_parsed", "updated_parsed"):
        t = getattr(entry, key, None)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


async def triage(client: AsyncAnthropic, title: str, summary: str) -> tuple[bool, str]:
    r = await client.messages.create(
        model=TRIAGE_MODEL,
        max_tokens=300,
        system=[{"type": "text", "text": TRIAGE_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        output_config={"format": {"type": "json_schema", "schema": TRIAGE_SCHEMA}},
        messages=[{"role": "user", "content": f"Tytuł: {title}\n\nZajawka: {summary[:600]}"}],
    )
    if r.stop_reason == "refusal":
        return False, "model odmówił oceny"
    data = json.loads(next(b.text for b in r.content if b.type == "text"))
    return data["worth_analyzing"], data["reason"]


async def analyze(client: AsyncAnthropic, title: str, summary: str,
                  source: str, published: datetime, symbols: list[str]) -> tuple[dict, dict]:
    r = await client.messages.create(
        model=ANALYSIS_MODEL,
        max_tokens=1500,
        thinking={"type": "adaptive"},
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA},
        },
        system=[{"type": "text", "text": ANALYSIS_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{
            "role": "user",
            "content": (
                f"Instrumenty w obserwacji: {', '.join(symbols)}\n"
                f"Źródło: {source}\n"
                f"Opublikowano: {published:%Y-%m-%d %H:%M} UTC\n"
                f"Tytuł: {title}\n\n{summary[:3000]}"
            ),
        }],
    )
    if r.stop_reason == "refusal":
        raise RuntimeError("model odmówił analizy")
    data = json.loads(next(b.text for b in r.content if b.type == "text"))
    usage = {
        "input": r.usage.input_tokens,
        "output": r.usage.output_tokens,
        "cache_read": getattr(r.usage, "cache_read_input_tokens", 0) or 0,
    }
    return data, usage


# Cennik (USD za milion tokenow) - do policzenia kosztu per artykul.
PRICE = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def cost_usd(model: str, usage: dict) -> float:
    inp, out = PRICE.get(model, (5.0, 25.0))
    # Odczyt z cache kosztuje ~10% ceny wejscia.
    billed_in = (usage["input"] - usage["cache_read"]) + usage["cache_read"] * 0.1
    return round(billed_in / 1e6 * inp + usage["output"] / 1e6 * out, 6)


async def run_cycle(conn, ai: AsyncAnthropic, http: httpx.AsyncClient, verbose: bool) -> dict:
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT id, symbol FROM ref.instrument WHERE active")
    instruments = {r["symbol"]: r["id"] for r in cur.fetchall()}
    symbols = sorted(instruments)

    cur.execute("""
        SELECT id, name, url, engine, poll_interval_s, last_polled_at
        FROM news.feed
        WHERE enabled
          AND (last_polled_at IS NULL
               OR last_polled_at < now() - make_interval(secs => poll_interval_s))
        ORDER BY last_polled_at NULLS FIRST
    """)
    feeds = cur.fetchall()

    stats = {"kanaly": len(feeds), "pobrane": 0, "nowe": 0, "przeszly_filtr": 0,
             "przeanalizowane": 0, "koszt": 0.0, "bledy": 0}

    for feed in feeds:
        entries, status = await fetch_feed(http, feed["url"])
        stats["pobrane"] += len(entries)
        if status != "ok":
            stats["bledy"] += 1

        seen = kept = 0
        for entry in entries[:40]:
            title = (getattr(entry, "title", "") or "").strip()
            summary = (getattr(entry, "summary", "") or getattr(entry, "description", "") or "").strip()
            if not title:
                continue
            seen += 1

            h = content_hash(title, summary)
            cur.execute("SELECT 1 FROM news.article WHERE content_hash = %s", (h,))
            if cur.fetchone():
                continue
            stats["nowe"] += 1

            published = entry_time(entry)
            # Artykuly starsze niz doba nie maja wartosci sygnalowej, a kosztuja
            # tyle samo - przy pierwszym uruchomieniu kanal potrafi zwrocic archiwum.
            if published < datetime.now(timezone.utc) - timedelta(days=1):
                continue

            try:
                worth, why = await triage(ai, title, summary)
            except Exception as e:                            # noqa: BLE001
                stats["bledy"] += 1
                if verbose:
                    print(f"    filtr padl: {type(e).__name__}: {e}")
                continue

            if not worth:
                if verbose:
                    print(f"    [odsiane] {title[:70]} — {why}")
                continue
            stats["przeszly_filtr"] += 1
            kept += 1

            try:
                data, usage = await analyze(ai, title, summary, feed["name"], published, symbols)
            except Exception as e:                            # noqa: BLE001
                stats["bledy"] += 1
                if verbose:
                    print(f"    analiza padla: {type(e).__name__}: {e}")
                continue

            if not data.get("relevant"):
                continue

            price = cost_usd(ANALYSIS_MODEL, usage)
            stats["koszt"] += price
            stats["przeanalizowane"] += 1

            cur.execute(
                "INSERT INTO news.article (feed_id, url, title, summary, content_hash,"
                " event_time, ingested_at) VALUES (%s,%s,%s,%s,%s,%s, now())"
                " ON CONFLICT (content_hash) DO NOTHING RETURNING id",
                (feed["id"], getattr(entry, "link", "") or feed["url"], title,
                 summary[:4000], h, published),
            )
            row = cur.fetchone()
            if not row:
                continue
            article_id = row["id"]

            cur.execute(
                "INSERT INTO news.article_analysis (article_id, model, prompt_version, direction,"
                " magnitude, horizon, confidence, novelty, reasoning, raw, input_tokens,"
                " output_tokens, cost_usd, event_time, ingested_at)"
                " VALUES (%s,%s,'v1',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())",
                (article_id, ANALYSIS_MODEL, data["direction"],
                 max(0.0, min(1.0, data["magnitude"])), data["horizon"],
                 max(0.0, min(1.0, data["confidence"])), max(0.0, min(1.0, data["novelty"])),
                 data["reasoning"], json.dumps(data), usage["input"], usage["output"],
                 price, published),
            )

            for sym in data.get("instruments", []):
                iid = instruments.get(sym.upper())
                if iid:
                    cur.execute(
                        "INSERT INTO news.article_instrument (article_id, instrument_id, relevance)"
                        " VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                        (article_id, iid, max(0.0, min(1.0, data["confidence"]))),
                    )

            if verbose:
                print(f"    [{data['direction']:>7s}] siła {data['magnitude']:.2f} "
                      f"nowość {data['novelty']:.2f}  {title[:60]}")
            conn.commit()

        cur.execute(
            "UPDATE news.feed SET last_polled_at = now(), last_status = %s,"
            " articles_seen = articles_seen + %s, articles_kept = articles_kept + %s"
            " WHERE id = %s",
            (status, seen, kept, feed["id"]),
        )
        conn.commit()

    return stats


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="jeden cykl zamiast petli")
    ap.add_argument("--interval", type=int, default=120, help="przerwa miedzy cyklami (s)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    async with httpx.AsyncClient(timeout=20.0) as http:
        with psycopg.connect(url) as conn:
            ai = AsyncAnthropic(api_key=anthropic_key(conn))
            while True:
                started = datetime.now(timezone.utc)
                try:
                    s = await run_cycle(conn, ai, http, verbose=not args.quiet)
                    took = (datetime.now(timezone.utc) - started).total_seconds()
                    print(f"[{started:%H:%M:%S}] kanałów {s['kanaly']}, wpisów {s['pobrane']}, "
                          f"nowych {s['nowe']}, przez filtr {s['przeszly_filtr']}, "
                          f"zapisanych {s['przeanalizowane']}, koszt ${s['koszt']:.4f}, "
                          f"błędów {s['bledy']}, {took:.0f} s")
                except Exception as e:                        # noqa: BLE001
                    print(f"[{started:%H:%M:%S}] cykl przerwany: {type(e).__name__}: {e}")
                    conn.rollback()

                if args.once:
                    return 0
                await asyncio.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
