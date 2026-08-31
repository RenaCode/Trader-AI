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
from services.engine.koszty import POSLIZG_MAKER, POSLIZG_TAKER, koszty
from services.engine.risk_rules import (
    DEFAULT_LIMITS, RiskState, evaluate_account, evaluate_limits, position_budget,
)
from services.engine.signals import classify_regime, combine, evaluate, precompute

# Parametry egzekucji. Prowizja i poslizg pochodza z services/engine/koszty.py,
# bo zaleza od RODZAJU instrumentu i typu zlecenia - wczesniej byla tu jedna
# stala 0,055%, czyli stawka taker kontraktow wieczystych, stosowana takze do
# instrumentow spotowych, gdzie Bybit liczy 0,1%.
START_EQUITY = 100_000.0
# Bazowa wielkosc zlecenia wyprowadzana z limitu ekspozycji i liczby
# instrumentow - patrz risk_rules.position_budget().

MIN_SD = 1e-4

# Zeby ostrzezenie o niewykonalnych krotkich pozycjach padlo raz, nie przy
# kazdym z tysiecy wywolan symulacji.
_OSTRZEZONO: dict[str, bool] = {}


@dataclass
class Position:
    side: int                 # +1 long, -1 short
    qty: float
    entry: float
    conf: float
    opened_at: datetime


@dataclass
class Egzekucja:
    """Jak skladamy zlecenia - i co za to placimy.

    RYNKOWA: przechodzimy przez spread, placimy prowizje taker i poslizg,
    ale wypelnienie jest pewne.

    Z LIMITEM: skladamy zlecenie po cenie zamkniecia swiecy decyzyjnej. Nie ma
    poslizgu i placimy tansza prowizje maker, ale wypelnienie NIE JEST PEWNE -
    zachodzi tylko wtedy, gdy nastepna swieca dotknie naszej ceny.

    To rozroznienie decyduje o wszystkim. Na kontrakcie wieczystym koszt rundy
    wynosi 0,190% przy zleceniu rynkowym i 0,040% przy limicie - siedmiokrotna
    roznica przy przewadze rzedu 0,12% na transakcje.

    Ale model wypelnien musi byc UCZCIWY, inaczej backtest zaklamie w druga
    strone niz dotad. Zlecenie z limitem wypelnia sie wtedy, gdy cena przyszla
    do nas - czyli czesto wtedy, gdy rynek idzie PRZECIWKO nam. Ruchy, ktore
    uciekaja natychmiast, sa dla nas stracone, a to wlasnie one bywaja
    najbardziej zyskowne. Ta selekcja jest w modelu zawarta z definicji: nie
    zgadujemy prawdopodobienstwa wypelnienia, tylko sprawdzamy, czy nastepna
    swieca faktycznie dotknela naszej ceny.
    """
    limit: bool = False


@dataclass
class PortfolioResult:
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    killswitch_at: datetime | None = None
    killswitch_reason: str | None = None
    blocked: int = 0          # ile wejsc odrzucil risk kernel
    downsized: int = 0        # ile obcial
    max_exposure: float = 0.0  # najwieksza jednoczesna ekspozycja
    base_notional: float = 0.0  # budzet na pozycje wyliczony z limitu
    niewypelnione_wejscia: int = 0
    niewypelnione_wyjscia: int = 0
    # Stawki faktycznie zastosowane - do sprawdzenia z zewnatrz, ze symulacja
    # policzyla te koszty, ktore deklaruje.
    prowizja: float = 0.0
    poslizg: float = 0.0


def precompute_panels(panels: dict[str, list[dict]],
                      atr_thresholds: dict[str, float | None],
                      h: Horizon) -> dict[str, list[tuple]]:
    """Rezim i komplet sygnalow dla kazdej swiecy - policzone RAZ.

    Ani rezim, ani pojedyncze sygnaly nie zaleza od wag; zalezy od nich dopiero
    ich zsumowanie. Trener sprawdza kilkadziesiat ukladow wag na tym samym oknie,
    wiec bez tej pamieci podrecznej liczyl komplet sygnalow od nowa dla kazdej
    proby - przy 24 instrumentach, 40 oknach i 60 probach to kilkaset milionow
    zbednych wywolan.

    Zwraca {symbol: [(rezim, sygnaly) na swiece]} w kolejnosci wierszy panelu.
    """
    cache: dict[str, list[tuple]] = {}
    for sym, rows in panels.items():
        atr_p80 = atr_thresholds.get(sym)
        cache[sym] = [
            (classify_regime(r["f"], atr_p80)[0], precompute(r["f"], h))
            for r in rows
        ]
    return cache


def _wypelnione(limit: float, swieca: dict, kupno: bool) -> bool:
    """Czy zlecenie z limitem doczekalo sie wypelnienia w tej swiecy.

    Kupno po cenie L wypelnia sie, gdy rynek zszedl do L (low <= L).
    Sprzedaz po L - gdy rynek doszedl do L (high >= L).

    To jest caly model selekcji negatywnej i jest w nim zawarty z definicji:
    wypelniamy sie wtedy, gdy cena PRZYSZLA DO NAS, czyli czesto wtedy, gdy
    rynek idzie przeciwko nam. Ruchy, ktore uciekaja natychmiast, sa dla nas
    stracone - a to wlasnie one bywaja najbardziej zyskowne. Nie zgadujemy tu
    zadnego prawdopodobienstwa; sprawdzamy fakt z danych swiecy.
    """
    return swieca["low"] <= limit if kupno else swieca["high"] >= limit


