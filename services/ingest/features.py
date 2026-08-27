#!/usr/bin/env python3
"""Liczenie cech z realnych swiec.

Cechy sa celowo proste i wytlumaczalne. Warstwa uczenia kalibruje ICH WAGI per
rezim rynkowy, a nie odkrywa cechy od zera - dzieki temu dashboard moze uczciwie
pokazac "dlaczego", bo naprawde wiadomo.

O DATOWANIU WSTECZ (to jest miejsce, w ktorym najlatwiej cicho unieważnić caly
backtest):

Cecha liczona wylacznie z zamknietych swiec jest funkcja danych, ktore w tamtym
momencie BYLY dostepne. Wolno jej wiec nadac `ingested_at = event_time + kilka
sekund`, bo gdyby system dzialal wtedy, policzylby ja wtedy. Nie ma tu zadnego
wycieku informacji z przyszlosci.

Czego NIE wolno datowac wstecz: wszystkiego, co zalezy od danych poznanych
pozniej - sentymentu z artykulu zescrapowanego z opoznieniem, korekt danych,
rewizji. Takie cechy dziedzicza `ingested_at` swojego zrodla. Dlatego
`news_sent_24h` liczy sie osobna sciezka, respektujaca `article_analysis.ingested_at`.

Uzycie:
    DATABASE_URL=... python services/ingest/features.py --backfill --days 90
    DATABASE_URL=... python services/ingest/features.py --once
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg
from psycopg.rows import dict_row

# Opoznienie obliczeniowe doliczane do ingested_at przy datowaniu wstecz.
# Realistyczne, a jednoczesnie na tyle male, ze nie gubi sygnalu.
COMPUTE_LAG = timedelta(seconds=3)

BATCH = 4000

# (nazwa, wersja, code_ref, params, opis)
FEATURES = [
    ("rsi_14", 1, "features.momentum:rsi", {"period": 14, "bar": "5m"},
     "Wskaźnik siły względnej na świecach 5-minutowych. Poniżej 30 = wyprzedanie."),
    ("ema_ratio_20_50", 1, "features.trend:ema_ratio", {"fast": 20, "slow": 50, "bar": "5m"},
     "Iloraz średnich wykładniczych. Powyżej 1 = trend wzrostowy."),
    ("atr_pct_14", 1, "features.volatility:atr_pct", {"period": 14, "bar": "5m"},
     "Średni rzeczywisty zakres jako procent ceny. Miara zmienności niezależna od poziomu ceny."),
    ("ret_1h", 1, "features.momentum:ret", {"window": "1h"},
     "Zwrot z ostatniej godziny."),
    ("ret_4h", 1, "features.momentum:ret", {"window": "4h"},
     "Zwrot z ostatnich czterech godzin."),
    ("ret_24h", 1, "features.momentum:ret", {"window": "24h"},
     "Zwrot z ostatniej doby."),
    ("volume_z_1h", 1, "features.flow:volume_z", {"window": "1h", "lookback": 288},
     "Odchylenie wolumenu od średniej z ostatniej doby, w odchyleniach standardowych."),
    ("range_pos_24h", 1, "features.reversion:range_position", {"window": "24h"},
     "Pozycja ceny w zakresie ostatniej doby: 0 = przy minimum, 1 = przy maksimum."),
    ("news_sent_24h", 1, "features.news:weighted_sentiment", {"lookback": "24h"},
     "Sentyment newsowy ważony siłą, pewnością i nowością. Zero, gdy brak artykułów."),
]

# Cechy liczone z samych cen mozna datowac wstecz; newsowa - nie.
PRICE_FEATURES = {f[0] for f in FEATURES if f[0] != "news_sent_24h"}


def ensure_definitions(cur) -> dict[str, int]:
    ids = {}
    for name, version, code_ref, params, desc in FEATURES:
        import json
        cur.execute(
            "INSERT INTO feat.feature_def (name, version, code_ref, params, description, active)"
            " VALUES (%s,%s,%s,%s,%s,true)"
            " ON CONFLICT (name, version) DO UPDATE"
            "   SET code_ref = EXCLUDED.code_ref, params = EXCLUDED.params,"
            "       description = EXCLUDED.description"
            " RETURNING id",
            (name, version, code_ref, json.dumps(params), desc),
        )
        ids[name] = cur.fetchone()["id"]
    return ids


# ------------------------------------------------------------------ matematyka
def rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder RSI. Zwraca None, dopoki nie ma pelnego okna."""
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(-period, 0):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def atr_pct(bars: list[dict], period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    last_close = bars[-1]["close"]
    return (sum(trs) / period) / last_close * 100 if last_close else None


def zscore(values: list[float]) -> float | None:
    if len(values) < 20:
        return None
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    sd = math.sqrt(var)
    return (values[-1] - mean) / sd if sd > 1e-12 else 0.0


# ------------------------------------------------------------------ liczenie
def compute_for_instrument(cur, instrument_id: int, feat_ids: dict[str, int],
                           since: datetime, until: datetime) -> int:
    """Liczy cechy cenowe na swiecach 5-minutowych zbudowanych z 1m."""
    cur.execute(
        """
        SELECT time_bucket('5 minutes', event_time) AS t,
               (array_agg(open  ORDER BY event_time))[1]                   AS open,
               max(high) AS high, min(low) AS low,
               (array_agg(close ORDER BY event_time DESC))[1]              AS close,
               sum(volume) AS volume
        FROM market.candle
        WHERE instrument_id = %s AND timeframe = '1m'
          AND event_time >= %s AND event_time < %s
        GROUP BY 1 ORDER BY 1
        """,
        (instrument_id, since - timedelta(days=2), until),
    )
    bars = [
        {"t": r["t"], "open": float(r["open"]), "high": float(r["high"]),
         "low": float(r["low"]), "close": float(r["close"]), "volume": float(r["volume"])}
        for r in cur.fetchall()
    ]
    if len(bars) < 60:
        return 0

    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]
    rows: list[tuple] = []

    # 12 swiec 5-minutowych = 1 h, 48 = 4 h, 288 = 24 h.
    for i in range(60, len(bars)):
        t = bars[i]["t"]
        if t < since:
            continue
        ingested = t + COMPUTE_LAG
        win_c = closes[: i + 1]
        win_b = bars[: i + 1]

        vals: dict[str, float | None] = {
            "rsi_14": rsi(win_c[-30:], 14),
            "atr_pct_14": atr_pct(win_b[-30:], 14),
            "ret_1h": (win_c[-1] / win_c[-13] - 1) * 100 if i >= 12 else None,
            "ret_4h": (win_c[-1] / win_c[-49] - 1) * 100 if i >= 48 else None,
            "ret_24h": (win_c[-1] / win_c[-289] - 1) * 100 if i >= 288 else None,
            "volume_z_1h": zscore(volumes[max(0, i - 287): i + 1]),
        }

        f, s = ema(win_c[-80:], 20), ema(win_c[-80:], 50)
        vals["ema_ratio_20_50"] = (f / s) if (f and s) else None

        if i >= 288:
            window = win_b[-288:]
            hi = max(b["high"] for b in window)
            lo = min(b["low"] for b in window)
            vals["range_pos_24h"] = ((win_c[-1] - lo) / (hi - lo)) if hi > lo else 0.5

        for name, v in vals.items():
            if v is None or not math.isfinite(v):
                continue
            rows.append((feat_ids[name], instrument_id, t, v, ingested))

    sql = ("INSERT INTO feat.feature_value (feature_def_id, instrument_id, event_time,"
           " value, ingested_at) VALUES (%s,%s,%s,%s,%s)"
           " ON CONFLICT (feature_def_id, instrument_id, event_time) DO NOTHING")
    for k in range(0, len(rows), BATCH):
        cur.executemany(sql, rows[k:k + BATCH])
    return len(rows)


