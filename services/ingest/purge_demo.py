#!/usr/bin/env python3
"""Usuwa WSZYSTKIE dane demonstracyjne, zostawiajac realne dane rynkowe.

Po tej operacji dashboard bedzie pusty do czasu, az silnik policzy pierwsze
cechy i podejmie pierwsze decyzje. To jest stan poprawny, nie awaria - pusty
panel mowiacy prawde jest lepszy niz zapelniony wygenerowanymi liczbami,
ktore niczego nie znacza.

ZOSTAJE:
    market.candle        - realne swiece z gieldy
    ref.*                - instrumenty, gieldy, limity ryzyka
    app.*                - konto, klucze, ustawienia
    news.feed            - konfiguracja kanalow (ale licznik zerowany)

ZNIKA:
    journal.*            - dziennik decyzji, wklady, zlecenia planowane
    exec.*               - zlecenia, wypelnienia, pozycje, krzywa kapitalu
    feat.*, sig.*        - cechy, rezimy, sygnaly (przeliczymy z realnych danych)
    learn.*              - modele, okna walk-forward, kalibracja, post-mortemy
    news.article*        - artykuly i ich analizy
    risk.violation, risk.limit_usage, risk.killswitch_event

UWAGA O UPRAWNIENIACH (od migracji 0013)

Skrypt uzywa TRUNCATE, ktorego rola aplikacji `trader_app` swiadomie NIE MA.
To nie jest przeoczenie - kasowanie hurtem jest operacja konserwacyjna i ma
wymagac roli wlasciciela. Uruchamiaj z DATABASE_URL wskazujacym na `trader`,
nie na `trader_app`; z rola aplikacji dostaniesz InsufficientPrivilege i to
jest zachowanie poprawne.
"""
from __future__ import annotations

import os
import sys

import psycopg

# journal.* jest append-only (trigger blokuje DELETE), wiec TRUNCATE.
TRUNCATE = """
TRUNCATE
    journal.decision, journal.decision_contribution, journal.planned_order,
    exec.order, exec.fill, exec.position, exec.equity_point,
    news.article, news.article_analysis, news.article_instrument, news.article_embedding,
    feat.feature_value, feat.feature_def, feat.regime,
    sig.signal_value, sig.signal_def,
    learn.model, learn.model_version, learn.training_run, learn.wf_window,
    learn.wf_metric, learn.calibration_bin, learn.drift_metric,
    learn.hypothesis_trial, learn.postmortem,
    risk.limit_usage, risk.violation, risk.killswitch_event
RESTART IDENTITY CASCADE
"""


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    force = "--yes" in sys.argv
    with psycopg.connect(url, autocommit=True) as conn:
        before = {}
        for t in ("market.candle", "journal.decision", "exec.position",
                  "feat.feature_value", "sig.signal_value", "news.article"):
            before[t] = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]

        print("Stan przed:")
        for t, n in before.items():
            print(f"  {t:24s} {n:>10,}")

        if not force:
            print("\nUruchom z --yes, zeby potwierdzic usuniecie.")
            return 1

        conn.execute(TRUNCATE)

        # learning_state i heartbeat sa czyszczone przez CASCADE (FK do model_version) -
        # odtwarzamy je w stanie poczatkowym, bo dashboard ich oczekuje.
        conn.execute("""
            INSERT INTO learn.learning_state (engine, status, reason)
            VALUES ('crypto','warmup','Brak danych treningowych — zbieranie w toku'),
                   ('equity','warmup','Brak danych treningowych — zbieranie w toku')
            ON CONFLICT (engine) DO UPDATE
              SET status='warmup', model_version_id=NULL, windows_total=0,
                  windows_positive=0, median_oos_sharpe=NULL, max_drawdown=NULL,
                  calibration_error=NULL, max_psi=NULL, trials_run=0,
                  reason=EXCLUDED.reason, updated_at=now()
        """)
        conn.execute("""
            INSERT INTO risk.engine_heartbeat (engine, mode, last_beat_at, status)
            VALUES ('crypto','demo', now(), 'halted'), ('equity','demo', now(), 'halted')
            ON CONFLICT (engine) DO UPDATE SET status='halted', last_beat_at=now()
        """)
        # Liczniki kanalow zerujemy - odnosily sie do wygenerowanych artykulow.
        conn.execute("UPDATE news.feed SET articles_seen=0, articles_kept=0,"
                     " last_polled_at=NULL, last_status=NULL, last_error=NULL")

        print("\nStan po:")
        for t in before:
            n = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            print(f"  {t:24s} {n:>10,}")

    print("\nDane demonstracyjne usuniete. Zostaly wylacznie realne swiece.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
