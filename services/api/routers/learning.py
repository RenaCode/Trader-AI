"""Zakladka STOPIEN NAUKI.

Kazdy endpoint tutaj zwraca metryki OUT-OF-SAMPLE. Nie ma tu miejsca, w ktore
mozna by wpisac "wynik backtestu" - to celowe ograniczenie, bo swietny backtest
jest domyslnie sygnalem przeuczenia, nie alfy.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from services.engine.horizons import DEFAULT_BAR
from ..db import fetch_all, fetch_one

router = APIRouter(prefix="/api/engines/{engine}/learning", tags=["learning"])


@router.get("/state")
async def state(engine: str):
    row = await fetch_one(
        """
        SELECT ls.status, ls.windows_total, ls.windows_positive, ls.median_oos_sharpe,
               ls.positive_ci_low, ls.positive_ci_high,
               ls.max_drawdown, ls.calibration_error, ls.max_psi, ls.trials_run,
               ls.reason, ls.updated_at,
               m.name AS model_name, mv.version AS model_version, mv.trained_from, mv.trained_to
        FROM learn.learning_state ls
        LEFT JOIN learn.model_version mv ON mv.id = ls.model_version_id
        LEFT JOIN learn.model         m  ON m.id  = mv.model_id
        -- Jawny filtr po horyzoncie: od migracji 0025 jeden silnik ma wiele
        -- stanow nauki, po jednym na trenowana swiece. Zakladka pokazuje ten,
        -- ktorym silnik faktycznie handluje - pozostale sa torami porownawczymi.
        WHERE ls.engine = %s AND ls.bar = %s
        """,
        (engine, DEFAULT_BAR),
    )
    if not row:
        return {"status": "warmup", "reason": "Brak stanu dla tego silnika"}

    total = row["windows_total"] or 0
    positive = row["windows_positive"] or 0
    return {
        "status": row["status"],
        "reason": row["reason"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "model": (f'{row["model_name"]} v{row["model_version"]}' if row["model_name"] else None),
        "trained_from": row["trained_from"].isoformat() if row["trained_from"] else None,
        "trained_to": row["trained_to"].isoformat() if row["trained_to"] else None,
        "windows_total": total,
        "windows_positive": positive,
        # To jest glowna liczba "jak dobrze sie uczy": odsetek okien walk-forward
        # z dodatnim wynikiem OUT-OF-SAMPLE. Prog wyjscia z demo: >= 70%.
        "windows_positive_pct": (positive / total * 100) if total else 0.0,
        # Przedzial ufnosci 95% podawany ZAWSZE razem z odsetkiem. Przy 40 oknach
        # ma szerokosc okolo 30 punktow procentowych, wiec sam odsetek zacheca
        # do czytania roznicy kilku punktow jako sygnalu - a to szum.
        "windows_positive_ci": (
            [row["positive_ci_low"] * 100, row["positive_ci_high"] * 100]
            if row["positive_ci_low"] is not None else None),
        "median_oos_sharpe": row["median_oos_sharpe"],
        "max_drawdown": row["max_drawdown"],
        "calibration_error": row["calibration_error"],
        "max_psi": row["max_psi"],
        # Licznik przetestowanych konfiguracji. Bez niego deflated Sharpe jest
        # nieobliczalny i sami siebie oszukamy selekcja.
        "trials_run": row["trials_run"],
    }


@router.get("/weights")
async def weights(engine: str, bar: str = Query("4h")):
    """Wagi sygnalow: wyliczone przez trening kontra zalozenia poczatkowe.

    To jest najbardziej konkretna odpowiedz na pytanie "czy algorytm sie uczy".
    Metryki mowia, czy wynik jest lepszy; ta tabela mowi, CO konkretnie model
    zmienil w swoim rozumowaniu - ktoremu sygnalowi w ktorym rezimie zaczal
    ufac bardziej, a ktoremu mniej.

    Gdy nie ma aktywnej wersji modelu, zwracamy same zalozenia poczatkowe
    i mowimy o tym wprost. Silnik pracuje wtedy wlasnie na nich i uczciwie
    jest to pokazac, zamiast wyswietlac pusta tabele.
    """
    from services.engine.signals import DEFAULT_WEIGHTS, SIGNALS

    row = await fetch_one(
        """
        SELECT mv.version, mv.params, mv.promoted_at, mv.trained_from, mv.trained_to
        FROM learn.model_version mv JOIN learn.model m ON m.id = mv.model_id
        WHERE m.name = %s AND mv.status = 'active'
        ORDER BY mv.version DESC LIMIT 1
        """,
        (f"{engine}_signal_weighting_{bar}",),
    )
    wyuczone = (row["params"] or {}).get("weights") if row else None

    etykiety = {n: SIGNALS[n][2] for n in SIGNALS}
    rodziny = {n: SIGNALS[n][1] for n in SIGNALS}
    rezimy = []
    for rezim, bazowe in DEFAULT_WEIGHTS.items():
        pozycje = []
        for nazwa in SIGNALS:
            baza = float(bazowe.get(nazwa, 0.0))
            teraz = float((wyuczone or {}).get(rezim, {}).get(nazwa, baza))
            pozycje.append({
                "sygnal": nazwa,
                "etykieta": etykiety[nazwa],
                "rodzina": rodziny[nazwa],
                "zalozenie": round(baza, 4),
                "wyuczona": round(teraz, 4),
                "zmiana": round(teraz - baza, 4),
            })
        pozycje.sort(key=lambda x: -x["wyuczona"])
        rezimy.append({"rezim": rezim, "sygnaly": pozycje})

    return {
        "swieca": bar,
        "wersja_modelu": row["version"] if row else None,
        "wyuczone": wyuczone is not None,
        "promowany": row["promoted_at"].isoformat() if row and row["promoted_at"] else None,
        "prog_wejscia": (row["params"] or {}).get("enter_threshold") if row else None,
        "trenowany_od": row["trained_from"].isoformat() if row and row["trained_from"] else None,
        "trenowany_do": row["trained_to"].isoformat() if row and row["trained_to"] else None,
        "rezimy": rezimy,
    }


@router.get("/walk-forward")
async def walk_forward(engine: str, limit: int = Query(40, le=200)):
    """Metryki per okno walk-forward. Jedyny dopuszczalny protokol oceny."""
    rows = await fetch_all(
        """
        SELECT w.idx, w.train_from, w.train_to, w.test_from, w.test_to,
               w.n_trades, w.dominant_regime,
               jsonb_object_agg(mt.metric, mt.value) AS metrics
        FROM learn.wf_window w
        JOIN learn.training_run tr ON tr.id = w.training_run_id
        JOIN learn.model_version mv ON mv.id = tr.model_version_id
        JOIN learn.model m ON m.id = mv.model_id
        LEFT JOIN learn.wf_metric mt ON mt.wf_window_id = w.id
        WHERE m.engine = %s
        GROUP BY w.id, w.idx, w.train_from, w.train_to, w.test_from, w.test_to,
                 w.n_trades, w.dominant_regime
        ORDER BY w.idx DESC
        LIMIT %s
        """,
        (engine, limit),
    )
    return [
        {
            "idx": r["idx"],
            "test_from": r["test_from"].isoformat(),
            "test_to": r["test_to"].isoformat(),
            "n_trades": r["n_trades"],
            "regime": r["dominant_regime"],
            "metrics": r["metrics"] or {},
        }
        for r in reversed(rows)
    ]


@router.get("/calibration")
async def calibration(engine: str):
    """Czy pewnosc 70% oznacza trafnosc ~70%. Rozjazd = model klamie o swojej pewnosci.

    Zwracamy tez PODSUMOWANIE, bo sam wykres latwo przeczytac zyczeniowo.
    Kluczowe pytanie nie brzmi "czy punkty leza blisko przekatnej", tylko
    "czy wyzsza deklarowana pewnosc oznacza wyzsza trafnosc". Miara przy nim to
    rozpietosc trafnosci miedzy kubelkami: gdy trafnosc jest plaska niezaleznie
    od pewnosci, liczba nie niesie informacji o jakosci sytuacji - i wtedy prog
    wejscia oparty na pewnosci filtruje przypadkowo.
    """
    rows = await fetch_all(
        """
        SELECT cb.bin_lo, cb.bin_hi, cb.predicted_mean, cb.observed_rate, cb.n_samples
        FROM learn.calibration_bin cb
        JOIN learn.model_version mv ON mv.id = cb.model_version_id
        JOIN learn.model m ON m.id = mv.model_id
        WHERE m.engine = %s
          AND cb.evaluated_at = (
              SELECT max(evaluated_at) FROM learn.calibration_bin
              WHERE model_version_id = cb.model_version_id)
        ORDER BY cb.bin_lo
        """,
        (engine,),
    )
    kubelki = [
        {"bin_lo": r["bin_lo"], "bin_hi": r["bin_hi"], "predicted": r["predicted_mean"],
         "observed": r["observed_rate"], "n": r["n_samples"]}
        for r in rows
    ]
    if not kubelki:
        return {"kubelki": [], "podsumowanie": None}

    n = sum(k["n"] for k in kubelki)
    trafnosc = sum(k["observed"] * k["n"] for k in kubelki) / n
    ece = sum(abs(k["observed"] - k["predicted"]) * k["n"] for k in kubelki) / n
    # Rozpietosc liczona tylko na kubelkach o sensownej liczebnosci - inaczej
    # jeden kubelek z czternastoma transakcjami wyznaczalby wniosek.
    istotne = [k for k in kubelki if k["n"] >= 50]
    rozpietosc = (max(k["observed"] for k in istotne) - min(k["observed"] for k in istotne)
                  if len(istotne) >= 2 else None)

    if rozpietosc is None:
        werdykt = "Za mało danych, żeby ocenić."
    elif rozpietosc < 0.10:
        werdykt = (f"Trafność jest praktycznie płaska ({trafnosc:.0%}) niezależnie od "
                   f"deklarowanej pewności. Ta liczba NIE porządkuje sytuacji według "
                   f"jakości — próg wejścia oparty na niej filtruje przypadkowo.")
    elif rozpietosc < 0.20:
        werdykt = (f"Pewność słabo różnicuje trafność (rozpiętość {rozpietosc:.0%} "
                   f"przy średniej {trafnosc:.0%}). Sygnał jest, ale wątły.")
    else:
        werdykt = (f"Pewność różnicuje trafność (rozpiętość {rozpietosc:.0%} przy "
                   f"średniej {trafnosc:.0%}).")

    return {
        "kubelki": kubelki,
        "podsumowanie": {
            "transakcji": n,
            "trafnosc": round(trafnosc, 4),
            "ece": round(ece, 4),
            "rozpietosc_trafnosci": round(rozpietosc, 4) if rozpietosc is not None else None,
            "kubelkow_istotnych": len(istotne),
            "werdykt": werdykt,
        },
    }


@router.get("/drift")
async def drift(engine: str, limit: int = Query(12, le=60)):
    """Dryf rozkladu cech (PSI). > 0.25 = rozklad realnie odjechal od treningowego."""
    rows = await fetch_all(
        """
        SELECT fd.name || '@' || fd.version AS feature, d.psi, d.ks_stat, d.event_time
        FROM learn.drift_metric d
        JOIN feat.feature_def fd ON fd.id = d.feature_def_id
        JOIN learn.model_version mv ON mv.id = d.model_version_id
        JOIN learn.model m ON m.id = mv.model_id
        WHERE m.engine = %s
          AND d.event_time = (SELECT max(event_time) FROM learn.drift_metric
                              WHERE model_version_id = d.model_version_id)
        ORDER BY d.psi DESC NULLS LAST
        LIMIT %s
        """,
        (engine, limit),
    )
    return [
        {"feature": r["feature"], "psi": r["psi"], "ks": r["ks_stat"],
         "at": r["event_time"].isoformat()}
        for r in rows
    ]


@router.get("/signal-performance")
async def signal_performance(engine: str):
    """Skutecznosc per sygnal - ktora hipoteza faktycznie cos wnosi."""
    rows = await fetch_all(
        """
        SELECT sd.name, sd.family,
               count(*)                                        AS used_count,
               avg(dc.contribution)                            AS avg_contribution,
               avg(CASE WHEN p.realized_pnl > 0 THEN 1.0 ELSE 0.0 END) AS hit_rate,
               sum(p.realized_pnl)                             AS total_pnl
        FROM journal.decision_contribution dc
        JOIN journal.decision d ON d.id = dc.decision_id
        JOIN sig.signal_def   sd ON sd.id = dc.ref_id AND dc.kind = 'signal'
        LEFT JOIN exec.position p ON p.open_decision_id = d.id AND p.closed_at IS NOT NULL
        WHERE d.engine = %s
        GROUP BY sd.name, sd.family
        ORDER BY count(*) DESC
        """,
        (engine,),
    )
    return [
        {
            "name": r["name"], "family": r["family"], "used_count": r["used_count"],
            "avg_contribution": float(r["avg_contribution"]) if r["avg_contribution"] is not None else None,
            "hit_rate": float(r["hit_rate"]) if r["hit_rate"] is not None else None,
            "total_pnl": float(r["total_pnl"]) if r["total_pnl"] is not None else 0.0,
        }
        for r in rows
    ]


@router.get("/postmortems")
async def postmortems(engine: str, limit: int = Query(5, le=50)):
    """Dziennik jakosciowy generowany przez Claude z zamknietych transakcji."""
    rows = await fetch_all(
        """
        SELECT id, period_from, period_to, model, summary, findings, created_at
        FROM learn.postmortem WHERE engine = %s
        ORDER BY created_at DESC LIMIT %s
        """,
        (engine, limit),
    )
    return [
        {
            "id": r["id"], "period_from": r["period_from"].isoformat(),
            "period_to": r["period_to"].isoformat(), "model": r["model"],
            "summary": r["summary"], "findings": r["findings"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]
