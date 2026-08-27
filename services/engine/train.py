#!/usr/bin/env python3
"""Trener walk-forward: kalibracja wag sygnalow per rezim.

Protokol jest jedyny dopuszczalny w tym projekcie i celowo niewygodny:

  okno 1:  [--- trening 14 dni ---][test 2 dni]
  okno 2:      [--- trening 14 dni ---][test 2 dni]
  ...

Wagi dobierane sa WYLACZNIE na oknie treningowym, a wynik raportowany WYLACZNIE
z okna testowego, ktorego optymalizacja nigdy nie widziala. Zwykla walidacja
krzyzowa jest tu niewazna, bo tasowanie szeregu czasowego wpuszcza przyszlosc
do treningu.

Kazda sprawdzona konfiguracja laduje w learn.hypothesis_trial. Bez tego licznika
deflated Sharpe jest nieobliczalny, a im wiecej konfiguracji sie przetestuje, tym
latwiej trafic dobry wynik czystym przypadkiem. Licznik jest po to, zeby nie dalo
sie oszukac samego siebie selekcja.

Uzycie:
    DATABASE_URL=... python services/engine/train.py --days 90 --trials 40
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg
from psycopg.rows import dict_row

from services.engine.signals import DEFAULT_WEIGHTS, REGIMES, SIGNALS, classify_regime, evaluate

TRAIN_DAYS = 14
TEST_DAYS = 2
STEP_DAYS = 2

ENTER = 0.25
EXIT = 0.12
MIN_CONF = 0.35
FEE = 0.00055
SLIP = 0.0004

SEED = 20260827


# ------------------------------------------------------------------ dane
def load_panel(cur, instrument_id: int, since: datetime, until: datetime) -> list[dict]:
    """Panel cech w ukladzie czas -> {cecha: wartosc}, plus cena i przyszly zwrot.

    `forward_ret` liczymy TYLKO do oceny wyniku symulacji. Nie trafia do sygnalow
    i nie jest widoczne dla wag - inaczej optymalizacja uczylaby sie przyszlosci.
    """
    cur.execute(
        """
        WITH f AS (
            SELECT fv.event_time, fd.name, fv.value
            FROM feat.feature_value fv JOIN feat.feature_def fd ON fd.id = fv.feature_def_id
            WHERE fv.instrument_id = %s AND fv.event_time BETWEEN %s AND %s
        ),
        p AS (
            SELECT time_bucket('5 minutes', event_time) AS t,
                   (array_agg(close ORDER BY event_time DESC))[1] AS close
            FROM market.candle
            WHERE instrument_id = %s AND timeframe='1m' AND event_time BETWEEN %s AND %s
            GROUP BY 1
        )
        SELECT f.event_time AS t, jsonb_object_agg(f.name, f.value) AS feats, p.close
        FROM f JOIN p ON p.t = f.event_time
        GROUP BY f.event_time, p.close ORDER BY f.event_time
        """,
        (instrument_id, since, until, instrument_id, since, until),
    )
    rows = [{"t": r["t"], "f": {k: float(v) for k, v in r["feats"].items()},
             "price": float(r["close"])} for r in cur.fetchall()]
    return rows


def atr_threshold(panel: list[dict]) -> float | None:
    vals = [r["f"]["atr_pct_14"] for r in panel if "atr_pct_14" in r["f"]]
    if len(vals) < 50:
        return None
    vals.sort()
    return vals[int(len(vals) * 0.8)]


# ------------------------------------------------------------------ symulacja
def simulate(panel: list[dict], weights: dict[str, dict[str, float]],
             atr_p80: float | None) -> dict:
    """Odtwarza strategie na panelu. Zwraca metryki i liste zwrotow z transakcji.

    Wejscie i wyjscie po cenie NASTEPNEJ swiecy, nie biezacej - decyzja podjeta
    na zamknieciu swiecy T nie moze zostac wykonana po cenie tej samej swiecy.
    To jeden z najczestszych i najbardziej optymistycznych bledow w backtestach.
    """
    position = None          # {"side","entry","idx"}
    trades: list[float] = []
    equity = 1.0
    curve = [1.0]

    for i in range(len(panel) - 1):
        row = panel[i]
        nxt = panel[i + 1]["price"]
        regime, _ = classify_regime(row["f"], atr_p80)
        score, conf, _ = evaluate(row["f"], regime, weights.get(regime))

        if position is None:
            if abs(score) >= ENTER and conf >= MIN_CONF:
                side = 1 if score > 0 else -1
                entry = nxt * (1 + SLIP * side)
                position = {"side": side, "entry": entry}
                equity *= (1 - FEE)
        else:
            same = (position["side"] > 0 and score > 0) or (position["side"] < 0 and score < 0)
            if not same or abs(score) < EXIT:
                exit_px = nxt * (1 - SLIP * position["side"])
                ret = position["side"] * (exit_px / position["entry"] - 1)
                equity *= (1 + ret) * (1 - FEE)
                trades.append(ret)
                position = None
        curve.append(equity)

    if not trades:
        return {"sharpe": 0.0, "sortino": 0.0, "max_dd": 0.0, "hit_rate": 0.0,
                "avg_r": 0.0, "profit_factor": 0.0, "n_trades": 0, "total_return": 0.0,
                "trades": []}

    mean = statistics.fmean(trades)
    sd = statistics.pstdev(trades) or 1e-9
    downside = [t for t in trades if t < 0]
    dsd = statistics.pstdev(downside) if len(downside) > 1 else sd

    # Annualizacja: swiece 5-minutowe, rynek 24/7 -> 105 120 obserwacji rocznie.
    # Skalujemy liczba transakcji, nie obserwacji, bo Sharpe liczymy na transakcjach.
    per_year = 105120 / max(1, len(panel)) * len(trades)
    sharpe = mean / sd * math.sqrt(max(1.0, per_year))
    sortino = mean / (dsd or 1e-9) * math.sqrt(max(1.0, per_year))

    peak = curve[0]
    max_dd = 0.0
    for v in curve:
        peak = max(peak, v)
        max_dd = min(max_dd, (v - peak) / peak * 100)

    gains = sum(t for t in trades if t > 0)
    losses = -sum(t for t in trades if t < 0)
    return {
        "sharpe": round(sharpe, 4), "sortino": round(sortino, 4),
        "max_dd": round(max_dd, 4),
        "hit_rate": round(sum(1 for t in trades if t > 0) / len(trades), 4),
        "avg_r": round(mean * 100, 4),
        "profit_factor": round(gains / losses, 4) if losses > 0 else 0.0,
        "n_trades": len(trades), "total_return": round((equity - 1) * 100, 4),
        "trades": trades,
    }


def random_weights(rng: random.Random) -> dict[str, dict[str, float]]:
    """Losowy zestaw wag per rezim, znormalizowany do sumy 1."""
    out = {}
    for regime in REGIMES:
        raw = {name: rng.random() for name in SIGNALS}
        total = sum(raw.values()) or 1.0
        out[regime] = {k: round(v / total, 4) for k, v in raw.items()}
    return out


# ------------------------------------------------------------------ trening
def train(conn, engine: str, days: int, trials_per_window: int) -> None:
    cur = conn.cursor()
    rng = random.Random(SEED)
    now = datetime.now(timezone.utc)

    cur.execute("SELECT id, symbol FROM ref.instrument WHERE engine=%s AND active ORDER BY symbol",
                (engine,))
    instruments = cur.fetchall()
    if not instruments:
        print("brak instrumentow")
        return

    cur.execute(
        "INSERT INTO learn.model (name, engine, kind, description) VALUES (%s,%s,%s,%s)"
        " ON CONFLICT (name) DO UPDATE SET kind = EXCLUDED.kind RETURNING id",
        (f"{engine}_signal_weighting", engine, "signal_weighting",
         "Kalibracja wag sygnałów per reżim rynkowy metodą walk-forward"))
    model_id = cur.fetchone()["id"]

    cur.execute("SELECT COALESCE(max(version), 0) + 1 AS v FROM learn.model_version"
                " WHERE model_id = %s", (model_id,))
    version = cur.fetchone()["v"]

    cur.execute("SELECT array_agg(id) AS ids FROM feat.feature_def WHERE active")
    feature_ids = cur.fetchone()["ids"] or []
    cur.execute("SELECT array_agg(id) AS ids FROM sig.signal_def WHERE engine = %s", (engine,))
    signal_ids = cur.fetchone()["ids"] or []

    cur.execute(
        "INSERT INTO learn.model_version (model_id, version, status, params, feature_def_ids,"
        " signal_def_ids, trained_from, trained_to) VALUES (%s,%s,'training',%s,%s,%s,%s,%s)"
        " RETURNING id",
        (model_id, version,
         json.dumps({"train_days": TRAIN_DAYS, "test_days": TEST_DAYS, "step_days": STEP_DAYS,
                     "trials_per_window": trials_per_window, "seed": SEED}),
         feature_ids, signal_ids, now - timedelta(days=days), now))
    mv_id = cur.fetchone()["id"]

    cur.execute(
        "INSERT INTO learn.training_run (model_version_id, train_from, train_to, status)"
        " VALUES (%s,%s,%s,'running') RETURNING id",
        (mv_id, now - timedelta(days=days), now))
    run_id = cur.fetchone()["id"]
    conn.commit()

    # Panel wczytujemy RAZ na instrument - to najdrozsza operacja w calym treningu.
    panels = {}
    for inst in instruments:
        p = load_panel(cur, inst["id"], now - timedelta(days=days), now)
        if len(p) > 500:
            panels[inst["symbol"]] = p
        print(f"  {inst['symbol']:10s} {len(p):>7,} punktów panelu")
    if not panels:
        print("za mało danych do treningu")
        cur.execute("UPDATE learn.training_run SET status='failed', finished_at=now(),"
                    " error='za mało punktów panelu' WHERE id=%s", (run_id,))
        conn.commit()
        return

    idx = 0
    positive = 0
    all_sharpes: list[float] = []
    worst_dd = 0.0
    predictions: list[tuple[float, bool]] = []
    start = now - timedelta(days=days)

    while start + timedelta(days=TRAIN_DAYS + TEST_DAYS) <= now:
        train_from = start
        train_to = start + timedelta(days=TRAIN_DAYS)
        test_from = train_to
        test_to = test_from + timedelta(days=TEST_DAYS)

        train_slices = {s: [r for r in p if train_from <= r["t"] < train_to]
                        for s, p in panels.items()}
        test_slices = {s: [r for r in p if test_from <= r["t"] < test_to]
                       for s, p in panels.items()}
        if min((len(v) for v in train_slices.values()), default=0) < 200:
            start += timedelta(days=STEP_DAYS)
            continue

        thresholds = {s: atr_threshold(train_slices[s]) for s in panels}

        # --- dobor wag WYLACZNIE na oknie treningowym ------------------------
        best_w, best_score = DEFAULT_WEIGHTS, -1e9
        for _ in range(trials_per_window):
            cand = random_weights(rng)
            score = statistics.fmean([
                simulate(train_slices[s], cand, thresholds[s])["sharpe"] for s in panels
            ])
            cfg_hash = hashlib.sha256(json.dumps(cand, sort_keys=True).encode()).digest()
            cur.execute(
                "INSERT INTO learn.hypothesis_trial (model_id, config_hash, params, n_windows)"
                " VALUES (%s,%s,%s,1) ON CONFLICT (model_id, config_hash) DO NOTHING",
                (model_id, cfg_hash, json.dumps(cand)))
            if score > best_score:
                best_w, best_score = cand, score

        # Zalozenia poczatkowe tez sa kandydatem - jesli losowanie nie potrafi ich
        # pobic na oknie treningowym, to znaczy, ze nie ma czego kalibrowac.
        base_score = statistics.fmean([
            simulate(train_slices[s], DEFAULT_WEIGHTS, thresholds[s])["sharpe"] for s in panels])
        if base_score > best_score:
            best_w, best_score = DEFAULT_WEIGHTS, base_score

        # --- ocena OUT-OF-SAMPLE --------------------------------------------
        oos = [simulate(test_slices[s], best_w, thresholds[s]) for s in panels if test_slices[s]]
        if not oos:
            start += timedelta(days=STEP_DAYS)
            continue

        agg = {
            "sharpe": statistics.fmean(m["sharpe"] for m in oos),
            "sortino": statistics.fmean(m["sortino"] for m in oos),
            "max_dd": min(m["max_dd"] for m in oos),
            "hit_rate": statistics.fmean(m["hit_rate"] for m in oos),
            "avg_r": statistics.fmean(m["avg_r"] for m in oos),
            "profit_factor": statistics.fmean(m["profit_factor"] for m in oos),
        }
        n_trades = sum(m["n_trades"] for m in oos)
        all_sharpes.append(agg["sharpe"])
        worst_dd = min(worst_dd, agg["max_dd"])
        if agg["sharpe"] > 0:
            positive += 1

        for m in oos:
            for t in m["trades"]:
                predictions.append((min(1.0, abs(t) * 50 + 0.5), t > 0))

        regimes = [classify_regime(r["f"], thresholds[s])[0]
                   for s in panels for r in test_slices[s][::20]]
        dominant = max(set(regimes), key=regimes.count) if regimes else None

        cur.execute(
            "INSERT INTO learn.wf_window (training_run_id, idx, train_from, train_to,"
            " test_from, test_to, n_trades, dominant_regime) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
            " RETURNING id",
            (run_id, idx, train_from, train_to, test_from, test_to, n_trades, dominant))
        w_id = cur.fetchone()["id"]
        cur.executemany(
            "INSERT INTO learn.wf_metric (wf_window_id, metric, value) VALUES (%s,%s,%s)",
            [(w_id, k, round(v, 4)) for k, v in agg.items()])
        conn.commit()

        print(f"  okno {idx:>2d}  {test_from:%m-%d}–{test_to:%m-%d}  "
              f"Sharpe(OOS)={agg['sharpe']:+.3f}  transakcje={n_trades}  reżim={dominant}")
        idx += 1
        start += timedelta(days=STEP_DAYS)

    # --- kalibracja -------------------------------------------------------
    ece = None
    if predictions:
        ece = 0.0
        for lo in [i / 10 for i in range(0, 10)]:
            bucket = [(p, hit) for p, hit in predictions if lo <= p < lo + 0.1]
            if len(bucket) < 5:
                continue
            pred = statistics.fmean(p for p, _ in bucket)
            obs = statistics.fmean(1.0 if h else 0.0 for _, h in bucket)
            ece += abs(pred - obs) * len(bucket) / len(predictions)
            cur.execute(
                "INSERT INTO learn.calibration_bin (model_version_id, evaluated_at, bin_lo,"
                " bin_hi, predicted_mean, observed_rate, n_samples)"
                " VALUES (%s, now(), %s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (mv_id, lo, lo + 0.1, round(pred, 4), round(obs, 4), len(bucket)))

    # --- dryf cech --------------------------------------------------------
    max_psi = 0.0
    cur.execute("SELECT id, name FROM feat.feature_def WHERE active")
    for fd in cur.fetchall():
        cur.execute(
            """
            SELECT stddev_pop(value) AS sd_old, avg(value) AS m_old
            FROM feat.feature_value WHERE feature_def_id=%s
              AND event_time BETWEEN %s AND %s
            """, (fd["id"], now - timedelta(days=days), now - timedelta(days=days // 3)))
        old = cur.fetchone()
        cur.execute(
            """
            SELECT stddev_pop(value) AS sd_new, avg(value) AS m_new
            FROM feat.feature_value WHERE feature_def_id=%s AND event_time >= %s
            """, (fd["id"], now - timedelta(days=days // 3)))
        new = cur.fetchone()
        if not (old and new and old["sd_old"] and new["sd_new"]):
            continue
        # Uproszczony wskaznik dryfu: przesuniecie sredniej w jednostkach
        # historycznego odchylenia plus zmiana samego odchylenia.
        psi = abs(float(new["m_new"]) - float(old["m_old"])) / float(old["sd_old"]) \
            + abs(float(new["sd_new"]) / float(old["sd_old"]) - 1)
        max_psi = max(max_psi, psi)
        cur.execute(
            "INSERT INTO learn.drift_metric (model_version_id, feature_def_id, event_time, psi)"
            " VALUES (%s,%s, now(), %s) ON CONFLICT DO NOTHING",
            (mv_id, fd["id"], round(psi, 4)))

    # --- podsumowanie -----------------------------------------------------
    total = len(all_sharpes)
    pct = (positive / total * 100) if total else 0.0
    median = statistics.median(all_sharpes) if all_sharpes else None
    cur.execute("SELECT count(*) AS n FROM learn.hypothesis_trial WHERE model_id=%s", (model_id,))
    trials = cur.fetchone()["n"]

    status = ("validated" if pct >= 70 and median and median > 0.3
              else "degraded" if total >= 5 and pct < 40 else "learning")
    reason = {
        "validated": f"Dodatnie {positive}/{total} okien out-of-sample ({pct:.0f}%). Kryteria spełnione.",
        "learning": f"Dodatnie {positive}/{total} okien out-of-sample ({pct:.0f}%). Próg przejścia na live: 70%.",
        "degraded": f"Dodatnie tylko {positive}/{total} okien ({pct:.0f}%) — strategia nie wykazuje przewagi.",
    }[status]

    cur.execute(
        "UPDATE learn.model_version SET status = CASE WHEN %s = 'degraded' THEN 'candidate'"
        " ELSE 'active' END, promoted_at = now() WHERE id = %s", (status, mv_id))
    cur.execute("UPDATE learn.model_version SET status='retired', retired_at=now()"
                " WHERE model_id=%s AND id<>%s AND status='active'", (model_id, mv_id))
    cur.execute("UPDATE learn.training_run SET status='done', finished_at=now(), n_windows=%s"
                " WHERE id=%s", (total, run_id))
    cur.execute(
        """
        INSERT INTO learn.learning_state (engine, status, model_version_id, windows_total,
            windows_positive, median_oos_sharpe, max_drawdown, calibration_error, max_psi,
            trials_run, reason, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
        ON CONFLICT (engine) DO UPDATE SET status=EXCLUDED.status,
            model_version_id=EXCLUDED.model_version_id, windows_total=EXCLUDED.windows_total,
            windows_positive=EXCLUDED.windows_positive,
            median_oos_sharpe=EXCLUDED.median_oos_sharpe, max_drawdown=EXCLUDED.max_drawdown,
            calibration_error=EXCLUDED.calibration_error, max_psi=EXCLUDED.max_psi,
            trials_run=EXCLUDED.trials_run, reason=EXCLUDED.reason, updated_at=now()
        """,
        (engine, status, mv_id, total, positive,
         round(median, 4) if median is not None else None,
         round(worst_dd, 4), round(ece, 4) if ece is not None else None,
         round(max_psi, 4), trials, reason))
    conn.commit()

    print(f"\nOkna: {total}, dodatnie: {positive} ({pct:.1f}%)")
    print(f"Mediana Sharpe (OOS): {median:+.3f}" if median is not None else "Mediana Sharpe: —")
    print(f"Najgorsze obsunięcie: {worst_dd:.2f}%")
    print(f"Przetestowanych konfiguracji: {trials}")
    print(f"Stan: {status} — {reason}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--trials", type=int, default=40)
    ap.add_argument("--engine", default="crypto")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    with psycopg.connect(url, row_factory=dict_row) as conn:
        train(conn, args.engine, args.days, args.trials)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
