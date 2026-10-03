#!/usr/bin/env python3
"""Liczenie cech z realnych swiec - sterowane horyzontem.

Cechy sa celowo proste i wytlumaczalne. Warstwa uczenia kalibruje ICH WAGI per
rezim rynkowy, a nie odkrywa cechy od zera - dzieki temu dashboard moze uczciwie
pokazac "dlaczego", bo naprawde wiadomo.

Cechy zapisujemy pod nazwami ROLOWYMI z sufiksem swiecy: `rsi__4h`, `ret_long__1h`.
Rola opisuje, CO cecha mierzy; horyzont mowi, w jakiej skali. Dzieki temu ten sam
zestaw sygnalow dziala na kazdej swiecy, a wyniki dwoch horyzontow da sie porownac,
bo to ta sama strategia, nie dwie rozne.

O DATOWANIU WSTECZ (to jest miejsce, w ktorym najlatwiej cicho uniewaznic caly
backtest):

Cecha liczona wylacznie z zamknietych swiec jest funkcja danych, ktore w tamtym
momencie BYLY dostepne. Wolno jej wiec nadac `ingested_at = event_time + kilka
sekund`, bo gdyby system dzialal wtedy, policzylby ja wtedy. Nie ma tu zadnego
wycieku informacji z przyszlosci.

Czego NIE wolno datowac wstecz: wszystkiego, co zalezy od danych poznanych
pozniej - sentymentu z artykulu zescrapowanego z opoznieniem, korekt, rewizji.
Dlatego `news_sent` liczy sie osobna sciezka: jej zapytanie filtruje po
`article_analysis.ingested_at <= T`, wiec wartosc w chwili T zna wylacznie
analizy gotowe przed T. Dopiero to uprawnia do datowania jej wstecz.

Uzycie:
    DATABASE_URL=... python services/ingest/features.py --bar 4h --backfill --days 1400
    DATABASE_URL=... python services/ingest/features.py --bar 4h --once
    DATABASE_URL=... python services/ingest/features.py --bar 4h,1h --interval 900
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg
from psycopg.rows import dict_row

from services.engine.horizons import DEFAULT_BAR, HORIZONS, Horizon, get as get_horizon
from services.engine.uniwersum import nazwa_przekrojowa, odcisk
from services.ingest import puls

# Nazwa w `risk.worker_heartbeat` - ROLA w potoku, nie egzemplarz procesu. Lista
# swiec idzie do `detail`, bo jest argumentem: gdyby wchodzila do klucza, zmiana
# `--bar` w charcie zakladalaby nowy wiersz i zostawialaby stary, ktory od tej
# chwili starzalby sie w nieskonczonosc jako „martwy worker".
#
# OSOBNY WIERSZ NA KAZDY SILNIK, tak samo jak w `market_worker` i
# `history_worker`. Cechy krypto i cechy equity licza DWA procesy o roznych
# cyklach; gdyby bily w ten sam wiersz, smierc jednego z nich bylaby dla czujki
# niewidoczna - drugi odswiezalby `last_beat_at` za obu.
PULS = "features"


def _puls(engine: str) -> str:
    """Nazwa pulsu dla silnika. `crypto` zostaje bez sufiksu, bo tak nazywa sie
    wiersz istniejacy od poczatku - zmiana nazwy zostawilaby stary jako
    „martwy worker" starzejacy sie w nieskonczonosc."""
    return PULS if engine == "crypto" else f"{PULS}-{engine}"


# Opoznienie obliczeniowe doliczane do ingested_at przy datowaniu wstecz.
COMPUTE_LAG = timedelta(seconds=3)


def znane_od(t: datetime, h: Horizon) -> datetime:
    """Kiedy cecha opisujaca swiece zaczynajaca sie o `t` byla NAPRAWDE znana.

    Znacznik `event_time` cechy to POCZATEK swiecy - taka jest konwencja
    time_bucket(). Ale wartosc liczy sie z ZAMKNIECIA tej swiecy, ktore nastepuje
    dopiero po `bar_minutes` minutach. Cecha swiecy 04:00-08:00 jest wiec znana
    najwczesniej o 08:00, a nie o 04:00.

    Wczesniejsza wersja zapisywala `ingested_at = t + 3 sekundy`, czyli twierdzila,
    ze wartosc byla dostepna trzy sekundy po OTWARCIU swiecy. To unieważniało
    caly straznik bitemporalny: feat.as_of() przepuszczalo o 04:00:05 wartosc,
    ktorej w tamtej chwili nie dalo sie policzyc, bo swieca trwala jeszcze cztery
    godziny.
    """
    return t + timedelta(minutes=h.bar_minutes) + COMPUTE_LAG


