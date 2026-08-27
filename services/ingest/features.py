#!/usr/bin/env python3
"""Liczenie cech z realnych swiec - sterowane horyzontem.

Cechy sa celowo proste i wytlumaczalne. Warstwa uczenia kalibruje ICH WAGI per
rezim rynkowy, a nie odkrywa cechy od zera - dzieki temu dashboard moze uczciwie
pokazac "dlaczego", bo naprawde wiadomo.

Cechy zapisujemy pod nazwami ROLOWYMI z sufiksem swiecy: `rsi__4h`, `ret_long__1h`.
Rola opisuje, CO cecha mierzy; horyzont mowi, w jakiej skali. Dzieki temu ten sam
zestaw sygnalow dziala na kazdej swiecy, a wyniki dwoch horyzontow da sie porownac,
bo to ta sama strategia, nie dwie rozne.

O DATOWANIU WSTECZ (to jest miejsce, w ktorym najlatwiej cicho uniewaznic caly
backtest):

Cecha liczona wylacznie z zamknietych swiec jest funkcja danych, ktore w tamtym
momencie BYLY dostepne. Wolno jej wiec nadac `ingested_at = event_time + kilka
sekund`, bo gdyby system dzialal wtedy, policzylby ja wtedy. Nie ma tu zadnego
wycieku informacji z przyszlosci.

Czego NIE wolno datowac wstecz: wszystkiego, co zalezy od danych poznanych
pozniej - sentymentu z artykulu zescrapowanego z opoznieniem, korekt, rewizji.
Dlatego `news_sent` liczy sie osobna sciezka: jej zapytanie filtruje po
`article_analysis.ingested_at <= T`, wiec wartosc w chwili T zna wylacznie
analizy gotowe przed T. Dopiero to uprawnia do datowania jej wstecz.

Uzycie:
    DATABASE_URL=... python services/ingest/features.py --bar 4h --backfill --days 1400
    DATABASE_URL=... python services/ingest/features.py --bar 4h --once
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg
from psycopg.rows import dict_row

from services.engine.horizons import DEFAULT_BAR, HORIZONS, Horizon, get as get_horizon

# Opoznienie obliczeniowe doliczane do ingested_at przy datowaniu wstecz.
COMPUTE_LAG = timedelta(seconds=3)

BATCH = 4000

# rola -> (code_ref, szablon opisu). {okno} podstawia sie z etykiet horyzontu.
ROLES = {
    "rsi":         ("features.momentum:rsi",
                    "Wskaźnik siły względnej ({period} świec {bar}). Poniżej 30 = wyprzedanie."),
    "ema_ratio":   ("features.trend:ema_ratio",
                    "Iloraz średnich wykładniczych {fast}/{slow} na świecach {bar}. "
                    "Powyżej 1 = trend wzrostowy."),
    "atr_pct":     ("features.volatility:atr_pct",
                    "Średni rzeczywisty zakres jako procent ceny ({period} świec {bar}). "
                    "Miara zmienności niezależna od poziomu ceny."),
    "ret_short":   ("features.momentum:ret", "Zwrot z ostatnich {okno}."),
    "ret_mid":     ("features.momentum:ret", "Zwrot z ostatnich {okno}."),
    "ret_long":    ("features.momentum:ret", "Zwrot z ostatnich {okno}."),
    "volume_z":    ("features.flow:volume_z",
                    "Odchylenie wolumenu świecy od średniej z {okno}, w odchyleniach standardowych."),
    "range_pos":   ("features.reversion:range_position",
                    "Pozycja ceny w zakresie {okno}: 0 = przy minimum, 1 = przy maksimum."),
    "news_sent":   ("features.news:weighted_sentiment",
                    "Sentyment newsowy z ostatniej doby, ważony siłą, pewnością i nowością. "
                    "Zero, gdy brak artykułów."),
    "funding_rate": ("features.derivatives:funding",
                     "Stawka finansowania perpetuala (co 8 h). Dodatnia = długie płacą krótkim, "
                     "czyli rynek jest wychylony na wzrost."),
    "funding_z":   ("features.derivatives:funding_z",
                    "Odchylenie stawki finansowania od średniej z 30 dni, w odchyleniach standardowych."),
}

# Role liczone z samych cen - wolno datowac wstecz bez zastrzezen.
PRICE_ROLES = ("rsi", "ema_ratio", "atr_pct", "ret_short", "ret_mid", "ret_long",
               "volume_z", "range_pos")


def describe(role: str, h: Horizon) -> str:
    tpl = ROLES[role][1]
    okno = h.labels.get(role) or h.labels.get("range" if role == "range_pos" else
                                              "volume" if role == "volume_z" else role, "")
    return tpl.format(okno=okno, bar=h.bar, period=h.rsi_period if role == "rsi" else h.atr_period,
                      fast=h.ema_fast, slow=h.ema_slow)


def params_for(role: str, h: Horizon) -> dict:
    p: dict = {"bar": h.bar, "role": role}
    if role == "rsi":
        p["period"] = h.rsi_period
    elif role == "atr_pct":
        p["period"] = h.atr_period
    elif role == "ema_ratio":
        p |= {"fast": h.ema_fast, "slow": h.ema_slow}
    elif role in ("ret_short", "ret_mid", "ret_long"):
        p |= {"bars": getattr(h, role), "okno": h.labels.get(role)}
    elif role == "range_pos":
        p |= {"bars": h.range_window, "okno": h.labels.get("range")}
    elif role == "volume_z":
        p |= {"bars": h.volume_lookback, "okno": h.labels.get("volume")}
    return p


def ensure_definitions(cur, h: Horizon) -> dict[str, int]:
    """Rejestruje definicje cech dla tego horyzontu i zwraca rola -> id."""
    ids = {}
    for role, (code_ref, _tpl) in ROLES.items():
        cur.execute(
            "INSERT INTO feat.feature_def (name, version, code_ref, params, description, active)"
            " VALUES (%s,1,%s,%s,%s,true)"
            " ON CONFLICT (name, version) DO UPDATE"
            "   SET code_ref = EXCLUDED.code_ref, params = EXCLUDED.params,"
            "       description = EXCLUDED.description, active = true"
            " RETURNING id",
            (h.name(role), code_ref, json.dumps(params_for(role, h)), describe(role, h)),
        )
        ids[role] = cur.fetchone()["id"]
    return ids


# ------------------------------------------------------------------ matematyka
def rsi(closes: list[float], period: int) -> float | None:
    """RSI w wariancie prostej sredniej z `period` ostatnich zmian (Cutler).

    Swiadomie NIE jest to wygladzanie Wildera: wariant Wildera zalezy od calej
    historii szeregu, wiec ta sama chwila policzona przy backfillu i na zywo
    dalaby inna wartosc, zaleznie od tego, gdzie zaczal sie bufor. Wariant
    prostej sredniej zalezy wylacznie od okna - i dzieki temu trening i produkcja
    licza dokladnie to samo.
    """
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
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def ema(values: list[float], period: int) -> float | None:
    """EMA zainicjowana srednia z pierwszych `period` wartosci.

    Bufor wejsciowy musi byc staly (patrz EMA_BUFFER), inaczej wartosc zalezy
    od dlugosci historii, a nie od danych.
    """
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def atr_pct(bars: list[dict], period: int) -> float | None:
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        hi, lo, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    last_close = bars[-1]["close"]
    return (sum(trs) / period) / last_close * 100 if last_close else None


def zscore(values: list[float]) -> float | None:
    if len(values) < 20:
        return None
    mean = sum(values) / len(values)
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
    return (values[-1] - mean) / sd if sd > 1e-12 else 0.0


# ------------------------------------------------------------------ swiece
def load_bars(cur, instrument_id: int, h: Horizon, since: datetime,
              until: datetime) -> list[dict]:
    """Swiece horyzontu: albo wprost z market.candle, albo agregat z nizszej."""
    if h.aggregate:
        sql = """
            SELECT time_bucket(%s::interval, event_time) AS t,
                   (array_agg(open  ORDER BY event_time))[1]      AS open,
                   max(high) AS high, min(low) AS low,
                   (array_agg(close ORDER BY event_time DESC))[1] AS close,
                   sum(volume) AS volume
            FROM market.candle
            WHERE instrument_id = %s AND timeframe = %s
              AND event_time >= %s AND event_time < %s
            GROUP BY 1 ORDER BY 1
        """
        args = (f"{h.bar_minutes} minutes", instrument_id, h.source_timeframe, since, until)
    else:
        sql = """
            SELECT event_time AS t, open, high, low, close, volume
            FROM market.candle
            WHERE instrument_id = %s AND timeframe = %s
              AND event_time >= %s AND event_time < %s
            ORDER BY 1
        """
        args = (instrument_id, h.source_timeframe, since, until)
    cur.execute(sql, args)
    return [{"t": r["t"], "open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"]),
             "volume": float(r["volume"])} for r in cur.fetchall()]


# ------------------------------------------------------------------ liczenie
def compute_prices(cur, instrument_id: int, h: Horizon, feat_ids: dict[str, int],
                   since: datetime, until: datetime) -> int:
    # Rozgrzewka: najdluzsze okno + zapas. Bez niej pierwsze swiece okresu
    # mialyby cechy policzone z niepelnej historii - czyli inne niz te same
    # swiece policzone przy nastepnym uruchomieniu.
    lookback = max(h.ret_long, h.range_window, h.volume_lookback,
                   h.ema_slow, h.warmup) + 5
    bars = load_bars(cur, instrument_id, h,
                     since - timedelta(minutes=h.bar_minutes * lookback), until)
    if len(bars) <= lookback:
        return 0

    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]
    # Staly bufor dla EMA - patrz komentarz przy ema().
    ema_buf = h.ema_slow * 4
    rows: list[tuple] = []

    for i in range(lookback, len(bars)):
        t = bars[i]["t"]
        if t < since:
            continue
        ingested = t + COMPUTE_LAG
        win_c, win_b = closes[: i + 1], bars[: i + 1]

        vals: dict[str, float | None] = {
            "rsi": rsi(win_c[-(h.rsi_period * 3):], h.rsi_period),
            "atr_pct": atr_pct(win_b[-(h.atr_period * 3):], h.atr_period),
            "ret_short": (win_c[-1] / win_c[-1 - h.ret_short] - 1) * 100,
            "ret_mid": (win_c[-1] / win_c[-1 - h.ret_mid] - 1) * 100,
            "ret_long": (win_c[-1] / win_c[-1 - h.ret_long] - 1) * 100,
            "volume_z": zscore(volumes[i - h.volume_lookback + 1: i + 1]),
        }

        fast, slow = ema(win_c[-ema_buf:], h.ema_fast), ema(win_c[-ema_buf:], h.ema_slow)
        vals["ema_ratio"] = (fast / slow) if (fast and slow) else None

        window = win_b[-h.range_window:]
        hi = max(b["high"] for b in window)
        lo = min(b["low"] for b in window)
        vals["range_pos"] = ((win_c[-1] - lo) / (hi - lo)) if hi > lo else 0.5

        for role, v in vals.items():
            if v is None or not math.isfinite(v):
                continue
            rows.append((feat_ids[role], instrument_id, t, v, ingested))

    return insert_values(cur, rows)


def compute_news(cur, instrument_id: int, h: Horizon, feat_id: int,
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
        FROM generate_series(%s, %s, %s::interval) AS b(t)
        LEFT JOIN news.article_instrument ai ON ai.instrument_id = %s
        LEFT JOIN news.article a  ON a.id  = ai.article_id
                                 AND a.event_time <= b.t
                                 AND a.event_time >  b.t - interval '24 hours'
        LEFT JOIN news.article_analysis an ON an.article_id = a.id
                                          AND an.ingested_at <= b.t
        GROUP BY b.t ORDER BY b.t
        """,
        (since, until, f"{h.bar_minutes} minutes", instrument_id),
    )
    rows = [(feat_id, instrument_id, r["t"], float(r["score"]), r["t"] + COMPUTE_LAG)
            for r in cur.fetchall()]
    return insert_values(cur, rows)


