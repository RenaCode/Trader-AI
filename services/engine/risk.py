"""Risk kernel: ostatnia instancja przed wyslaniem zlecenia.

Model moze CHCIEC wejsc w pozycje. Ten modul decyduje, czy wolno - i jest
celowo napisany tak, zeby nie dalo sie go obejsc przez prompt, konfiguracje
modelu ani zmiane wag. Limity pochodza z risk.limit_def w bazie i sa sprawdzane
przy kazdej decyzji.

Kolejnosc kontroli jest istotna: najpierw to, co zatrzymuje CALY silnik
(kill-switch, dzienna strata, obsuniecie), potem to, co dotyczy pojedynczego
zlecenia (wielkosc, ekspozycja, czestotliwosc). Inaczej system moglby zmniejszyc
zlecenie i wyslac je mimo aktywnego kill-switcha.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from services.engine.risk_rules import (
    RiskState, RiskVerdict, evaluate_account, evaluate_limits,
)


class RiskKernel:
    def __init__(self, conn, engine: str, mode: str = "demo"):
        self.conn = conn
        self.engine = engine
        self.mode = mode
        self._limits = self._load_limits()

    def _load_limits(self) -> dict[str, dict]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, name, value, unit, action, instrument_id FROM risk.limit_def"
            " WHERE engine = %s AND mode = %s AND enabled",
            (self.engine, self.mode),
        )
        return {r["name"]: dict(r) for r in cur.fetchall()}

    # ------------------------------------------------------------- stan konta
    def _equity(self) -> tuple[Decimal, Decimal, Decimal]:
        """Zwraca (kapital biezacy, szczyt, kapital sprzed doby)."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT equity FROM exec.equity_point WHERE engine=%s AND mode=%s"
            " ORDER BY event_time DESC LIMIT 1", (self.engine, self.mode))
        row = cur.fetchone()
        current = Decimal(row["equity"]) if row else Decimal(0)

        cur.execute(
            "SELECT max(equity) AS peak FROM exec.equity_point WHERE engine=%s AND mode=%s",
            (self.engine, self.mode))
        peak = Decimal((cur.fetchone() or {}).get("peak") or current)

        cur.execute(
            "SELECT equity FROM exec.equity_point WHERE engine=%s AND mode=%s"
            " AND event_time <= now() - interval '24 hours' ORDER BY event_time DESC LIMIT 1",
            (self.engine, self.mode))
        row = cur.fetchone()
        day_ago = Decimal(row["equity"]) if row else current
        return current, peak, day_ago

    def _open_exposure(self) -> tuple[Decimal, dict[int, Decimal]]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT instrument_id, sum(qty * avg_price) AS notional FROM exec.position"
            " WHERE engine=%s AND mode=%s AND closed_at IS NULL GROUP BY instrument_id",
            (self.engine, self.mode))
        per = {r["instrument_id"]: Decimal(r["notional"] or 0) for r in cur.fetchall()}
        return sum(per.values(), Decimal(0)), per

    def _orders_last_hour(self) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT count(*) AS n FROM exec.order WHERE engine=%s AND mode=%s"
            " AND submitted_at > now() - interval '1 hour'", (self.engine, self.mode))
        return (cur.fetchone() or {}).get("n", 0)

    def killswitch_active(self) -> dict | None:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, reason, triggered_at FROM risk.killswitch_event"
            " WHERE engine=%s AND mode=%s AND released_at IS NULL"
            " ORDER BY triggered_at DESC LIMIT 1", (self.engine, self.mode))
        return cur.fetchone()

    @property
    def limits(self) -> dict[str, dict]:
        """Wczytane limity - do wyliczenia budzetu na pozycje po stronie petli."""
        return self._limits

    # ------------------------------------------------------------- ocena
    def check_account(self) -> RiskVerdict:
        """Limity CALEGO KONTA, sprawdzane w kazdym cyklu.

        Wolane niezaleznie od tego, czy silnik chce w cokolwiek wejsc. Bez tego
        obsuniecie kapitalu podczas trzymania pozycji nie bylo przez nikogo
        oceniane i kill-switch nie mial szans zadzialac - patrz komentarz przy
        evaluate_account().
        """
        equity, peak, day_ago = self._equity()
        exposure, per_instrument = self._open_exposure()
        state = RiskState(
            equity=equity, peak=peak, day_ago=day_ago, exposure=exposure,
            per_instrument=per_instrument, orders_last_hour=0,
            killswitch=self.killswitch_active(),
        )
        verdict = evaluate_account(state, self._limits)
        if verdict.trigger_killswitch:
            self._trigger_killswitch(verdict.trigger_killswitch)
        return verdict

    def check(self, instrument_id: int, notional: Decimal,
              qty: Decimal) -> RiskVerdict:
        """Zbiera stan konta z bazy i oddaje ocene czystym regulom.

        Cala logika limitow siedzi w services.engine.risk_rules i jest wspolna
        z symulacja walk-forward. Ten modul odpowiada wylacznie za to, SKAD
        pochodzi stan - nie za to, jak sie go ocenia. Dzieki temu backtest nie
        moze mierzyc innego systemu niz ten, ktory naprawde handluje.
        """
        equity, peak, day_ago = self._equity()
        exposure, per_instrument = self._open_exposure()

        state = RiskState(
            equity=equity, peak=peak, day_ago=day_ago,
            exposure=exposure, per_instrument=per_instrument,
            orders_last_hour=self._orders_last_hour(),
            killswitch=self.killswitch_active(),
        )
        verdict = evaluate_limits(state, self._limits, instrument_id, notional, qty)
        if verdict.trigger_killswitch:
            self._trigger_killswitch(verdict.trigger_killswitch)
        return verdict

    def _trigger_killswitch(self, reason: str) -> None:
        """Kill-switch wymaga RECZNEGO odblokowania.

        Automatyczne wznowienie po obsunieciu to najprostsza droga do powtorzenia
        tej samej straty - dlatego tu nie ma zadnego licznika ani wygasania.
        """
        self.conn.cursor().execute(
            "INSERT INTO risk.killswitch_event (engine, mode, reason) VALUES (%s,%s,%s)",
            (self.engine, self.mode, reason))

    def record_usage(self, usage: list[dict]) -> None:
        cur = self.conn.cursor()
        for u in usage:
            lim = self._limits.get(u["name"])
            if not lim:
                continue
            util = (u["used"] / u["threshold"]) if u["threshold"] else 0.0
            cur.execute(
                "INSERT INTO risk.limit_usage (limit_def_id, event_time, used, threshold, utilization)"
                " VALUES (%s, now(), %s, %s, %s) ON CONFLICT DO NOTHING",
                (lim["id"], u["used"], u["threshold"], util))

    def record_violations(self, violations: list[dict], decision_id: int | None) -> None:
        cur = self.conn.cursor()
        for v in violations:
            lim = self._limits.get(v["limit"])
            if not lim:
                continue
            cur.execute(
                "INSERT INTO risk.violation (limit_def_id, engine, decision_id, observed,"
                " threshold, action_taken, detail) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (lim["id"], self.engine, decision_id, v["observed"], v["threshold"],
                 v["action"], __import__("json").dumps(v)))