def na_siatce(t: datetime, h: Horizon) -> datetime:
    """Zaokragla czas W DOL do poczatku swiecy tego horyzontu.

    KRYTYCZNE, a przez dlugi czas pomijane. Cechy cenowe powstaja z
    `time_bucket(...)`, wiec siedza dokladnie na granicach swiec: 00:00, 04:00,
    08:00. Cechy liczone na wlasnej siatce - funding i news - zaczynaly od
    `now() - N dni`, czyli od przypadkowej chwili z mikrosekundami, i laduly
    na znacznikach w rodzaju 16:19:49.408934.

    Skutek byl asymetryczny i przez to trudny do zauwazenia:

      * TRENING laczy cechy z cenami przez `p.t = f.event_time`, wiec funding
        i news NIGDY nie wchodzily do panelu. Oba sygnaly byly martwe w kazdym
        dotychczasowym przebiegu walk-forward.
      * PRODUKCJA czyta przez `feat.as_of()`, ktore bierze najswiezsza wartosc
        kazdej cechy w oknie swiezosci - i tam funding dzialal normalnie.

    Czyli dokladnie ten rozjazd trening-produkcja, przed ktorym broni cala
    reszta architektury: silnik uzywal sygnalu, ktorego trener nigdy nie widzial.
    """
    krok = h.bar_minutes * 60
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    n = int((t - epoch).total_seconds() // krok)
    return epoch + timedelta(seconds=n * krok)

BATCH = 4000

# Ponizej tylu instrumentow notowanych jednoczesnie ranking przekrojowy nie
# niesie informacji o zbiorze - mowi tylko, ktory z trzech rosnie najmocniej.
MIN_UNIVERSE = 8

# Ponizej tylu snapshotow w swiecy srednia mowi wiecej o momencie pomiaru
# niz o rynku. Przy tempie ~45 snapshotow na godzine swieca 4h ma ich ~180.
MIN_SNAPSHOTS = 20

# Ponizej tylu obserwacji w oknie odchylenie standardowe opisuje glownie samo
# okno, a nie szereg - ten sam prog co w `zscore()` dla cech cenowych.
MIN_PROBEK_Z = 20

# rola -> (code_ref, szablon opisu). {okno} podstawia sie z etykiet horyzontu.
ROLES = {
    "rsi":         ("features.momentum:rsi",
                    "Wskaźnik siły względnej ({period} świec {bar}). Poniżej 30 = wyprzedanie."),
    "ema_ratio":   ("features.trend:ema_ratio",
                    "Iloraz średnich wykładniczych {fast}/{slow} na świecach {bar}. "
                    "Powyżej 1 = trend wzrostowy."),
    "atr_pct":     ("features.volatility:atr_pct",
                    "Średni rzeczywisty zakres jako procent ceny ({period} świec {bar}). "
                    "Miara zmienności niezależna od poziomu ceny."),
    "ret_short":   ("features.momentum:ret", "Zwrot z ostatnich {okno}."),
    "ret_mid":     ("features.momentum:ret", "Zwrot z ostatnich {okno}."),
    "ret_long":    ("features.momentum:ret", "Zwrot z ostatnich {okno}."),
    "volume_z":    ("features.flow:volume_z",
                    "Odchylenie wolumenu świecy od średniej z {okno}, w odchyleniach standardowych."),
    "range_pos":   ("features.reversion:range_position",
                    "Pozycja ceny w zakresie {okno}: 0 = przy minimum, 1 = przy maksimum."),
    "news_sent":   ("features.news:weighted_sentiment",
                    "Sentyment newsowy z ostatniej doby, ważony siłą, pewnością i nowością. "
                    "Zero, gdy brak artykułów."),
    "funding_rate": ("features.derivatives:funding",
                     "Stawka finansowania perpetuala (co 8 h). Dodatnia = długie płacą krótkim, "
                     "czyli rynek jest wychylony na wzrost."),
    "funding_z":   ("features.derivatives:funding_z",
                    "Odchylenie stawki finansowania od średniej z 30 dni, w odchyleniach standardowych."),
    "long_ratio":  ("features.derivatives:long_ratio",
                    "Udział KONT po stronie długiej na perpetualu, w procentach. "
                    "Nie udział kapitału — jeden duży gracz waży tyle samo co mały."),
    "long_ratio_z": ("features.derivatives:long_ratio_z",
                     "Odchylenie udziału kont długich od średniej z 30 dni, "
                     "w odchyleniach standardowych. Skrajne wychylenie tłumu."),
    "oi_zmiana":   ("features.derivatives:open_interest_change",
                    "Zmiana otwartych pozycji wobec poprzedniej świecy, w procentach. "
                    "Poziom nie jest porównywalny w czasie — informacją jest zmiana."),
    "book_imbalance": ("features.microstructure:imbalance",
                       "Nierównowaga arkusza zleceń uśredniona po świecy {bar}: "
                       "+1 = sama strona kupna, −1 = sama strona sprzedaży."),
    "book_spread_bps": ("features.microstructure:spread",
                        "Spread arkusza jako część ceny, w punktach bazowych, "
                        "uśredniony po świecy {bar}. Miara płynności."),
    "rel_strength": ("features.crosssection:relative_strength",
                     "Siła względna wobec pozostałych obserwowanych rynków: −1 = najsłabszy, "
                     "+1 = najmocniejszy w tej chwili. Liczona ze zwrotu z {okno}."),
}

# Role liczone z samych cen - wolno datowac wstecz bez zastrzezen.
PRICE_ROLES = ("rsi", "ema_ratio", "atr_pct", "ret_short", "ret_mid", "ret_long",
               "volume_z", "range_pos")


# Rola -> klucz etykiety okna w horyzoncie. Bez tego opis cechy w interfejsie
# ma pusta dziure zamiast okresu.
OKNO_ROLI = {"range_pos": "range", "volume_z": "volume", "rel_strength": "ret_mid"}

# Role PRZEKROJOWE - ich wartosc zalezy od ZBIORU, nie tylko od instrumentu.
# Nazwa takiej cechy niesie odcisk uniwersum (services/engine/uniwersum.py),
# zeby wartosc policzona dla innego zbioru przestala byc widoczna, zamiast po
# cichu znaczyc co innego.
ROLE_PRZEKROJOWE = ("rel_strength",)


def describe(role: str, h: Horizon) -> str:
    tpl = ROLES[role][1]
    okno = h.labels.get(role) or h.labels.get(OKNO_ROLI.get(role, role), "")
    return tpl.format(okno=okno, bar=h.bar, period=h.rsi_period if role == "rsi" else h.atr_period,
                      fast=h.ema_fast, slow=h.ema_slow)


def params_for(role: str, h: Horizon) -> dict:
    p: dict = {"bar": h.bar, "role": role}
    if role == "rsi":
        p["period"] = h.rsi_period
    elif role == "atr_pct":
        p["period"] = h.atr_period
    elif role == "ema_ratio":
        p |= {"fast": h.ema_fast, "slow": h.ema_slow}
    elif role in ("ret_short", "ret_mid", "ret_long"):
        p |= {"bars": getattr(h, role), "okno": h.labels.get(role)}
    elif role == "range_pos":
        p |= {"bars": h.range_window, "okno": h.labels.get("range")}
    elif role == "volume_z":
        p |= {"bars": h.volume_lookback, "okno": h.labels.get("volume")}
    elif role == "rel_strength":
        p |= {"zrodlo": "ret_mid", "bars": h.ret_mid, "okno": h.labels.get("ret_mid"),
              "min_universe": MIN_UNIVERSE}
    return p


def ensure_definitions(cur, h: Horizon, symbole: list[str] | None = None) -> dict[str, int]:
    """Rejestruje definicje cech dla tego horyzontu i zwraca rola -> id.

    CECHY PRZEKROJOWE DOSTAJA ODCISK UNIWERSUM w nazwie i w `params`. Bez tego
    zmiana zbioru zostawia w bazie wartosci, ktore WYGLADAJA na aktualne: nie ma
    awarii, nie ma pustego pola, jest liczba - tyle ze opisujaca inny swiat.

    `symbole` jest wymagane dla tych rol. Brak listy oznacza, ze wolajacy nie wie,
    dla jakiego zbioru liczy - a wtedy jedyna uczciwa odpowiedzia jest odmowa,
    nie zapis pod nazwa bez odcisku.
    """
    ids = {}
    for role, (code_ref, _tpl) in ROLES.items():
        nazwa = h.name(role)
        params = params_for(role, h)
        if role in ROLE_PRZEKROJOWE:
            if not symbole:
                continue
            nazwa = nazwa_przekrojowa(nazwa, symbole)
            params = params | {"uniwersum": odcisk(symbole),
                               "symbole": sorted({s.upper() for s in symbole})}
        cur.execute(
            "INSERT INTO feat.feature_def (name, version, code_ref, params, description, active)"
            " VALUES (%s,1,%s,%s,%s,true)"
            " ON CONFLICT (name, version) DO UPDATE"
            "   SET code_ref = EXCLUDED.code_ref, params = EXCLUDED.params,"
            "       description = EXCLUDED.description, active = true"
            " RETURNING id",
            (nazwa, code_ref, json.dumps(params), describe(role, h)),
        )
        ids[role] = cur.fetchone()["id"]
    return ids


# ------------------------------------------------------------------ matematyka
def rsi(closes: list[float], period: int) -> float | None:
    """RSI w wariancie prostej sredniej z `period` ostatnich zmian (Cutler).

    Swiadomie NIE jest to wygladzanie Wildera: wariant Wildera zalezy od calej
    historii szeregu, wiec ta sama chwila policzona przy backfillu i na zywo
    dalaby inna wartosc, zaleznie od tego, gdzie zaczal sie bufor. Wariant
    prostej sredniej zalezy wylacznie od okna - i dzieki temu trening i produkcja
    licza dokladnie to samo.
    """
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(-period, 0):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def ema(values: list[float], period: int) -> float | None:
    """EMA zainicjowana srednia z pierwszych `period` wartosci.

    Bufor wejsciowy musi byc staly (patrz EMA_BUFFER), inaczej wartosc zalezy
    od dlugosci historii, a nie od danych.
    """
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def atr_pct(bars: list[dict], period: int) -> float | None:
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        hi, lo, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    last_close = bars[-1]["close"]
    return (sum(trs) / period) / last_close * 100 if last_close else None


def zscore(values: list[float]) -> float | None:
    if len(values) < 20:
        return None
    mean = sum(values) / len(values)
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
    return (values[-1] - mean) / sd if sd > 1e-12 else 0.0


# ------------------------------------------------------------------ swiece
def load_bars(cur, instrument_id: int, h: Horizon, since: datetime,
              until: datetime, *, rozgrzewka: int = 0) -> list[dict]:
    """Swiece horyzontu: albo wprost z market.candle, albo agregat z nizszej.

    `rozgrzewka` to liczba swiec PRZED `since`, a NIE odcinek czasu.

    DLACZEGO TO MUSI BYC LICZBA SWIEC. Do 2026-09-24 wolajacy cofal `since`
    o `bar_minutes * lookback` MINUT, co jest tym samym tylko na rynku
    notowanym bez przerwy. Gielda amerykanska daje SIEDEM swiec 1h na dzien
    handlowy i zero przez ~17 h nocy oraz ~65 h weekendu, wiec 205 swiec
    rozgrzewki rozciaga sie tam na ~41 dni kalendarzowych, nie na 205 godzin.

    Skutek byl niewidoczny, bo nie byl bledem: w oknie 205 godzin lezalo
    okolo 55 swiec equity, `compute_prices` trafialo na `len(bars) <= lookback`
    i zwracalo zero. Potok konczyl cykl z kodem 0, bil puls i drukowal
    "0 cen" dla kazdego instrumentu - proces zdrowy, ktory nie policzyl
    ANI JEDNEJ cechy. Zmierzone na produkcji 2026-09-24: krypto 228 swiec
    w tym samym oknie (liczy sie normalnie), equity 55 (nie liczy sie nigdy).

    Rozgrzewka pobierana po LICZBIE swiec jest poprawna na obu rynkach naraz
    i nie wymaga od potoku wiedzy o godzinach sesji ani o kalendarzu swiat.
    """
    # Przy agregacji jedna swieca horyzontu powstaje z kilku swiec zrodlowych,
    # wiec limit stawiamy na zrodle i tam przelicznik jest potrzebny.
    limit_zrodla = rozgrzewka * h.swiec_zrodlowych_na_swiece
    # ZRODLO to rozgrzewka (ostatnie N swiec przed `since`) SKLEJONA z oknem
    # wlasciwym. Osobny podzapytaniem, bo rozgrzewka wybiera sie po LIMIT,
    # a okno po zakresie czasu - jednym warunkiem WHERE sie tego nie wyrazi.
    zrodlo = """
        (SELECT event_time, open, high, low, close, volume
           FROM market.candle
          WHERE instrument_id = %s AND timeframe = %s AND event_time < %s
          ORDER BY event_time DESC LIMIT %s)
        UNION ALL
        (SELECT event_time, open, high, low, close, volume
           FROM market.candle
          WHERE instrument_id = %s AND timeframe = %s
            AND event_time >= %s AND event_time < %s)
    """
    args = (instrument_id, h.source_timeframe, since, limit_zrodla,
            instrument_id, h.source_timeframe, since, until)
    if h.aggregate:
        sql = f"""
            WITH zrodlo AS ({zrodlo})
            SELECT time_bucket(%s::interval, event_time) AS t,
                   (array_agg(open  ORDER BY event_time))[1]      AS open,
                   max(high) AS high, min(low) AS low,
                   (array_agg(close ORDER BY event_time DESC))[1] AS close,
                   sum(volume) AS volume,
                   count(*) AS n
            FROM zrodlo
            GROUP BY 1 ORDER BY 1
        """
        args = args + (f"{h.bar_minutes} minutes",)
    else:
        sql = f"""
            WITH zrodlo AS ({zrodlo})
            SELECT event_time AS t, open, high, low, close, volume
            FROM zrodlo ORDER BY 1
        """
    cur.execute(sql, args)
    # `n` - ile swiec zrodlowych weszlo do agregatu. Bez niego nie da sie
    # odroznic kubla 4h zlozonego z czterech zamknietych swiec 1h od kubla,
    # w ktorym jest dopiero jedna (patrz `_ostatnia_gotowa`).
    return [{"t": r["t"], "open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"]),
             "volume": float(r["volume"]),
             **({"n": int(r["n"])} if "n" in r else {})} for r in cur.fetchall()]


