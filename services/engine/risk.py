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

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal


@dataclass
class RiskVerdict:
    verdict: str                      # 'allow' | 'downsize' | 'block'
    reason: str | None = None
    qty: Decimal | None = None        # wielkosc po ewentualnym obcieciu
    violations: list[dict] = field(default_factory=list)
    usage: list[dict] = field(default_factory=list)


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

    # ------------------------------------------------------------- ocena
    def check(self, instrument_id: int, notional: Decimal,
              qty: Decimal) -> RiskVerdict:
        violations: list[dict] = []
        usage: list[dict] = []

        # 1. Kill-switch. Nic dalej nie ma znaczenia.
        ks = self.killswitch_active()
        if ks:
            return RiskVerdict("block",
                               f"Kill-switch aktywny od {ks['triggered_at']:%Y-%m-%d %H:%M}: {ks['reason']}",
                               violations=violations)

        equity, peak, day_ago = self._equity()
        exposure, per_instrument = self._open_exposure()

        # 2. Strata dzienna.
        lim = self._limits.get("max_daily_loss")
        if lim and day_ago > 0:
            loss_pct = float((day_ago - equity) / day_ago * 100)
            usage.append({"name": "max_daily_loss", "used": max(0.0, loss_pct),
                          "threshold": float(lim["value"])})
            if loss_pct >= float(lim["value"]):
                violations.append({"limit": "max_daily_loss", "observed": loss_pct,
                                   "threshold": float(lim["value"]), "action": lim["action"]})
                if lim["action"] == "killswitch":
                    self._trigger_killswitch(
                        f"Strata dzienna {loss_pct:.2f}% przekroczyła limit {lim['value']}%")
                return RiskVerdict("block",
                                   f"Strata dzienna {loss_pct:.2f}% osiągnęła limit {lim['value']}%",
                                   violations=violations, usage=usage)

        # 3. Obsuniecie od szczytu.
        lim = self._limits.get("max_drawdown")
        if lim and peak > 0:
            dd_pct = float((peak - equity) / peak * 100)
            usage.append({"name": "max_drawdown", "used": max(0.0, dd_pct),
                          "threshold": float(lim["value"])})
            if dd_pct >= float(lim["value"]):
                violations.append({"limit": "max_drawdown", "observed": dd_pct,
                                   "threshold": float(lim["value"]), "action": lim["action"]})
                if lim["action"] == "killswitch":
                    self._trigger_killswitch(
                        f"Obsunięcie kapitału {dd_pct:.2f}% przekroczyło limit {lim['value']}%")
                return RiskVerdict("block",
                                   f"Obsunięcie {dd_pct:.2f}% osiągnęło limit {lim['value']}%",
                                   violations=violations, usage=usage)

        # 4. Czestotliwosc zlecen - ochrona przed petla bledu.
        lim = self._limits.get("max_orders_per_hour")
        if lim:
            n = self._orders_last_hour()
            usage.append({"name": "max_orders_per_hour", "used": float(n),
                          "threshold": float(lim["value"])})
            if n >= float(lim["value"]):
                violations.append({"limit": "max_orders_per_hour", "observed": float(n),
                                   "threshold": float(lim["value"]), "action": lim["action"]})
                return RiskVerdict("block",
                                   f"Wykonano {n} zleceń w ostatniej godzinie przy limicie {lim['value']}",
                                   violations=violations, usage=usage)

        # 5. Laczna ekspozycja.
        lim = self._limits.get("max_total_exposure")
        if lim:
            after = exposure + notional
            usage.append({"name": "max_total_exposure", "used": float(exposure),
                          "threshold": float(lim["value"])})
            if after > Decimal(lim["value"]):
                violations.append({"limit": "max_total_exposure", "observed": float(after),
                                   "threshold": float(lim["value"]), "action": lim["action"]})
                return RiskVerdict("block",
                                   f"Ekspozycja po wejściu wyniosłaby {after:.0f} przy limicie {lim['value']:.0f}",
                                   violations=violations, usage=usage)

        # 6. Wielkosc pojedynczej pozycji - tutaj obcinamy zamiast blokowac.
        lim = self._limits.get("max_position_notional")
        if lim:
            cap = Decimal(lim["value"])
            current = per_instrument.get(instrument_id, Decimal(0))
            usage.append({"name": "max_position_notional", "used": float(current),
                          "threshold": float(cap)})
            if current + notional > cap:
                allowed = cap - current
                if allowed <= 0:
                    violations.append({"limit": "max_position_notional", "observed": float(current),
                                       "threshold": float(cap), "action": "block"})
                    return RiskVerdict("block",
                                       f"Pozycja na tym instrumencie już wypełnia limit {cap:.0f}",
                                       violations=violations, usage=usage)
                factor = allowed / notional
                violations.append({"limit": "max_position_notional",
                                   "observed": float(current + notional),
                                   "threshold": float(cap), "action": "downsize"})
                return RiskVerdict("downsize",
                                   f"Zlecenie obcięte do limitu wielkości pozycji ({cap:.0f})",
                                   qty=(qty * factor).quantize(Decimal("0.000001")),
                                   violations=violations, usage=usage)

        return RiskVerdict("allow", qty=qty, usage=usage)

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
