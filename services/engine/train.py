#!/usr/bin/env python3
"""Trener walk-forward: kalibracja wag sygnalow per rezim.

Protokol jest jedyny dopuszczalny w tym projekcie i celowo niewygodny:

  okno 1:  [--- trening 14 dni ---][test 2 dni]
  okno 2:      [--- trening 14 dni ---][test 2 dni]
  ...

Wagi dobierane sa WYLACZNIE na oknie treningowym, a wynik raportowany WYLACZNIE
z okna testowego, ktorego optymalizacja nigdy nie widziala. Zwykla walidacja
krzyzowa jest tu niewazna, bo tasowanie szeregu czasowego wpuszcza przyszlosc
do treningu.

Kazda sprawdzona konfiguracja laduje w learn.hypothesis_trial. Bez tego licznika
deflated Sharpe jest nieobliczalny, a im wiecej konfiguracji sie przetestuje, tym
latwiej trafic dobry wynik czystym przypadkiem. Licznik jest po to, zeby nie dalo
sie oszukac samego siebie selekcja.

Uzycie:
    DATABASE_URL=... python services/engine/train.py --days 90 --trials 40

    # porownanie wariantow doboru wag, bez dotykania bazy:
    ... python services/engine/train.py --sucho --wybor ic
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg
from psycopg.rows import dict_row

from services.engine.horizons import DEFAULT_BAR, HORIZONS, Horizon, get as get_horizon
from services.engine.portfolio import (
    Egzekucja, kup_i_trzymaj, metrics as portfolio_metrics, precompute_panels,
    simulate_portfolio,
)
from services.engine.statystyka import spearman, wilson
from services.engine.signals import (
    DEFAULT_WEIGHTS, REGIMES, SIGNALS, classify_regime, combine, evaluate,
)

# Prog wejscia NIE jest stala. Jest percentylem rozkladu |wyniku| policzonego
# na oknie TRENINGOWYM - a wiec dobieranym pod zadana CZESTOTLIWOSC handlu,
# nigdy pod zwrot. To rozroznienie jest tu najwazniejsze: dobieranie progu az
# wynik wyjdzie dodatni to definicja przeuczenia, natomiast powiedzenie
# "chce handlowac w 12% najsilniejszych sytuacji" jest decyzja podjeta przed
# zobaczeniem jakiegokolwiek wyniku.
#
# Stala 0,25 dobrana byla pod rozklad wynikow ze swiecy 5-minutowej. Po zmianie
# normalizacji na bezwymiarowa rozklad sie przesunal i ta sama stala dawala
# 2 transakcje na okno testowe - za malo, by cokolwiek zmierzyc. Percentyl
# przenosi sie miedzy horyzontami sam.
ENTER_PCTL = 0.88
EXIT_RATIO = 0.45          # wyjscie przy spadku sily sygnalu do 45% progu wejscia
MIN_CONF = 0.25

# Minimum informatywnych okien, ponizej ktorego NIE WOLNO orzec "zwalidowane".
# Bez tego jedno dodatnie okno przy 40 pominietych dawalo 100% skutecznosci
# i status otwierajacy droge do handlu na zywo.
MIN_WINDOWS_FOR_VALIDATION = 20

# Minimalna liczba instrumentow, ktore musza byc notowane w danym oknie.
# Portfel z garstki rynkow to inna strategia niz portfel z dwudziestu czterech.
MIN_INSTRUMENTS = 8
FEE = 0.00055
SLIP = 0.0004

SEED = 20260827


# ------------------------------------------------------------------ dane
def load_panel(cur, instrument_id: int, h: Horizon,
               since: datetime, until: datetime) -> list[dict]:
    """Panel cech w ukladzie czas -> {rola: wartosc}, plus cena swiecy.

    Cechy filtrujemy po `params->>'bar'`, wiec panel horyzontu 4 h nigdy nie
    zobaczy cech policzonych na swiecy 5-minutowej. Klucze slownika to ROLE,
    dzieki czemu sygnaly nie musza znac horyzontu.

    STRAZNIK BITEMPORALNY - `fv.ingested_at <= fv.event_time + DWIE swiece`:

    Znacznik `event_time` cechy to POCZATEK swiecy, a wartosc liczy sie z jej
    ZAMKNIECIA - czyli jest znana dopiero po jednej swiecy. Druga swieca to
    zapas na policzenie i zapis. Cecha, ktora trafila do bazy pozniej, w tamtym
    momencie NIE byla dostepna i nie wolno na niej trenowac.

    Wczesniejsza wersja tego zapytania nie filtrowala po `ingested_at` w ogole,
    a potok datowal cechy na `poczatek_swiecy + 3 sekundy` - twierdzil wiec, ze
    wartosc byla znana trzy sekundy po OTWARCIU swiecy, choc pochodzila z jej
    zamkniecia cztery godziny pozniej. Straznik przepuszczal to bez mrugniecia,
    bo sam byl dobrany pod te bledna date.
    """
    if h.aggregate:
        price_cte = """
            SELECT time_bucket(%(bar)s::interval, event_time) AS t,
                   (array_agg(close ORDER BY event_time DESC))[1] AS close,
                   max(high) AS high, min(low) AS low
            FROM market.candle
            WHERE instrument_id = %(iid)s AND timeframe = %(tf)s
              AND event_time BETWEEN %(since)s AND %(until)s
            GROUP BY 1
        """
    else:
        price_cte = """
            SELECT event_time AS t, close, high, low
            FROM market.candle
            WHERE instrument_id = %(iid)s AND timeframe = %(tf)s
              AND event_time BETWEEN %(since)s AND %(until)s
        """

    cur.execute(
        f"""
        WITH f AS (
            SELECT fv.event_time, fd.params->>'role' AS role, fv.value
            FROM feat.feature_value fv
            JOIN feat.feature_def fd ON fd.id = fv.feature_def_id
            WHERE fv.instrument_id = %(iid)s
              AND fv.event_time BETWEEN %(since)s AND %(until)s
              AND fd.params->>'bar' = %(bar_name)s
              AND fv.ingested_at <= fv.event_time + 2 * %(bar)s::interval
        ),
        p AS ({price_cte})
        SELECT f.event_time AS t, jsonb_object_agg(f.role, f.value) AS feats,
               p.close, p.high, p.low
        FROM f JOIN p ON p.t = f.event_time
        GROUP BY f.event_time, p.close, p.high, p.low ORDER BY f.event_time
        """,
        {"iid": instrument_id, "since": since, "until": until, "tf": h.source_timeframe,
         "bar": f"{h.bar_minutes} minutes", "bar_name": h.bar},
    )
    return [{"t": r["t"], "f": {k: float(v) for k, v in r["feats"].items()},
             "price": float(r["close"]), "high": float(r["high"]),
             "low": float(r["low"])} for r in cur.fetchall()]


def atr_threshold(panel: list[dict]) -> float | None:
    vals = [r["f"]["atr_pct"] for r in panel if "atr_pct" in r["f"]]
    if len(vals) < 50:
        return None
    vals.sort()
    return vals[int(len(vals) * 0.8)]


# ------------------------------------------------------------------ symulacja
def score_threshold(panel: list[dict], weights: dict[str, dict[str, float]],
                    atr_p80: float | None, h: Horizon,
                    cache: list[tuple] | None = None) -> tuple[float, float]:
    """Progi wejscia i wyjscia z rozkladu |wyniku| na TYM panelu.

    Wolno to policzyc wylacznie na oknie treningowym. Policzone na testowym
    byloby zagladaniem w przyszlosc - prog znalby wtedy rozklad, ktorego
    w tamtym momencie nie dalo sie znac.
    """
    scores = []
    for i, row in enumerate(panel):
        if cache is not None:
            regime, sygnaly = cache[i]
            sc, conf, _ = combine(sygnaly, regime, weights.get(regime), with_contributions=False)
        else:
            regime, _ = classify_regime(row["f"], atr_p80)
            sc, conf, _ = evaluate(row["f"], regime, weights.get(regime), h)
        if conf >= MIN_CONF:
            scores.append(abs(sc))
    if len(scores) < 50:
        return 0.25, 0.12
    scores.sort()
    enter = scores[min(len(scores) - 1, int(len(scores) * ENTER_PCTL))]
    enter = max(enter, 1e-4)
    return enter, enter * EXIT_RATIO


def martwe_sygnaly(cache: dict[str, list[tuple]], prog: float = 0.01) -> set[str]:
    """Sygnaly, ktore na tym oknie NIE ZMIENIAJA sie wcale.

    Przyklad z produkcji: `news_shock` bez klucza Anthropic zwraca zero przy
    kazdej swiecy. Losowe przeszukiwanie wag przypisalo mu mimo to 0,169 w rezimie
    konsolidacji - najwiecej ze wszystkich sygnalow w tym rezimie.

    Nie bylo to obojetne. Martwy sygnal:
      * zjada czesc budzetu wag, rozcienczajac te, ktore cos wnosza,
      * ZANIZA PEWNOSC, bo wchodzi do mianownika (conf_den += weight) z wlasna
        pewnoscia 0,1 - a pewnosc to wlasnie ta wielkosc, ktorej kalibracje
        mierzymy,
      * dostaje wage zupelnie przypadkowa, ktora w dniu wpiecia klucza nagle
        zaczyna dzialac, nigdy nie bedac skalibrowana.

    Sygnal uznajemy za martwy, gdy niezerowy wynik pojawia sie rzadziej niz
    w `prog` czesci swiec.
    """
    liczniki = {name: 0 for name in SIGNALS}
    razem = 0
    for wpisy in cache.values():
        for _regime, sygnaly in wpisy:
            razem += 1
            for name, out in sygnaly.items():
                if abs(out.score) > 1e-9:
                    liczniki[name] += 1
    if razem == 0:
        return set()
    return {name for name, n in liczniki.items() if n / razem < prog}


def _bez_martwych(wagi: dict[str, dict[str, float]],
                  martwe: set[str]) -> dict[str, dict[str, float]]:
    """Zeruje wagi martwych sygnalow i renormalizuje reszte.

    Normalizacja po sumie WARTOSCI BEZWZGLEDNYCH, bo wagi moga byc ujemne -
    suma zwykla moglaby wyjsc bliska zeru i rozdmuchac pozostale wagi.
    """
    if not martwe:
        return wagi
    out = {}
    for regime, w in wagi.items():
        total = sum(abs(v) for k, v in w.items() if k not in martwe) or 1.0
        out[regime] = {k: (0.0 if k in martwe else round(w[k] / total, 4)) for k in w}
    return out


def ic_weights(panels: dict[str, list[dict]], cache: dict[str, list[tuple]],
               martwe: set[str], horyzont_swiec: int = 3) -> dict[str, dict[str, float]]:
    """Wagi WYLICZONE ze zdolnosci predykcyjnej sygnalow, nie wylosowane.

    DLACZEGO LOSOWE PRZESZUKIWANIE NIE DZIALALO

    Przestrzen wag ma 40 wymiarow (8 sygnalow x 5 rezimow). Szescdziesiat
    losowych prob to w takiej przestrzeni nic - "uczenie" sprowadzalo sie do
    wybrania najlepszego z 60 losowych punktow.

    Widac to bylo wprost: po dopuszczeniu wag ujemnych tylko 48% znakow wag
    zgadzalo sie ze zmierzonym wspolczynnikiem informacji, czyli dokladnie tyle,
    ile daja znaki losowe. Sygnal volume_breakout ma IC -0,039 - najmocniejsze
    w calym zestawie - a losowanie przypisalo mu +0,395 w rezimie trendu, czyli
    odwrotnie.

    ZAMIAST SZUKAC PO OMACKU - POLICZYC

    Waga sygnalu proporcjonalna do jego korelacji rangowej z przyszlym zwrotem,
    zmierzonej NA OKNIE TRENINGOWYM. To estymator o niskiej wariancji, ktory
    uzywa wszystkich obserwacji z okna, zamiast probkowac 60 punktow
    z czterdziestowymiarowej kostki.

    Znak wychodzi sam: sygnal o ujemnym IC dostaje ujemna wage, czyli zostaje
    odwrocony - bez zgadywania.

    NADAL OUT-OF-SAMPLE: IC liczy sie wylacznie z okna treningowego, a wynik
    ocenia sie na testowym, ktorego ta funkcja nigdy nie oglada.
    """
    # {rezim: {sygnal: ([wyniki], [zwroty])}}
    zebrane: dict[str, dict[str, tuple[list, list]]] = {}
    for sym, rows in panels.items():
        ceny = [r["price"] for r in rows]
        for i, (regime, sygnaly) in enumerate(cache[sym]):
            j = i + 1 + horyzont_swiec
            if j >= len(ceny) or ceny[i + 1] == 0:
                continue
            zwrot = ceny[j] / ceny[i + 1] - 1
            for name, out in sygnaly.items():
                if name in martwe or out.score == 0.0:
                    continue
                w = zebrane.setdefault(regime, {}).setdefault(name, ([], []))
                w[0].append(out.score)
                w[1].append(zwrot)

    # IC liczone na calym oknie - zapas dla rezimow o malej liczbie swiec.
    ogolne: dict[str, tuple[list, list]] = {}
    for per_sig in zebrane.values():
        for name, (xs, ys) in per_sig.items():
            w = ogolne.setdefault(name, ([], []))
            w[0].extend(xs)
            w[1].extend(ys)

    out: dict[str, dict[str, float]] = {}
    for regime in REGIMES:
        surowe = {}
        for name in SIGNALS:
            if name in martwe:
                surowe[name] = 0.0
                continue
            xs, ys = zebrane.get(regime, {}).get(name, ([], []))
            ic = spearman(xs, ys)
            if ic is None:
                xs, ys = ogolne.get(name, ([], []))
                ic = spearman(xs, ys)
            surowe[name] = ic or 0.0
        total = sum(abs(v) for v in surowe.values())
        if total < 1e-9:
            out[regime] = dict(DEFAULT_WEIGHTS[regime])
        else:
            out[regime] = {k: round(v / total, 4) for k, v in surowe.items()}
    return out


def random_weights(rng: random.Random) -> dict[str, dict[str, float]]:
    """Losowy zestaw wag per rezim, znormalizowany do sumy WARTOSCI BEZWZGLEDNYCH.

    WAGI MOGA BYC UJEMNE - i to jest zmiana, nie drobiazg.

    Poprzednia wersja losowala z przedzialu [0,1), wiec kalibracja mogla sygnal
    najwyzej WYCISZYC, nigdy ODWROCIC. Przy nieujemnych wagach zdanie "ten
    sygnal trafnie porzadkuje sytuacje, tylko w druga strone" bylo niewyrazalne.

    Pomiar wspolczynnika informacji (services/engine/diagnose_signals.py) pokazal,
    ze to ograniczenie kosztuje. Na czterech latach i 24 instrumentach:

        mean_reversion_rsi  IC = +0,036     powrot do sredniej
        range_extreme       IC = +0,019     powrot do sredniej
        cross_section       IC = -0,015     momentum
        momentum_multi      IC = -0,019     momentum
        trend_follow        IC = -0,022     momentum
        volume_breakout     IC = -0,039     momentum

    Wzor jest jednoznaczny: KAZDY sygnal powrotu do sredniej ma IC dodatnie,
    KAZDY sygnal momentum - ujemne. Skladniki niosa informacje; cztery z nich
    niosa ja z odwroconym znakiem. Nieujemne wagi pozwalaly tylko zblizyc te
    cztery do zera, przez co dodatnie i ujemne wklady sie znosily - i stad
    wynik dokladnie 50% dodatnich okien.

    CENA: przestrzen hipotez rosnie dwukrotnie, bo doszedl wybor znaku przy
    kazdym sygnale. Walk-forward placi za to uczciwie - znak wybierany jest
    WYLACZNIE na oknie treningowym, a oceniany poza nim. Licznik hipotez rosnie
    odpowiednio i przy kazdym przyszlym wyniku trzeba go czytac razem z nim.

    Normalizacja idzie po sumie wartosci bezwzglednych, wiec laczny "budzet"
    wagi pozostaje taki sam jak przy wagach nieujemnych.
    """
    out = {}
    for regime in REGIMES:
        raw = {name: rng.uniform(-1.0, 1.0) for name in SIGNALS}
        total = sum(abs(v) for v in raw.values()) or 1.0
        out[regime] = {k: round(v / total, 4) for k, v in raw.items()}
    return out


# ------------------------------------------------------------------ trening
def train(conn, engine: str, days: int, trials_per_window: int, h: Horizon,
          kind: str = "spot", egzekucja: Egzekucja | None = None,
          sucho: bool = False, wybor: str = "argmax",
          tylko: set[str] | None = None) -> None:
    cur = conn.cursor()
    rng = random.Random(SEED)
    now = datetime.now(timezone.utc)

    # `tradeable` decyduje o uniwersum produkcyjnym - ta sama flaga, ktora czyta
    # petla decyzyjna (0020). Flaga --instrumenty sluzy WYLACZNIE eksperymentom
    # i zaweza to, co juz z bazy wyszlo; nie potrafi dodac niczego z powrotem.
    cur.execute("SELECT id, symbol FROM ref.instrument WHERE engine=%s AND active"
                " AND tradeable AND kind <> 'perp' ORDER BY symbol", (engine,))
    instruments = cur.fetchall()
    # NADPISANIE uniwersum na potrzeby eksperymentu.
    #
    # Bez tej flagi trener bierze uniwersum decyzyjne z bazy (`tradeable`) - to
    # samo, ktore obstawia petla. Z flaga wybiera sposrod WSZYSTKICH aktywnych,
    # takze niehandlowalnych, bo inaczej nie dalo by sie porownac uniwersum
    # kandydujacego z obowiazujacym PRZED jego wlaczeniem. Porownanie ma
    # poprzedzac przelaczenie, nie odwrotnie.
    #
    # Dlatego flaga wymaga --sucho: przebieg na uniwersum, ktorego produkcja nie
    # obstawia, nie moze promowac wersji modelu.
    if tylko:
        cur.execute("SELECT id, symbol FROM ref.instrument WHERE engine=%s AND active"
                    " AND kind <> 'perp' ORDER BY symbol", (engine,))
        instruments = cur.fetchall()
        znane = {i["symbol"] for i in instruments}
        brakujace = sorted(set(tylko) - znane)
        if brakujace:
            print(f"  pominięte, brak w bazie: {', '.join(brakujace)}")
        instruments = [i for i in instruments if i["symbol"] in tylko]
    if not instruments:
        print("brak instrumentow")
        return
    if tylko:
        print(f"  uniwersum zawężone do {len(instruments)}: "
              f"{', '.join(i['symbol'] for i in instruments)}")

    cur.execute(
        "INSERT INTO learn.model (name, engine, kind, description) VALUES (%s,%s,%s,%s)"
        " ON CONFLICT (name) DO UPDATE SET kind = EXCLUDED.kind RETURNING id",
        (f"{engine}_signal_weighting_{h.bar}", engine, "signal_weighting",
         f"Kalibracja wag sygnałów per reżim rynkowy metodą walk-forward, świeca {h.bar}"))
    model_id = cur.fetchone()["id"]

    cur.execute("SELECT COALESCE(max(version), 0) + 1 AS v FROM learn.model_version"
                " WHERE model_id = %s", (model_id,))
    version = cur.fetchone()["v"]

    cur.execute("SELECT array_agg(id) AS ids FROM feat.feature_def"
                " WHERE active AND params->>'bar' = %s", (h.bar,))
    feature_ids = cur.fetchone()["ids"] or []
    cur.execute("SELECT array_agg(id) AS ids FROM sig.signal_def WHERE engine = %s", (engine,))
    signal_ids = cur.fetchone()["ids"] or []

    cur.execute(
        "INSERT INTO learn.model_version (model_id, version, status, params, feature_def_ids,"
        " signal_def_ids, trained_from, trained_to) VALUES (%s,%s,'training',%s,%s,%s,%s,%s)"
        " RETURNING id",
        (model_id, version,
         json.dumps({"bar": h.bar, "train_days": h.train_days, "test_days": h.test_days,
                     "step_days": h.step_days, "min_trades": h.min_trades,
                     "trials_per_window": trials_per_window, "seed": SEED,
                     "wybor": wybor}),
         feature_ids, signal_ids, now - timedelta(days=days), now))
    mv_id = cur.fetchone()["id"]

    cur.execute(
        "INSERT INTO learn.training_run (model_version_id, train_from, train_to, status)"
        " VALUES (%s,%s,%s,'running') RETURNING id",
        (mv_id, now - timedelta(days=days), now))
    run_id = cur.fetchone()["id"]
    conn.commit()

    # Panel wczytujemy RAZ na instrument - to najdrozsza operacja w calym treningu.
    panels = {}
    inst_ids = {inst["symbol"]: inst["id"] for inst in instruments}
    for inst in instruments:
        p = load_panel(cur, inst["id"], h, now - timedelta(days=days), now)
        if len(p) > 500:
            panels[inst["symbol"]] = p
        print(f"  {inst['symbol']:10s} {len(p):>7,} punktów panelu")
    if not panels:
        print("za mało danych do treningu")
        cur.execute("UPDATE learn.training_run SET status='failed', finished_at=now(),"
                    " error='za mało punktów panelu' WHERE id=%s", (run_id,))
        conn.commit()
        return

    idx = 0
    positive = 0
    gate_history: list[float] = []
    weight_history: list[dict] = []
    wybrane_ic = 0
    # Porownanie z nierobieniem niczego. Bez tego odsetek dodatnich okien nie
    # mowi, czy strategia jest LEPSZA od kupienia i trzymania - a strategia
    # wylacznie dluga na rosnacym rynku ma duzo dodatnich okien z samej bety.
    pokonane_odniesienie = 0
    porownywalne = 0
    all_sharpes: list[float] = []
    worst_dd = 0.0
    predictions: list[tuple[float, bool]] = []
    start = now - timedelta(days=days)

    while start + timedelta(days=h.train_days + h.test_days) <= now:
        train_from = start
        train_to = start + timedelta(days=h.train_days)
        test_from = train_to
        test_to = test_from + timedelta(days=h.test_days)

        # Instrument, ktory w tym oknie jeszcze nie byl notowany, ZOSTAJE POMINIETY -
        # nie kasuje calego okna.
        #
        # Poprzednia wersja sprawdzala min() po WSZYSTKICH instrumentach, wiec
        # jeden mlody szereg (SUI, TIA i SEI weszly na gielde w 2023) zerowal
        # warunek i wyrzucal komplet wczesniejszych okien. Przy 24 instrumentach
        # kosztowalo to dziewiec okien z czterdziestu - czyli prawie czwarta
        # czesc dowodow, i to bez zadnego komunikatu.
        min_train_bars = max(100, h.train_days * 1440 // h.bar_minutes // 4)
        aktywne = [s for s, p in panels.items()
                   if sum(1 for r in p if train_from <= r["t"] < train_to) >= min_train_bars]
        # Portfel z garstki instrumentow to juz inna strategia, nie ta sama
        # w chudszym okresie - ponizej tego progu okno naprawde nie niesie
        # informacji o tym, co testujemy.
        if len(aktywne) < MIN_INSTRUMENTS:
            start += timedelta(days=h.step_days)
            continue

        train_slices = {s: [r for r in panels[s] if train_from <= r["t"] < train_to]
                        for s in aktywne}
        test_slices = {s: [r for r in panels[s] if test_from <= r["t"] < test_to]
                       for s in aktywne}

        thresholds = {s: atr_threshold(train_slices[s]) for s in aktywne}
        # Rezimy i sygnaly liczymy RAZ na okno. Bez tego kazda z prob liczyla
        # komplet sygnalow od nowa, choc nie zaleza one od wag.
        cache_tr = precompute_panels(train_slices, thresholds, h)
        # Sygnaly stale na tym oknie wykluczamy z kalibracji - patrz
        # komentarz przy martwe_sygnaly().
        martwe = martwe_sygnaly(cache_tr)
        if martwe and idx == 0:
            print(f"  sygnały bez zmienności na danych, wyłączone z kalibracji: "
                  f"{', '.join(sorted(martwe))}")
        cache_te = precompute_panels({s: v for s, v in test_slices.items() if v},
                                     thresholds, h)
        # Progi wyniku ustalane raz na okno, z zalozen poczatkowych i wylacznie
        # na danych treningowych. Te same progi obowiazuja wszystkie hipotezy
        # w tym oknie - inaczej kazda konfiguracja dostawalaby wlasny prog
        # dobrany pod siebie, co jest kolejnym stopniem swobody do przeuczenia.
        #
        # Wagi bazowe MUSZA byc te same, ktorymi potem sie handluje, czyli juz
        # po wyzerowaniu martwych sygnalow. Liczenie progu z pelnych zalozen,
        # a handlowanie wagami po renormalizacji dawalo wyniki systematycznie
        # wieksze od progu - liczba transakcji na okno skoczyla wtedy z ~40 do
        # ~120, co nie bylo zmiana strategii, tylko rozjazdem miedzy progiem
        # a tym, co go przekracza.
        bazowe = _bez_martwych(DEFAULT_WEIGHTS, martwe)
        gates = {s: score_threshold(train_slices[s], bazowe, thresholds[s], h,
                                    cache_tr[s])
                 for s in aktywne}

        # --- dobor wag WYLACZNIE na oknie treningowym ------------------------
        # Celem jest Sharpe CALEGO PORTFELA, nie srednia z Sharpe'ow liczonych
        # osobno per instrument. Roznica nie jest kosmetyczna: portfel podlega
        # limitowi lacznej ekspozycji i kill-switchowi, wiec uklad wag, ktory
        # wygląda dobrze na kazdym instrumencie z osobna, moze byc niewykonalny,
        # gdy wszystkie chca wejsc jednoczesnie.
        def ocen(w, sl, c):
            res = simulate_portfolio(sl, w, thresholds, gates, h, MIN_CONF,
                                     instrument_ids=inst_ids, cache=c,
                                     kind=kind, egzekucja=egzekucja)
            return portfolio_metrics(res, h.min_trades)

        def zapisz_hipoteze(cand):
            if sucho:
                return
            cfg_hash = hashlib.sha256(json.dumps(cand, sort_keys=True).encode()).digest()
            cur.execute(
                "INSERT INTO learn.hypothesis_trial (model_id, config_hash, params,"
                " n_windows) VALUES (%s,%s,%s,1)"
                " ON CONFLICT (model_id, config_hash) DO NOTHING",
                (model_id, cfg_hash, json.dumps(cand)))

        # WARIANT WYBORU. Domyslny `argmax` bierze najlepszego z trials_per_window
        # losowan, zalozen poczatkowych i wag z IC - czyli MAKSIMUM Z PROBKI.
        # Maksimum z probki jest na oknie treningowym zawyzone o klatwe zwyciezcy
        # i tym bardziej, im wiecej losowan; poza oknem to zawyzenie znika, wiec
        # samo "wygral kandydat X" nic nie mowi o jego wartosci.
        #
        # `ic` i `bazowe` nie wybieraja niczego - biora jeden kandydat wyliczony
        # albo ustalony z gory - wiec nie maja czego zawyzac. Roznica OOS miedzy
        # wariantami na TYCH SAMYCH oknach, progach i bramkach jest jedynym
        # uczciwym pomiarem tego, czy losowe przeszukiwanie cokolwiek wnosi.
        if wybor == "bazowe":
            best_w = bazowe
        elif wybor == "ic":
            best_w = ic_weights(train_slices, cache_tr, martwe)
            zapisz_hipoteze(best_w)
            wybrane_ic += 1
        else:
            best_w, best_score = DEFAULT_WEIGHTS, -1e9
            for _ in range(trials_per_window):
                cand = _bez_martwych(random_weights(rng), martwe)
                score = ocen(cand, train_slices, cache_tr)["sharpe"]
                zapisz_hipoteze(cand)
                if score > best_score:
                    best_w, best_score = cand, score

            # Zalozenia poczatkowe tez sa kandydatem - jesli losowanie nie potrafi ich
            # pobic na oknie treningowym, to znaczy, ze nie ma czego kalibrowac.
            base_score = ocen(bazowe, train_slices, cache_tr)["sharpe"]
            if base_score > best_score:
                best_w, best_score = bazowe, base_score

            # Kandydat WYLICZONY, nie wylosowany: wagi proporcjonalne do zdolnosci
            # predykcyjnej sygnalow na tym oknie. Patrz komentarz przy ic_weights().
            ic_w = ic_weights(train_slices, cache_tr, martwe)
            ic_score = ocen(ic_w, train_slices, cache_tr)["sharpe"]
            zapisz_hipoteze(ic_w)
            if ic_score > best_score:
                best_w, best_score = ic_w, ic_score
                wybrane_ic += 1

        # --- ocena OUT-OF-SAMPLE --------------------------------------------
        oos_res = simulate_portfolio(
            {s: v for s, v in test_slices.items() if v}, best_w, thresholds, gates, h,
            MIN_CONF, instrument_ids=inst_ids, cache=cache_te,
            kind=kind, egzekucja=egzekucja)
        oos_m = portfolio_metrics(oos_res, h.min_trades)
        if not oos_res.equity_curve:
            start += timedelta(days=h.step_days)
            continue

        # Transakcje pochodza z JEDNEJ symulacji portfela, wiec sa juz wspolne
        # dla wszystkich instrumentow i podlegaly wspolnym limitom.
        #
        # ZASTRZEZENIE, ktore trzeba czytac razem z wynikiem: instrumenty krypto
        # sa silnie skorelowane, wiec te transakcje nie sa niezalezne. Efektywna
        # liczba obserwacji jest MNIEJSZA niz ich liczba, a istotnosc
        # statystyczna odpowiednio nizsza, niz sugerowalby sam licznik.
        n_trades = oos_m["n_trades"]
        informative = not oos_m.get("insufficient")
        agg = {k: oos_m[k] for k in
               ("sharpe", "sortino", "max_dd", "hit_rate", "avg_r", "profit_factor")}
        if informative:
            all_sharpes.append(agg["sharpe"])
            worst_dd = min(worst_dd, agg["max_dd"])
            if agg["sharpe"] > 0:
                positive += 1

        # Kalibracja: deklarowana PRZED wejsciem pewnosc kontra faktyczny wynik.
        # Wczesniejsza wersja budowala "prognoze" z wielkosci samego zwrotu
        # (abs(t) * 50 + 0.5), czyli z rzeczy znanej dopiero po fakcie - wykres
        # kalibracji mierzyl wtedy wlasny wynik i zawsze wygladal dobrze.
        for t in oos_res.trades:
            predictions.append((t["conf"], t["ret"] > 0))

        step = max(1, len(next(iter(test_slices.values()), [])) // 50)
        regimes = [classify_regime(r["f"], thresholds[s])[0]
                   for s in aktywne for r in test_slices[s][::step]]
        dominant = max(set(regimes), key=regimes.count) if regimes else None

        if not sucho:
            cur.execute(
                "INSERT INTO learn.wf_window (training_run_id, idx, train_from, train_to,"
                " test_from, test_to, n_trades, dominant_regime)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (run_id, idx, train_from, train_to, test_from, test_to, n_trades, dominant))
            w_id = cur.fetchone()["id"]
            cur.executemany(
                "INSERT INTO learn.wf_metric (wf_window_id, metric, value) VALUES (%s,%s,%s)",
                [(w_id, k, round(v, 4)) for k, v in agg.items()])
            conn.commit()

        gate_history.extend(g[0] for g in gates.values())
        weight_history.append(best_w)

        odniesienie = kup_i_trzymaj({s: v for s, v in test_slices.items() if v}, kind)
        if informative:
            porownywalne += 1
            if oos_m["total_return"] > odniesienie:
                pokonane_odniesienie += 1
        mark = "" if informative else (f"  (poniżej {h.min_trades} transakcji — "
                                       "pominięte w ocenie)")
        if informative:
            mark += (f"  [strategia {oos_m['total_return']:+.2f}% vs "
                     f"kup-i-trzymaj {odniesienie:+.2f}%]")
        if oos_res.killswitch_at:
            mark += f"  [KILL-SWITCH {oos_res.killswitch_at:%m-%d %H:%M}]"
        if oos_m["blocked"]:
            mark += f"  odrzuconych wejść: {oos_m['blocked']}"
        print(f"  okno {idx:>2d}  {test_from:%m-%d}–{test_to:%m-%d}  "
              f"Sharpe/transakcję={agg['sharpe']:+.4f}  transakcje={n_trades}  "
              f"reżim={dominant}{mark}")
        idx += 1
        start += timedelta(days=h.step_days)

    # --- kalibracja -------------------------------------------------------
    ece = None
    if predictions:
        ece = 0.0
        for lo in [i / 10 for i in range(0, 10)]:
            bucket = [(p, hit) for p, hit in predictions if lo <= p < lo + 0.1]
            if len(bucket) < 5:
                continue
            pred = statistics.fmean(p for p, _ in bucket)
            obs = statistics.fmean(1.0 if h else 0.0 for _, h in bucket)
            ece += abs(pred - obs) * len(bucket) / len(predictions)
            cur.execute(
                "INSERT INTO learn.calibration_bin (model_version_id, evaluated_at, bin_lo,"
                " bin_hi, predicted_mean, observed_rate, n_samples)"
                " VALUES (%s, now(), %s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (mv_id, lo, lo + 0.1, round(pred, 4), round(obs, 4), len(bucket)))

    # --- dryf cech --------------------------------------------------------
    max_psi = 0.0
    cur.execute("SELECT id, name FROM feat.feature_def WHERE active")
    for fd in cur.fetchall():
        cur.execute(
            """
            SELECT stddev_pop(value) AS sd_old, avg(value) AS m_old
            FROM feat.feature_value WHERE feature_def_id=%s
              AND event_time BETWEEN %s AND %s
            """, (fd["id"], now - timedelta(days=days), now - timedelta(days=days // 3)))
        old = cur.fetchone()
        cur.execute(
            """
            SELECT stddev_pop(value) AS sd_new, avg(value) AS m_new
            FROM feat.feature_value WHERE feature_def_id=%s AND event_time >= %s
            """, (fd["id"], now - timedelta(days=days // 3)))
        new = cur.fetchone()
        if not (old and new and old["sd_old"] and new["sd_new"]):
            continue
        # Uproszczony wskaznik dryfu: przesuniecie sredniej w jednostkach
        # historycznego odchylenia plus zmiana samego odchylenia.
        psi = abs(float(new["m_new"]) - float(old["m_old"])) / float(old["sd_old"]) \
            + abs(float(new["sd_new"]) / float(old["sd_old"]) - 1)
        max_psi = max(max_psi, psi)
        cur.execute(
            "INSERT INTO learn.drift_metric (model_version_id, feature_def_id, event_time, psi)"
            " VALUES (%s,%s, now(), %s) ON CONFLICT DO NOTHING",
            (mv_id, fd["id"], round(psi, 4)))

    # --- podsumowanie -----------------------------------------------------
    total = len(all_sharpes)
    pct = (positive / total * 100) if total else 0.0
    median = statistics.median(all_sharpes) if all_sharpes else None
    cur.execute("SELECT count(*) AS n FROM learn.hypothesis_trial WHERE model_id=%s", (model_id,))
    trials = cur.fetchone()["n"]

    # Prog dobrany do miary NA TRANSAKCJE, nie anualizowanej. 0,05 oznacza,
    # ze sredni zwrot z transakcji to 5% jej wlasnego odchylenia - skromnie,
    # ale mierzalnie powyzej zera po kosztach.
    #
    # MIN_WINDOWS_FOR_VALIDATION jest warunkiem koniecznym, nie ozdoba. Bez niego
    # przebieg, w ktorym 39 okien pominieto z braku transakcji, a jedno wyszlo
    # dodatnio, dawal 100% skutecznosci i status "kryteria spelnione" - czyli
    # zielone swiatlo do handlu prawdziwymi pieniedzmi na podstawie jednego okna.
    ci_lo, ci_hi = wilson(positive, total)

    # PRZEWAGA NAD NIEROBIENIEM NICZEGO jest osobnym warunkiem, nie ozdoba.
    #
    # Odsetek dodatnich okien mierzy, jak czesto okno wychodzi na plus - a nie
    # czy warto bylo cokolwiek robic. Strategia o malej ekspozycji na spadajacym
    # rynku ma duzo dodatnich okien z samego nietrzymania pozycji. Pomiar
    # z 2026-09-04: wariant `ic` mial 75% dodatnich okien i 52,5% okien lepszych
    # od kupienia i trzymania, czyli rzut moneta wobec odniesienia.
    #
    # Prog stawiamy na DOLNEJ granicy przedzialu ufnosci, nie na samym odsetku,
    # z tego samego powodu, dla ktorego odsetek podajemy zawsze z przedzialem:
    # przy 26-40 oknach roznica kilku punktow nie jest sygnalem.
    odn_lo, odn_hi = wilson(pokonane_odniesienie, porownywalne) if porownywalne else (0.0, 1.0)
    bije_odniesienie = porownywalne >= MIN_WINDOWS_FOR_VALIDATION and odn_lo > 0.5

    if total < MIN_WINDOWS_FOR_VALIDATION:
        status = "learning"
        reason = (f"Za mało informatywnych okien: {total} z wymaganych "
                  f"{MIN_WINDOWS_FOR_VALIDATION}. Strategia zawiera zbyt mało transakcji, "
                  f"żeby cokolwiek orzec — to nie jest wynik ujemny, to brak wyniku.")
    else:
        status = ("validated" if pct >= 70 and median and median > 0.05
                  and bije_odniesienie
                  else "degraded" if pct < 40 else "learning")
        # Przedzial podajemy ZAWSZE razem z odsetkiem. Sam odsetek zacheca do
        # czytania roznicy dwoch punktow jako sygnalu, a przy 40 oknach
        # przedzial ma szerokosc okolo 30 punktow procentowych.
        ci = f" Przedział ufności 95%: {ci_lo:.0%}–{ci_hi:.0%}."
        # Gdy jedyna niespelniona rzecza jest przewaga nad odniesieniem, trzeba
        # to powiedziec wprost. Sam odsetek powyzej progu wyglada jak sukces.
        brak_przewagi = ""
        if pct >= 70 and median and median > 0.05 and not bije_odniesienie:
            brak_przewagi = (f" Kup-i-trzymaj pokonane w {pokonane_odniesienie}/{porownywalne} "
                             f"okien ({odn_lo:.0%}–{odn_hi:.0%}) — to nie jest przewaga, "
                             f"więc kryteria NIE są spełnione mimo odsetka powyżej progu.")
        reason = {
            "validated": f"Dodatnie {positive}/{total} okien out-of-sample ({pct:.0f}%).{ci} Kryteria spełnione.",
            "learning": f"Dodatnie {positive}/{total} okien out-of-sample ({pct:.0f}%).{ci}"
                        + (brak_przewagi or " Próg przejścia na live: 70%."),
            "degraded": f"Dodatnie tylko {positive}/{total} okien ({pct:.0f}%).{ci} Strategia nie wykazuje przewagi.",
        }[status]

    # Progi obowiazujace produkcje - mediana z okien. Petla decyzyjna czyta je
    # z rejestru, zeby nie ustalac ich wlasna, rownolegla logika.
    prod_enter = round(statistics.median(gate_history), 6) if gate_history else 0.25

    # WAGI DLA PRODUKCJI.
    #
    # Bierzemy MEDIANE wag po oknach, nie wagi z ostatniego okna. Pojedyncze
    # okno to jedno losowanie; mediana po kilkudziesieciu jest odporna na to,
    # ze akurat ostatnie wypadlo nietypowo. Suma po wartosciach bezwzglednych,
    # bo wagi moga byc ujemne.
    prod_weights = {}
    if weight_history:
        for regime in REGIMES:
            per_signal = {
                name: statistics.median([w[regime].get(name, 0.0) for w in weight_history])
                for name in SIGNALS
            }
            total_w = sum(abs(v) for v in per_signal.values()) or 1.0
            prod_weights[regime] = {k: round(v / total_w, 4) for k, v in per_signal.items()}

    # W trybie SUCHYM nie dotykamy bazy w ogole: porownanie wariantow nie moze
    # promowac wersji modelu ani nadpisywac stanu nauki. Podsumowanie i tak
    # sie wypisuje - po to ten tryb istnieje.
    if not sucho:
        cur.execute(
            "UPDATE learn.model_version SET params = params || %s::jsonb WHERE id = %s",
            (json.dumps({"enter_threshold": prod_enter,
                         "exit_threshold": round(prod_enter * EXIT_RATIO, 6),
                         "min_confidence": MIN_CONF,
                         "weights": prod_weights}), mv_id))

        # KOLEJNOSC MA ZNACZENIE: najpierw wycofujemy poprzednia wersje, dopiero
        # potem promujemy nowa. Odwrotna kolejnosc lamie unikalny indeks
        # "jedna aktywna wersja na model".
        cur.execute("UPDATE learn.model_version SET status='retired', retired_at=now()"
                    " WHERE model_id=%s AND id<>%s AND status='active'", (model_id, mv_id))
        cur.execute(
            "UPDATE learn.model_version SET status = (CASE WHEN %s = 'degraded'"
            " THEN 'candidate' ELSE 'active' END)::learn.mv_status,"
            " promoted_at = now() WHERE id = %s", (status, mv_id))
        cur.execute("UPDATE learn.training_run SET status='done', finished_at=now(),"
                    " n_windows=%s WHERE id=%s", (total, run_id))
        cur.execute(
            """
            INSERT INTO learn.learning_state (engine, bar, status, model_version_id, windows_total,
                windows_positive, median_oos_sharpe, max_drawdown, calibration_error, max_psi,
                trials_run, reason, positive_ci_low, positive_ci_high, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (engine, bar) DO UPDATE SET status=EXCLUDED.status,
                model_version_id=EXCLUDED.model_version_id, windows_total=EXCLUDED.windows_total,
                windows_positive=EXCLUDED.windows_positive,
                median_oos_sharpe=EXCLUDED.median_oos_sharpe, max_drawdown=EXCLUDED.max_drawdown,
                calibration_error=EXCLUDED.calibration_error, max_psi=EXCLUDED.max_psi,
                trials_run=EXCLUDED.trials_run, reason=EXCLUDED.reason,
                positive_ci_low=EXCLUDED.positive_ci_low,
                positive_ci_high=EXCLUDED.positive_ci_high, updated_at=now()
            """,
            (engine, h.bar, status, mv_id, total, positive,
             round(median, 4) if median is not None else None,
             round(worst_dd, 4), round(ece, 4) if ece is not None else None,
             round(max_psi, 4), trials, reason, round(ci_lo, 4), round(ci_hi, 4)))

        # DZIENNIK, nie kopia stanu. learning_state to jeden wiersz nadpisywany
        # przy kazdym przebiegu, wiec po zapisie nie da sie juz powiedziec, czy
        # cokolwiek sie zmienilo - a to jedyne pytanie, ktore warto zadawac
        # codziennie. Bez tych wierszy dozor musialby albo milczec, albo odzywac
        # sie zawsze (patrz services/engine/dozor.py, migracja 0024).
        cur.execute(
            "INSERT INTO learn.learning_state_log (engine, bar, status, windows_total,"
            " windows_positive, positive_ci_low, positive_ci_high, median_oos_sharpe,"
            " max_drawdown, max_psi, trials_run, model_version_id)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (engine, h.bar, status, total, positive, round(ci_lo, 4), round(ci_hi, 4),
             round(median, 4) if median is not None else None,
             round(worst_dd, 4), round(max_psi, 4), trials, mv_id))
    else:
        cur.execute("UPDATE learn.training_run SET status='done', finished_at=now(),"
                    " error='tryb suchy - porownanie wariantow' WHERE id=%s", (run_id,))
    conn.commit()

    if wybor == "argmax":
        print(f"\nWagi z IC wybrane w {wybrane_ic}/{idx} okien "
              f"(reszta: losowanie albo założenia początkowe)")
    else:
        print(f"\nWariant wyboru: {wybor} — bez losowania, "
              f"jeden kandydat na każde z {idx} okien")
    if porownywalne:
        pct_odn = pokonane_odniesienie / porownywalne * 100
        print(f"Pokonano kup-i-trzymaj w {pokonane_odniesienie}/{porownywalne} oknach "
              f"({pct_odn:.1f}%)   przedział 95%: {odn_lo:.1%}–{odn_hi:.1%}")
    print(f"Okna: {total}, dodatnie: {positive} ({pct:.1f}%)"
          f"   przedział ufności 95%: {ci_lo:.1%}–{ci_hi:.1%}")
    print(f"Mediana Sharpe (OOS): {median:+.3f}" if median is not None else "Mediana Sharpe: —")
    print(f"Najgorsze obsunięcie: {worst_dd:.2f}%")
    print(f"Przetestowanych konfiguracji: {trials}")
    print(f"Stan: {status} — {reason}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1400)
    ap.add_argument("--trials", type=int, default=40)
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--bar", default=DEFAULT_BAR, choices=sorted(HORIZONS))
    ap.add_argument("--kind", default="spot", choices=("spot", "spot_eu", "perp"),
                    help="rodzaj instrumentu - wyznacza stawki prowizji; "
                         "spot_eu to cennik podmiotu unijnego (taker 0,250%%)")
    ap.add_argument("--limit", action="store_true",
                    help="zlecenia z limitem zamiast rynkowych")
    ap.add_argument("--sucho", action="store_true",
                    help="nie zapisuj wyniku do bazy (do porownywania wariantow)")
    ap.add_argument("--instrumenty", default="",
                    help="lista symboli po przecinku - NADPISUJE uniwersum decyzyjne "
                         "z bazy, takze o instrumenty jeszcze niehandlowalne. "
                         "Do porownywania uniwersum kandydujacego z obowiazujacym; "
                         "wymaga --sucho. Puste = uniwersum z bazy")
    ap.add_argument("--wybor", default="ic", choices=("argmax", "ic", "bazowe"),
                    help="jak wybierany jest kandydat na oknie treningowym: "
                         "argmax = najlepszy z losowan + zalozenia + IC (do 2026-09-04 produkcja), "
                         "ic = wylacznie wagi wyliczone z IC, "
                         "bazowe = wylacznie zalozenia poczatkowe. "
                         "Warianty inne niz domyslny wymagaja --sucho")
    args = ap.parse_args()

    # Wariant eksperymentalny NIE MOZE promowac wersji modelu ani nadpisywac
    # stanu nauki - inaczej jeden przebieg porownawczy po cichu zmienialby to,
    # czym handluje produkcja.
    #
    # Porownanie idzie z DOMYSLNA wartoscia, nie z nazwa wpisana na sztywno:
    # gdy pomiar wskaze inny wariant i domyslny sie zmieni, blokada przesuwa
    # sie razem z nim, zamiast zaczac blokowac wlasna produkcje.
    domyslny = ap.get_default("wybor")
    if args.wybor != domyslny and not args.sucho:
        sys.exit(f"--wybor inny niz {domyslny} wymaga --sucho")
    # Ten sam powod: uniwersum produkcyjne zmienia sie migracja, nie flaga.
    if args.instrumenty and not args.sucho:
        sys.exit("--instrumenty nadpisuje uniwersum decyzyjne i wymaga --sucho")

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    with psycopg.connect(url, row_factory=dict_row) as conn:
        h = get_horizon(args.bar)
        from services.engine.koszty import runda
        egz = Egzekucja(limit=args.limit)
        print(f"Walk-forward: świeca {h.bar}, trening {h.train_days} dni / "
              f"test {h.test_days} dni, krok {h.step_days} dni")
        print(f"Egzekucja: {args.kind}, zlecenia "
              f"{'z limitem' if args.limit else 'rynkowe'}, "
              f"koszt rundy {runda(args.kind, args.limit) * 100:.3f}%")
        print(f"Wybór wag na oknie treningowym: {args.wybor}"
              f"{f' ({args.trials} losowań + założenia + IC)' if args.wybor == 'argmax' else ' (bez losowania)'}\n")
        tylko = {s.strip().upper() for s in args.instrumenty.split(",") if s.strip()}
        train(conn, args.engine, args.days, args.trials, h, args.kind, egz,
              sucho=args.sucho, wybor=args.wybor, tylko=tylko or None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