def _ostatnia_gotowa(bar: dict, h: Horizon, until: datetime) -> bool:
    """Czy OSTATNIA swieca panelu jest domknieta i kompletna.

    DLACZEGO WARUNEK, A NIE "ZAWSZE POMIJAJ OSTATNIA"

    Do 2026-10-02 `compute_prices` pomijalo ostatnia swiece bezwarunkowo,
    z uzasadnieniem "jest jeszcze otwarta". Bylo to prawda do 2026-09-06 -
    od tego dnia `backfill.py` zapisuje wylacznie swiece ZAMKNIETE
    (`event_time + step <= now`), wiec pomijana byla swieca zamknieta.
    Skutek: cecha swiecy B pojawiala sie dopiero, gdy istniala swieca PO
    niej. Dla 4h z 1h to ~godzina pozniej (kubel B+4h musi dostac pierwsza
    swiece 1h), dla equity 1h - nastepnego notowania, czyli po sesji.

    Teraz swieca liczy sie wtedy, gdy:
      * jej koniec nie wychodzi poza `until` (zegar, nie pozycja w liscie);
      * przy agregacji - ma KOMPLET swiec zrodlowych. Kubel 12:00 o 13:20
        ma jedna swiece 1h z czterech, a `insert_values` poprawia wiersz
        dopiero przy nastepnym przebiegu - petla zdazylaby zdecydowac na
        kawalku swiecy.

    Brak `n` (wywolanie spoza `load_bars`) znaczy "nie wiadomo" i swieca
    jest pomijana - tak jak dotad.

    Warunek dotyczy WYLACZNIE ostatniej swiecy panelu. Wczesniejsze kuble
    z dziurami w danych liczyly sie zawsze i dalej sie licza: zmiana ich
    zestawu przesunelaby indeksy okien w calej historii, na ktorej stoi
    trening.
    """
    if bar["t"] + timedelta(minutes=h.bar_minutes) > until:
        return False
    if h.aggregate:
        return bar.get("n", 0) >= h.swiec_zrodlowych_na_swiece
    return True


# ------------------------------------------------------------------ liczenie
def compute_prices(cur, instrument_id: int, h: Horizon, feat_ids: dict[str, int],
                   since: datetime, until: datetime) -> int:
    # Rozgrzewka: najdluzsze okno + zapas. Bez niej pierwsze swiece okresu
    # mialyby cechy policzone z niepelnej historii - czyli inne niz te same
    # swiece policzone przy nastepnym uruchomieniu.
    lookback = max(h.ret_long, h.range_window, h.volume_lookback,
                   h.ema_slow, h.warmup) + 5
    bars = load_bars(cur, instrument_id, h, since, until, rozgrzewka=lookback)
    if len(bars) <= lookback:
        # NIE CICHE ZERO. Ten warunek znaczy "ten instrument nie dostanie
        # ZADNEJ cechy", a nie "nie przybylo nowych" - i przez rok wygladal
        # w logu tak samo jak spokojny cykl bez nowych swiec. Dopoki milczal,
        # potok equity meldowal zdrowie, nie liczac niczego.
        print(f"    UWAGA instrument {instrument_id}: {len(bars)} świec "
              f"przy wymaganej rozgrzewce {lookback} — cech NIE BĘDZIE. "
              f"Za krótka historia albo za rzadka siatka notowań.")
        return 0

    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]
    # Staly bufor dla EMA - patrz komentarz przy ema().
    ema_buf = h.ema_slow * 4
    rows: list[tuple] = []

    # Ostatnia swieca panelu liczy sie tylko domknieta i kompletna - patrz
    # `_ostatnia_gotowa`. Do 2026-10-02 pomijana byla ZAWSZE, takze zamknieta.
    koniec = len(bars) if _ostatnia_gotowa(bars[-1], h, until) else len(bars) - 1

    for i in range(lookback, koniec):
        t = bars[i]["t"]
        if t < since:
            continue
        ingested = znane_od(t, h)
        win_c, win_b = closes[: i + 1], bars[: i + 1]

        vals: dict[str, float | None] = {
            "rsi": rsi(win_c[-(h.rsi_period * 3):], h.rsi_period),
            "atr_pct": atr_pct(win_b[-(h.atr_period * 3):], h.atr_period),
            "ret_short": (win_c[-1] / win_c[-1 - h.ret_short] - 1) * 100,
            "ret_mid": (win_c[-1] / win_c[-1 - h.ret_mid] - 1) * 100,
            "ret_long": (win_c[-1] / win_c[-1 - h.ret_long] - 1) * 100,
            "volume_z": zscore(volumes[i - h.volume_lookback + 1: i + 1]),
        }

        fast, slow = ema(win_c[-ema_buf:], h.ema_fast), ema(win_c[-ema_buf:], h.ema_slow)
        vals["ema_ratio"] = (fast / slow) if (fast and slow) else None

        window = win_b[-h.range_window:]
        hi = max(b["high"] for b in window)
        lo = min(b["low"] for b in window)
        vals["range_pos"] = ((win_c[-1] - lo) / (hi - lo)) if hi > lo else 0.5

        for role, v in vals.items():
            if v is None or not math.isfinite(v):
                continue
            rows.append((feat_ids[role], instrument_id, t, v, ingested))

    return insert_values(cur, rows)


# KTORE ARTYKULY SKLADAJA SIE NA `news_sent` W CHWILI `t` - JEDNA DEFINICJA.
#
# DLACZEGO STALA, A NIE DWA ZAPYTANIA
#
# Od 2026-09-24 pyta o to takze petla decyzyjna: zapisuje do
# `journal.decision_contribution` wiersze `kind='news'`, zeby po fakcie dalo sie
# powiedziec, KTORY artykul stal za transakcja - a nie tylko, ze "sentyment
# wynosil -0,21". Druga kopia tego zapytania rozjechalaby sie przy pierwszej
# zmianie okna albo reguly `DISTINCT ON`, a rozjazd nie objawilby sie awaria:
# atrybucja opisywalaby INNY zbior artykulow niz ten, ktory wytworzyl liczbe.
#
# ZAPYTANIE JEST ODTWARZALNE i to jest warunek uczciwosci atrybucji. Oba filtry
# czasowe sa domkniete na `{chwila}` - takze bitemporalny `an.ingested_at`
# - wiec ten sam `(instrument, chwila)` daje ten sam zbior niezaleznie od tego,
# KIEDY zapytanie zostanie uruchomione. Artykul, ktory przyszedl pozniej, nie
# wchodzi do atrybucji decyzji podjetej wczesniej.
SQL_WKLAD_NEWSOW = """
SELECT DISTINCT ON (a.url)
       a.id AS article_id, a.url, a.title,
       an.direction, an.magnitude, an.confidence, an.novelty, ai.relevance,
       (CASE an.direction WHEN 'bullish' THEN 1
                          WHEN 'bearish' THEN -1 ELSE 0 END
        * an.magnitude * an.confidence * an.novelty
        * ai.relevance) AS wklad
FROM news.article_instrument ai
JOIN news.article a  ON a.id = ai.article_id
JOIN news.article_analysis an ON an.article_id = a.id
WHERE ai.instrument_id = {instrument}
  AND a.event_time <= {chwila}
  AND a.event_time >  {chwila} - interval '24 hours'
  AND an.ingested_at <= {chwila}
-- Najswiezsza migawka TEJ historii, znana w chwili `chwila`.
ORDER BY a.url, an.ingested_at DESC
"""