def compute_funding(cur, instrument_id: int, h: Horizon, feat_ids: dict[str, int],
                    since: datetime, until: datetime) -> int:
    """Stawka finansowania perpetuala, przeniesiona na siatke swiec.

    Instrumenty spot nie maja wlasnego fundingu - bierzemy go z perpetuala na tej
    samej parze (`ref.instrument.meta->>'funding_from'`). To jest sygnal o
    pozycjonowaniu calego rynku, wiec stosuje sie do instrumentu bazowego.

    Kazda swieca dostaje OSTATNIA stawke ogloszona przed jej zamknieciem -
    nigdy nadchodzaca. Stawka publikowana o 08:00 nie moze wplywac na decyzje
    z 04:00, choc opisuje to samo osmiogodzinne okno.

    Wypelnianie w przod robimy jednym przejsciem po dwoch posortowanych listach,
    a nie skorelowanym podzapytaniem per punkt siatki. Wersja z podzapytaniem
    wykonywala 8 400 osobnych wyszukiwan na instrument po hipertabeli i zajmowala
    ponad dwie minuty na instrument; to samo przejsciem scalajacym trwa ulamek
    sekundy.
    """
    cur.execute("SELECT (meta->>'funding_from')::int AS src FROM ref.instrument WHERE id=%s",
                (instrument_id,))
    row = cur.fetchone()
    src = row["src"] if row else None
    if not src:
        return 0

    # Zapas przed poczatkiem okresu, zeby pierwsza swieca tez miala czym byc
    # wypelniona (stawka ogloszona jeszcze przed `since`).
    cur.execute(
        "SELECT event_time, rate FROM market.funding_rate"
        " WHERE instrument_id = %s AND event_time >= %s AND event_time <= %s"
        " ORDER BY event_time",
        (src, since - timedelta(days=2), until),
    )
    rates = [(r["event_time"], float(r["rate"])) for r in cur.fetchall()]
    if not rates:
        return 0

    step = timedelta(minutes=h.bar_minutes)
    # Okno z-score: 30 dni wyrazone w swiecach tego horyzontu.
    win = max(20, 30 * 1440 // h.bar_minutes)

    rows: list[tuple] = []
    hist: deque[float] = deque(maxlen=win)
    j = 0
    last: float | None = None
    t = since
    while t <= until:
        while j < len(rates) and rates[j][0] <= t:
            last = rates[j][1]
            j += 1
        if last is not None:
            ingested = t + COMPUTE_LAG
            hist.append(last)
            rows.append((feat_ids["funding_rate"], instrument_id, t, last * 100, ingested))
            if len(hist) >= 20:
                mean = sum(hist) / len(hist)
                sd = math.sqrt(sum((v - mean) ** 2 for v in hist) / len(hist))
                if sd > 1e-12:
                    z = (last - mean) / sd
                    if math.isfinite(z):
                        rows.append((feat_ids["funding_z"], instrument_id, t, z, ingested))
        t += step

    return insert_values(cur, rows)


def insert_values(cur, rows: list[tuple]) -> int:
    sql = ("INSERT INTO feat.feature_value (feature_def_id, instrument_id, event_time,"
           " value, ingested_at) VALUES (%s,%s,%s,%s,%s)"
           " ON CONFLICT (feature_def_id, instrument_id, event_time) DO NOTHING")
    for k in range(0, len(rows), BATCH):
        cur.executemany(sql, rows[k:k + BATCH])
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bar", default=DEFAULT_BAR, choices=sorted(HORIZONS))
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--once", action="store_true", help="tylko ostatnia doba")
    ap.add_argument("--engine", default="crypto")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    h = get_horizon(args.bar)
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=args.days if args.backfill else 1)
    print(f"Cechy na świecach {h.bar} od {since:%Y-%m-%d}\n")

    with psycopg.connect(url, row_factory=dict_row) as conn:
        cur = conn.cursor()
        feat_ids = ensure_definitions(cur, h)
        conn.commit()

        cur.execute(
            "SELECT id, symbol FROM ref.instrument WHERE engine = %s AND active"
            " AND kind <> 'perp' ORDER BY symbol", (args.engine,))
        instruments = cur.fetchall()

        total = 0
        for inst in instruments:
            n = compute_prices(cur, inst["id"], h, feat_ids, since, now)
            conn.commit()
            m = compute_news(cur, inst["id"], h, feat_ids["news_sent"], since, now)
            conn.commit()
            f = compute_funding(cur, inst["id"], h, feat_ids, since, now)
            conn.commit()
            total += n + m + f
            print(f"  {inst['symbol']:10s} {n:>9,} cen  {m:>7,} news  {f:>7,} funding")

        print(f"\nRazem {total:,} wartości cech")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
