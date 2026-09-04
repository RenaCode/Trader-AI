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

from services.engine.horizons import DEFAULT_BAR, HORIZONS, Horizon, get as get_horizon
from services.engine.risk import RiskKernel
from services.engine.koszty import koszty
from services.engine.risk_rules import position_budget
from services.engine.signals import SIGNALS, classify_regime, evaluate

# Progi awaryjne - obowiazuja WYLACZNIE, dopoki nie ma aktywnej wersji modelu.
# Docelowo pochodza z rejestru (learn.model_version.params), gdzie zapisuje je
# trener, wyliczone z rozkladu wynikow na oknach treningowych. Trzymanie ich
# w dwoch miejscach bylo pewna droga do rozjazdu trening-produkcja.
ENTER_THRESHOLD = 0.25
EXIT_THRESHOLD = 0.12
MIN_CONFIDENCE = 0.25

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


def active_model(cur, engine: str, h: Horizon) -> dict:
    """Wagi i progi z AKTYWNEJ wersji modelu - albo zalozenia poczatkowe.

    Wersja o statusie 'candidate' (czyli taka, ktora nie przeszla walidacji)
    swiadomie NIE jest tu brana. Model, ktory nie wykazal przewagi out-of-sample,
    nie ma prawa sterowac produkcja - a jedyne, co go od niej dzieli, to ten
    warunek na status.
    """
    cur.execute(
        "SELECT mv.id, mv.version, mv.params FROM learn.model_version mv"
        " JOIN learn.model m ON m.id = mv.model_id"
        " WHERE m.name = %s AND mv.status = 'active'"
        " ORDER BY mv.version DESC LIMIT 1",
        (f"{engine}_signal_weighting_{h.bar}",))
    row = cur.fetchone()
    if not row or not (row["params"] or {}).get("weights"):
        return {"weights": None, "enter": ENTER_THRESHOLD, "exit": EXIT_THRESHOLD,
                "min_conf": MIN_CONFIDENCE, "version": None}
    p = row["params"]
    return {
        "weights": p["weights"],
        "enter": float(p.get("enter_threshold", ENTER_THRESHOLD)),
        "exit": float(p.get("exit_threshold", EXIT_THRESHOLD)),
        "min_conf": float(p.get("min_confidence", MIN_CONFIDENCE)),
        "version": row["version"],
        "model_version_id": row["id"],
    }


def horizon_feature_ids(cur, h: Horizon) -> list[int]:
    """Identyfikatory cech NALEZACYCH DO TEGO HORYZONTU.

    feat.as_of() z NULL zwrocilby komplet cech wszystkich horyzontow naraz -
    silnik 4-godzinny dostalby wtedy takze cechy 5-minutowe i podejmowal decyzje
    na mieszance, ktorej trener nigdy nie widzial.
    """
    cur.execute("SELECT array_agg(id) AS ids FROM feat.feature_def"
                " WHERE active AND params->>'bar' = %s", (h.bar,))
    return cur.fetchone()["ids"] or []


def latest_features(cur, instrument_id: int, as_of: datetime, h: Horizon,
                    feature_ids: list[int]) -> dict:
    """Odczyt point-in-time. Jedyna dozwolona sciezka - patrz komentarz modulu."""
    cur.execute(
        "SELECT feature_name, value FROM feat.as_of(%s, %s, %s, %s::interval)",
        (instrument_id, as_of, feature_ids or None, f"{h.max_staleness_min} minutes"))
    # feature_name ma postac 'rola__swieca@wersja'; do sygnalow idzie sama rola.
    return {Horizon.role_of(r["feature_name"]): r["value"] for r in cur.fetchall()}


