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

from services.engine.horizons import DEFAULT_BAR, HORIZONS, Horizon, get as get_horizon
from services.engine.signals import DEFAULT_WEIGHTS, REGIMES, SIGNALS, classify_regime, evaluate

# Podloga odchylenia standardowego zwrotow (0,01% = jedna dziesiata prowizji).
MIN_SD = 1e-4

# Prog wejscia NIE jest stala. Jest percentylem rozkladu |wyniku| policzonego
# na oknie TRENINGOWYM - a wiec dobieranym pod zadana CZESTOTLIWOSC handlu,
# nigdy pod zwrot. To rozroznienie jest tu najwazniejsze: dobieranie progu az
# wynik wyjdzie dodatni to definicja przeuczenia, natomiast powiedzenie
# "chce handlowac w 12% najsilniejszych sytuacji" jest decyzja podjeta przed
# zobaczeniem jakiegokolwiek wyniku.
#
# Stala 0,25 dobrana byla pod rozklad wynikow ze swiecy 5-minutowej. Po zmianie
# normalizacji na bezwymiarowa rozklad sie przesunal i ta sama stala dawala
# 2 transakcje na okno testowe - za malo, by cokolwiek zmierzyc. Percentyl
# przenosi sie miedzy horyzontami sam.
ENTER_PCTL = 0.88
EXIT_RATIO = 0.45          # wyjscie przy spadku sily sygnalu do 45% progu wejscia
MIN_CONF = 0.25

# Minimum informatywnych okien, ponizej ktorego NIE WOLNO orzec "zwalidowane".
# Bez tego jedno dodatnie okno przy 40 pominietych dawalo 100% skutecznosci
# i status otwierajacy droge do handlu na zywo.
MIN_WINDOWS_FOR_VALIDATION = 20
FEE = 0.00055
SLIP = 0.0004

SEED = 20260827


# ------------------------------------------------------------------ dane
def load_panel(cur, instrument_id: int, h: Horizon,
               since: datetime, until: datetime) -> list[dict]:
    """Panel cech w ukladzie czas -> {rola: wartosc}, plus cena swiecy.

    Cechy filtrujemy po `params->>'bar'`, wiec panel horyzontu 4 h nigdy nie
    zobaczy cech policzonych na swiecy 5-minutowej. Klucze slownika to ROLE,
    dzieki czemu sygnaly nie musza znac horyzontu.

    STRAZNIK BITEMPORALNY - `fv.ingested_at <= fv.event_time + jedna swieca`:

    Warunek nie brzmi `ingested_at <= event_time`, bo cechy sa datowane wstecz
    z trzysekundowym opoznieniem obliczeniowym i taki warunek odrzucilby
    wszystko. Brzmi "najpozniej jedna swieca po zdarzeniu", bo tyle realnie
    trwa domkniecie swiecy i policzenie z niej cechy. Cecha, ktora trafila do
    bazy pozniej, w tamtym momencie NIE byla dostepna i nie wolno na niej
    trenowac. Wczesniejsza wersja tego zapytania nie filtrowala po
    `ingested_at` w ogole - polegala na tym, ze potok datuje poprawnie, zamiast
    to sprawdzac. Roznica ujawnia sie dokladnie wtedy, gdy potok przestanie
    datowac poprawnie, czyli wtedy, gdy jest za pozno.
    """
    if h.aggregate:
        price_cte = """
            SELECT time_bucket(%(bar)s::interval, event_time) AS t,
                   (array_agg(close ORDER BY event_time DESC))[1] AS close
            FROM market.candle
            WHERE instrument_id = %(iid)s AND timeframe = %(tf)s
              AND event_time BETWEEN %(since)s AND %(until)s
            GROUP BY 1
        """
    else:
        price_cte = """
            SELECT event_time AS t, close
            FROM market.candle
            WHERE instrument_id = %(iid)s AND timeframe = %(tf)s
              AND event_time BETWEEN %(since)s AND %(until)s
        """

    cur.execute(
        f"""
        WITH f AS (
            SELECT fv.event_time, fd.params->>'role' AS role, fv.value
            FROM feat.feature_value fv
            JOIN feat.feature_def fd ON fd.id = fv.feature_def_id
            WHERE fv.instrument_id = %(iid)s
              AND fv.event_time BETWEEN %(since)s AND %(until)s
              AND fd.params->>'bar' = %(bar_name)s
              AND fv.ingested_at <= fv.event_time + %(bar)s::interval
        ),
        p AS ({price_cte})
        SELECT f.event_time AS t, jsonb_object_agg(f.role, f.value) AS feats, p.close
        FROM f JOIN p ON p.t = f.event_time
        GROUP BY f.event_time, p.close ORDER BY f.event_time
        """,
        {"iid": instrument_id, "since": since, "until": until, "tf": h.source_timeframe,
         "bar": f"{h.bar_minutes} minutes", "bar_name": h.bar},
    )
    return [{"t": r["t"], "f": {k: float(v) for k, v in r["feats"].items()},
             "price": float(r["close"])} for r in cur.fetchall()]