def wklady_newsow(cur, instrument_id: int, chwila) -> list[dict]:
    """Artykuly, ktore zlozyly sie na `news_sent` tego instrumentu w `chwila`.

    Ta sama stala, ktorej uzywa `compute_news` - wiec suma `wklad` z tej listy
    jest DOKLADNIE wartoscia cechy dla tej swiecy. Wolacz, ktory chce to
    sprawdzic, ma to zrobic jawnie: rozjazd znaczy blad, a nie szczegol.
    """
    cur.execute(SQL_WKLAD_NEWSOW.format(instrument="%s", chwila="%s"),
                (instrument_id, chwila, chwila, chwila))
    return cur.fetchall()


def compute_news(cur, instrument_id: int, h: Horizon, feat_id: int,
                 since: datetime, until: datetime) -> int:
    """Sentyment newsowy - z jawnym poszanowaniem opoznienia analizy.

    Wartosc w chwili T bierze pod uwage wylacznie artykuly, ktorych analiza byla
    GOTOWA przed T (an.ingested_at <= T). Bez tego warunku model uczylby sie
    z newsow, ktorych wtedy jeszcze nie znal.

    BRAK DANYCH NIE JEST POMIAREM ROWNYM ZERU - i to jest poprawka z 2026-09-23.

    Zapytanie idzie `FROM generate_series(...)` z samymi `LEFT JOIN`-ami, wiec
    KAZDA swieca okna dostaje wiersz. `COALESCE(..., 0)` zamienial przy tym brak
    danych w twarde zero, nierozroznialne od „wiadomosci wywazone".

    Kosztowalo to 13 dni szeregu. Od 09.09 do 22.09.2026 analiza stala (pusty
    limit u dostawcy), artykuly byly zapisywane, a `news.article_analysis`
    pozostawalo puste. `compute_news` wypisalo `news_sent__4h = 0.0` dla kazdej
    swiecy i kazdego instrumentu, trener zobaczyl pelny, nie-NULL szereg
    wygladajacy jak „rynek bez newsow" i skalibrowal na nim wage cechy.

    Gdyby wiersza nie bylo, cecha wypadlaby z decyzji JAWNIE - dokladnie tak
    zachowal sie `funding_z` i to bylo WIDOCZNE (patrz `compute_funding_z`
    nizej). Ta sama wada zostala zamknieta po stronie API commitem `01d9bf1`
    („brak pomiaru przestaje byc zwracany jako pomiar rowny zeru"); tutaj
    zostawala otwarta.

    PROG JEST JEDNA ANALIZA, nie wiecej. Pytanie brzmi „czy w oknie 24 h bylo
    cokolwiek, co ta cecha mierzy", a nie „czy probka jest duza". Jedna analiza
    o kierunku `neutral` daje uczciwe zero - to JEST pomiar. Zero z zera analiz
    nim nie jest.

    JEDNA HISTORIA LICZY SIE RAZ - poprawka z 2026-09-24.

    Kanaly typu „live updates" to relacja na zywo: ten sam URL wraca przy
    kazdym pobraniu z DOPISANA trescia. `content_hash` sluszznie widzi za kazdym
    razem co innego, wiec w `news.article` powstaje osobny wiersz - a wszystkie
    dziedzicza `event_time` PIERWSZEJ publikacji, bo tyle podaje RSS.

    Zmierzone na produkcji: jedna relacja CoinDesk z 2026-09-15 06:37 ma
    DZIEWIEC wierszy, pobranych od 06:39 do 18:49 tego samego dnia, wszystkie
    z tym samym `event_time`. Sumowanie po wierszach wliczalo wiec sentyment
    jednej historii dziewiec razy do kazdej swiecy 24-godzinnego okna. Przy 27
    artykulach uznanych za istotne w calej bazie taka dziewiatka to znaczna
    czesc probki - cecha mierzyla nie „co sie dzieje", tylko „jak czesto
    redakcja dopisuje akapit".

    Stad `DISTINCT ON (a.url)`: jedna historia wnosi JEDEN wklad, i to ten
    z NAJSWIEZSZEJ analizy znanej w chwili `t`. Kolejne migawki nie sa smieciem
    do skasowania - relacja o 18:49 mowila co innego niz o 06:39 - wiec zostaja
    w bazie, a wybor „ktora obowiazuje" zapada przy liczeniu cechy. Filtr
    `an.ingested_at <= b.t` dalej pilnuje, ze wybiera sie wylacznie z migawek
    znanych wtedy.
    """
    since = na_siatce(since, h)
    # `until` cofamy o jedna swiece: ostatnia swieca jest jeszcze otwarta.
    until = na_siatce(until, h) - timedelta(minutes=h.bar_minutes)
    # Podzapytanie jest WSTRZYKIWANE ZE STALEJ, a nie przepisane tutaj -
    # `SQL_WKLAD_NEWSOW` jest jedynym miejscem, w ktorym zyje definicja "ktore
    # artykuly sie licza". `{chwila}` staje sie tu `b.t` (kolumna generatora),
    # a przy atrybucji decyzji - parametrem. To ta sama regula, oba razy.
    cur.execute(
        """
        SELECT b.t,
               COALESCE(sum(h.wklad), 0) AS score,
               count(h.wklad) AS analiz
        FROM generate_series(%s, %s, %s::interval) AS b(t)
        -- JEDEN WKLAD NA HISTORIE, nie na migawke - patrz docstring.
        -- LATERAL, bo podzapytanie musi znac `b.t`: i okno 24 h, i granica
        -- bitemporalna `an.ingested_at <= b.t` zaleza od liczonej swiecy.
        LEFT JOIN LATERAL (
        """ + SQL_WKLAD_NEWSOW.format(instrument="%s", chwila="b.t") + """
        ) h ON true
        GROUP BY b.t ORDER BY b.t
        """,
        (since, until, f"{h.bar_minutes} minutes", instrument_id),
    )
    # Swieca bez ani jednej analizy w oknie NIE DOSTAJE WIERSZA. Decyzja pytajaca
    # o te ceche dostanie wtedy `None` i cecha wypadnie z niej jawnie - tak jak
    # wypadala przy martwym `funding_z`. Cisza, ktora widac, jest jedyna cisza,
    # o ktorej ktokolwiek sie dowie.
    rows = [(feat_id, instrument_id, r["t"], float(r["score"]), znane_od(r["t"], h))
            for r in cur.fetchall() if r["analiz"]]
    return insert_values(cur, rows)