def compute_news_feature(cur, instrument_id: int, feat_id: int,
                         since: datetime, until: datetime) -> int:
    """Sentyment newsowy - z jawnym poszanowaniem opoznienia analizy.

    Wartosc w chwili T bierze pod uwage wylacznie artykuly, ktorych analiza byla
    GOTOWA przed T (an.ingested_at <= T). Bez tego warunku model uczylby sie
    z newsow, ktorych wtedy jeszcze nie znal.
    """
    cur.execute(
        """
        SELECT b.t,
               COALESCE(sum(
                   CASE an.direction WHEN 'bullish' THEN 1 WHEN 'bearish' THEN -1 ELSE 0 END
                   * an.magnitude * an.confidence * an.novelty * ai.relevance
               ), 0) AS score
        FROM generate_series(%s, %s, interval '5 minutes') AS b(t)
        LEFT JOIN news.article_instrument ai ON ai.instrument_id = %s
        LEFT JOIN news.article a  ON a.id  = ai.article_id
                                 AND a.event_time <= b.t
                                 AND a.event_time >  b.t - interval '24 hours'
        LEFT JOIN news.article_analysis an ON an.article_id = a.id
                                          AND an.ingested_at <= b.t
        GROUP BY b.t ORDER BY b.t
        """,
        (since, until, instrument_id),
    )
    rows = [(feat_id, instrument_id, r["t"], float(r["score"]), r["t"] + COMPUTE_LAG)
            for r in cur.fetchall()]
    sql = ("INSERT INTO feat.feature_value (feature_def_id, instrument_id, event_time,"
           " value, ingested_at) VALUES (%s,%s,%s,%s,%s)"
           " ON CONFLICT (feature_def_id, instrument_id, event_time) DO NOTHING")
    for k in range(0, len(rows), BATCH):
        cur.executemany(sql, rows[k:k + BATCH])
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--once", action="store_true", help="tylko ostatnia doba")
    ap.add_argument("--engine", default="crypto")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=args.days if args.backfill else 1)

    with psycopg.connect(url, row_factory=dict_row) as conn:
        cur = conn.cursor()
        feat_ids = ensure_definitions(cur)
        conn.commit()

        cur.execute(
            "SELECT id, symbol FROM ref.instrument WHERE engine = %s AND active ORDER BY symbol",
            (args.engine,),
        )
        instruments = cur.fetchall()

        total = 0
        for inst in instruments:
            n = compute_for_instrument(cur, inst["id"], feat_ids, since, now)
            conn.commit()
            m = compute_news_feature(cur, inst["id"], feat_ids["news_sent_24h"], since, now)
            conn.commit()
            total += n + m
            print(f"  {inst['symbol']:10s} {n:>8,} cen  {m:>7,} news")

        print(f"\nRazem {total:,} wartosci cech")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