def current_bar(as_of: datetime, h: Horizon) -> datetime:
    """Poczatek swiecy, ktora wlasnie sie domknela."""
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    span = h.bar_minutes * 60
    n = int((as_of - epoch).total_seconds() // span)
    return epoch + timedelta(seconds=(n - 1) * span)


def decided_for_bar(cur, engine: str, mode: str, instrument_id: int,
                    bar_start: datetime) -> bool:
    """Czy dla tej swiecy podjeto juz decyzje.

    Petla chodzi czesciej niz zamykaja sie swiece (co 5 minut przy swiecy
    4-godzinnej). Bez tego warunku silnik podjalby do 48 decyzji na jedna
    swiece, a symulacja, na ktorej byl kalibrowany, zaklada dokladnie jedna.
    Rozjazd trening-produkcja bralby sie wtedy z samego harmonogramu.
    """
    cur.execute(
        "SELECT 1 FROM journal.decision WHERE engine=%s AND mode=%s AND instrument_id=%s"
        " AND decided_at >= %s LIMIT 1",
        (engine, mode, instrument_id, bar_start))
    return cur.fetchone() is not None


def atr_percentile_80(cur, instrument_id: int, as_of: datetime,
                      h: Horizon) -> float | None:
    """Prog wysokiej zmiennosci liczony z HISTORII TEGO instrumentu.

    Stala wartosc nie ma sensu: 0,3% na 5 min to spokoj dla SOL i szok dla SPY.
    Okno historii skaluje sie z horyzontem - na swiecy 4-godzinnej 14 dni to
    zaledwie 84 obserwacje, czyli za malo na stabilny percentyl.
    """
    cur.execute(
        """
        SELECT percentile_cont(0.8) WITHIN GROUP (ORDER BY fv.value) AS p80
        FROM feat.feature_value fv JOIN feat.feature_def fd ON fd.id = fv.feature_def_id
        WHERE fd.name = %s AND fv.instrument_id = %s
          AND fv.event_time BETWEEN %s - %s::interval AND %s
        """,
        (h.name("atr_pct"), instrument_id, as_of,
         f"{h.train_days} days", as_of))
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
                    signal_ids: dict[str, int], latency_ms: int,
                    model_version_id: int | None = None) -> int | None:
    # model_version_id mowi, KTORA wersja modelu podjela te decyzje. Kolumna
    # istniala z kluczem obcym od migracji 0006, ale nikt jej nie wypelnial -
    # bo do tej pory petla nie uzywala zadnego modelu, wiec nie bylo czego
    # zapisac. NULL znaczy "zalozenia poczatkowe, brak zwalidowanego modelu"
    # i jest to stan poprawny, nie brak danych.
    cur.execute(
        """
        INSERT INTO journal.decision (engine, mode, instrument_id, decided_at, action,
            target_qty, target_price, confidence, regime, feature_snapshot,
            risk_verdict, risk_reason, rationale, rationale_model, latency_ms,
            model_version_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'reguły',%s,%s)
        ON CONFLICT (engine, instrument_id, decided_at) DO NOTHING
        RETURNING id
        """,
        (engine, mode, instrument_id, as_of, action, qty, price, confidence, regime,
         json.dumps({k: round(v, 6) for k, v in features.items()}),
         verdict.verdict, verdict.reason, rationale, latency_ms, model_version_id))
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