def compute_cross_section(cur, h: Horizon, feat_ids: dict[str, int],
                         since: datetime, until: datetime,
                         instrument_ids: list[int] | None = None,
                         *, engine: str) -> int:
    """Siła względna: pozycja instrumentu w rankingu zwrotów CAŁEGO zbioru.

    DLACZEGO TO JEST INNA INFORMACJA NIŻ POZOSTAŁE SYGNALY

    Wszystkie dotychczasowe cechy opisuja instrument SAM ZE SOBA: jego wlasny
    zwrot, jego wlasne RSI, jego wlasna zmiennosc. W rynku, w ktorym wszystko
    porusza sie razem - a krypto jest takim rynkiem - wiekszosc tej informacji
    to wspolny czynnik, czyli po prostu "czy dzis rosnie caly rynek".

    Ranking zwrotow ODEJMUJE ten wspolny czynnik z definicji: gdy wszystko rosnie
    o 5%, kazdy instrument ma ten sam zwrot bezwzgledny, ale ranking dalej mowi,
    kto rosnie mocniej od reszty. To jest tresc, ktorej w cechach absolutnych
    nie ma.

    KIERUNEK - decyzja podjeta PRZED zobaczeniem wyniku. Sygnal liczymy
    w wariancie momentum przekrojowego (mocniejsi utrzymuja przewage), bo na
    horyzoncie tygodniowym to wlasnie ten efekt jest udokumentowany. Gdyby
    wyszlo zero, wariant odwrotny (powrot do sredniej przekrojowej) jest
    ODDZIELNA hipoteza do osobnego testu - a nie powodem, zeby odwrocic znak
    i policzyc jeszcze raz.

    DATOWANIE WSTECZ jest tu uprawnione: ranking w chwili T korzysta wylacznie
    ze zwrotow znanych w chwili T, tyle ze u wielu instrumentow naraz.

    ZBIOR RANKINGU MUSI BYC TYM SAMYM ZBIOREM, KTORY NAZYWA ODCISK CECHY

    `engine` jest tu argumentem OBOWIAZKOWYM, a nie opcja - i to jest naprawa
    z 2026-09-25. Do tego dnia zapytanie filtrowalo `i.active AND i.tradeable`,
    ale NIE `i.engine` ani `i.kind <> 'perp'`, podczas gdy odcisk uniwersum
    (`przebieg` nizej) liczy sie ZE `engine` i BEZ perpetualow. Dzis oba zbiory
    sa identyczne, bo `tradeable` maja wylacznie dziesiec par USDC Bybita,
    a migracja 0047 zdjela te flage wszystkim 21 akcjom - poprawka nie zmienia
    wiec ANI JEDNEJ wartosci i jest to sprawdzone testem porownujacym licznosci
    obu zapytan.

    CZEGO NIE ZMIENIA DZIS, A CO ZMIENI PO WLACZENIU UNIWERSUM EQUITY. Migracja
    0047 zapowiada wprost osobna migracje wlaczajaca akcje do uniwersum. Od tej
    chwili zapytanie bez `engine` liczylo by percentyl w rankingu 31
    instrumentow naraz - 21 akcji i 10 par krypto - czyli `rel_strength__1h`
    przestalby znaczyc "czy ten instrument rosnie mocniej od swojego rynku",
    a zaczal znaczyc "czy krypto rosnie szybciej niz akcje". Sygnal, ktory ma
    ODEJMOWAC wspolny czynnik rynku, mierzylby wtedy roznice miedzy dwoma
    rynkami; nazwa cechy nadal nosilaby odcisk zbioru, ktory jej nie opisuje.
    Po tamtej migracji poprawka przestaje byc odwracalna, bo wartosci policzone
    przekrojowo przez dwa rynki wyglądaja jak kazde inne.
    """
    cur.execute(
        """
        WITH r AS (
            SELECT fv.instrument_id, fv.event_time, fv.value
            FROM feat.feature_value fv
            -- WYLACZNIE uniwersum decyzyjne. Baza trzyma historie szerzej, niz
            -- handlujemy (0020), a od czasu rejestracji par USDC ten sam aktyw
            -- wystepuje w niej DWA RAZY - jako *USDT i jako *USDC. Ranking po
            -- wszystkim liczylby wtedy BTC podwojnie i przekrecal sile wzgledna
            -- calego zbioru.
            JOIN ref.instrument i ON i.id = fv.instrument_id
                 AND i.active
                 -- TEN SAM WARUNEK, Z KTOREGO LICZY SIE ODCISK UNIWERSUM.
                 -- `engine`, bo ranking przez dwa rynki naraz mierzy roznice
                 -- miedzy rynkami, a nie pozycje w swoim; `kind <> 'perp'`, bo
                 -- perpetual i spot tego samego aktywa to dwa wiersze o niemal
                 -- identycznym zwrocie - liczony podwojnie przesuwa percentyle
                 -- calego zbioru. Oba warunki stoja w odcisku od zawsze, tu
                 -- brakowalo ich do 2026-09-25.
                 AND i.engine::text = %s
                 AND i.kind <> 'perp'
                 -- Nawiasy jawnie: bez podanej listy obowiazuje uniwersum
                 -- decyzyjne, z lista - dokladnie ona.
                 AND ((%s::int[] IS NULL AND i.tradeable)
                      OR fv.instrument_id = ANY(%s))
            WHERE fv.feature_def_id = %s
              AND fv.event_time >= %s AND fv.event_time < %s
        ),
        licznosc AS (
            SELECT event_time, count(*) AS n FROM r GROUP BY event_time
        )
        SELECT r.instrument_id, r.event_time,
               percent_rank() OVER (PARTITION BY r.event_time ORDER BY r.value) AS pr
        FROM r JOIN licznosc l ON l.event_time = r.event_time
        -- Ranking z garstki instrumentow nie niesie informacji o zbiorze.
        WHERE l.n >= %s
        """,
        (engine, instrument_ids, instrument_ids, feat_ids["ret_mid"],
         since, until, MIN_UNIVERSE),
    )
    rows = [
        (feat_ids["rel_strength"], r["instrument_id"], r["event_time"],
         (float(r["pr"]) - 0.5) * 2, znane_od(r["event_time"], h))
        for r in cur.fetchall()
    ]
    return insert_values(cur, rows)


def compute_orderbook(cur, instrument_id: int, h: Horizon, feat_ids: dict[str, int],
                      since: datetime, until: datetime) -> int:
    """Cechy z arkusza zlecen, usrednione po swiecy.

    DLACZEGO LICZYMY JE, ZANIM DA SIE JE PRZETESTOWAC

    `market.book_snapshot` ma retencje 90 dni - surowe snapshoty starsze niz
    trzy miesiace sa kasowane. Zbieramy je dopiero od wczoraj, wiec hipotezy
    o mikrostrukturze nie da sie dzis zbadac walk-forwardem na czterech latach.

    Ale bez wyliczania cech TERAZ nie da sie jej zbadac NIGDY: za trzy miesiace
    surowe dane sprzed trzech miesiecy juz nie beda istniec. Tabela cech nie ma
    retencji, wiec kazda swieca policzona dzisiaj zostaje na zawsze.

    Sygnal korzystajacy z tych cech celowo jeszcze NIE ISTNIEJE. Kalibrowanie go
    na jednym dniu danych nie powiedzialoby nic o rynku, a dodaloby stopien
    swobody. Dodamy go, gdy historia bedzie miala sens - kilka miesiecy.
    """
    if "book_imbalance" not in feat_ids:
        return 0
    since = na_siatce(since, h)
    until = na_siatce(until, h) - timedelta(minutes=h.bar_minutes)
    cur.execute(
        """
        SELECT time_bucket(%s::interval, event_time) AS t,
               avg(imbalance)                              AS imbalance,
               avg(spread / NULLIF(mid, 0)) * 10000        AS spread_bps,
               count(*)                                    AS n
        FROM market.book_snapshot
        WHERE instrument_id = %s AND event_time >= %s AND event_time < %s
        GROUP BY 1 ORDER BY 1
        """,
        (f"{h.bar_minutes} minutes", instrument_id, since,
         until + timedelta(minutes=h.bar_minutes)),
    )
    rows: list[tuple] = []
    for r in cur.fetchall():
        # Swieca z garstka snapshotow daje srednia, ktora mowi wiecej o momencie
        # pomiaru niz o rynku.
        if r["n"] < MIN_SNAPSHOTS or r["t"] > until:
            continue
        ingested = znane_od(r["t"], h)
        if r["imbalance"] is not None:
            rows.append((feat_ids["book_imbalance"], instrument_id, r["t"],
                         float(r["imbalance"]), ingested))
        if r["spread_bps"] is not None and math.isfinite(float(r["spread_bps"])):
            rows.append((feat_ids["book_spread_bps"], instrument_id, r["t"],
                         float(r["spread_bps"]), ingested))
    return insert_values(cur, rows)


