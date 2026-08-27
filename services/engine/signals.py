"""Sygnaly i klasyfikacja rezimu rynkowego.

Kazdy sygnal jest prosta, wytlumaczalna hipoteza zwracajaca wartosc w zakresie
-1..1 (kierunek i sila) oraz pewnosc 0..1. Zaden nie jest madry sam z siebie -
madrosc ma sie brac z KALIBRACJI ich wag per rezim, ktora robi warstwa uczenia.

Wagi ponizej to jawne zalozenia poczatkowe, nie wynik dopasowania do danych.
Sa tu po to, zeby system mial od czego zaczac, i maja zostac zastapione wagami
wyliczonymi z okien walk-forward. Dopoki tak sie nie stanie, kazda decyzja jest
decyzja tych zalozen - i tak nalezy ja czytac.
"""
from __future__ import annotations

from dataclasses import dataclass

REGIMES = ("trend_up", "trend_down", "range", "high_vol", "news_shock")


@dataclass(frozen=True)
class SignalOutput:
    score: float        # -1..1, znak = kierunek
    confidence: float   # 0..1
    inputs: dict        # cechy, z ktorych powstal - do wyswietlenia w UI


def _clip(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


# ------------------------------------------------------------------ sygnaly
def momentum_multi(f: dict) -> SignalOutput | None:
    """Zgodnosc zwrotow w trzech horyzontach.

    Pojedynczy zwrot to szum. Dopiero zgodnosc 1 h, 4 h i 24 h mowi cokolwiek
    o kierunku - i wlasnie brak zgodnosci obniza tu pewnosc, a nie wynik.
    """
    r1, r4, r24 = f.get("ret_1h"), f.get("ret_4h"), f.get("ret_24h")
    if r1 is None or r4 is None or r24 is None:
        return None
    parts = [_clip(r1 / 1.5), _clip(r4 / 3.0), _clip(r24 / 6.0)]
    score = sum(parts) / 3
    agree = sum(1 for p in parts if (p > 0) == (score > 0)) / 3
    return SignalOutput(_clip(score), agree * min(1.0, abs(score) * 2),
                        {"ret_1h": r1, "ret_4h": r4, "ret_24h": r24, "zgodnosc": agree})


def mean_reversion_rsi(f: dict) -> SignalOutput | None:
    """Skrajne RSI. Kupno przy wyprzedaniu, sprzedaz przy wykupieniu."""
    rsi = f.get("rsi_14")
    if rsi is None:
        return None
    if 35 <= rsi <= 65:
        return SignalOutput(0.0, 0.1, {"rsi_14": rsi, "uwaga": "strefa neutralna"})
    score = _clip((50 - rsi) / 30)
    return SignalOutput(score, min(1.0, abs(50 - rsi) / 30), {"rsi_14": rsi})


def range_extreme(f: dict) -> SignalOutput | None:
    """Pozycja w zakresie doby. Przy krawedziach zaklada powrot do srodka."""
    pos = f.get("range_pos_24h")
    if pos is None:
        return None
    score = _clip((0.5 - pos) * 2.5)
    return SignalOutput(score, min(1.0, abs(pos - 0.5) * 2.4), {"range_pos_24h": pos})


def trend_follow(f: dict) -> SignalOutput | None:
    """Iloraz srednich wykladniczych - kierunek trendu sredniookresowego."""
    ratio = f.get("ema_ratio_20_50")
    if ratio is None:
        return None
    dev = (ratio - 1.0) * 100          # w procentach
    return SignalOutput(_clip(dev / 0.5), min(1.0, abs(dev) / 0.5),
                        {"ema_ratio_20_50": ratio, "odchylenie_pct": dev})


def volume_breakout(f: dict) -> SignalOutput | None:
    """Nietypowy wolumen POTWIERDZA kierunek, sam go nie wyznacza.

    Wolumen bez kierunku jest bezwartosciowy - stad iloczyn z krotkim zwrotem.
    """
    z, r1 = f.get("volume_z_1h"), f.get("ret_1h")
    if z is None or r1 is None:
        return None
    if z < 1.5:
        return SignalOutput(0.0, 0.1, {"volume_z_1h": z, "uwaga": "wolumen w normie"})
    direction = 1.0 if r1 > 0 else -1.0
    return SignalOutput(_clip(direction * min(1.0, (z - 1.5) / 2.5)),
                        min(1.0, (z - 1.5) / 3), {"volume_z_1h": z, "ret_1h": r1})


def news_shock(f: dict) -> SignalOutput | None:
    """Sentyment newsowy juz wazony sila, pewnoscia i NOWOSCIA.

    Nowosc jest w wadze celowo: news, ktory rynek wycenil, ma wartosc zero
    niezaleznie od tego, jak mocno brzmi.
    """
    s = f.get("news_sent_24h")
    if s is None:
        return None
    if abs(s) < 0.05:
        return SignalOutput(0.0, 0.1, {"news_sent_24h": s, "uwaga": "brak istotnych newsów"})
    return SignalOutput(_clip(s / 1.5), min(1.0, abs(s) / 2), {"news_sent_24h": s})


SIGNALS = {
    "momentum_multi":     (momentum_multi,     "momentum",       "Zgodność zwrotów 1 h / 4 h / 24 h"),
    "mean_reversion_rsi": (mean_reversion_rsi, "mean_reversion", "Powrót do średniej przy skrajnym RSI"),
    "range_extreme":      (range_extreme,      "mean_reversion", "Pozycja przy krawędzi zakresu dobowego"),
    "trend_follow":       (trend_follow,       "momentum",       "Kierunek trendu z ilorazu EMA"),
    "volume_breakout":    (volume_breakout,    "flow",           "Nietypowy wolumen potwierdzający kierunek"),
    "news_shock":         (news_shock,         "news",           "Sentyment newsowy ważony nowością"),
}

# Wagi per rezim. ZALOZENIA POCZATKOWE - do zastapienia kalibracja walk-forward.
# Logika: w trendzie ufamy momentum, w konsolidacji powrotom do sredniej,
# przy wysokiej zmiennosci nie ufamy niczemu mocno, a przy szoku newsowym
# news dominuje, bo pozostale sygnaly opisuja rynek sprzed zdarzenia.
DEFAULT_WEIGHTS = {
    "trend_up":   {"momentum_multi": 0.30, "trend_follow": 0.25, "volume_breakout": 0.15,
                   "mean_reversion_rsi": 0.10, "range_extreme": 0.05, "news_shock": 0.15},
    "trend_down": {"momentum_multi": 0.30, "trend_follow": 0.25, "volume_breakout": 0.15,
                   "mean_reversion_rsi": 0.10, "range_extreme": 0.05, "news_shock": 0.15},
    "range":      {"mean_reversion_rsi": 0.30, "range_extreme": 0.25, "momentum_multi": 0.10,
                   "trend_follow": 0.10, "volume_breakout": 0.10, "news_shock": 0.15},
    "high_vol":   {"momentum_multi": 0.15, "trend_follow": 0.15, "mean_reversion_rsi": 0.15,
                   "range_extreme": 0.15, "volume_breakout": 0.20, "news_shock": 0.20},
    "news_shock": {"news_shock": 0.45, "momentum_multi": 0.20, "volume_breakout": 0.20,
                   "trend_follow": 0.10, "mean_reversion_rsi": 0.03, "range_extreme": 0.02},
}


def classify_regime(f: dict, atr_p80: float | None) -> tuple[str, float]:
    """Zwraca (rezim, pewnosc).

    Kolejnosc sprawdzania jest istotna: szok newsowy i wysoka zmiennosc
    przebijaja trend, bo w takich warunkach zwykle zaleznosci przestaja
    obowiazywac - a to wlasnie wtedy modele przeuczone do "normalnego" rynku
    trace najwiecej.
    """
    news = f.get("news_sent_24h") or 0.0
    atr = f.get("atr_pct_14")
    ratio = f.get("ema_ratio_20_50")
    r24 = f.get("ret_24h")

    if abs(news) > 1.2:
        return "news_shock", min(1.0, abs(news) / 2.5)
    if atr is not None and atr_p80 is not None and atr > atr_p80:
        return "high_vol", min(1.0, atr / (atr_p80 * 1.6))
    if ratio is not None and r24 is not None:
        dev = (ratio - 1.0) * 100
        if dev > 0.12 and r24 > 0:
            return "trend_up", min(1.0, dev / 0.4)
        if dev < -0.12 and r24 < 0:
            return "trend_down", min(1.0, abs(dev) / 0.4)
    return "range", 0.6


def evaluate(features: dict, regime: str,
             weights: dict[str, float] | None = None) -> tuple[float, float, list[dict]]:
    """Laczy sygnaly w jeden wynik.

    Zwraca (wynik -1..1, pewnosc 0..1, rozbicie na wklady). Rozbicie jest
    zwracane ZAWSZE, takze dla sygnalow o zerowym wkladzie - dashboard ma
    pokazywac, co zostalo rozwazone, a nie tylko to, co zadzialalo.
    """
    w = weights or DEFAULT_WEIGHTS.get(regime, DEFAULT_WEIGHTS["range"])
    contributions = []
    total = 0.0
    conf_num = conf_den = 0.0

    for name, (fn, family, label) in SIGNALS.items():
        out = fn(features)
        if out is None:
            continue
        weight = w.get(name, 0.0)
        contribution = out.score * weight * out.confidence
        total += contribution
        conf_num += out.confidence * weight
        conf_den += weight
        contributions.append({
            "name": name, "family": family, "label": label,
            "score": round(out.score, 4), "confidence": round(out.confidence, 4),
            "weight": round(weight, 4), "contribution": round(contribution, 5),
            "inputs": out.inputs,
        })

    confidence = (conf_num / conf_den) if conf_den else 0.0
    return _clip(total), min(1.0, confidence), contributions