def _sizing(score: float, price: float, baza: float) -> tuple[float, float]:
    """Wielkosc zlecenia dokladnie jak w petli decyzyjnej."""
    notional = baza * min(1.0, abs(score) * 2)
    return notional, notional / price


def simulate_portfolio(panels: dict[str, list[dict]],
                       weights: dict[str, dict[str, float]],
                       atr_thresholds: dict[str, float | None],
                       gates: dict[str, tuple[float, float]],
                       h: Horizon,
                       min_conf: float,
                       limits: dict | None = None,
                       instrument_ids: dict[str, int] | None = None,
                       cache: dict[str, list[tuple]] | None = None,
                       kind: str = "spot",
                       egzekucja: Egzekucja | None = None) -> PortfolioResult:
    """Jedno konto, wszystkie instrumenty, wspolne limity.

    Wejscie i wyjscie po cenie NASTEPNEJ swiecy - decyzja z zamkniecia swiecy T
    nie moze zostac wykonana po cenie tej samej swiecy.
    """
    limits = limits or DEFAULT_LIMITS
    egz = egzekucja or Egzekucja()
    k = koszty(kind)
    fee = k.maker if egz.limit else k.taker
    slip = POSLIZG_MAKER if egz.limit else POSLIZG_TAKER
    if not k.krotka_pozycja and not _OSTRZEZONO.get(kind):
        _OSTRZEZONO[kind] = True
    ids = instrument_ids or {s: i for i, s in enumerate(sorted(panels), start=1)}
    baza = float(position_budget(limits, len(panels)))
    # Bez podanej pamieci podrecznej liczymy ja tutaj - wygodne przy pojedynczym
    # wywolaniu, kosztowne przy dziesiatkach prob na tym samym oknie.
    cache = cache if cache is not None else precompute_panels(panels, atr_thresholds, h)

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
    max_exposure = 0.0
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
            regime, sygnaly = cache[sym][cur["idx"]]
            score, _conf, _ = combine(sygnaly, regime, weights.get(regime), with_contributions=False)
            pos = positions[sym]
            enter_gate, exit_gate = gates[sym]
            same = (pos.side > 0 and score > 0) or (pos.side < 0 and score < 0)
            if same and abs(score) >= exit_gate:
                continue
            if egz.limit:
                # Wyjscie po cenie zamkniecia swiecy decyzyjnej. Gdy sie nie
                # wypelni, POZYCJA ZOSTAJE OTWARTA - nieudane wyjscie
                # w spadajacym rynku kosztuje najwiecej i musi byc w modelu.
                limit_px = cur["row"]["price"]
                if not _wypelnione(limit_px, rows[nxt_idx], kupno=pos.side < 0):
                    res.niewypelnione_wyjscia += 1
                    continue
                exit_px = limit_px
            else:
                exit_px = rows[nxt_idx]["price"] * (1 - slip * pos.side)
            pnl = pos.side * (exit_px - pos.entry) * pos.qty
            realized += pnl
            fees_paid += pos.qty * exit_px * fee
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

        max_exposure = max(max_exposure, float(exposure))

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

        # Kandydatow sortujemy po SILE SYGNALU, nie po nazwie instrumentu.
        # Gdy limit ekspozycji nie pozwala wejsc we wszystkie, kapital ma
        # dostac najmocniejszy sygnal, a nie ten, ktory jest wczesniej
        # w alfabecie. Poprzednia wersja iterowala po sorted(present) i przez
        # to systematycznie faworyzowala ADAUSDT wzgledem XRPUSDT.
        kandydaci = []
        for sym, cur in present.items():
            if sym in positions:
                continue
            rows = panels[sym]
            if cur["idx"] + 1 >= len(rows):
                continue
            regime, sygnaly = cache[sym][cur["idx"]]
            score, conf, _ = combine(sygnaly, regime, weights.get(regime), with_contributions=False)
            enter_gate, _exit_gate = gates[sym]
            if abs(score) < enter_gate or conf < min_conf:
                continue
            kandydaci.append((abs(score), sym, cur, score, conf))
        kandydaci.sort(key=lambda k: (-k[0], k[1]))

        for _sila, sym, cur, score, conf in kandydaci:
            rows = panels[sym]
            nxt_idx = cur["idx"] + 1
            price = rows[nxt_idx]["price"]
            notional, qty = _sizing(score, price, baza)

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
            if egz.limit:
                limit_px = cur["row"]["price"]
                if not _wypelnione(limit_px, rows[nxt_idx], kupno=side > 0):
                    res.niewypelnione_wejscia += 1
                    continue
                entry = limit_px
            else:
                entry = price * (1 + slip * side)
            positions[sym] = Position(side=side, qty=qty, entry=entry, conf=conf, opened_at=t)
            fees_paid += qty * entry * fee
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
        # Domkniecie po ostatniej swiecy idzie ZAWSZE po cenie rynkowej -
        # koniec okna nie czeka na wypelnienie limitu.
        exit_px = px * (1 - POSLIZG_TAKER * pos.side)
        realized += pos.side * (exit_px - pos.entry) * pos.qty
        fees_paid += pos.qty * exit_px * k.taker
        res.trades.append({"ret": pos.side * (exit_px / pos.entry - 1), "conf": pos.conf,
                           "symbol": sym, "opened_at": pos.opened_at, "closed_at": last_t})
    res.equity_curve.append((last_t, START_EQUITY + realized - fees_paid))
    res.max_exposure = max_exposure
    res.base_notional = baza
    res.prowizja = fee
    res.poslizg = slip
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
            "killswitch": res.killswitch_reason,
            "niewypelnione": res.niewypelnione_wejscia + res.niewypelnione_wyjscia}
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
