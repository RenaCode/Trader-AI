#!/usr/bin/env python3
"""Petla decyzyjna silnika.

Jeden przebieg = jedna decyzja per instrument:
    odczyt cech point-in-time -> rezim -> sygnaly -> risk kernel -> zapis -> egzekucja

Cechy czytamy WYLACZNIE przez feat.as_of(). To nie jest wygoda, tylko jedyna
gwarancja, ze silnik na zywo i trening widza dokladnie to samo - osobna sciezka
odczytu "na szybko" po stronie live to definicja training/serving skew.

Egzekucja ma dwa tryby:
  internal_paper - symulacja po realnych cenach rynkowych (gdy brak kluczy)
  venue_demo     - zlecenia na koncie demo gieldy (gdy klucze sa wpiete)
Oba operuja na PRAWDZIWYCH cenach; roznica jest w tym, kto liczy wypelnienie.

Uzycie:
    DATABASE_URL=... python services/engine/loop.py --once
    DATABASE_URL=... python services/engine/loop.py --interval 300
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import zlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg
from psycopg.rows import dict_row

from services.engine.risk import RiskKernel
from services.engine.signals import SIGNALS, classify_regime, evaluate

# Progi decyzyjne. Ponizej ENTER nie wchodzimy - lepiej nie zrobic nic niz
# handlowac szumem; wiekszosc kosztow w tradingu to prowizje od decyzji,
# ktore nie mialy przewagi.
ENTER_THRESHOLD = 0.25
EXIT_THRESHOLD = 0.12
MIN_CONFIDENCE = 0.35

FEE_RATE = Decimal("0.00055")        # prowizja taker Bybit spot
SLIPPAGE = Decimal("0.0004")         # ostrozne zalozenie poslizgu

START_EQUITY = Decimal("100000")


def ensure_signal_defs(cur, engine: str) -> dict[str, int]:
    ids = {}
    for name, (_fn, family, label) in SIGNALS.items():
        cur.execute(
            "INSERT INTO sig.signal_def (name, version, engine, family, code_ref, description, enabled)"
            " VALUES (%s,1,%s,%s,%s,%s,true)"
            " ON CONFLICT (name, version) DO UPDATE SET description = EXCLUDED.description"
            " RETURNING id",
            (name, engine, family, f"services.engine.signals:{name}", label))
        ids[name] = cur.fetchone()["id"]
    return ids


def latest_features(cur, instrument_id: int, as_of: datetime) -> dict:
    """Odczyt point-in-time. Jedyna dozwolona sciezka - patrz komentarz modulu."""
    cur.execute(
        "SELECT feature_name, value FROM feat.as_of(%s, %s, NULL, interval '90 minutes')",
        (instrument_id, as_of))
    # feature_name ma postac 'nazwa@wersja' - do sygnalow idzie sama nazwa.
    return {r["feature_name"].split("@")[0]: r["value"] for r in cur.fetchall()}


def atr_percentile_80(cur, instrument_id: int, as_of: datetime) -> float | None:
    """Prog wysokiej zmiennosci liczony z HISTORII TEGO instrumentu.

    Stala wartosc nie ma sensu: 0,3% na 5 min to spokoj dla SOL i szok dla SPY.
    """
    cur.execute(
        """
        SELECT percentile_cont(0.8) WITHIN GROUP (ORDER BY fv.value) AS p80
        FROM feat.feature_value fv JOIN feat.feature_def fd ON fd.id = fv.feature_def_id
        WHERE fd.name = 'atr_pct_14' AND fv.instrument_id = %s
          AND fv.event_time BETWEEN %s - interval '14 days' AND %s
        """,
        (instrument_id, as_of, as_of))
    row = cur.fetchone()
    return float(row["p80"]) if row and row["p80"] is not None else None


def mark_price(cur, instrument_id: int, as_of: datetime) -> Decimal | None:
    cur.execute(
        "SELECT close FROM market.candle WHERE instrument_id = %s AND timeframe='1m'"
        " AND event_time <= %s ORDER BY event_time DESC LIMIT 1", (instrument_id, as_of))
    row = cur.fetchone()
    return Decimal(row["close"]) if row else None


def open_position(cur, engine: str, mode: str, instrument_id: int) -> dict | None:
    cur.execute(
        "SELECT id, side, qty, avg_price, opened_at FROM exec.position"
        " WHERE engine=%s AND mode=%s AND instrument_id=%s AND closed_at IS NULL LIMIT 1",
        (engine, mode, instrument_id))
    return cur.fetchone()


def seed_equity(cur, engine: str, mode: str) -> None:
    cur.execute("SELECT 1 FROM exec.equity_point WHERE engine=%s AND mode=%s LIMIT 1",
                (engine, mode))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO exec.equity_point (engine, mode, event_time, equity, cash,"
            " unrealized, realized_cum, exposure) VALUES (%s,%s, now(), %s, %s, 0, 0, 0)",
            (engine, mode, START_EQUITY, START_EQUITY))


def update_equity(cur, engine: str, mode: str, as_of: datetime) -> None:
    """Przelicza kapital: gotowka + niezrealizowany wynik otwartych pozycji."""
    cur.execute(
        "SELECT COALESCE(sum(realized_pnl - fees_paid), 0) AS realized FROM exec.position"
        " WHERE engine=%s AND mode=%s AND closed_at IS NOT NULL", (engine, mode))
    realized = Decimal(cur.fetchone()["realized"] or 0)

    cur.execute(
        "SELECT p.id, p.instrument_id, p.side, p.qty, p.avg_price FROM exec.position p"
        " WHERE p.engine=%s AND p.mode=%s AND p.closed_at IS NULL", (engine, mode))
    unrealized = Decimal(0)
    exposure = Decimal(0)
    for pos in cur.fetchall():
        px = mark_price(cur, pos["instrument_id"], as_of)
        if px is None:
            continue
        qty, entry = Decimal(pos["qty"]), Decimal(pos["avg_price"])
        diff = (px - entry) if pos["side"] == "long" else (entry - px)
        unrealized += diff * qty
        exposure += qty * px

    equity = START_EQUITY + realized + unrealized
    cur.execute(
        "INSERT INTO exec.equity_point (engine, mode, event_time, equity, cash, unrealized,"
        " realized_cum, exposure) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (engine, mode, event_time) DO NOTHING",
        (engine, mode, as_of.replace(second=0, microsecond=0),
         equity, equity - exposure, unrealized, realized, exposure))


def record_decision(cur, engine: str, mode: str, instrument_id: int, as_of: datetime,
                    action: str, qty, price, confidence: float, regime: str,
                    features: dict, verdict, rationale: str, contributions: list[dict],
                    signal_ids: dict[str, int], latency_ms: int) -> int | None:
    cur.execute(
        """
        INSERT INTO journal.decision (engine, mode, instrument_id, decided_at, action,
            target_qty, target_price, confidence, regime, feature_snapshot,
            risk_verdict, risk_reason, rationale, rationale_model, latency_ms)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'reguły',%s)
        ON CONFLICT (engine, instrument_id, decided_at) DO NOTHING
        RETURNING id
        """,
        (engine, mode, instrument_id, as_of, action, qty, price, confidence, regime,
         json.dumps({k: round(v, 6) for k, v in features.items()}),
         verdict.verdict, verdict.reason, rationale, latency_ms))
    row = cur.fetchone()
    if not row:
        return None
    did = row["id"]

    rows = []
    for c in contributions:
        sid = signal_ids.get(c["name"])
        if sid:
            rows.append((did, "signal", sid, c["label"], c["score"], c["weight"],
                         c["contribution"]))
    # Cecha nie ma wlasnego identyfikatora w tej tabeli (klucz glowny to
    # decision_id+kind+ref_id), wiec wyprowadzamy go z nazwy. Swiadomie crc32,
    # a NIE wbudowany hash(): ten jest losowany przy kazdym starcie interpretera
    # (PYTHONHASHSEED), wiec ta sama cecha dostawalaby inne ref_id po restarcie,
    # a ON CONFLICT przestalby dzialac.
    for name, value in features.items():
        rows.append((did, "feature", zlib.crc32(name.encode()) % 2147483647,
                     name, value, 0.0, 0.0))
    cur.executemany(
        "INSERT INTO journal.decision_contribution (decision_id, kind, ref_id, label,"
        " value, weight, contribution) VALUES (%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT DO NOTHING", rows)
    return did


def execute_paper(cur, engine: str, mode: str, instrument_id: int, decision_id: int,
                  side: str, qty: Decimal, price: Decimal, as_of: datetime) -> None:
    """Symulacja wypelnienia po realnej cenie, z prowizja i poslizgiem."""
    fill = price * (Decimal(1) + SLIPPAGE) if side == "buy" else price * (Decimal(1) - SLIPPAGE)
    fee = qty * fill * FEE_RATE

    cur.execute(
        "INSERT INTO exec.order (decision_id, engine, mode, instrument_id, client_order_id,"
        " side, order_type, qty, status, submitted_at, raw)"
        " VALUES (%s,%s,%s,%s,%s,%s,'market',%s,'filled',%s,%s) RETURNING id",
        (decision_id, engine, mode, instrument_id, f"tai-{decision_id}", side, qty, as_of,
         json.dumps({"execution": "internal_paper", "slippage": str(SLIPPAGE)})))
    oid = cur.fetchone()["id"]
    cur.execute(
        "INSERT INTO exec.fill (order_id, venue_fill_id, qty, price, fee, fee_ccy, event_time)"
        " VALUES (%s,%s,%s,%s,%s,'USDT',%s)",
        (oid, f"paper-{oid}", qty, fill, fee, as_of))
    cur.execute(
        "INSERT INTO exec.position (engine, mode, instrument_id, side, qty, avg_price,"
        " opened_at, fees_paid, open_decision_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (engine, mode, instrument_id, "long" if side == "buy" else "short",
         qty, fill, as_of, fee, decision_id))


def close_paper(cur, position: dict, price: Decimal, as_of: datetime,
                decision_id: int) -> Decimal:
    qty, entry = Decimal(position["qty"]), Decimal(position["avg_price"])
    exit_px = price * (Decimal(1) - SLIPPAGE) if position["side"] == "long" \
        else price * (Decimal(1) + SLIPPAGE)
    fee = qty * exit_px * FEE_RATE
    diff = (exit_px - entry) if position["side"] == "long" else (entry - exit_px)
    pnl = diff * qty
    cur.execute(
        "UPDATE exec.position SET closed_at=%s, realized_pnl=%s,"
        " fees_paid = fees_paid + %s, close_decision_id=%s WHERE id=%s",
        (as_of, pnl, fee, decision_id, position["id"]))
    return pnl


async def run_once(conn, engine: str = "crypto", mode: str = "demo",
                   verbose: bool = True) -> dict:
    cur = conn.cursor()
    signal_ids = ensure_signal_defs(cur, engine)
    seed_equity(cur, engine, mode)
    conn.commit()

    risk = RiskKernel(conn, engine, mode)
    now = datetime.now(timezone.utc)
    stats = {"rozwazone": 0, "wejscia": 0, "wyjscia": 0, "wstrzymane": 0, "zablokowane": 0}

    cur.execute("SELECT id, symbol FROM ref.instrument WHERE engine=%s AND active ORDER BY symbol",
                (engine,))
    for inst in cur.fetchall():
        started = time.perf_counter()
        iid, symbol = inst["id"], inst["symbol"]
        features = latest_features(cur, iid, now)
        if len(features) < 4:
            if verbose:
                print(f"  {symbol:10s} pominięty — za mało cech ({len(features)})")
            continue

        stats["rozwazone"] += 1
        price = mark_price(cur, iid, now)
        if price is None:
            continue

        regime, regime_conf = classify_regime(features, atr_percentile_80(cur, iid, now))
        score, confidence, contributions = evaluate(features, regime)
        position = open_position(cur, engine, mode, iid)

        # --- wybor akcji ----------------------------------------------------
        if position:
            same_way = (position["side"] == "long" and score > 0) or \
                       (position["side"] == "short" and score < 0)
            if not same_way or abs(score) < EXIT_THRESHOLD:
                action = "exit"
            else:
                action = "hold"
        elif abs(score) >= ENTER_THRESHOLD and confidence >= MIN_CONFIDENCE:
            action = "enter_long" if score > 0 else "enter_short"
        else:
            action = "hold"

        # Wielkosc pozycji rosnie z sila sygnalu, ale nigdy powyzej limitu -
        # o tym decyduje risk kernel, nie ten kod.
        qty = Decimal(0)
        notional = Decimal(0)
        if action in ("enter_long", "enter_short"):
            notional = (Decimal(3000) * Decimal(str(min(1.0, abs(score) * 2)))).quantize(Decimal("0.01"))
            qty = (notional / price).quantize(Decimal("0.000001"))

        verdict = risk.check(iid, notional, qty) if action.startswith("enter") \
            else type("V", (), {"verdict": "allow", "reason": None, "qty": qty,
                                "violations": [], "usage": []})()

        top = sorted(contributions, key=lambda c: -abs(c["contribution"]))[:2]
        top_txt = ", ".join(f"{c['label'].lower()} ({c['contribution']:+.3f})" for c in top)
        rationale = (
            f"Reżim: {regime}. Wynik złożony {score:+.3f} przy pewności {confidence:.0%}. "
            f"Największy wkład: {top_txt}."
            + (f" Ryzyko: {verdict.reason}." if verdict.reason else "")
        )

        latency = int((time.perf_counter() - started) * 1000)
        did = record_decision(cur, engine, mode, iid, now, action,
                              verdict.qty if action.startswith("enter") else (
                                  position["qty"] if position else None),
                              price, confidence, regime, features, verdict,
                              rationale, contributions, signal_ids, latency)

        risk.record_usage(verdict.usage)
        risk.record_violations(verdict.violations, did)

        if did and verdict.verdict != "block":
            if action == "exit" and position:
                close_paper(cur, position, price, now, did)
                stats["wyjscia"] += 1
            elif action.startswith("enter") and verdict.qty and verdict.qty > 0:
                execute_paper(cur, engine, mode, iid, did,
                              "buy" if action == "enter_long" else "sell",
                              verdict.qty, price, now)
                stats["wejscia"] += 1
            else:
                stats["wstrzymane"] += 1
        elif verdict.verdict == "block":
            stats["zablokowane"] += 1

        if verbose:
            print(f"  {symbol:10s} {regime:11s} wynik={score:+.3f} pewność={confidence:.0%} "
                  f"-> {action:12s} [{verdict.verdict}]")
        conn.commit()

    update_equity(cur, engine, mode, now)
    cur.execute(
        "INSERT INTO risk.engine_heartbeat (engine, mode, last_beat_at, status, detail)"
        " VALUES (%s,%s, now(), 'running', %s)"
        " ON CONFLICT (engine) DO UPDATE SET last_beat_at=now(), status='running',"
        " detail=EXCLUDED.detail",
        (engine, mode, json.dumps(stats)))
    conn.commit()
    return stats


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    with psycopg.connect(url, row_factory=dict_row) as conn:
        while True:
            t0 = datetime.now(timezone.utc)
            try:
                s = await run_once(conn, args.engine, verbose=not args.quiet)
                print(f"[{t0:%H:%M:%S}] rozważone {s['rozwazone']}, wejścia {s['wejscia']}, "
                      f"wyjścia {s['wyjscia']}, wstrzymane {s['wstrzymane']}, "
                      f"zablokowane {s['zablokowane']}")
            except Exception as e:                            # noqa: BLE001
                conn.rollback()
                print(f"[{t0:%H:%M:%S}] przebieg przerwany: {type(e).__name__}: {e}")
            if args.once:
                return 0
            await asyncio.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