def compute_funding(cur, instrument_id: int, h: Horizon, feat_ids: dict[str, int],
                    since: datetime, until: datetime) -> int:
    """Stawka finansowania perpetuala, przeniesiona na siatke swiec.

    Instrumenty spot nie maja wlasnego fundingu - bierzemy go z perpetuala na tej
    samej parze (`ref.instrument.meta->>'funding_from'`). To jest sygnal o
    pozycjonowaniu calego rynku, wiec stosuje sie do instrumentu bazowego.

    Kazda swieca dostaje OSTATNIA stawke ogloszona przed jej zamknieciem -
    nigdy nadchodzaca. Stawka publikowana o 08:00 nie moze wplywac na decyzje
    z 04:00, choc opisuje to samo osmiogodzinne okno.

    Wypelnianie w przod robimy jednym przejsciem po dwoch posortowanych listach,
    a nie skorelowanym podzapytaniem per punkt siatki. Wersja z podzapytaniem
    wykonywala 8 400 osobnych wyszukiwan na instrument po hipertabeli i zajmowala
    ponad dwie minuty na instrument; to samo przejsciem scalajacym trwa ulamek
    sekundy.

    OKNO Z-SCORE ROZGRZEWAMY HISTORIA, tak samo jak `compute_prices` rozgrzewa
    RSI i EMA. Bez tego odchylenie liczylo sie WYLACZNIE ze swiec objetych
    biezacym przebiegiem, a nie z 30 dni, ktore cecha deklaruje w swoim opisie -
    i skutek zalezal od dlugosci okna przebiegu, nie od rynku:

      * Cykliczny worker liczy okno JEDNEJ DOBY (features.py: `since = now - 1
        dzien` bez --backfill). Na swiecy 4-godzinnej to 6 punktow siatki, wiec
        `len(hist) >= MIN_PROBEK_Z` nie zachodzilo NIGDY i `funding_z` nie
        powstawal w ogole. Cecha pojawiala sie w bazie tylko po recznym
        `--backfill`, ktory dopisywal ja WSTECZ z datowaniem `znane_od` - wiec
        wygladala na znana o czasie, choc w chwili decyzji wiersza nie bylo.
        Sygnal `funding_carry` - jedyny w zestawie niepochodzacy z ceny - wypadal
        po cichu z kazdej decyzji, bo `f.get("funding_z")` zwracalo None.
        Zmierzone na produkcji 2026-09-08: w 48 h ZERO decyzji z `funding_z`
        w migawce, przy komplecie wierszy `funding_z__4h` w `feat.feature_value`.
      * Na swiecy 1-godzinnej doba to 24 punkty, wiec prog przechodzil - ale
        z-score liczyl sie z okna 20-godzinnego zamiast 30-dniowego. Cecha
        istniala i znaczyla co innego, niz mowi jej definicja. To gorszy
        przypadek niz brak: brak widac.
    """
    cur.execute("SELECT (meta->>'funding_from')::int AS src FROM ref.instrument WHERE id=%s",
                (instrument_id,))
    row = cur.fetchone()
    src = row["src"] if row else None
    if not src:
        return 0

    step = timedelta(minutes=h.bar_minutes)
    # Okno z-score: 30 dni wyrazone w swiecach tego horyzontu.
    win = max(MIN_PROBEK_Z, 30 * 1440 // h.bar_minutes)
    rozgrzewka = step * win

    # Zapas przed poczatkiem okresu: rozgrzewka okna z-score plus dwie doby na
    # to, zeby pierwszy punkt siatki mial czym byc wypelniony (stawka ogloszona
    # jeszcze przed nim - Bybit oglasza co 8 h).
    cur.execute(
        "SELECT event_time, rate FROM market.funding_rate"
        " WHERE instrument_id = %s AND event_time >= %s AND event_time <= %s"
        " ORDER BY event_time",
        (src, since - rozgrzewka - timedelta(days=2), until),
    )
    rates = [(r["event_time"], float(r["rate"])) for r in cur.fetchall()]
    if not rates:
        return 0

    # Siatka MUSI byc ta sama co dla cech cenowych - patrz na_siatce().
    od = na_siatce(since, h)                 # od tad ZAPISUJEMY
    until = na_siatce(until, h) - step

    rows: list[tuple] = []
    hist: deque[float] = deque(maxlen=win)
    j = 0
    last: float | None = None
    t = od - rozgrzewka                      # od tad tylko LICZYMY
    while t <= until:
        while j < len(rates) and rates[j][0] <= t:
            last = rates[j][1]
            j += 1
        if last is not None:
            hist.append(last)
            if t >= od:
                ingested = znane_od(t, h)
                rows.append((feat_ids["funding_rate"], instrument_id, t, last * 100,
                             ingested))
                if len(hist) >= MIN_PROBEK_Z:
                    mean = sum(hist) / len(hist)
                    sd = math.sqrt(sum((v - mean) ** 2 for v in hist) / len(hist))
                    if sd > 1e-12:
                        z = (last - mean) / sd
                        if math.isfinite(z):
                            rows.append((feat_ids["funding_z"], instrument_id, t, z,
                                         ingested))
        t += step

    return insert_values(cur, rows)


def compute_positioning(cur, instrument_id: int, h: Horizon,
                        feat_ids: dict[str, int],
                        since: datetime, until: datetime) -> int:
    """Pozycjonowanie tłumu przeniesione na siatkę świec.

    Ta sama konstrukcja co `compute_funding` i z tego samego powodu: instrumenty
    spot nie mają własnych otwartych pozycji, więc bierzemy je z perpetuala na
    tym samym aktywie (`meta.funding_from`). To jest sygnał o rynku, nie
    o naszym instrumencie.

    KAŻDA ŚWIECA DOSTAJE OSTATNI ODCZYT SPRZED SWOJEGO ZAMKNIĘCIA, nigdy
    nadchodzący — `while ... <= t` niżej. Odczyt z 08:00 nie może wpływać na
    decyzję z 04:00, choć opisuje sąsiadujące okno. Znacznik `znane_od` mówi
    dodatkowo, od kiedy wartość była NAPRAWDĘ znana, więc backtest nie może jej
    użyć wcześniej, niż istniała.

    ZMIANA OTWARTYCH POZYCJI, NIE POZIOM. Poziom zależy od wielkości rynku
    i rośnie latami, więc jako cecha byłby głównie zegarem — model nauczyłby się
    z niego daty, nie zachowania. Porównujemy wobec POPRZEDNIEJ ŚWIECY tego
    horyzontu, a nie wobec poprzedniego odczytu godzinowego: inaczej cecha
    świecy czterogodzinnej opisywałaby jedną godzinę z czterech.

    OKNO Z-SCORE ROZGRZEWAMY HISTORIĄ — z tego samego powodu i tym samym
    sposobem co `compute_funding`; opis przyczyny jest tam. W skrócie: liczone
    wyłącznie ze świec objętych bieżącym przebiegiem, `long_ratio_z` nie
    powstawał w cyklicznym workerze ani razu, bo doba to sześć świec 4 h.
    """
    cur.execute("SELECT (meta->>'funding_from')::int AS src FROM ref.instrument WHERE id=%s",
                (instrument_id,))
    row = cur.fetchone()
    src = row["src"] if row else None
    if not src:
        return 0

    step = timedelta(minutes=h.bar_minutes)
    win = max(MIN_PROBEK_Z, 30 * 1440 // h.bar_minutes)
    rozgrzewka = step * win

    cur.execute(
        "SELECT event_time, long_ratio, open_interest FROM market.positioning"
        " WHERE instrument_id = %s AND event_time >= %s AND event_time <= %s"
        "   AND (long_ratio IS NOT NULL OR open_interest IS NOT NULL)"
        " ORDER BY event_time",
        (src, since - rozgrzewka - timedelta(days=2), until),
    )
    odczyty = [(r["event_time"],
                float(r["long_ratio"]) if r["long_ratio"] is not None else None,
                float(r["open_interest"]) if r["open_interest"] is not None else None)
               for r in cur.fetchall()]
    if not odczyty:
        return 0

    # Siatka MUSI byc ta sama co dla cech cenowych - patrz na_siatce().
    od = na_siatce(since, h)                 # od tad ZAPISUJEMY
    until = na_siatce(until, h) - step

    rows: list[tuple] = []
    hist: deque[float] = deque(maxlen=win)
    j = 0
    udzial: float | None = None
    oi: float | None = None
    oi_poprzednie: float | None = None
    t = od - rozgrzewka                      # od tad tylko LICZYMY
    while t <= until:
        while j < len(odczyty) and odczyty[j][0] <= t:
            # Każde źródło niesie się osobno: brak jednego w danej godzinie nie
            # może skasować ostatniej znanej wartości drugiego.
            if odczyty[j][1] is not None:
                udzial = odczyty[j][1]
            if odczyty[j][2] is not None:
                oi = odczyty[j][2]
            j += 1

        ingested = znane_od(t, h)
        if udzial is not None:
            hist.append(udzial)
            if t >= od:
                rows.append((feat_ids["long_ratio"], instrument_id, t, udzial * 100,
                             ingested))
                if len(hist) >= MIN_PROBEK_Z:
                    mean = sum(hist) / len(hist)
                    sd = math.sqrt(sum((v - mean) ** 2 for v in hist) / len(hist))
                    if sd > 1e-12:
                        z = (udzial - mean) / sd
                        if math.isfinite(z):
                            rows.append((feat_ids["long_ratio_z"], instrument_id, t, z,
                                         ingested))
        if oi is not None:
            if oi_poprzednie is not None and oi_poprzednie > 0 and t >= od:
                zmiana = (oi / oi_poprzednie - 1.0) * 100
                if math.isfinite(zmiana):
                    rows.append((feat_ids["oi_zmiana"], instrument_id, t, zmiana,
                                 ingested))
            oi_poprzednie = oi
        t += step

    return insert_values(cur, rows)


def insert_values(cur, rows: list[tuple]) -> int:
    """Zapis wartosci cech, POPRAWIAJACY istniejace - i DATUJACY poprawke.

    DLACZEGO NIE "DO NOTHING"

    Cecha jest deterministyczna funkcja swiec. Gdy swieca byla w bazie bledna,
    bledna byla tez cecha - a przy pomijaniu konfliktu zostawala taka na zawsze,
    bo kazdy pozniejszy przebieg trafial na istniejacy wiersz i milczal.

    Zdarzylo sie to 2026-09-06: swiece godzinowe zapisywane w trakcie ich
    trwania mialy zakres rowny 39% faktycznego, a wszystkie cechy z nich
    policzone - ATR, polozenie w zakresie, rezim - dziedziczyly to zanizenie.
    Poprawienie swiec nie naprawilo cech; dopiero to.

    DWIE DATY, BO TO SA DWA ROZNE FAKTY (migracja 0070)

    * `ingested_at = znane_od(t, h)` - od kiedy wartosc BYLA DO WYLICZENIA
      z danych dostepnych wtedy na gieldzie. Informacja istniala; zepsuta byla
      nasza kopia. Dlatego ta data zostaje wyliczana tak samo przy poprawce, a nie
      ustawiana na teraz - inaczej `train.load_panel` wyrzucilby kazda przeliczona
      wstecz ceche i trener uczylby sie na dziurach dokladnie tam, gdzie dane
      byly zle.
    * `revised_at = now()` - KIEDY nasza kopia zostala poprawiona. Ustawiane
      WYLACZNIE przy nadpisaniu; nowy wiersz ma NULL.

    Do 2026-10-03 byla tylko pierwsza data, a docstring nazywal to swiadomym
    wyborem ("to nie jest przepisywanie historii"). Dla WARTOSCI to prawda; dla
    DECYZJI nie: silnik w chwili T przeczytal przez `feat.as_of` stara liczbe,
    a po poprawce baza twierdzila, ze od T byla tam nowa. Oba strazniki
    bitemporalne przepuszczaly rewizje z definicji, wiec rozjazd "na czym silnik
    decydowal" vs "na czym trener sie uczy" byl niemierzalny. `revised_at` go
    mierzy: trener raportuje odsetek wartosci zrewidowanych po chwili decyzji,
    a kontrola startowa silnika - liczbe takich wartosci z ostatnich 7 dni.

    Warunek IS DISTINCT FROM sprawia, ze przebieg bez zmian nie dotyka bazy -
    i nie przesuwa `revised_at`: rewizja to ZMIANA wartosci, nie przeliczenie.
    """
    sql = ("INSERT INTO feat.feature_value (feature_def_id, instrument_id, event_time,"
           " value, ingested_at) VALUES (%s,%s,%s,%s,%s)"
           " ON CONFLICT (feature_def_id, instrument_id, event_time) DO UPDATE SET"
           " value = EXCLUDED.value, ingested_at = EXCLUDED.ingested_at,"
           " revised_at = now()"
           " WHERE feat.feature_value.value IS DISTINCT FROM EXCLUDED.value")
    for k in range(0, len(rows), BATCH):
        cur.executemany(sql, rows[k:k + BATCH])
    return len(rows)


# Ile cykli pod rzad zakonczonych bledem konczy proces. Wpis w logu NIE JEST
# widocznoscia - logu poda nikt nie czyta. Restart poda jest: rosnie licznik
# restartow i pod wpada w CrashLoopBackOff.
POD_RZAD_MAX = 5


def parsuj_bary(spec: str) -> list[str]:
    """Jedna swieca albo lista po przecinku - JEDEN PROCES liczy wszystkie.

    Chart wolal dotad `for B in 4h 1h; do ... --bar $B --once || true; done`,
    czyli petle po horyzontach w shellu. Lista wchodzi tu, zeby pod mial jeden
    proces z jedna petla, a nie skrypt uruchamiajacy Pythona dwa razy na cykl.

    Nieznana swieca konczy proces OD RAZU: to blad konfiguracji, a nie awaria
    przebiegu, wiec ponawianie go w petli tylko odsuwaloby moment, w ktorym
    ktos to zobaczy.
    """
    bary = [s.strip() for s in spec.split(",") if s.strip()]
    if not bary:
        sys.exit("--bar nie moze byc puste")
    zle = [b for b in bary if b not in HORIZONS]
    if zle:
        sys.exit(f"nieznane swiece: {', '.join(zle)}; "
                 f"dostepne: {', '.join(sorted(HORIZONS))}")
    return list(dict.fromkeys(bary))     # kolejnosc zachowana, bez powtorzen


def przebieg(conn, h: Horizon, *, engine: str, tylko: list[str],
             tylko_przekroj: bool, since: datetime, now: datetime,
             verbose: bool = True) -> int:
    """Policzenie cech JEDNEGO horyzontu na GOTOWYM polaczeniu.

    Polaczenie przychodzi z zewnatrz, bo petla w `main` otwiera je na kazdy
    cykl - patrz komentarz tamze.
    """
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        "SELECT id, symbol FROM ref.instrument WHERE engine = %s AND active"
        " AND kind <> 'perp' AND (%s::text[] IS NULL OR symbol = ANY(%s))"
        " ORDER BY symbol", (engine, tylko or None, tylko or None))
    instruments = cur.fetchall()
    if not instruments:
        sys.exit(f"brak instrumentów: {', '.join(tylko) or engine}")
    brak = sorted(set(tylko) - {i["symbol"] for i in instruments})
    if brak:
        sys.exit(f"nie ma w bazie albo nieaktywne: {', '.join(brak)}")

    # UNIWERSUM PRZEKROJOWE to nie to samo co lista liczonych instrumentow.
    #
    # Bez --instrumenty petla per instrument idzie po WSZYSTKICH aktywnych
    # (historie zbieramy szerzej, niz handlujemy), ale ranking liczy sie
    # wylacznie po uniwersum decyzyjnym - to samo `tradeable`, ktore czyta
    # silnik. Odcisk musi opisywac ten drugi zbior, inaczej produkcja
    # szukalaby cechy pod nazwa, ktorej nikt nie zapisal.
    if tylko:
        uniwersum = sorted(tylko)
    else:
        cur.execute(
            "SELECT symbol FROM ref.instrument WHERE engine = %s AND active"
            " AND tradeable AND kind <> 'perp' ORDER BY symbol", (engine,))
        uniwersum = [r["symbol"] for r in cur.fetchall()]

    feat_ids = ensure_definitions(cur, h, uniwersum or None)
    conn.commit()

    total = 0
    for inst in instruments:
        if tylko_przekroj:
            break
        n = compute_prices(cur, inst["id"], h, feat_ids, since, now)
        conn.commit()
        m = compute_news(cur, inst["id"], h, feat_ids["news_sent"], since, now)
        conn.commit()
        f = compute_funding(cur, inst["id"], h, feat_ids, since, now)
        f += compute_positioning(cur, inst["id"], h, feat_ids, since, now)
        conn.commit()
        b = compute_orderbook(cur, inst["id"], h, feat_ids, since, now)
        conn.commit()
        total += n + m + f + b
        if verbose:
            print(f"  {inst['symbol']:10s} {n:>9,} cen  {m:>7,} news  {f:>7,} funding"
                  f"  {b:>5,} arkusz")

    # Przekrojowosc liczymy RAZ, po policzeniu zwrotow - z natury potrzebuje
    # wszystkich instrumentow zbioru naraz.
    #
    # Zbiorem jest UNIWERSUM DECYZYJNE albo, przy --instrumenty, dokladnie
    # podana lista. To drugie jest konieczne, zeby przygotowac uniwersum
    # kandydujace PRZED jego wlaczeniem: bez sily wzglednej porownanie
    # szloby na siedmiu sygnalach zamiast osmiu, czyli mierzyloby inna
    # strategie, a nie inne uniwersum.
    # PROG DOTYCZY ZBIORU RANKINGU, nie listy liczonych instrumentow.
    #
    # Bez --instrumenty `instruments` to wszystkie aktywne (34), a ranking
    # idzie po uniwersum decyzyjnym (10). Sprawdzanie tej pierwszej liczby
    # przepuszczaloby ranking po trzech parach, dopoki w bazie lezy historia
    # trzydziestu czterech - czyli dokladnie wtedy, gdy prog jest potrzebny.
    if len(uniwersum) < MIN_UNIVERSE:
        print(f"\n  siła względna: pominięta, {len(uniwersum)} instrumentów "
              f"w uniwersum przy progu {MIN_UNIVERSE}")
    else:
        if "rel_strength" not in feat_ids:
            sys.exit("brak definicji cechy przekrojowej — uniwersum puste?")
        x = compute_cross_section(cur, h, feat_ids, since, now,
                                  [i["id"] for i in instruments] if tylko else None,
                                  engine=engine)
        conn.commit()
        total += x
        zbior = (f"podany zbiór, odcisk {odcisk(uniwersum)}" if tylko
                 else f"uniwersum decyzyjne, odcisk {odcisk(uniwersum)}")
        if verbose:
            print(f"\n  siła względna ({zbior}): {x:>9,}")

    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    # Bez `choices`, bo wartoscia moze byc LISTA po przecinku; sprawdza
    # `parsuj_bary`, ktore wypisuje dostepne swiece tak samo jak argparse.
    ap.add_argument("--bar", default=DEFAULT_BAR,
                    help="świeca albo lista po przecinku (np. 4h,1h) — jeden proces "
                         f"liczy wszystkie. Dostępne: {', '.join(sorted(HORIZONS))}")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--days", type=int, default=90,
                    help="glebokosc JEDNORAZOWEGO backfillu w dniach; "
                         "cyklu rutynowego NIE dotyczy — ten ma --okno-dni")
    # OKNO CYKLU RUTYNOWEGO, OSOBNO OD GLEBOKOSCI BACKFILLU.
    #
    # Do 2026-09-24 okno cyklu bylo zaszyte na sztywno jako jedna doba, a
    # `--days` dzialalo wylacznie z `--backfill`. Na rynku notowanym bez przerwy
    # doba wystarcza: po kazdej swiecy nastepna przychodzi godzine pozniej,
    # wiec kazda zdazy trafic w okno, zanim to okno ja minie.
    #
    # Na gieldzie nie. `compute_prices` pomija swiece OSTATNIA z panelu, bo ta
    # jest jeszcze otwarta - swieca dostaje cechy dopiero w cyklu, w ktorym
    # istnieje juz swieca po niej. Po piatkowym zamknieciu (19:00 UTC) nastepna
    # swieca pojawia sie w poniedzialek o 13:00, czyli 65 godzin pozniej -
    # a wtedy piatkowa lezy juz POZA oknem jednodobowym i nie dostanie cech
    # NIGDY. Zmierzone: przy oknie 1 dnia cykl poniedzialkowy zapisuje 0
    # wartosci dla piatkowej swiecy, przy oknie 7 dni - komplet osmiu.
    #
    # Dziura byla systematyczna (co tydzien, kazdy instrument) i niewidoczna:
    # backfill na 90 dniach zasypuje ja natychmiast, bo tam okno obejmuje
    # nastepna swiece. Widac ja wylacznie w pracy ciaglej.
    #
    # SIEDEM DNI, nie cztery: cztery starcza na zwykly dlugi weekend, ale nie
    # na przerwy swiateczne ani na zamkniecia nadzwyczajne (po 11 wrzesnia
    # gielda stala cztery sesje). Okno liczy sie ponownie co cykl i wpisy
    # ida przez ON CONFLICT DO NOTHING, wiec szersze okno kosztuje tylko
    # powtorzone porownanie kilkudziesieciu swiec - a wezsze gubi dane trwale.
    ap.add_argument("--okno-dni", type=int, default=1, dest="okno_dni",
                    help="ile dni wstecz przelicza CYKL RUTYNOWY. Domyslnie 1 "
                         "(rynek ciagly). Dla rynku z przerwami musi przykryc "
                         "najdluzsza przerwe w notowaniach — dla gieldy USA 7")
    ap.add_argument("--once", action="store_true", help="jeden cykl zamiast pętli")
    ap.add_argument("--interval", type=int, default=900,
                    help="przerwa między cyklami (s)")
    ap.add_argument("--quiet", action="store_true",
                    help="wycisz rutynowy postęp; OPISY BŁĘDÓW idą zawsze")
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--tylko-przekroj", action="store_true", dest="tylko_przekroj",
                    help="przelicz WYLACZNIE sile wzgledna, pomijajac cechy "
                         "per instrument. Do naprawy rozjazdu po zmianie "
                         "uniwersum: ranking przekrojowy zalezy od ZBIORU, wiec "
                         "zmiana uniwersum zmienia historyczne wartosci tej "
                         "cechy - a pozostale sa od zbioru niezalezne i ich "
                         "przeliczanie tylko kosztuje")
    ap.add_argument("--instrumenty", default="",
                    help="lista symboli po przecinku - liczy cechy WYLACZNIE dla nich, "
                         "a sile wzgledna w obrebie dokladnie tego zbioru. Sluzy "
                         "przygotowaniu uniwersum kandydujacego przed jego wlaczeniem. "
                         "Puste = uniwersum z bazy")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    bary = parsuj_bary(args.bar)
    tylko = [s.strip().upper() for s in args.instrumenty.split(",") if s.strip()]

    # --backfill KONCZY SIE PO JEDNYM PRZEBIEGU, nawet bez --once.
    #
    # To operacja reczna na 1400 dniach, trwajaca dziesiatki minut. Wpadniecie
    # z nia w petle domyslna oznaczaloby przeliczanie calej historii w kolko,
    # az ktos to zauwazy - a w logu wyglada to dokladnie jak dlugo trwajacy
    # backfill.
    jednorazowo = args.once or args.backfill

    print(f"Cechy na świecach {', '.join(bary)}"
          + ("" if jednorazowo else f", cykl co {args.interval} s"))

    # PETLA JEST TUTAJ, NIE W SHELLU, A POLACZENIE OTWIERA SIE NA KAZDY CYKL.
    #
    # Do 2026-09-07 pod chodzil na `while true; do for B in 4h 1h; do
    # features.py --bar $B --once || true; done; sleep 900; done`. `|| true`
    # polykal KAZDY niezerowy wynik, wiec cechy mogly stac od dni, a pod stal
    # "Running" z zerowym licznikiem restartow. Przy okazji: `--once` w tamtej
    # komendzie NIC NIE ROBILO - flaga byla martwa (`args.once` nie bylo nigdzie
    # czytane), a jej opis "tylko ostatnia doba" opisywal zachowanie domyslne.
    #
    # Polaczenie trzymane na caly proces byloby zmiana NA GORSZE: po restarcie
    # bazy kazdy kolejny cykl padalby na tym samym zerwanym gniezdzie,
    # w nieskonczonosc - martwy proces wygladajacy na zywy.
    pod_rzad = 0
    puls.zyje(args.interval)
    while True:
        t0 = datetime.now(timezone.utc)
        # OKNO LICZONE NA KAZDY CYKL. Wyliczone raz przed petla zamarzloby
        # w chwili startu procesu i po tygodniu pracy proces liczylby cechy
        # dla doby sprzed tygodnia.
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=args.days if args.backfill else args.okno_dni)
        bledy: list[str] = []
        total = 0
        try:
            with psycopg.connect(url, row_factory=dict_row) as conn:
                for bar in bary:
                    # Awaria jednego horyzontu nie moze pominac pozostalych
                    # w tym samym cyklu - to jedyne, co bylo dobrego w `|| true`.
                    try:
                        n = przebieg(conn, get_horizon(bar), engine=args.engine,
                                     tylko=tylko, tylko_przekroj=args.tylko_przekroj,
                                     since=since, now=now, verbose=not args.quiet)
                        total += n
                        if not args.quiet:
                            print(f"  [{bar}] {n:,} wartości cech")
                    except Exception as e:                    # noqa: BLE001
                        conn.rollback()
                        bledy.append(f"{bar}: {type(e).__name__}: {e}")

                # PULS PO CALYM CYKLU, nie po kazdym horyzoncie: cykl to
                # przeliczenie WSZYSTKICH swiec z `--bar`. Bicie po pierwszym
                # horyzoncie meldowaloby zdrowie procesu, ktory od tygodnia
                # wywala sie na drugim - a to jest dokladnie ta awaria, ktora
                # 2026-09-04 kosztowala 21 godzin martwych cech 1h.
                #
                # Warunek jest TYM SAMYM warunkiem, ktory nizej zeruje licznik
                # `pod_rzad`: pusta lista bledow.
                if bledy:
                    puls.zglos_awarie(conn, _puls(args.engine), args.interval,
                                      pod_rzad + 1,
                                      bledy[0])
                else:
                    # Blad zapisu pulsu NIE jest tu lapiany (patrz `puls.bij`):
                    # wpadnie do `except` nizej i policzy sie jako awaria cyklu.
                    # Worker, ktorego czujka nie widzi, jest dla nadzoru
                    # nieodrozninalny od workera, ktorego nie ma.
                    puls.bij(conn, _puls(args.engine), args.interval,
                             {"engine": args.engine, "bary": bary,
                              "wartosci": total, "backfill": bool(args.backfill)})
        except Exception as e:                                # noqa: BLE001
            # Tu wpada przede wszystkim nieudane POLACZENIE z baza.
            bledy.append(f"{type(e).__name__}: {e}")

        if not args.quiet:
            print(f"[{t0:%H:%M:%S}] razem {total:,} wartości cech, "
                  f"błędów {len(bledy)}")
        # OPISY BLEDOW WYPISUJEMY ZAWSZE, TAKZE PRZY --quiet: `--quiet` wycisza
        # rutynowy postep, a nie awarie.
        for blad in dict.fromkeys(bledy):
            print(f"    BŁĄD {blad}")

        if bledy:
            pod_rzad += 1
            print(f"[{t0:%H:%M:%S}] cykl nieudany ({pod_rzad}/{POD_RZAD_MAX})")
            if pod_rzad >= POD_RZAD_MAX:
                print(f"{POD_RZAD_MAX} cykli pod rząd zakończonych błędem — "
                      f"kończę proces, żeby restart był WIDOCZNY zamiast cichy")
                return 1
        else:
            pod_rzad = 0

        # Obrot petli, takze nieudany - sonda zywotnosci (`puls.zyje`).
        puls.zyje(args.interval)
        if jednorazowo:
            return 1 if bledy else 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