def atr_threshold(panel: list[dict]) -> float | None:
    vals = [r["f"]["atr_pct"] for r in panel if "atr_pct" in r["f"]]
    if len(vals) < 50:
        return None
    vals.sort()
    return vals[int(len(vals) * 0.8)]


# ------------------------------------------------------------------ symulacja
def score_threshold(panel: list[dict], weights: dict[str, dict[str, float]],
                    atr_p80: float | None, h: Horizon) -> tuple[float, float]:
    """Progi wejscia i wyjscia z rozkladu |wyniku| na TYM panelu.

    Wolno to policzyc wylacznie na oknie treningowym. Policzone na testowym
    byloby zagladaniem w przyszlosc - prog znalby wtedy rozklad, ktorego
    w tamtym momencie nie dalo sie znac.
    """
    scores = []
    for row in panel:
        regime, _ = classify_regime(row["f"], atr_p80)
        sc, conf, _ = evaluate(row["f"], regime, weights.get(regime), h)
        if conf >= MIN_CONF:
            scores.append(abs(sc))
    if len(scores) < 50:
        return 0.25, 0.12
    scores.sort()
    enter = scores[min(len(scores) - 1, int(len(scores) * ENTER_PCTL))]
    enter = max(enter, 1e-4)
    return enter, enter * EXIT_RATIO


