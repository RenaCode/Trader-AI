"""Symulacja PORTFELA - jedna kasa, wspolne limity, ten sam risk kernel.

Poprzednia symulacja liczyla kazdy instrument osobno i sumowala transakcje na
koncu. Bylo to wygodne i falszywe z trzech powodow:

  1. Limit lacznej ekspozycji i limit czestotliwosci zlecen sa wlasnosciami
     KONTA, nie instrumentu. Dwanascie niezaleznych symulacji nigdy ich nie
     naruszy, bo kazda widzi tylko siebie.
  2. Kill-switch zatrzymuje CALY silnik. W symulacji per instrument nie ma
     czego zatrzymac - strata na SOL nie wstrzymywala handlu na BTC, choc
     w produkcji wstrzymalaby.
  3. Obsuniecie kapitalu liczone per instrument jest mniejsze niz obsuniecie
     portfela, bo instrumenty spadaja jednoczesnie (korelacja krypto jest
     wysoka). Pierwsza iteracja raportowala -37% wlasnie dlatego, ze nikt tego
     obsuniecia nie zatrzymywal.

Rezultat tamtego podejscia: backtest pokazywal obsuniecie 37% przy
skonfigurowanym kill-switchu na 10%. Opisywal wiec strategie, ktorej ten system
nie wykonalby - i to jest gorsze niz brak backtestu, bo wyglada na wynik.

Reguly limitow sa wspolne z produkcja (services.engine.risk_rules). Ten modul
dostarcza im tylko stan trzymany w pamieci zamiast w bazie.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from services.engine.horizons import Horizon
from services.engine.risk_rules import (
    DEFAULT_LIMITS, RiskState, evaluate_account, evaluate_limits,
)
from services.engine.signals import classify_regime, evaluate

# Parametry egzekucji - te same, ktore stosuje services/engine/loop.py.
FEE = 0.00055
SLIP = 0.0004
START_EQUITY = 100_000.0
BASE_NOTIONAL = 3000.0      # bazowa wielkosc zlecenia przed skalowaniem sila sygnalu

MIN_SD = 1e-4


@dataclass
class Position:
    side: int                 # +1 long, -1 short
    qty: float
    entry: float
    conf: float
    opened_at: datetime


@dataclass
class PortfolioResult:
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    killswitch_at: datetime | None = None
    killswitch_reason: str | None = None
    blocked: int = 0          # ile wejsc odrzucil risk kernel
    downsized: int = 0        # ile obcial


def _sizing(score: float, price: float) -> tuple[float, float]:
    """Wielkosc zlecenia dokladnie jak w petli decyzyjnej."""
    notional = BASE_NOTIONAL * min(1.0, abs(score) * 2)
    return notional, notional / price


def simulate_portfolio(panels: dict[str, list[dict]],
                       weights: dict[str, dict[str, float]],
                       atr_thresholds: dict[str, float | None],
                       gates: dict[str, tuple[float, float]],
                       h: Horizon,
                       min_conf: float,
                       limits: dict | None = None,
                       instrument_ids: dict[str, int] | None = None) -> PortfolioResult:
    """Jedno konto, wszystkie instrumenty, wspolne limity.

    Wejscie i wyjscie po cenie NASTEPNEJ swiecy - decyzja z zamkniecia swiecy T
    nie moze zostac wykonana po cenie tej samej swiecy.
    """
    limits = limits or DEFAULT_LIMITS
    ids = instrument_ids or {s: i for i, s in enumerate(sorted(panels), start=1)}

    # Wspolna os czasu. Instrumenty moga miec rozne pokrycie, wiec bierzemy
    # sume ich znacznikow, a nie przeciecie - inaczej jeden krotszy szereg
    # obcinalby cala symulacje.
    by_time: dict[datetime, dict[str, dict]] = {}
    for sym, rows in panels.items():
        for i, r in enumerate(rows):
            by_time.setdefault(r["t"], {})[sym] = {"row": r, "idx": i}
    timeline = sorted(by_time)
    if len(timeline) < 3:
        return PortfolioResult()

    positions: dict[str, Position] = {}
    realized = 0.0
    fees_paid = 0.0
    res = PortfolioResult()
    order_times: list[datetime] = []
    killswitch: dict | None = None
    peak = START_EQUITY
    equity_hist: list[tuple[datetime, float]] = []

    for ti, t in enumerate(timeline[:-1]):
        present = by_time[t]

        # --- wycena biezaca -------------------------------------------------
        unreal = 0.0
        for sym, pos in positions.items():
            cur = present.get(sym)
            px = cur["row"]["price"] if cur else pos.entry
            unreal += pos.side * (px - pos.entry) * pos.qty
        equity = START_EQUITY + realized - fees_paid + unreal
        peak = max(peak, equity)
        equity_hist.append((t, equity))
        res.equity_curve.append((t, equity))

        # Kapital sprzed doby - do limitu straty dziennej.
        cutoff = t - timedelta(hours=24)
        day_ago = START_EQUITY
        for et, ev in reversed(equity_hist):
            if et <= cutoff:
                day_ago = ev
                break

        # --- wyjscia (NIGDY nie podlegaja limitom - wyjsc wolno zawsze) -----
        for sym in list(positions):
            cur = present.get(sym)
            if not cur:
                continue
            nxt_idx = cur["idx"] + 1
            rows = panels[sym]
            if nxt_idx >= len(rows):
                continue
            row = cur["row"]
            regime, _ = classify_regime(row["f"], atr_thresholds.get(sym))
            score, _conf, _ = evaluate(row["f"], regime, weights.get(regime), h)
            pos = positions[sym]
            enter_gate, exit_gate = gates[sym]
            same = (pos.side > 0 and score > 0) or (pos.side < 0 and score < 0)
            if same and abs(score) >= exit_gate:
                continue
            exit_px = rows[nxt_idx]["price"] * (1 - SLIP * pos.side)
            pnl = pos.side * (exit_px - pos.entry) * pos.qty
            realized += pnl
            fees_paid += pos.qty * exit_px * FEE
            res.trades.append({
                "ret": pos.side * (exit_px / pos.entry - 1),
                "conf": pos.conf, "symbol": sym,
                "opened_at": pos.opened_at, "closed_at": rows[nxt_idx]["t"],
            })
            del positions[sym]
            order_times.append(t)

        # Po wyjsciach kapital i ekspozycja sie zmienily - przeliczamy.
        unreal = sum(p.side * ((present[s]["row"]["price"] if s in present else p.entry) - p.entry)
                     * p.qty for s, p in positions.items())
        equity = START_EQUITY + realized - fees_paid + unreal
        peak = max(peak, equity)

        # --- limity KONTA, sprawdzane w kazdej swiecy ------------------------
        # Niezaleznie od tego, czy cokolwiek chcemy kupic. Obsuniecie podczas
        # samego trzymania pozycji tez musi uruchomic kill-switch - inaczej
        # limit dziala tylko wtedy, gdy system probuje wejsc, czyli nie wtedy,
        # kiedy jest najbardziej potrzebny.
        per_inst = {ids[s]: Decimal(str(p.qty * p.entry)) for s, p in positions.items()}
        exposure = sum(per_inst.values(), Decimal(0))
        hour_ago = t - timedelta(hours=1)
        orders_h = sum(1 for ot in order_times if ot > hour_ago)

        if killswitch is None:
            konto = evaluate_account(
                RiskState(equity=Decimal(str(equity)), peak=Decimal(str(peak)),
                          day_ago=Decimal(str(day_ago)), exposure=exposure,
                          per_instrument=per_inst, orders_last_hour=orders_h,
                          killswitch=None),
                limits)
            if konto.trigger_killswitch:
                killswitch = {"reason": konto.trigger_killswitch, "triggered_at": t}
                res.killswitch_at = t
                res.killswitch_reason = konto.trigger_killswitch

        for sym, cur in sorted(present.items()):
            if sym in positions:
                continue
            rows = panels[sym]
            nxt_idx = cur["idx"] + 1
            if nxt_idx >= len(rows):
                continue
            row = cur["row"]
            regime, _ = classify_regime(row["f"], atr_thresholds.get(sym))
            score, conf, _ = evaluate(row["f"], regime, weights.get(regime), h)
            enter_gate, _exit_gate = gates[sym]
            if abs(score) < enter_gate or conf < min_conf:
                continue

            price = rows[nxt_idx]["price"]
            notional, qty = _sizing(score, price)

            state = RiskState(
                equity=Decimal(str(equity)), peak=Decimal(str(peak)),
                day_ago=Decimal(str(day_ago)), exposure=exposure,
                per_instrument=per_inst, orders_last_hour=orders_h,
                killswitch=killswitch,
            )
            v = evaluate_limits(state, limits, ids[sym],
                                Decimal(str(notional)), Decimal(str(qty)))

            if v.trigger_killswitch and killswitch is None:
                # Kill-switch wymaga RECZNEGO odblokowania, wiec do konca okna
                # nie ma juz zadnych wejsc. To nie jest ostroznosc symulacji -
                # tak zachowa sie produkcja.
                killswitch = {"reason": v.trigger_killswitch, "triggered_at": t}
                res.killswitch_at = t
                res.killswitch_reason = v.trigger_killswitch

            if v.verdict == "block":
                res.blocked += 1
                continue
            if v.verdict == "downsize":
                res.downsized += 1
                qty = float(v.qty)
                notional = qty * price

            side = 1 if score > 0 else -1
            entry = price * (1 + SLIP * side)
            positions[sym] = Position(side=side, qty=qty, entry=entry, conf=conf, opened_at=t)
            fees_paid += qty * entry * FEE
            order_times.append(t)

            per_inst[ids[sym]] = per_inst.get(ids[sym], Decimal(0)) + Decimal(str(qty * entry))
            exposure += Decimal(str(qty * entry))
            orders_h += 1

    # Domkniecie po ostatniej swiecy - pozycja otwarta na koncu okna liczy sie
    # po ostatniej znanej cenie, inaczej jej wynik znikalby z oceny.
    last_t = timeline[-1]
    for sym, pos in list(positions.items()):
        cur = by_time[last_t].get(sym)
        px = cur["row"]["price"] if cur else pos.entry
        exit_px = px * (1 - SLIP * pos.side)
        realized += pos.side * (exit_px - pos.entry) * pos.qty
        fees_paid += pos.qty * exit_px * FEE
        res.trades.append({"ret": pos.side * (exit_px / pos.entry - 1), "conf": pos.conf,
                           "symbol": sym, "opened_at": pos.opened_at, "closed_at": last_t})
    res.equity_curve.append((last_t, START_EQUITY + realized - fees_paid))
    return res


def metrics(res: PortfolioResult, min_trades: int) -> dict:
    """Miary z symulacji portfelowej. Obsuniecie z krzywej KAPITALU, nie z transakcji."""
    rets = [t["ret"] for t in res.trades]
    curve = [e for _, e in res.equity_curve] or [START_EQUITY]

    peak = curve[0]
    max_dd = 0.0
    for v in curve:
        peak = max(peak, v)
        max_dd = min(max_dd, (v - peak) / peak * 100)

    total_return = (curve[-1] / START_EQUITY - 1) * 100

    base = {"max_dd": round(max_dd, 4), "n_trades": len(rets),
            "total_return": round(total_return, 4), "trades": res.trades,
            "blocked": res.blocked, "downsized": res.downsized,
            "killswitch": res.killswitch_reason}
    if len(rets) < min_trades:
        return base | {"sharpe": 0.0, "sortino": 0.0, "hit_rate": 0.0, "avg_r": 0.0,
                       "profit_factor": 0.0, "insufficient": True}

    mean = statistics.fmean(rets)
    sd = max(statistics.pstdev(rets), MIN_SD)
    down = [r for r in rets if r < 0]
    dsd = max(statistics.pstdev(down) if len(down) > 1 else sd, MIN_SD)
    gains = sum(r for r in rets if r > 0)
    losses = -sum(r for r in rets if r < 0)
    return base | {
        "sharpe": round(mean / sd, 4), "sortino": round(mean / dsd, 4),
        "hit_rate": round(sum(1 for r in rets if r > 0) / len(rets), 4),
        "avg_r": round(mean * 100, 4),
        "profit_factor": round(gains / losses, 4) if losses > 0 else 0.0,
    }
