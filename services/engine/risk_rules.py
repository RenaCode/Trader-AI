"""Czyste reguly limitow ryzyka - bez bazy danych.

DLACZEGO OSOBNY MODUL

Symulacja walk-forward musi stosowac DOKLADNIE te same limity co produkcja,
inaczej mierzy strategie, ktorej system nigdy nie wykona. Backtest, ktory
pokazuje obsuniecie 37% przy skonfigurowanym kill-switchu na 10%, nie opisuje
niczego, co moglo sie wydarzyc.

Napisanie drugiej implementacji limitow "na potrzeby symulacji" byloby
gwarancja rozjazdu przy pierwszej zmianie regul - to ten sam blad, przed ktorym
broni `feat.as_of()` po stronie cech. Dlatego reguly zyja tutaj, jako funkcja
czysta operujaca na przekazanym STANIE, a oba tory tylko dostarczaja ten stan:

  - RiskKernel        -> stan czyta z bazy (pozycje, kapital, zlecenia)
  - symulacja portfela -> stan trzyma w pamieci

Kolejnosc kontroli jest istotna i zachowana: najpierw to, co zatrzymuje CALY
silnik (kill-switch, strata dzienna, obsuniecie), potem to, co dotyczy
pojedynczego zlecenia (czestotliwosc, ekspozycja, wielkosc). Inaczej system
moglby zmniejszyc zlecenie i wyslac je mimo aktywnego kill-switcha.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class RiskState:
    """Stan konta w chwili decyzji. Zrodlo (baza / pamiec) nie ma znaczenia."""
    equity: Decimal
    peak: Decimal
    day_ago: Decimal
    exposure: Decimal
    per_instrument: dict[int, Decimal]
    orders_last_hour: int
    killswitch: dict | None = None


@dataclass
class RiskVerdict:
    verdict: str                      # 'allow' | 'downsize' | 'block'
    reason: str | None = None
    qty: Decimal | None = None        # wielkosc po ewentualnym obcieciu
    violations: list[dict] = field(default_factory=list)
    usage: list[dict] = field(default_factory=list)
    # Nazwa limitu, ktory zada uruchomienia kill-switcha (albo None).
    trigger_killswitch: str | None = None


def evaluate_account(state: RiskState, limits: dict[str, dict]) -> RiskVerdict:
    """Limity dotyczace CALEGO KONTA - kill-switch, strata dzienna, obsuniecie.

    Wydzielone z evaluate_limits, bo musza byc sprawdzane W KAZDYM CYKLU, a nie
    tylko przy probie wejscia w pozycje.

    Powod jest konkretny i byl realna luka: silnik wolal risk.check() wylacznie
    dla akcji zaczynajacych sie od "enter". Jesli trzymal otwarte pozycje i nie
    probowal wchodzic w nowe - a wlasnie tak zachowuje sie podczas gwaltownego
    spadku, bo sygnaly sa niespojne - to obsuniecie kapitalu NIE BYLO PRZEZ
    NIKOGO OCENIANE. Kill-switch na 10% nie mial szans zadzialac dokladnie w tej
    sytuacji, dla ktorej istnieje.

    Wykryte przez test symulacji portfelowej, ktory nie potrafil doprowadzic do
    uruchomienia kill-switcha mimo straty przekraczajacej prog.
    """
    violations: list[dict] = []
    usage: list[dict] = []

    # 1. Kill-switch. Nic dalej nie ma znaczenia.
    if state.killswitch:
        ts = state.killswitch.get("triggered_at")
        kiedy = f" od {ts:%Y-%m-%d %H:%M}" if ts else ""
        return RiskVerdict("block",
                           f"Kill-switch aktywny{kiedy}: {state.killswitch.get('reason')}")

    # 2. Strata dzienna.
    lim = limits.get("max_daily_loss")
    if lim and state.day_ago > 0:
        loss_pct = float((state.day_ago - state.equity) / state.day_ago * 100)
        usage.append({"name": "max_daily_loss", "used": max(0.0, loss_pct),
                      "threshold": float(lim["value"])})
        if loss_pct >= float(lim["value"]):
            violations.append({"limit": "max_daily_loss", "observed": loss_pct,
                               "threshold": float(lim["value"]), "action": lim["action"]})
            return RiskVerdict(
                "block", f"Strata dzienna {loss_pct:.2f}% osiągnęła limit {lim['value']}%",
                violations=violations, usage=usage,
                trigger_killswitch=(f"Strata dzienna {loss_pct:.2f}% przekroczyła limit "
                                    f"{lim['value']}%") if lim["action"] == "killswitch" else None)

    # 3. Obsuniecie od szczytu.
    lim = limits.get("max_drawdown")
    if lim and state.peak > 0:
        dd_pct = float((state.peak - state.equity) / state.peak * 100)
        usage.append({"name": "max_drawdown", "used": max(0.0, dd_pct),
                      "threshold": float(lim["value"])})
        if dd_pct >= float(lim["value"]):
            violations.append({"limit": "max_drawdown", "observed": dd_pct,
                               "threshold": float(lim["value"]), "action": lim["action"]})
            return RiskVerdict(
                "block", f"Obsunięcie {dd_pct:.2f}% osiągnęło limit {lim['value']}%",
                violations=violations, usage=usage,
                trigger_killswitch=(f"Obsunięcie kapitału {dd_pct:.2f}% przekroczyło limit "
                                    f"{lim['value']}%") if lim["action"] == "killswitch" else None)

    return RiskVerdict("allow", usage=usage)


def evaluate_limits(state: RiskState, limits: dict[str, dict],
                    instrument_id: int, notional: Decimal,
                    qty: Decimal) -> RiskVerdict:
    """Pelna ocena zlecenia: najpierw limity konta, potem limity zlecenia."""
    konto = evaluate_account(state, limits)
    if konto.verdict != "allow":
        return konto
    violations: list[dict] = list(konto.violations)
    usage: list[dict] = list(konto.usage)

    # 4. Czestotliwosc zlecen - ochrona przed petla bledu.
    lim = limits.get("max_orders_per_hour")
    if lim:
        n = state.orders_last_hour
        usage.append({"name": "max_orders_per_hour", "used": float(n),
                      "threshold": float(lim["value"])})
        if n >= float(lim["value"]):
            violations.append({"limit": "max_orders_per_hour", "observed": float(n),
                               "threshold": float(lim["value"]), "action": lim["action"]})
            return RiskVerdict(
                "block", f"Wykonano {n} zleceń w ostatniej godzinie przy limicie {lim['value']}",
                violations=violations, usage=usage)

    # 5. Laczna ekspozycja.
    lim = limits.get("max_total_exposure")
    if lim:
        after = state.exposure + notional
        usage.append({"name": "max_total_exposure", "used": float(state.exposure),
                      "threshold": float(lim["value"])})
        if after > Decimal(lim["value"]):
            violations.append({"limit": "max_total_exposure", "observed": float(after),
                               "threshold": float(lim["value"]), "action": lim["action"]})
            return RiskVerdict(
                "block",
                f"Ekspozycja po wejściu wyniosłaby {after:.0f} przy limicie {lim['value']:.0f}",
                violations=violations, usage=usage)

    # 6. Wielkosc pojedynczej pozycji - tutaj obcinamy zamiast blokowac.
    lim = limits.get("max_position_notional")
    if lim:
        cap = Decimal(lim["value"])
        current = state.per_instrument.get(instrument_id, Decimal(0))
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


# Limity domyslne - uzywane przez symulacje, gdy nie da sie ich odczytac z bazy.
# Muszą odpowiadać wierszom zasianym w risk.limit_def; rozjazd tutaj oznaczałby,
# że backtest mierzy inny system niż produkcja.
DEFAULT_LIMITS = {
    "max_daily_loss":        {"value": Decimal("2"),     "action": "killswitch"},
    "max_drawdown":          {"value": Decimal("10"),    "action": "killswitch"},
    "max_orders_per_hour":   {"value": Decimal("20"),    "action": "block"},
    "max_position_notional": {"value": Decimal("5000"),  "action": "downsize"},
    "max_total_exposure":    {"value": Decimal("15000"), "action": "block"},
}