def simulate(panel: list[dict], weights: dict[str, dict[str, float]],
             atr_p80: float | None, h: Horizon,
             enter: float, exit_: float) -> dict:
    """Odtwarza strategie na panelu. Zwraca metryki i liste transakcji.

    Wejscie i wyjscie po cenie NASTEPNEJ swiecy, nie biezacej - decyzja podjeta
    na zamknieciu swiecy T nie moze zostac wykonana po cenie tej samej swiecy.
    To jeden z najczestszych i najbardziej optymistycznych bledow w backtestach.

    Kazda transakcja zapamietuje PEWNOSC Z CHWILI WEJSCIA. Bez niej nie da sie
    policzyc kalibracji: pytanie brzmi "czy sygnal, ktory twierdzil 70 %, trafial
    w 70 % przypadkow", a odpowiedzi nie da sie odtworzyc z samego wyniku.

    Krzywa kapitalu jest wyceniana co swiece (mark-to-market), nie tylko przy
    zamknieciu pozycji. Krzywa liczona wylacznie na zrealizowanym wyniku
    pokazuje obsuniecie mniejsze niz prawdziwe, bo pozycja przeceniona
    w miedzyczasie o kilkanascie procent nie zostawia w niej sladu, o ile
    zdazy wrocic przed zamknieciem.
    """
    position = None          # {"side","entry","conf","score"}
    trades: list[dict] = []
    equity = 1.0
    curve = [1.0]

    for i in range(len(panel) - 1):
        row = panel[i]
        nxt = panel[i + 1]["price"]
        regime, _ = classify_regime(row["f"], atr_p80)
        score, conf, _ = evaluate(row["f"], regime, weights.get(regime), h)

        if position is None:
            if abs(score) >= enter and conf >= MIN_CONF:
                side = 1 if score > 0 else -1
                position = {"side": side, "entry": nxt * (1 + SLIP * side),
                            "conf": conf, "score": score}
                equity *= (1 - FEE)
        else:
            same = (position["side"] > 0 and score > 0) or (position["side"] < 0 and score < 0)
            if not same or abs(score) < exit_:
                exit_px = nxt * (1 - SLIP * position["side"])
                ret = position["side"] * (exit_px / position["entry"] - 1)
                equity *= (1 + ret) * (1 - FEE)
                trades.append({"ret": ret, "conf": position["conf"],
                               "score": position["score"]})
                position = None

        # Wycena biezaca: kapital + niezrealizowany wynik otwartej pozycji.
        if position is None:
            curve.append(equity)
        else:
            unreal = position["side"] * (nxt / position["entry"] - 1)
            curve.append(equity * (1 + unreal))

    rets = [t["ret"] for t in trades]

    # Ponizej progu kazda statystyka jest szumem. Zwracamy jawne zero
    # i licznik transakcji, zeby okno dalo sie odroznic od takiego, ktore
    # naprawde wyszlo na zero.
    peak = curve[0]
    max_dd = 0.0
    for v in curve:
        peak = max(peak, v)
        max_dd = min(max_dd, (v - peak) / peak * 100)

    if len(trades) < h.min_trades:
        return {"sharpe": 0.0, "sortino": 0.0, "max_dd": round(max_dd, 4), "hit_rate": 0.0,
                "avg_r": 0.0, "profit_factor": 0.0, "n_trades": len(trades),
                "total_return": round((equity - 1) * 100, 4), "trades": trades,
                "insufficient": True}

    mean = statistics.fmean(rets)
    sd = statistics.pstdev(rets)
    downside = [r for r in rets if r < 0]
    dsd = statistics.pstdev(downside) if len(downside) > 1 else sd

    # Podloga na odchyleniu. Bez niej kilka niemal identycznych transakcji daje
    # sd bliskie zeru i Sharpe rzedu milionow - liczbe, ktora nic nie znaczy,
    # a wyglada jak wynik.
    sd = max(sd, MIN_SD)
    dsd = max(dsd, MIN_SD)

    # Sharpe NA TRANSAKCJE, bez anualizacji. Anualizowanie wyniku z krotkiego
    # okna mnozy szum i produkuje liczby nieporownywalne miedzy oknami.
    gains = sum(r for r in rets if r > 0)
    losses = -sum(r for r in rets if r < 0)
    return {
        "sharpe": round(mean / sd, 4), "sortino": round(mean / dsd, 4),
        "max_dd": round(max_dd, 4),
        "hit_rate": round(sum(1 for r in rets if r > 0) / len(rets), 4),
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
def train(conn, engine: str, days: int, trials_per_window: int, h: Horizon) -> None:
    cur = conn.cursor()
    rng = random.Random(SEED)
    now = datetime.now(timezone.utc)

    cur.execute("SELECT id, symbol FROM ref.instrument WHERE engine=%s AND active"
                " AND kind <> 'perp' ORDER BY symbol", (engine,))
    instruments = cur.fetchall()
    if not instruments:
        print("brak instrumentow")
        return

    cur.execute(
        "INSERT INTO learn.model (name, engine, kind, description) VALUES (%s,%s,%s,%s)"
        " ON CONFLICT (name) DO UPDATE SET kind = EXCLUDED.kind RETURNING id",
        (f"{engine}_signal_weighting_{h.bar}", engine, "signal_weighting",
         f"Kalibracja wag sygnałów per reżim rynkowy metodą walk-forward, świeca {h.bar}"))
    model_id = cur.fetchone()["id"]

    cur.execute("SELECT COALESCE(max(version), 0) + 1 AS v FROM learn.model_version"
                " WHERE model_id = %s", (model_id,))
    version = cur.fetchone()["v"]

    cur.execute("SELECT array_agg(id) AS ids FROM feat.feature_def"
                " WHERE active AND params->>'bar' = %s", (h.bar,))
    feature_ids = cur.fetchone()["ids"] or []
    cur.execute("SELECT array_agg(id) AS ids FROM sig.signal_def WHERE engine = %s", (engine,))
    signal_ids = cur.fetchone()["ids"] or []

    cur.execute(
        "INSERT INTO learn.model_version (model_id, version, status, params, feature_def_ids,"
        " signal_def_ids, trained_from, trained_to) VALUES (%s,%s,'training',%s,%s,%s,%s,%s)"
        " RETURNING id",
        (model_id, version,
         json.dumps({"bar": h.bar, "train_days": h.train_days, "test_days": h.test_days,
                     "step_days": h.step_days, "min_trades": h.min_trades,
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
        p = load_panel(cur, inst["id"], h, now - timedelta(days=days), now)
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
    gate_history: list[float] = []
    weight_history: list[dict] = []
    all_sharpes: list[float] = []
    worst_dd = 0.0
    predictions: list[tuple[float, bool]] = []
    start = now - timedelta(days=days)

    while start + timedelta(days=h.train_days + h.test_days) <= now:
        train_from = start
        train_to = start + timedelta(days=h.train_days)
        test_from = train_to
        test_to = test_from + timedelta(days=h.test_days)

        train_slices = {s: [r for r in p if train_from <= r["t"] < train_to]
                        for s, p in panels.items()}
        test_slices = {s: [r for r in p if test_from <= r["t"] < test_to]
                       for s, p in panels.items()}
        min_train_bars = max(100, h.train_days * 1440 // h.bar_minutes // 4)
        if min((len(v) for v in train_slices.values()), default=0) < min_train_bars:
            start += timedelta(days=h.step_days)
            continue

        thresholds = {s: atr_threshold(train_slices[s]) for s in panels}
        # Progi wyniku ustalane raz na okno, z zalozen poczatkowych i wylacznie
        # na danych treningowych. Te same progi obowiazuja wszystkie hipotezy
        # w tym oknie - inaczej kazda konfiguracja dostawalaby wlasny prog
        # dobrany pod siebie, co jest kolejnym stopniem swobody do przeuczenia.
        gates = {s: score_threshold(train_slices[s], DEFAULT_WEIGHTS, thresholds[s], h)
                 for s in panels}

        # --- dobor wag WYLACZNIE na oknie treningowym ------------------------
        best_w, best_score = DEFAULT_WEIGHTS, -1e9
        for _ in range(trials_per_window):
            cand = random_weights(rng)
            score = statistics.fmean([
                simulate(train_slices[s], cand, thresholds[s], h, *gates[s])["sharpe"]
                for s in panels
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
            simulate(train_slices[s], DEFAULT_WEIGHTS, thresholds[s], h, *gates[s])["sharpe"]
            for s in panels])
        if base_score > best_score:
            best_w, best_score = DEFAULT_WEIGHTS, base_score

        # --- ocena OUT-OF-SAMPLE --------------------------------------------
        oos = [simulate(test_slices[s], best_w, thresholds[s], h, *gates[s])
               for s in panels if test_slices[s]]
        if not oos:
            start += timedelta(days=h.step_days)
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
        # Okno bez minimum transakcji nie jest ani dodatnie, ani ujemne -
        # jest bez informacji. Wliczanie go jako "niedodatnie" zanizaloby
        # ocene, a jako "dodatnie" zawyzalo.
        informative = any(not m.get("insufficient") for m in oos)
        if informative:
            all_sharpes.append(agg["sharpe"])
            worst_dd = min(worst_dd, agg["max_dd"])
            if agg["sharpe"] > 0:
                positive += 1

        # Kalibracja: deklarowana PRZED wejsciem pewnosc kontra faktyczny wynik.
        # Wczesniejsza wersja budowala "prognoze" z wielkosci samego zwrotu
        # (abs(t) * 50 + 0.5), czyli z rzeczy znanej dopiero po fakcie - wykres
        # kalibracji mierzyl wtedy wlasny wynik i zawsze wygladal dobrze.
        for m in oos:
            for t in m["trades"]:
                predictions.append((t["conf"], t["ret"] > 0))

        step = max(1, len(next(iter(test_slices.values()), [])) // 50)
        regimes = [classify_regime(r["f"], thresholds[s])[0]
                   for s in panels for r in test_slices[s][::step]]
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

        gate_history.extend(g[0] for g in gates.values())
        weight_history.append(best_w)
        mark = "" if informative else "  (za mało transakcji — pominięte w ocenie)"
        print(f"  okno {idx:>2d}  {test_from:%m-%d}–{test_to:%m-%d}  "
              f"Sharpe/transakcję={agg['sharpe']:+.4f}  transakcje={n_trades}  "
              f"reżim={dominant}{mark}")
        idx += 1
        start += timedelta(days=h.step_days)

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

    # Prog dobrany do miary NA TRANSAKCJE, nie anualizowanej. 0,05 oznacza,
    # ze sredni zwrot z transakcji to 5% jej wlasnego odchylenia - skromnie,
    # ale mierzalnie powyzej zera po kosztach.
    #
    # MIN_WINDOWS_FOR_VALIDATION jest warunkiem koniecznym, nie ozdoba. Bez niego
    # przebieg, w ktorym 39 okien pominieto z braku transakcji, a jedno wyszlo
    # dodatnio, dawal 100% skutecznosci i status "kryteria spelnione" - czyli
    # zielone swiatlo do handlu prawdziwymi pieniedzmi na podstawie jednego okna.
    if total < MIN_WINDOWS_FOR_VALIDATION:
        status = "learning"
        reason = (f"Za mało informatywnych okien: {total} z wymaganych "
                  f"{MIN_WINDOWS_FOR_VALIDATION}. Strategia zawiera zbyt mało transakcji, "
                  f"żeby cokolwiek orzec — to nie jest wynik ujemny, to brak wyniku.")
    else:
        status = ("validated" if pct >= 70 and median and median > 0.05
                  else "degraded" if pct < 40 else "learning")
        reason = {
            "validated": f"Dodatnie {positive}/{total} okien out-of-sample ({pct:.0f}%). Kryteria spełnione.",
            "learning": f"Dodatnie {positive}/{total} okien out-of-sample ({pct:.0f}%). Próg przejścia na live: 70%.",
            "degraded": f"Dodatnie tylko {positive}/{total} okien ({pct:.0f}%) — strategia nie wykazuje przewagi.",
        }[status]

    # Progi obowiazujace produkcje - mediana z okien. Petla decyzyjna czyta je
    # z rejestru, zeby nie ustalac ich wlasna, rownolegla logika.
    prod_enter = round(statistics.median(gate_history), 6) if gate_history else 0.25

    # WAGI DLA PRODUKCJI.
    #
    # Do tej pory `best_w` bylo liczone w kazdym oknie, uzywane do oceny
    # out-of-sample i WYRZUCANE. Nigdzie nie trafialo, a petla decyzyjna nie
    # czytala zadnych wag - dzialala wiecznie na zalozeniach poczatkowych.
    # Warstwa uczenia mierzyla wiec, czy kalibracja POMOGLABY, nie mogac jej
    # nigdy zastosowac.
    #
    # Bierzemy MEDIANE wag po oknach, nie wagi z ostatniego okna. Pojedyncze
    # okno to jedno losowanie; mediana po kilkudziesieciu jest odporna na to,
    # ze akurat ostatnie wypadlo nietypowo.
    prod_weights = {}
    if weight_history:
        for regime in REGIMES:
            per_signal = {
                name: statistics.median([w[regime].get(name, 0.0) for w in weight_history])
                for name in SIGNALS
            }
            total_w = sum(per_signal.values()) or 1.0
            prod_weights[regime] = {k: round(v / total_w, 4) for k, v in per_signal.items()}

    cur.execute(
        "UPDATE learn.model_version SET params = params || %s::jsonb WHERE id = %s",
        (json.dumps({"enter_threshold": prod_enter,
                     "exit_threshold": round(prod_enter * EXIT_RATIO, 6),
                     "min_confidence": MIN_CONF,
                     "weights": prod_weights}), mv_id))

    # KOLEJNOSC MA ZNACZENIE: najpierw wycofujemy poprzednia wersje, dopiero
    # potem promujemy nowa. Odwrotna kolejnosc lamie unikalny indeks
    # "jedna aktywna wersja na model" i wywraca caly przebieg NA SAMYM KONCU -
    # a co gorsza tylko wtedy, gdy wynik byl DOBRY, bo zly konczy sie statusem
    # 'candidate' i konfliktu nie ma. Blad ujawnial sie wiec dokladnie w tym
    # jednym przypadku, na ktorym najbardziej zalezy.
    cur.execute("UPDATE learn.model_version SET status='retired', retired_at=now()"
                " WHERE model_id=%s AND id<>%s AND status='active'", (model_id, mv_id))
    cur.execute(
        "UPDATE learn.model_version SET status = (CASE WHEN %s = 'degraded' THEN 'candidate'"
        " ELSE 'active' END)::learn.mv_status, promoted_at = now() WHERE id = %s", (status, mv_id))
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
    ap.add_argument("--days", type=int, default=1400)
    ap.add_argument("--trials", type=int, default=40)
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--bar", default=DEFAULT_BAR, choices=sorted(HORIZONS))
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    with psycopg.connect(url, row_factory=dict_row) as conn:
        h = get_horizon(args.bar)
        print(f"Walk-forward: świeca {h.bar}, trening {h.train_days} dni / "
              f"test {h.test_days} dni, krok {h.step_days} dni\n")
        train(conn, args.engine, args.days, args.trials, h)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