async def sprawdz(conn, engine: str = "crypto", h: Horizon | None = None) -> int:
    """Przebieg KONTROLNY: wykonuje wszystkie odczyty, nie zapisuje niczego.

    DLACZEGO ISTNIEJE

    Test dymny sprawdza warstwe webowa. Petla decyzyjna nie miala zadnego testu
    i blad w nazwie kolumny (`event_time` zamiast `decided_at`) wywracal ja przy
    kazdym przebiegu przez kilkanascie minut - jedynym sladem byl wpis w logu
    poda, ktorego nikt nie czytal.

    Ten tryb wykonuje dokladnie te same zapytania co produkcja, w tej samej
    kolejnosci, i konczy sie kodem niezerowym, gdy ktorekolwiek zawiedzie. Nie
    zastepuje testu jednostkowego - wylapuje inna klase bledow: rozjazd miedzy
    kodem a schematem bazy, ktorego zadne przenosne testy nie widza.

    Uzycie:  DATABASE_URL=... python services/engine/loop.py --sprawdz
    """
    h = h or get_horizon(DEFAULT_BAR)
    cur = conn.cursor()
    now = datetime.now(timezone.utc)
    bar_start = current_bar(now, h)
    bledy: list[str] = []

    def probuj(nazwa, fn):
        try:
            wynik = fn()
            print(f"  [ok  ] {nazwa}")
            return wynik
        except Exception as e:                                # noqa: BLE001
            print(f"  [BŁĄD] {nazwa}: {type(e).__name__}: {e}")
            bledy.append(nazwa)
            conn.rollback()
            return None

    print(f"Kontrola pętli decyzyjnej — silnik {engine}, świeca {h.bar}\n")
    risk = RiskKernel(conn, engine, "demo")
    probuj("limity ryzyka konta", risk.check_account)
    ids = probuj("identyfikatory cech horyzontu", lambda: horizon_feature_ids(cur, h))
    model = probuj("aktywna wersja modelu", lambda: active_model(cur, engine, h))

    cur.execute("SELECT id, symbol FROM ref.instrument WHERE engine=%s AND active"
                " AND tradeable AND kind <> 'perp' ORDER BY symbol LIMIT 3", (engine,))
    for inst in cur.fetchall():
        iid, sym = inst["id"], inst["symbol"]
        probuj(f"{sym}: decyzja w tej świecy?",
               lambda i=iid: decided_for_bar(cur, engine, "demo", i, bar_start))
        f = probuj(f"{sym}: odczyt cech point-in-time",
                   lambda i=iid: latest_features(cur, i, now, h, ids or []))
        probuj(f"{sym}: cena odniesienia", lambda i=iid: mark_price(cur, i, now))
        probuj(f"{sym}: próg zmienności", lambda i=iid: atr_percentile_80(cur, i, now, h))
        probuj(f"{sym}: otwarta pozycja", lambda i=iid: open_position(cur, engine, "demo", i))
        if f:
            regime, _ = classify_regime(f, None)
            probuj(f"{sym}: ocena sygnałów ({len(f)} cech, reżim {regime})",
                   lambda ff=f, r=regime: evaluate(
                       ff, r, (model or {}).get("weights", {}).get(r), h))

    # KONTROLA STRUKTURALNA: czy wszystkie cechy horyzontu siedza na siatce swiec.
    # Cechy liczone na wlasnej siatce (funding, news) potrafily wyladowac na
    # znacznikach typu 16:19:49 i wtedy NIGDY nie wchodzily do panelu trenera,
    # dzialajac jednoczesnie w produkcji przez feat.as_of(). Rozjazd
    # trening-produkcja nie do wykrycia po samych wynikach.
    def kontrola_siatki():
        krok = h.bar_minutes * 60
        cur.execute(
            """
            SELECT fd.name,
                   count(*) AS n,
                   sum(CASE WHEN extract(epoch FROM fv.event_time)::bigint %% %s = 0
                            THEN 1 ELSE 0 END) AS na_siatce
            FROM feat.feature_value fv
            JOIN feat.feature_def fd ON fd.id = fv.feature_def_id
            WHERE fd.params->>'bar' = %s
              AND fv.event_time > now() - interval '7 days'
            GROUP BY 1
            """,
            (krok, h.bar),
        )
        krzywe = [(r["name"], r["n"], r["na_siatce"]) for r in cur.fetchall()
                  if r["n"] and r["na_siatce"] < r["n"]]
        if krzywe:
            raise RuntimeError(
                "cechy poza siatką świec: " +
                ", ".join(f"{n} ({na}/{tot})" for n, tot, na in krzywe))
        return True

    probuj("cechy leżą na siatce świec", kontrola_siatki)

    # KONTROLA STRUKTURALNA: czy nie liczymy cech z jeszcze OTWARTEJ swiecy.
    # time_bucket() zwraca biezaca swiece z niepelnymi danymi, a przez
    # ON CONFLICT DO NOTHING taka czastkowa wartosc zostaje w bazie na zawsze -
    # pozniejszy przebieg nie nadpisze istniejacego wiersza.
    def kontrola_otwartej_swiecy():
        biezaca = current_bar(datetime.now(timezone.utc), h) + timedelta(
            minutes=h.bar_minutes)
        cur.execute(
            """
            SELECT count(*) AS n FROM feat.feature_value fv
            JOIN feat.feature_def fd ON fd.id = fv.feature_def_id
            WHERE fd.params->>'bar' = %s AND fv.event_time >= %s
            """,
            (h.bar, biezaca),
        )
        n = cur.fetchone()["n"]
        if n:
            raise RuntimeError(
                f"{n} cech policzonych dla świecy {biezaca:%H:%M}, która jeszcze trwa")
        return True

    probuj("brak cech z niezamkniętej świecy", kontrola_otwartej_swiecy)

    # KONTROLA STRUKTURALNA: czy datowanie nie twierdzi wczesniejszej wiedzy,
    # niz bylo mozliwe. Cecha swiecy 04:00-08:00 nie mogla byc znana o 04:00.
    def kontrola_datowania():
        cur.execute(
            """
            SELECT count(*) AS n FROM feat.feature_value fv
            JOIN feat.feature_def fd ON fd.id = fv.feature_def_id
            WHERE fd.params->>'bar' = %s
              AND fv.event_time > now() - interval '7 days'
              AND fv.ingested_at < fv.event_time + %s::interval
            """,
            (h.bar, f"{h.bar_minutes} minutes"),
        )
        n = cur.fetchone()["n"]
        if n:
            raise RuntimeError(
                f"{n} cech datowanych PRZED zamknięciem własnej świecy")
        return True

    probuj("datowanie nie wyprzedza zamknięcia świecy", kontrola_datowania)

    conn.rollback()
    print()
    if bledy:
        print(f"NIEPOWODZENIE: {len(bledy)} zapytań zawiodło")
        for b in bledy:
            print(f"  - {b}")
        return 1
    print("Wszystkie zapytania pętli decyzyjnej wykonały się poprawnie.")
    return 0


