#!/usr/bin/env python3
"""Generator danych demonstracyjnych dla Trader-AI.

CEL: zapelnic wszystkie tabele realistycznymi danymi, zeby dalo sie zweryfikowac
API i dashboard end-to-end zanim podlaczymy prawdziwa gielde.

To NIE jest backtest ani symulacja strategii - to atrapa o wlasciwym KSZTALCIE.
Liczby sa wygenerowane losowo z ustalonym ziarnem i nie znacza nic poza tym,
ze wykresy maja co rysowac.

Uruchomienie:  DATABASE_URL=... python services/seed_demo.py [--reset]
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import sys
from datetime import datetime, timedelta, timezone

import psycopg

SEED = 20260827
random.seed(SEED)

NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)
DAYS = 45

FEATURES = [
    ("rsi_14",             "features.momentum:rsi",           {"period": 14}),
    ("ema_ratio_20_50",    "features.trend:ema_ratio",        {"fast": 20, "slow": 50}),
    ("atr_pct_14",         "features.volatility:atr_pct",     {"period": 14}),
    ("book_imbalance_5m",  "features.flow:book_imbalance",    {"window": "5m"}),
    ("volume_zscore_1h",   "features.flow:volume_z",          {"window": "1h"}),
    ("news_sentiment_24h", "features.news:weighted_sentiment", {"lookback": "24h"}),
]

SIGNALS = [
    ("momentum_breakout",  "momentum",       "signals.momentum:breakout"),
    ("mean_reversion_rsi", "mean_reversion", "signals.reversion:rsi_extreme"),
    ("flow_imbalance",     "flow",           "signals.flow:imbalance"),
    ("news_shock",         "news",           "signals.news:shock"),
    ("session_open_drift", "seasonality",    "signals.seasonality:session_open"),
]

REGIMES = ["trend_up", "trend_down", "range", "high_vol", "news_shock"]

HEADLINES = {
    "bullish": [
        "Instytucjonalne napływy do funduszy ETF na {sym} najwyższe od kwartału",
        "{sym} przebija poziom oporu po danych o inflacji",
        "Duży adres akumuluje {sym} — dane on-chain wskazują na podaż schodzącą z giełd",
        "Analitycy podnoszą prognozę dla {sym} po wynikach kwartalnych",
    ],
    "bearish": [
        "Regulator zapowiada zaostrzenie nadzoru — {sym} pod presją",
        "{sym} traci po wyprzedaży na rynkach ryzykownych aktywów",
        "Wzrost rezerw giełdowych {sym} sugeruje presję podażową",
        "Słabsze dane makro ciążą notowaniom {sym}",
    ],
    "neutral": [
        "{sym} w konsolidacji — rynek czeka na decyzję Fed",
        "Podsumowanie tygodnia: {sym} bez wyraźnego kierunku",
        "Wolumen na {sym} poniżej średniej 30-dniowej",
    ],
}


def h(*parts) -> bytes:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()


def gbm_series(n: int, start: float, drift: float, vol: float) -> list[float]:
    """Geometryczny ruch Browna - daje szereg cen o realistycznym ksztalcie."""
    out, price = [], start
    for _ in range(n):
        price *= math.exp(drift + vol * random.gauss(0, 1))
        out.append(price)
    return out


def main(reset: bool) -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    with psycopg.connect(url) as conn:
        cur = conn.cursor()

        if reset:
            # journal.* jest append-only (trigger), wiec TRUNCATE zamiast DELETE.
            cur.execute("""
                TRUNCATE journal.decision, journal.decision_contribution, journal.planned_order,
                         exec.order, exec.fill, exec.position, exec.equity_point,
                         news.article, news.article_analysis, news.article_instrument,
                         news.article_embedding,
                         feat.feature_value, feat.feature_def, feat.regime,
                         sig.signal_value, sig.signal_def,
                         learn.model, learn.model_version, learn.training_run,
                         learn.wf_window, learn.wf_metric, learn.calibration_bin,
                         learn.drift_metric, learn.hypothesis_trial, learn.postmortem,
                         risk.limit_usage, risk.violation, risk.killswitch_event
                RESTART IDENTITY CASCADE
            """)
            cur.execute("UPDATE learn.learning_state SET model_version_id = NULL")

        cur.execute("SELECT id, symbol, engine FROM ref.instrument ORDER BY id")
        instruments = cur.fetchall()
        by_engine: dict[str, list] = {}
        for iid, sym, eng in instruments:
            by_engine.setdefault(eng, []).append((iid, sym))

        # ---------------------------------------------------------- swiece
        print("swiece...", end=" ", flush=True)
        start_prices = {"BTCUSDT": 64000, "ETHUSDT": 3100, "SOLUSDT": 145, "SPY": 545, "AAPL": 228}
        candle_rows, closes = [], {}
        # 15-minutowe swiece przez DAYS dni: dosc na wykresy, malo na dysku.
        n_bars = DAYS * 24 * 4
        for iid, sym, _eng in instruments:
            base = start_prices.get(sym, 100)
            series = gbm_series(n_bars, base, drift=0.00002, vol=0.0035)
            closes[iid] = series
            t0 = NOW - timedelta(minutes=15 * n_bars)
            for k, close in enumerate(series):
                t = t0 + timedelta(minutes=15 * k)
                o = series[k - 1] if k else close
                hi = max(o, close) * (1 + abs(random.gauss(0, 0.0012)))
                lo = min(o, close) * (1 - abs(random.gauss(0, 0.0012)))
                candle_rows.append((iid, "15m", t, o, hi, lo, close,
                                    abs(random.gauss(1000, 300)), random.randint(50, 400), t))
        cur.executemany(
            "INSERT INTO market.candle (instrument_id, timeframe, event_time, open, high, low,"
            " close, volume, trade_count, ingested_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT DO NOTHING", candle_rows)
        print(f"{len(candle_rows)}")

        # ---------------------------------------------------------- cechy i sygnaly
        print("cechy/sygnaly...", end=" ", flush=True)
        feat_ids = {}
        for name, code_ref, params in FEATURES:
            cur.execute(
                "INSERT INTO feat.feature_def (name, version, code_ref, params, description)"
                " VALUES (%s,1,%s,%s,%s) ON CONFLICT (name, version) DO UPDATE SET code_ref=EXCLUDED.code_ref"
                " RETURNING id", (name, code_ref, json.dumps(params), f"Cecha {name}"))
            feat_ids[name] = cur.fetchone()[0]

        sig_ids = {}
        for eng in by_engine:
            for name, family, code_ref in SIGNALS:
                cur.execute(
                    "INSERT INTO sig.signal_def (name, version, engine, family, code_ref, description)"
                    " VALUES (%s,1,%s,%s,%s,%s) ON CONFLICT (name, version) DO UPDATE"
                    " SET family=EXCLUDED.family RETURNING id",
                    (name, eng, family, code_ref, f"Sygnal {name}"))
                sig_ids[(eng, name)] = cur.fetchone()[0]

        fv_rows, sv_rows, regime_rows = [], [], []
        for iid, sym, eng in instruments:
            series = closes[iid]
            t0 = NOW - timedelta(minutes=15 * n_bars)
            # Co 8 slupkow (2h) - dosc gesto na wykresy, nie zapycha bazy.
            for k in range(0, n_bars, 8):
                t = t0 + timedelta(minutes=15 * k)
                window = series[max(0, k - 14):k + 1]
                chg = (series[k] / series[max(0, k - 14)] - 1) if k >= 14 else 0.0
                vals = {
                    "rsi_14": max(2, min(98, 50 + chg * 900)),
                    "ema_ratio_20_50": 1 + chg * 0.6,
                    "atr_pct_14": (max(window) - min(window)) / series[k] * 100 if window else 0.5,
                    "book_imbalance_5m": random.gauss(0, 0.28),
                    "volume_zscore_1h": random.gauss(0, 1.1),
                    "news_sentiment_24h": random.gauss(0, 0.35),
                }
                for name, v in vals.items():
                    # ingested_at ~ event_time + male opoznienie: tak wyglada rzeczywistosc.
                    fv_rows.append((feat_ids[name], iid, t, v, t + timedelta(seconds=random.randint(1, 40))))

                rsi = vals["rsi_14"]
                atr = vals["atr_pct_14"]
                regime = ("high_vol" if atr > 3.2 else
                          "trend_up" if chg > 0.02 else
                          "trend_down" if chg < -0.02 else "range")
                regime_rows.append((iid, t, regime, round(random.uniform(0.55, 0.93), 3), t))

                for name, _fam, _ref in SIGNALS:
                    if name == "mean_reversion_rsi":
                        score = max(-1, min(1, (50 - rsi) / 45))
                    elif name == "momentum_breakout":
                        score = max(-1, min(1, chg * 22))
                    elif name == "flow_imbalance":
                        score = max(-1, min(1, vals["book_imbalance_5m"] * 2.2))
                    elif name == "news_shock":
                        score = max(-1, min(1, vals["news_sentiment_24h"] * 1.8))
                    else:
                        score = max(-1, min(1, random.gauss(0, 0.3)))
                    sv_rows.append((sig_ids[(eng, name)], iid, t, round(score, 4),
                                    round(random.uniform(0.4, 0.95), 3),
                                    json.dumps({"src": name}), t + timedelta(seconds=5)))

        cur.executemany(
            "INSERT INTO feat.feature_value (feature_def_id, instrument_id, event_time, value,"
            " ingested_at) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", fv_rows)
        cur.executemany(
            "INSERT INTO feat.regime (instrument_id, event_time, regime, confidence, ingested_at)"
            " VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", regime_rows)
        cur.executemany(
            "INSERT INTO sig.signal_value (signal_def_id, instrument_id, event_time, score,"
            " confidence, inputs, ingested_at) VALUES (%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT DO NOTHING", sv_rows)
        print(f"{len(fv_rows)} cech, {len(sv_rows)} sygnalow")

        # ---------------------------------------------------------- newsy
        print("newsy...", end=" ", flush=True)
        cur.execute("SELECT id, name, engine FROM news.feed ORDER BY id")
        feeds = cur.fetchall()
        art_rows, an_rows, ai_rows = [], [], []
        art_id = 0
        for day in range(DAYS):
            for _ in range(random.randint(4, 11)):
                art_id += 1
                fid, fname, feng = random.choice(feeds)
                pool = by_engine.get(feng) or [x for v in by_engine.values() for x in v]
                iid, sym = random.choice(pool)
                direction = random.choices(["bullish", "bearish", "neutral"], [0.38, 0.32, 0.30])[0]
                title = random.choice(HEADLINES[direction]).format(sym=sym.replace("USDT", ""))
                published = NOW - timedelta(days=DAYS - day, minutes=random.randint(0, 1439))
                # KLUCZOWE: opoznienie miedzy publikacja a poznaniem przez system.
                # Decyzja podjeta przed `ingested` nie moze tego newsa widziec.
                ingested = published + timedelta(seconds=random.randint(45, 700))
                art_rows.append((fid, f"https://example.invalid/a/{art_id}", title,
                                 h(title, art_id), published, ingested))
                an_rows.append((art_id, direction, round(random.uniform(0.1, 0.95), 3),
                                random.choice(["minutes", "hours", "days", "weeks"]),
                                round(random.uniform(0.35, 0.95), 3),
                                round(random.uniform(0.05, 0.95), 3),
                                published, ingested + timedelta(seconds=random.randint(2, 25))))
                ai_rows.append((art_id, iid, round(random.uniform(0.4, 1.0), 3)))

        cur.executemany(
            "INSERT INTO news.article (feed_id, url, title, content_hash, event_time, ingested_at)"
            " VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", art_rows)
        cur.executemany(
            "INSERT INTO news.article_analysis (article_id, model, prompt_version, direction,"
            " magnitude, horizon, confidence, novelty, reasoning, raw, input_tokens, output_tokens,"
            " cost_usd, event_time, ingested_at) VALUES (%s,'claude-opus-5','v1',%s,%s,%s,%s,%s,"
            "'Analiza wygenerowana dla danych demonstracyjnych.','{}'::jsonb,"
            "1800,220,0.014,%s,%s) ON CONFLICT DO NOTHING", an_rows)
        cur.executemany(
            "INSERT INTO news.article_instrument (article_id, instrument_id, relevance)"
            " VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", ai_rows)
        cur.execute("""
            UPDATE news.feed f SET
                last_polled_at = now() - (random() * interval '5 minutes'),
                last_status    = CASE WHEN random() < 0.9 THEN 'ok' ELSE 'timeout' END,
                articles_seen  = sub.seen * 14,
                articles_kept  = sub.seen
            FROM (SELECT feed_id, count(*) AS seen FROM news.article GROUP BY feed_id) sub
            WHERE f.id = sub.feed_id
        """)
        print(f"{len(art_rows)}")

        # ---------------------------------------------------------- modele i walk-forward
        print("uczenie...", end=" ", flush=True)
        model_version_by_engine = {}
        for eng in by_engine:
            cur.execute(
                "INSERT INTO learn.model (name, engine, kind, description) VALUES (%s,%s,%s,%s)"
                " ON CONFLICT (name) DO UPDATE SET kind = EXCLUDED.kind RETURNING id",
                (f"{eng}_signal_weighting", eng, "signal_weighting",
                 "Kalibracja wag sygnalow per rezim rynkowy"))
            model_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO learn.model_version (model_id, version, status, params,"
                " feature_def_ids, signal_def_ids, trained_from, trained_to, promoted_at, git_sha)"
                " VALUES (%s,3,'active',%s,%s,%s,%s,%s,now(),'demo-seed') RETURNING id",
                (model_id, json.dumps({"lookback_days": 30, "l2": 0.01, "per_regime": True}),
                 list(feat_ids.values()),
                 [v for (e, _n), v in sig_ids.items() if e == eng],
                 NOW - timedelta(days=DAYS), NOW - timedelta(days=3)))
            mv_id = cur.fetchone()[0]
            model_version_by_engine[eng] = mv_id

            cur.execute(
                "INSERT INTO learn.training_run (model_version_id, started_at, finished_at,"
                " status, train_from, train_to, n_windows) VALUES (%s,%s,%s,'done',%s,%s,%s)"
                " RETURNING id",
                (mv_id, NOW - timedelta(days=3), NOW - timedelta(days=3, hours=-2),
                 NOW - timedelta(days=DAYS), NOW - timedelta(days=3), 18))
            run_id = cur.fetchone()[0]

            # Okna walk-forward: rolling, bez nakladania train/test.
            positive = 0
            for idx in range(18):
                train_from = NOW - timedelta(days=DAYS - idx * 2)
                train_to = train_from + timedelta(days=14)
                test_from = train_to
                test_to = test_from + timedelta(days=2)
                sharpe = random.gauss(0.35 if eng == "crypto" else 0.22, 0.85)
                if sharpe > 0:
                    positive += 1
                regime = random.choice(REGIMES)
                cur.execute(
                    "INSERT INTO learn.wf_window (training_run_id, idx, train_from, train_to,"
                    " test_from, test_to, n_trades, dominant_regime)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (run_id, idx, train_from, train_to, test_from, test_to,
                     random.randint(4, 28), regime))
                w_id = cur.fetchone()[0]
                metrics = {
                    "sharpe": round(sharpe, 3),
                    "sortino": round(sharpe * random.uniform(1.0, 1.4), 3),
                    "max_dd": round(-abs(random.gauss(3.5, 1.8)), 3),
                    "hit_rate": round(min(0.75, max(0.30, 0.5 + sharpe * 0.06)), 3),
                    "avg_r": round(sharpe * 0.28, 3),
                    "profit_factor": round(max(0.4, 1 + sharpe * 0.3), 3),
                }
                cur.executemany(
                    "INSERT INTO learn.wf_metric (wf_window_id, metric, value) VALUES (%s,%s,%s)",
                    [(w_id, k, v) for k, v in metrics.items()])

            # Kalibracja: czy pewnosc 70% oznacza trafnosc ~70%.
            ece = 0.0
            for lo in [i / 10 for i in range(3, 10)]:
                pred = lo + 0.05
                obs = max(0.0, min(1.0, pred + random.gauss(-0.03, 0.07)))
                ece += abs(pred - obs) / 7
                cur.execute(
                    "INSERT INTO learn.calibration_bin (model_version_id, evaluated_at, bin_lo,"
                    " bin_hi, predicted_mean, observed_rate, n_samples) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (mv_id, NOW - timedelta(hours=6), lo, lo + 0.1, round(pred, 3),
                     round(obs, 3), random.randint(30, 260)))

            max_psi = 0.0
            for name, fid_ in feat_ids.items():
                psi = abs(random.gauss(0.09, 0.08))
                max_psi = max(max_psi, psi)
                cur.execute(
                    "INSERT INTO learn.drift_metric (model_version_id, feature_def_id, event_time,"
                    " psi, ks_stat) VALUES (%s,%s,%s,%s,%s)",
                    (mv_id, fid_, NOW - timedelta(hours=2), round(psi, 4),
                     round(abs(random.gauss(0.08, 0.05)), 4)))

            # Licznik przetestowanych konfiguracji - bez niego deflated Sharpe
            # jest nieobliczalny i selekcja nas oszuka.
            trials = random.randint(60, 190)
            for tnum in range(min(trials, 40)):
                params = {"l2": round(random.uniform(0.001, 0.2), 4),
                          "lookback_days": random.choice([14, 21, 30, 45])}
                cur.execute(
                    "INSERT INTO learn.hypothesis_trial (model_id, config_hash, params,"
                    " oos_sharpe, n_windows) VALUES (%s,%s,%s,%s,18)"
                    " ON CONFLICT DO NOTHING",
                    (model_id, h(eng, tnum, json.dumps(params)), json.dumps(params),
                     round(random.gauss(0.1, 0.7), 3)))

            cur.execute("""
                SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY mt.value)
                FROM learn.wf_metric mt JOIN learn.wf_window w ON w.id = mt.wf_window_id
                WHERE w.training_run_id = %s AND mt.metric = 'sharpe'
            """, (run_id,))
            median_sharpe = cur.fetchone()[0]
            cur.execute("""
                SELECT min(mt.value) FROM learn.wf_metric mt
                JOIN learn.wf_window w ON w.id = mt.wf_window_id
                WHERE w.training_run_id = %s AND mt.metric = 'max_dd'
            """, (run_id,))
            worst_dd = cur.fetchone()[0]

            pct = positive / 18 * 100
            status = ("validated" if pct >= 70 and median_sharpe and median_sharpe > 0.3
                      else "degraded" if pct < 40 else "learning")
            reason = {
                "validated": "Kryteria wyjscia z demo spelnione w oknie obserwacji.",
                "learning": f"Dodatnie {positive}/18 okien OOS. Prog przejscia na live: 70%.",
                "degraded": "Odsetek dodatnich okien ponizej 40% - strategia nie wykazuje alfy.",
            }[status]
            # UPSERT, nie UPDATE: TRUNCATE ... CASCADE na learn.model_version czysci
            # rowniez learning_state (FK), wiec wiersz moze nie istniec.
            cur.execute("""
                INSERT INTO learn.learning_state (engine, status, model_version_id, windows_total,
                    windows_positive, median_oos_sharpe, max_drawdown, calibration_error,
                    max_psi, trials_run, reason, updated_at)
                VALUES (%s,%s,%s,18,%s,%s,%s,%s,%s,%s,%s,now())
                ON CONFLICT (engine) DO UPDATE SET
                    status=EXCLUDED.status, model_version_id=EXCLUDED.model_version_id,
                    windows_total=EXCLUDED.windows_total,
                    windows_positive=EXCLUDED.windows_positive,
                    median_oos_sharpe=EXCLUDED.median_oos_sharpe,
                    max_drawdown=EXCLUDED.max_drawdown,
                    calibration_error=EXCLUDED.calibration_error,
                    max_psi=EXCLUDED.max_psi, trials_run=EXCLUDED.trials_run,
                    reason=EXCLUDED.reason, updated_at=now()
            """, (eng, status, mv_id, positive,
                  round(median_sharpe, 3) if median_sharpe else None,
                  round(worst_dd, 3) if worst_dd else None, round(ece, 4), round(max_psi, 4),
                  trials, reason))

            cur.execute("""
                INSERT INTO learn.postmortem (engine, period_from, period_to, model, summary, findings)
                VALUES (%s,%s,%s,'claude-opus-5',%s,%s)
            """, (eng, NOW - timedelta(days=7), NOW,
                  "Tydzien zdominowany przez rezim range. Sygnal mean_reversion_rsi odpowiada za "
                  "wiekszosc dodatniego wyniku; momentum_breakout generowal falszywe wejscia przy "
                  "niskiej zmiennosci i obniza laczny rezultat.",
                  json.dumps([
                      {"finding": "momentum_breakout ma ujemne oczekiwanie w rezimie range",
                       "action": "Obnizyc wage sygnalu w tym rezimie"},
                      {"finding": "Wejscia w ciagu 15 min po newsie o niskiej nowosci sa nietrafione",
                       "action": "Podniesc prog novelty dla sygnalu news_shock"},
                  ])))
        print("ok")

        # ---------------------------------------------------------- decyzje i egzekucja
        print("decyzje/egzekucja...", end=" ", flush=True)
        cur.execute("SELECT id, name FROM sig.signal_def")
        sig_name_by_id = {r[0]: r[1] for r in cur.fetchall()}
        feat_name_by_id = {v: k for k, v in feat_ids.items()}

        n_dec = 0
        for eng, insts in by_engine.items():
            mv_id = model_version_by_engine[eng]
            equity = 100000.0
            realized_cum = 0.0
            eq_rows = []
            for day in range(DAYS):
                day_start = NOW - timedelta(days=DAYS - day)
                # Krzywa kapitalu co 6h.
                for hh in range(0, 24, 6):
                    t = day_start + timedelta(hours=hh)
                    equity *= math.exp(random.gauss(0.00035, 0.0042))
                    exposure = abs(random.gauss(6000, 2500))
                    eq_rows.append((eng, "demo", t, round(equity, 4),
                                    round(equity - exposure * 0.15, 4),
                                    round(random.gauss(0, 300), 4),
                                    round(realized_cum, 4), round(exposure, 4)))

                for _ in range(random.randint(0, 3)):
                    n_dec += 1
                    iid, sym = random.choice(insts)
                    decided_at = day_start + timedelta(minutes=random.randint(0, 1439))
                    action = random.choices(
                        ["enter_long", "enter_short", "exit", "hold"], [0.30, 0.20, 0.25, 0.25])[0]
                    conf = round(random.uniform(0.42, 0.94), 3)
                    regime = random.choice(REGIMES)
                    verdict = random.choices(["allow", "downsize", "block"], [0.78, 0.14, 0.08])[0]
                    risk_reason = {
                        "allow": None,
                        "downsize": "Zlecenie przekracza max_position_notional - wielkosc obcieta",
                        "block": "Wykorzystanie max_total_exposure powyzej progu",
                    }[verdict]
                    px = closes[iid][random.randint(0, len(closes[iid]) - 1)]
                    qty = round(random.uniform(0.02, 0.6), 6)

                    snapshot = {feat_name_by_id[fid_]: round(random.gauss(0, 1), 4)
                                for fid_ in feat_ids.values()}
                    rationale = (
                        f"Rezim {regime}. Sygnal dominujacy wskazuje kierunek zgodny z akcja "
                        f"{action}; potwierdzenie z przeplywu zlecen. Pewnosc {conf:.0%}."
                        + (f" Ryzyko: {risk_reason}." if risk_reason else "")
                    )
                    cur.execute("""
                        INSERT INTO journal.decision (engine, mode, instrument_id, decided_at, action,
                            target_qty, target_price, confidence, regime, model_version_id,
                            feature_snapshot, risk_verdict, risk_reason, rationale,
                            rationale_model, latency_ms)
                        VALUES (%s,'demo',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'claude-opus-5',%s)
                        ON CONFLICT DO NOTHING RETURNING id
                    """, (eng, iid, decided_at, action, qty, round(px, 6), conf, regime, mv_id,
                          json.dumps(snapshot), verdict, risk_reason, rationale,
                          random.randint(45, 900)))
                    row = cur.fetchone()
                    if not row:
                        continue
                    dec_id = row[0]

                    # Rozbicie na wklady - to odpowiada na "dlaczego" w UI.
                    contribs = []
                    for sid, sname in sig_name_by_id.items():
                        if random.random() < 0.6:
                            val = round(random.uniform(-1, 1), 3)
                            w = round(random.uniform(0.05, 0.35), 3)
                            contribs.append((dec_id, "signal", sid, sname, val, w,
                                             round(val * w, 4)))
                    for fname_, fid_ in list(feat_ids.items())[:3]:
                        val = snapshot[fname_]
                        w = round(random.uniform(0.02, 0.2), 3)
                        contribs.append((dec_id, "feature", fid_, fname_, val, w,
                                         round(val * w, 4)))
                    cur.execute(
                        "SELECT a.id, a.title FROM news.article a"
                        " JOIN news.article_instrument ai ON ai.article_id = a.id"
                        " WHERE ai.instrument_id = %s AND a.ingested_at <= %s"
                        " ORDER BY a.event_time DESC LIMIT 2", (iid, decided_at))
                    for a_id, a_title in cur.fetchall():
                        w = round(random.uniform(0.03, 0.22), 3)
                        contribs.append((dec_id, "news", a_id, a_title[:80], 1.0, w, w))
                    cur.executemany(
                        "INSERT INTO journal.decision_contribution (decision_id, kind, ref_id,"
                        " label, value, weight, contribution) VALUES (%s,%s,%s,%s,%s,%s,%s)"
                        " ON CONFLICT DO NOTHING", contribs)

                    if verdict == "block":
                        cur.execute("""
                            INSERT INTO risk.violation (limit_def_id, engine, decision_id,
                                occurred_at, observed, threshold, action_taken, detail)
                            SELECT id, %s, %s, %s, %s, value, 'block', '{}'::jsonb
                            FROM risk.limit_def
                            WHERE engine=%s AND mode='demo' AND name='max_total_exposure' LIMIT 1
                        """, (eng, dec_id, decided_at, round(random.uniform(15001, 19000), 2), eng))
                        continue

                    if action in ("enter_long", "enter_short") and random.random() < 0.75:
                        side = "buy" if action == "enter_long" else "sell"
                        cur.execute("""
                            INSERT INTO exec.order (decision_id, engine, mode, instrument_id,
                                client_order_id, side, order_type, qty, limit_price, status,
                                submitted_at, venue_order_id)
                            VALUES (%s,%s,'demo',%s,%s,%s,'market',%s,NULL,'filled',%s,%s)
                            RETURNING id
                        """, (dec_id, eng, iid, f"tai-{dec_id}", side, qty, decided_at,
                              f"v{dec_id}{random.randint(100,999)}"))
                        oid = cur.fetchone()[0]
                        fill_px = px * (1 + random.gauss(0, 0.0004))
                        fee = qty * fill_px * 0.00055
                        cur.execute(
                            "INSERT INTO exec.fill (order_id, venue_fill_id, qty, price, fee,"
                            " fee_ccy, event_time) VALUES (%s,%s,%s,%s,%s,'USDT',%s)",
                            (oid, f"f{oid}", qty, round(fill_px, 6), round(fee, 6), decided_at))

                        closed = random.random() < 0.8
                        pnl = round(random.gauss(35, 210), 4) if closed else 0.0
                        realized_cum += pnl if closed else 0.0
                        cur.execute("""
                            INSERT INTO exec.position (engine, mode, instrument_id, side, qty,
                                avg_price, opened_at, closed_at, realized_pnl, fees_paid,
                                open_decision_id)
                            VALUES (%s,'demo',%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """, (eng, iid, "long" if side == "buy" else "short", qty,
                              round(fill_px, 6), decided_at,
                              decided_at + timedelta(hours=random.randint(2, 40)) if closed else None,
                              pnl, round(fee, 6), dec_id))

                    if action in ("enter_long", "enter_short") and random.random() < 0.18:
                        cur.execute("""
                            INSERT INTO journal.planned_order (decision_id, instrument_id, side, qty,
                                order_type, limit_price, trigger, valid_until, created_at)
                            VALUES (%s,%s,%s,%s,'limit',%s,%s,%s,%s)
                        """, (dec_id, iid, "buy" if action == "enter_long" else "sell", qty,
                              round(px * 0.985, 6),
                              json.dumps({"warunek": "cena <= EMA20 - 1.5 * ATR",
                                          "poziom": round(px * 0.985, 2),
                                          "potwierdzenie": "wolumen > 1.4x sredniej 1h"}),
                              NOW + timedelta(hours=random.randint(4, 48)), decided_at))

            cur.executemany(
                "INSERT INTO exec.equity_point (engine, mode, event_time, equity, cash,"
                " unrealized, realized_cum, exposure) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT DO NOTHING", eq_rows)

            cur.execute("""
                INSERT INTO risk.engine_heartbeat (engine, mode, last_beat_at, status, detail)
                VALUES (%s,'demo',now(),'running',%s)
                ON CONFLICT (engine) DO UPDATE SET last_beat_at=now(), status='running',
                    detail=EXCLUDED.detail
            """, (eng, json.dumps({"loop": "ok", "source": "seed_demo"})))

        # Wykorzystanie limitow - zasila paski w zakladce RYZYKO.
        cur.execute("""
            INSERT INTO risk.limit_usage (limit_def_id, event_time, used, threshold, utilization)
            SELECT id, now(), value * u, value, u
            FROM risk.limit_def, LATERAL (SELECT random() * 0.85 AS u) s
            ON CONFLICT DO NOTHING
        """)
        print(f"{n_dec} decyzji")

        conn.commit()
        print("\nGotowe. Dane demonstracyjne zaladowane.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main("--reset" in sys.argv))
