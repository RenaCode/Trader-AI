"""Sygnaly i klasyfikacja rezimu rynkowego.

Kazdy sygnal jest prosta, wytlumaczalna hipoteza zwracajaca wartosc w zakresie
-1..1 (kierunek i sila) oraz pewnosc 0..1. Zaden nie jest madry sam z siebie -
madrosc ma sie brac z KALIBRACJI ich wag per rezim, ktora robi warstwa uczenia.

DWIE ZASADY, KTORE TRZYMAJA TEN PLIK NIEZALEZNYM OD HORYZONTU:

1. Sygnaly czytaja ROLE cech (`ret_short`, `rsi`), nie nazwy z horyzontem
   (`ret_short__4h`). Panel podaje im slownik juz przemapowany na role.

2. Zwroty normalizuje sie przez ZMIENNOSC, nie przez stala. Pierwsza wersja
   dzielila zwrot przez wpisane na sztywno 1,5 % / 3 % / 6 % - liczby dobrane
   pod okna 1 h / 4 h / 24 h na swiecy 5-minutowej. Ten sam kod na swiecy
   4-godzinnej dostaje zwroty 24-godzinne, 7- i 30-dniowe, wiec kazda z tych
   stalych nasycalaby sie natychmiast i sygnal zwracalby +-1 niemal zawsze.
   Dzielenie przez oczekiwany ruch (`atr_pct * sqrt(liczba_swiec)`) daje
   wielkosc bezwymiarowa: "ile odchylen standardowych" zamiast "ile procent".
   Dziala tak samo na kazdej swiecy i na kazdym instrumencie - SOL nie wymaga
   innych progow niz BTC.

Wagi ponizej to jawne zalozenia poczatkowe, nie wynik dopasowania do danych.
Sa tu po to, zeby system mial od czego zaczac, i maja zostac zastapione wagami
wyliczonymi z okien walk-forward. Dopoki tak sie nie stanie, kazda decyzja jest
decyzja tych zalozen - i tak nalezy ja czytac.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from services.engine.horizons import Horizon

REGIMES = ("trend_up", "trend_down", "range", "high_vol", "news_shock")


@dataclass(frozen=True)
class SignalOutput:
    score: float        # -1..1, znak = kierunek
    confidence: float   # 0..1
    inputs: dict        # cechy, z ktorych powstal - do wyswietlenia w UI


def _clip(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _expected_move(f: dict, bars: int) -> float | None:
    """Oczekiwany ruch przez `bars` swiec, w procentach.

    Blad losowy skaluje sie z pierwiastkiem czasu, wiec ruch przez n swiec to
    w przyblizeniu zmiennosc jednej swiecy razy sqrt(n). Podloga chroni przed
    dzieleniem przez zero w martwym rynku.
    """
    atr = f.get("atr_pct")
    if atr is None or atr <= 0:
        return None
    return max(atr * math.sqrt(bars), 1e-6)


# ------------------------------------------------------------------ sygnaly
def momentum_multi(f: dict, h: Horizon) -> SignalOutput | None:
    """Zgodnosc zwrotow w trzech horyzontach.

    Pojedynczy zwrot to szum. Dopiero zgodnosc trzech skal mowi cokolwiek
    o kierunku - i wlasnie brak zgodnosci obniza tu pewnosc, a nie wynik.
    """
    vals = [(f.get("ret_short"), h.ret_short), (f.get("ret_mid"), h.ret_mid),
            (f.get("ret_long"), h.ret_long)]
    if any(v is None for v, _ in vals):
        return None
    parts = []
    for v, bars in vals:
        scale = _expected_move(f, bars)
        if scale is None:
            return None
        parts.append(_clip(v / scale))
    score = sum(parts) / 3
    if score == 0:
        return SignalOutput(0.0, 0.1, {"uwaga": "zwroty się znoszą"})
    agree = sum(1 for p in parts if (p > 0) == (score > 0)) / 3
    return SignalOutput(
        _clip(score), agree * min(1.0, abs(score) * 2),
        {f"zwrot_{h.labels['ret_short']}": round(vals[0][0], 3),
         f"zwrot_{h.labels['ret_mid']}": round(vals[1][0], 3),
         f"zwrot_{h.labels['ret_long']}": round(vals[2][0], 3),
         "zgodnosc": round(agree, 2)})


def mean_reversion_rsi(f: dict, h: Horizon) -> SignalOutput | None:
    """Skrajne RSI. Kupno przy wyprzedaniu, sprzedaz przy wykupieniu."""
    r = f.get("rsi")
    if r is None:
        return None
    if 35 <= r <= 65:
        return SignalOutput(0.0, 0.1, {"rsi": round(r, 1), "uwaga": "strefa neutralna"})
    return SignalOutput(_clip((50 - r) / 30), min(1.0, abs(50 - r) / 30), {"rsi": round(r, 1)})


def range_extreme(f: dict, h: Horizon) -> SignalOutput | None:
    """Pozycja w zakresie okresu. Przy krawedziach zaklada powrot do srodka."""
    pos = f.get("range_pos")
    if pos is None:
        return None
    return SignalOutput(_clip((0.5 - pos) * 2.5), min(1.0, abs(pos - 0.5) * 2.4),
                        {"pozycja_w_zakresie": round(pos, 3), "okno": h.labels.get("range")})


def trend_follow(f: dict, h: Horizon) -> SignalOutput | None:
    """Iloraz srednich wykladniczych, mierzony w jednostkach zmiennosci."""
    ratio = f.get("ema_ratio")
    if ratio is None:
        return None
    dev = (ratio - 1.0) * 100
    # Rozjazd srednich porownujemy do ruchu oczekiwanego na dystansie miedzy nimi.
    scale = _expected_move(f, max(1, h.ema_slow - h.ema_fast))
    if scale is None:
        return None
    return SignalOutput(_clip(dev / scale), min(1.0, abs(dev) / scale),
                        {"ema_ratio": round(ratio, 5), "odchylenie_pct": round(dev, 3)})


def volume_breakout(f: dict, h: Horizon) -> SignalOutput | None:
    """Nietypowy wolumen POTWIERDZA kierunek, sam go nie wyznacza.

    Wolumen bez kierunku jest bezwartosciowy - stad iloczyn z krotkim zwrotem.
    """
    z, r = f.get("volume_z"), f.get("ret_short")
    if z is None or r is None:
        return None
    if z < 1.5:
        return SignalOutput(0.0, 0.1, {"volume_z": round(z, 2), "uwaga": "wolumen w normie"})
    direction = 1.0 if r > 0 else -1.0
    return SignalOutput(_clip(direction * min(1.0, (z - 1.5) / 2.5)),
                        min(1.0, (z - 1.5) / 3),
                        {"volume_z": round(z, 2), "zwrot_krotki": round(r, 3)})


def news_shock(f: dict, h: Horizon) -> SignalOutput | None:
    """Sentyment newsowy juz wazony sila, pewnoscia i NOWOSCIA.

    Nowosc jest w wadze celowo: news, ktory rynek wycenil, ma wartosc zero
    niezaleznie od tego, jak mocno brzmi.
    """
    s = f.get("news_sent")
    if s is None:
        return None
    if abs(s) < 0.05:
        return SignalOutput(0.0, 0.1, {"news_sent": round(s, 3),
                                       "uwaga": "brak istotnych newsów"})
    return SignalOutput(_clip(s / 1.5), min(1.0, abs(s) / 2), {"news_sent": round(s, 3)})


def funding_carry(f: dict, h: Horizon) -> SignalOutput | None:
    """Skrajna stawka finansowania perpetuala - sygnal KONTRARIANSKI.

    Dodatni funding znaczy, ze dlugie place krotkim, czyli rynek jest wychylony
    na wzrost i za to wychylenie placi. Skrajnie dodatni funding to obraz tlumu
    lewarowanego w jedna strone - stan, ktory historycznie konczy sie wycisnieciem
    tej strony, nie jej dalszym zyskiem. Stad znak minus.

    To jedyny sygnal w tym zestawie, ktory nie pochodzi z ceny. Ma wlasna,
    niezalezna tresc informacyjna - i wlasnie dlatego wart jest testu.
    """
    z = f.get("funding_z")
    rate = f.get("funding_rate")
    if z is None:
        return None
    if abs(z) < 1.0:
        return SignalOutput(0.0, 0.1, {"funding_z": round(z, 2), "stawka_pct": rate,
                                       "uwaga": "finansowanie w normie"})
    return SignalOutput(_clip(-z / 2.5), min(1.0, (abs(z) - 1.0) / 2.0),
                        {"funding_z": round(z, 2), "stawka_pct": rate})


def cross_section(f: dict, h: Horizon) -> SignalOutput | None:
    """Sila wzgledna wobec calego zbioru rynkow - momentum przekrojowe.

    Wszystkie pozostale sygnaly opisuja instrument SAM ZE SOBA. W rynku, w ktorym
    wszystko porusza sie razem - a krypto jest takim rynkiem - wiekszosc tej
    informacji to wspolny czynnik, czyli po prostu "czy dzis rosnie caly rynek".
    Ranking odejmuje ten czynnik z definicji: gdy wszystko rosnie o 5%, ranking
    dalej mowi, kto rosnie mocniej od reszty.

    To jedyny sygnal w zestawie, ktory patrzy na instrument PRZEZ PRYZMAT
    pozostalych - i dlatego jego tresc informacyjna jest najmniej skorelowana
    z reszta.

    Srodek rankingu jest celowo martwy. Instrument w okolicach mediany nie mowi
    nic o niczym, a bez tej strefy sygnal generowalby transakcje przy kazdym
    drobnym przetasowaniu kolejnosci.
    """
    rs = f.get("rel_strength")
    if rs is None:
        return None
    if abs(rs) < 0.5:                       # srodkowa polowa stawki
        return SignalOutput(0.0, 0.1, {"sila_wzgledna": round(rs, 3),
                                       "uwaga": "środek stawki"})
    # Skalujemy tak, zeby skrajne 25% stawki dawalo pelna sile.
    return SignalOutput(_clip((abs(rs) - 0.5) / 0.5 * (1 if rs > 0 else -1)),
                        min(1.0, (abs(rs) - 0.5) / 0.4),
                        {"sila_wzgledna": round(rs, 3), "okno": h.labels.get("ret_mid")})


SIGNALS = {
    "momentum_multi":     (momentum_multi,     "momentum",       "Zgodność zwrotów w trzech skalach"),
    "mean_reversion_rsi": (mean_reversion_rsi, "mean_reversion", "Powrót do średniej przy skrajnym RSI"),
    "range_extreme":      (range_extreme,      "mean_reversion", "Pozycja przy krawędzi zakresu"),
    "trend_follow":       (trend_follow,       "momentum",       "Kierunek trendu z ilorazu EMA"),
    "volume_breakout":    (volume_breakout,    "flow",           "Nietypowy wolumen potwierdzający kierunek"),
    "news_shock":         (news_shock,         "news",           "Sentyment newsowy ważony nowością"),
    "funding_carry":      (funding_carry,      "derivatives",    "Skrajne finansowanie perpetuala (kontrariańsko)"),
    "cross_section":      (cross_section,      "cross_section",  "Siła względna wobec pozostałych rynków"),
}

# Wagi per rezim. ZALOZENIA POCZATKOWE - do zastapienia kalibracja walk-forward.
# Logika: w trendzie ufamy momentum, w konsolidacji powrotom do sredniej,
# przy wysokiej zmiennosci nie ufamy niczemu mocno, a przy szoku newsowym
# news dominuje, bo pozostale sygnaly opisuja rynek sprzed zdarzenia.
DEFAULT_WEIGHTS = {
    "trend_up":   {"momentum_multi": 0.24, "trend_follow": 0.19, "volume_breakout": 0.11,
                   "mean_reversion_rsi": 0.07, "range_extreme": 0.03, "news_shock": 0.11,
                   "funding_carry": 0.10, "cross_section": 0.15},
    "trend_down": {"momentum_multi": 0.24, "trend_follow": 0.19, "volume_breakout": 0.11,
                   "mean_reversion_rsi": 0.07, "range_extreme": 0.03, "news_shock": 0.11,
                   "funding_carry": 0.10, "cross_section": 0.15},
    "range":      {"mean_reversion_rsi": 0.22, "range_extreme": 0.19, "momentum_multi": 0.07,
                   "trend_follow": 0.07, "volume_breakout": 0.08, "news_shock": 0.10,
                   "funding_carry": 0.13, "cross_section": 0.14},
    "high_vol":   {"momentum_multi": 0.11, "trend_follow": 0.11, "mean_reversion_rsi": 0.11,
                   "range_extreme": 0.11, "volume_breakout": 0.15, "news_shock": 0.14,
                   "funding_carry": 0.13, "cross_section": 0.14},
    "news_shock": {"news_shock": 0.38, "momentum_multi": 0.16, "volume_breakout": 0.15,
                   "trend_follow": 0.08, "mean_reversion_rsi": 0.03, "range_extreme": 0.02,
                   "funding_carry": 0.08, "cross_section": 0.10},
}


def classify_regime(f: dict, atr_p80: float | None) -> tuple[str, float]:
    """Zwraca (rezim, pewnosc).

    Kolejnosc sprawdzania jest istotna: szok newsowy i wysoka zmiennosc
    przebijaja trend, bo w takich warunkach zwykle zaleznosci przestaja
    obowiazywac - a to wlasnie wtedy modele przeuczone do "normalnego" rynku
    trace najwiecej.
    """
    news = f.get("news_sent") or 0.0
    atr = f.get("atr_pct")
    ratio = f.get("ema_ratio")
    r_long = f.get("ret_long")

    if abs(news) > 1.2:
        return "news_shock", min(1.0, abs(news) / 2.5)
    if atr is not None and atr_p80 is not None and atr > atr_p80:
        return "high_vol", min(1.0, atr / (atr_p80 * 1.6))
    if ratio is not None and r_long is not None and atr is not None and atr > 0:
        # Prog rozjazdu srednich tez skalujemy zmiennoscia - inaczej "trend"
        # znaczylby co innego na BTC i co innego na SOL.
        dev = (ratio - 1.0) * 100 / atr
        if dev > 0.25 and r_long > 0:
            return "trend_up", min(1.0, dev / 0.8)
        if dev < -0.25 and r_long < 0:
            return "trend_down", min(1.0, abs(dev) / 0.8)
    return "range", 0.6


def precompute(features: dict, horizon: Horizon) -> dict[str, SignalOutput]:
    """Wylicza WSZYSTKIE sygnaly raz, bez wag.

    Sygnaly nie zaleza od wag - zalezy od nich dopiero ich zsumowanie. Trener
    sprawdza kilkadziesiat ukladow wag na tym samym oknie i pierwotnie liczyl
    komplet sygnalow od nowa dla kazdego z nich. Przy dwudziestu czterech
    instrumentach, czterdziestu oknach i szescdziesieciu probach dawalo to
    kilkaset milionow zbednych wywolan - caly przebieg trwal okolo czterdziestu
    minut, z czego wiekszosc to bylo liczenie w kolko tego samego.

    Wynik tej funkcji jest niezmienny dla danej swiecy, wiec da sie go policzyc
    raz i uzyc przy kazdej probie.
    """
    out = {}
    for name, (fn, _family, _label) in SIGNALS.items():
        res = fn(features, horizon)
        if res is not None:
            out[name] = res
    return out


def combine(precomputed: dict[str, SignalOutput], regime: str,
            weights: dict[str, float] | None,
            with_contributions: bool = True) -> tuple[float, float, list[dict]]:
    """Skleja policzone wczesniej sygnaly w jeden wynik wedlug wag.

    Musi dawac DOKLADNIE to samo co evaluate() - jest na to test.

    `with_contributions=False` pomija budowanie rozbicia na wklady. Rozbicie jest
    potrzebne WYLACZNIE do zapisu w dzienniku i pokazania w interfejsie; symulacja
    walk-forward go nie czyta, a budowala je przy kazdej swiecy kazdej proby.
    Profiler pokazal 4,3 miliona wywolan round() w jednym przebiegu - okolo 30%
    calego czasu szlo na zaokraglanie liczb, ktore natychmiast ladowaly w koszu.
    """
    w = weights or DEFAULT_WEIGHTS.get(regime, DEFAULT_WEIGHTS["range"])
    total = 0.0
    conf_num = conf_den = 0.0

    if not with_contributions:
        for name, out in precomputed.items():
            weight = w.get(name, 0.0)
            total += out.score * weight * out.confidence
            conf_num += out.confidence * abs(weight)
            conf_den += abs(weight)
        confidence = (conf_num / conf_den) if conf_den else 0.0
        return _clip(total), min(1.0, confidence), []

    contributions = []
    for name, (_fn, family, label) in SIGNALS.items():
        out = precomputed.get(name)
        if out is None:
            continue
        weight = w.get(name, 0.0)
        contribution = out.score * weight * out.confidence
        total += contribution
        # Wartosc bezwzgledna: sygnal uzyty ODWROTNIE nadal wnosi tyle samo
        # pewnosci co uzyty wprost - odwrocenie zmienia kierunek, nie sile.
        conf_num += out.confidence * abs(weight)
        conf_den += abs(weight)
        contributions.append({
            "name": name, "family": family, "label": label,
            "score": round(out.score, 4), "confidence": round(out.confidence, 4),
            "weight": round(weight, 4), "contribution": round(contribution, 5),
            "inputs": out.inputs,
        })

    confidence = (conf_num / conf_den) if conf_den else 0.0
    return _clip(total), min(1.0, confidence), contributions


def evaluate(features: dict, regime: str, weights: dict[str, float] | None,
             horizon: Horizon) -> tuple[float, float, list[dict]]:
    """Laczy sygnaly w jeden wynik.

    Zwraca (wynik -1..1, pewnosc 0..1, rozbicie na wklady). Rozbicie jest
    zwracane ZAWSZE, takze dla sygnalow o zerowym wkladzie - dashboard ma
    pokazywac, co zostalo rozwazone, a nie tylko to, co zadzialalo.
    """
    return combine(precompute(features, horizon), regime, weights)