async def run_once(conn, engine: str = "crypto", mode: str = "demo",
                   verbose: bool = True, h: Horizon | None = None) -> dict:
    h = h or get_horizon(DEFAULT_BAR)
    cur = conn.cursor()
    signal_ids = ensure_signal_defs(cur, engine)
    seed_equity(cur, engine, mode)
    conn.commit()

    risk = RiskKernel(conn, engine, mode)

    # Limity konta sprawdzamy RAZ NA CYKL, zanim cokolwiek rozwazymy.
    # Wczesniej risk.check() wolane bylo wylacznie przy probie wejscia, wiec
    # obsuniecie kapitalu podczas samego trzymania pozycji nie bylo oceniane
    # przez nikogo - kill-switch nie mogl zadzialac dokladnie w tej sytuacji,
    # dla ktorej istnieje.
    konto = risk.check_account()
    conn.commit()
    wejscia_wstrzymane = konto.verdict != "allow"
    if wejscia_wstrzymane and verbose:
        print(f"  UWAGA: wejścia wstrzymane — {konto.reason}")

    now = datetime.now(timezone.utc)
    bar_start = current_bar(now, h)
    feature_ids = horizon_feature_ids(cur, h)
    model = active_model(cur, engine, h)
    if verbose:
        print(f"  wagi: {'wersja modelu ' + str(model['version']) if model['version'] else 'założenia początkowe (brak zwalidowanego modelu)'}"
              f", próg wejścia {model['enter']:.4f}")
    stats = {"rozwazone": 0, "wejscia": 0, "wyjscia": 0, "wstrzymane": 0,
             "zablokowane": 0, "poza_swieca": 0}

    # `tradeable`, nie `active`: historię zbieramy szerzej, niż handlujemy.
    # Obstawianie instrumentu, którego giełda z kontem nie oferuje, to decyzja
    # niewykonalna - a trener kalibruje wagi na tym samym zbiorze (0020).
    cur.execute("SELECT id, symbol, kind FROM ref.instrument WHERE engine=%s AND active"
                " AND tradeable AND kind <> 'perp' ORDER BY symbol", (engine,))
    instrumenty = cur.fetchall()

    # Budzet na pozycje wyprowadzony z limitu lacznej ekspozycji i liczby
    # obserwowanych instrumentow. Stala 3000 przy limicie 15 000 i dwunastu
    # instrumentach oznaczala, ze system chce zaangazowac 36 000 - dwa i pol
    # raza ponad limit - i realnie miescilo sie piec pozycji zamiast dwunastu.
    baza = position_budget(risk.limits, len(instrumenty))

    # PRZEBIEG PIERWSZY: ocena wszystkich instrumentow, bez podejmowania akcji.
    #
    # Rozdzielenie oceny od dzialania jest tu konieczne, bo przy niedoborze
    # kapitalu kolejnosc rozpatrywania decyduje o tym, kto go dostanie.
    # Poprzednia wersja przetwarzala instrumenty alfabetycznie i przez to
    # systematycznie faworyzowala ADAUSDT wzgledem XRPUSDT - o przydziale
    # decydowala nazwa, nie sila sygnalu.
    oceny = []
    for inst in instrumenty:
        iid, symbol = inst["id"], inst["symbol"]
        if decided_for_bar(cur, engine, mode, iid, bar_start):
            stats["poza_swieca"] += 1
            continue
        features = latest_features(cur, iid, now, h, feature_ids)
        if len(features) < 4:
            if verbose:
                print(f"  {symbol:10s} pominięty — za mało cech ({len(features)})")
            continue
        price = mark_price(cur, iid, now)
        if price is None:
            continue
        stats["rozwazone"] += 1
        regime, regime_conf = classify_regime(features, atr_percentile_80(cur, iid, now, h))
        weights = (model["weights"] or {}).get(regime)
        score, confidence, contributions = evaluate(features, regime, weights, h)
        oceny.append({"iid": iid, "symbol": symbol, "kind": inst["kind"],
                      "features": features, "price": price,
                      "regime": regime, "regime_conf": regime_conf, "score": score,
                      "confidence": confidence, "contributions": contributions,
                      "position": open_position(cur, engine, mode, iid)})

    # PRZEBIEG DRUGI: najpierw wyjscia (nie konkuruja o kapital i zwalniaja go),
    # potem wejscia w kolejnosci malejacej sily sygnalu.
    def priorytet(o):
        wychodzi = o["position"] is not None
        return (0 if wychodzi else 1, -abs(o["score"]), o["symbol"])

    for ocena in sorted(oceny, key=priorytet):
        started = time.perf_counter()
        iid, symbol = ocena["iid"], ocena["symbol"]
        features, price = ocena["features"], ocena["price"]
        regime, regime_conf = ocena["regime"], ocena["regime_conf"]
        score, confidence = ocena["score"], ocena["confidence"]
        contributions = ocena["contributions"]

        position = ocena["position"]

        # --- wybor akcji ----------------------------------------------------
        if position:
            same_way = (position["side"] == "long" and score > 0) or \
                       (position["side"] == "short" and score < 0)
            if not same_way or abs(score) < model["exit"]:
                action = "exit"
            else:
                action = "hold"
        elif abs(score) >= model["enter"] and confidence >= model["min_conf"]:
            action = "enter_long" if score > 0 else "enter_short"
            # Krotkiej pozycji na spocie nie da sie zajac bez pozyczki marzowej.
            # Silnik podejmowal je swobodnie - 8 z 12 pozycji w dzienniku to byly
            # shorty na instrumentach spotowych, czyli transakcje niewykonalne.
            if action == "enter_short" and not koszty(ocena["kind"]).krotka_pozycja:
                action = "hold"
                stats["short_niewykonalny"] = stats.get("short_niewykonalny", 0) + 1
        else:
            action = "hold"

        # Wielkosc pozycji rosnie z sila sygnalu, ale nigdy powyzej limitu -
        # o tym decyduje risk kernel, nie ten kod.
        qty = Decimal(0)
        notional = Decimal(0)
        if action in ("enter_long", "enter_short"):
            notional = (baza * Decimal(str(min(1.0, abs(score) * 2)))).quantize(Decimal("0.01"))
            qty = (notional / price).quantize(Decimal("0.000001"))

        if action.startswith("enter") and wejscia_wstrzymane:
            # Kill-switch albo przekroczony limit konta - wejscie odpada,
            # ale decyzje i tak zapisujemy, zeby dziennik pokazywal, ze system
            # CHCIAL wejsc i co go zatrzymalo.
            verdict = konto
            action = "hold"
        elif action.startswith("enter"):
            verdict = risk.check(iid, notional, qty)
        else:
            verdict = type("V", (), {"verdict": "allow", "reason": None, "qty": qty,
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
                              rationale, contributions, signal_ids, latency,
                              model.get("model_version_id"))

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
    ap.add_argument("--bar", default=DEFAULT_BAR, choices=sorted(HORIZONS))
    ap.add_argument("--sprawdz", action="store_true",
                    help="wykonaj wszystkie odczyty bez zapisu i zakończ")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    h = get_horizon(args.bar)

    if args.sprawdz:
        with psycopg.connect(url, row_factory=dict_row) as conn:
            return await sprawdz(conn, args.engine, h)

    print(f"Silnik {args.engine}, świeca {h.bar} — jedna decyzja na świecę.\n")

    with psycopg.connect(url, row_factory=dict_row) as conn:
        while True:
            t0 = datetime.now(timezone.utc)
            try:
                s = await run_once(conn, args.engine, verbose=not args.quiet, h=h)
                print(f"[{t0:%H:%M:%S}] rozważone {s['rozwazone']}, wejścia {s['wejscia']}, "
                      f"wyjścia {s['wyjscia']}, wstrzymane {s['wstrzymane']}, "
                      f"zablokowane {s['zablokowane']}, "
                      f"już rozstrzygnięte w tej świecy {s['poza_swieca']}")
            except Exception as e:                            # noqa: BLE001
                conn.rollback()
                print(f"[{t0:%H:%M:%S}] przebieg przerwany: {type(e).__name__}: {e}")
            if args.once:
                return 0
            await asyncio.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
