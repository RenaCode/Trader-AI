"""Brak przecieku przyszłości — WŁASNOŚĆ KAŻDEJ ROLI CECHY (audyt Fable U-2).

DLACZEGO TEN PLIK ISTNIEJE

`test_przeciek_przyszlosci.py` pilnuje strażników czasu (`feat.as_of`,
`train.load_panel`) i sam przyznaje, czego nie potrafi: potok wpisuje
`ingested_at = znane_od(event_time)` każdej roli, więc strażnik przepuści
cechę policzoną z przyszłości bez śladu. Pytanie „czy wartość cechy w T jest
funkcją wyłącznie przeszłości" nie miało testu dla żadnej roli wprost.

WŁASNOŚĆ, KTÓRĄ SPRAWDZAMY

Dla świecy T (początek; wartość znana od jej zamknięcia `T + bar`):

  1. ZMIANA PRZYSZŁOŚCI nie zmienia cechy w chwilach <= T. Przyszłość to
     wszystko, co zdarzyło się od zamknięcia świecy T: świece od `T + bar`,
     stawki finansowania, odczyty pozycjonowania, migawki arkusza, artykuły
     i analizy z czasem >= `T + bar`.
  2. BRAK PRZYSZŁOŚCI daje w T tę samą liczbę co pełna historia — czyli
     wartość, którą silnik dostaje na żywo w chwili `znane_od(T)`, jest tą
     samą, na której potem uczy się trener (backfill).
  3. Kanarek: zmiana przyszłości ZMIENIA cechę po T. Bez tego (1) byłoby
     prawdziwe także dla testu, który niczego nie zmienił.

DWA TORY

  * Bez bazy — role liczone w Pythonie (`compute_prices`, `compute_funding`,
    `compute_positioning`). Kursor odtwarza WYŁĄCZNIE warunki czasowe zapytań
    (granice z parametrów), bo to one są częścią własności; arytmetyka cechy
    wykonuje się naprawdę.
  * Z bazą (TEST_DB_URL, CI ma ją zawsze) — WSZYSTKIE role przez prawdziwe
    zapytania. Role agregowane w SQL (`news_sent`, `book_*`, `rel_strength`)
    mają tę własność w klauzulach WHERE / `time_bucket` / `percent_rank`,
    a atrapa, która sama odtwarza te klauzule, dowodziłaby tylko atrapy.
"""
from __future__ import annotations

import os
import random
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from services.engine.horizons import H1H, H4H
from services.ingest import features
from services.ingest.features import ROLES, znane_od

# Wszystkie role, każda w dokładnie jednym torze. Nowa rola w `ROLES` bez
# przypisania wywraca `test_kazda_rola_ma_tor` — inaczej wypadłaby z testu cicho.
ROLE_PYTHON = (*features.PRICE_ROLES, "funding_rate", "funding_z",
               "long_ratio", "long_ratio_z", "oi_zmiana")
ROLE_SQL = ("news_sent", "book_imbalance", "book_spread_bps", "rel_strength")

T = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
ZRODLO = 99                         # perpetual, z którego spot bierze funding


def test_kazda_rola_ma_tor():
    assert sorted(ROLE_PYTHON + ROLE_SQL) == sorted(ROLES), (
        "rola w `features.ROLES` bez testu przecieku — dopisz ją do ROLE_PYTHON "
        "albo ROLE_SQL")
    assert not set(ROLE_PYTHON) & set(ROLE_SQL)


# ------------------------------------------------------------------ dane

def _krok(h):
    return timedelta(minutes=h.bar_minutes)


def _lookback(h):
    return max(h.ret_long, h.range_window, h.volume_lookback, h.ema_slow,
               h.warmup, features.bufor_ema(h)) + 5


def dane(h, seed=7):
    """Historia z zapasem na najdłuższą rozgrzewkę i 30 świec po T."""
    rnd = random.Random(seed)
    krok = _krok(h)
    start = T - krok * (_lookback(h) + 40)
    koniec = T + krok * 30
    swiece, cena, t = [], 100.0, start
    while t < koniec:
        o = cena
        cena *= 1 + rnd.gauss(0, 0.01)
        swiece.append({"t": t, "open": o, "high": max(o, cena) * (1 + rnd.random() * 0.004),
                       "low": min(o, cena) * (1 - rnd.random() * 0.004), "close": cena,
                       "volume": 1000 * (0.5 + rnd.random())})
        t += krok
    # Finansowanie i pozycjonowanie co godzinę — od 45 dni przed T, bo z-score
    # rozgrzewa się 30 dniami historii. Bybit ogłasza stawkę co 8 h, ale gęstsza
    # siatka jest tu CZULSZA: odczyt na samej granicy `T + bar` istnieje dla
    # obu horyzontów, więc podejrzenie choćby jednej świecy naprzód wywraca test.
    od = T - timedelta(days=45)
    funding = [(od + timedelta(hours=k), rnd.gauss(0.0001, 0.0002))
               for k in range(int((koniec - od) / timedelta(hours=1)) + 1)]
    oi, pozycjonowanie = 1e6, []
    for k in range(int((koniec - od) / timedelta(hours=1)) + 1):
        oi *= 1 + rnd.gauss(0, 0.01)
        pozycjonowanie.append((od + timedelta(hours=k), 0.5 + rnd.gauss(0, 0.05), oi))
    return {"swiece": swiece, "funding": funding, "pozycjonowanie": pozycjonowanie}


def zmien_przyszlosc(d, granica):
    """Wszystko od `granica` (zamknięcie świecy T) — inne, i to wyraźnie."""
    return {
        "swiece": [b if b["t"] < granica else
                   {**b, **{k: b[k] * 1.37 for k in ("open", "high", "low", "close")},
                    "volume": b["volume"] * 3}
                   for b in d["swiece"]],
        "funding": [(t, r if t < granica else -5 * r + 0.001) for t, r in d["funding"]],
        "pozycjonowanie": [(t, lr, oi) if t < granica else (t, 1 - lr, oi * 2)
                           for t, lr, oi in d["pozycjonowanie"]],
    }


def utnij_przyszlosc(d, granica):
    return {"swiece": [b for b in d["swiece"] if b["t"] < granica],
            "funding": [x for x in d["funding"] if x[0] < granica],
            "pozycjonowanie": [x for x in d["pozycjonowanie"] if x[0] < granica]}


# ------------------------------------------------------------------ tor bez bazy

class KursorCech:
    """Odpowiada na zapytania potoku cech, odtwarzając WYŁĄCZNIE ich granice
    czasowe (z parametrów). Zapis (`insert_values`) zbiera wiersze."""

    def __init__(self, d):
        self.d, self.wiersze, self._wynik = d, [], []

    def execute(self, sql, params=None):
        if "funding_from" in sql:
            self._wynik = [{"src": ZRODLO}]
        elif "FROM market.funding_rate" in sql:
            src, od, do = params
            assert src == ZRODLO
            self._wynik = [{"event_time": t, "rate": r}
                           for t, r in self.d["funding"] if od <= t <= do]
        elif "FROM market.positioning" in sql:
            src, od, do = params
            assert src == ZRODLO
            self._wynik = [{"event_time": t, "long_ratio": lr, "open_interest": oi}
                           for t, lr, oi in self.d["pozycjonowanie"] if od <= t <= do]
        else:
            raise AssertionError(f"zapytanie spoza toru bez bazy: {sql[:80]}")

    def fetchone(self):
        return self._wynik[0] if self._wynik else None

    def fetchall(self):
        return self._wynik

    def executemany(self, sql, rows):
        self.wiersze.extend(rows)


def _load_bars(d):
    """`load_bars` z tymi samymi granicami co zapytanie: rozgrzewka to N świec
    przed `since`, okno to [since, until); przy agregacji `n` liczy świece
    źródłowe zaczęte przed `until` (kubeł w trakcie ma ich mniej)."""
    def load_bars(cur, instrument_id, h, since, until, *, rozgrzewka=0):
        przed = [b for b in d["swiece"] if b["t"] < since][-rozgrzewka:] if rozgrzewka else []
        okno = [b for b in d["swiece"] if since <= b["t"] < until]
        out = []
        for b in przed + okno:
            b = dict(b)
            if h.aggregate:
                zrodlo = timedelta(minutes=h.bar_minutes // h.swiec_zrodlowych_na_swiece)
                b["n"] = sum(1 for k in range(h.swiec_zrodlowych_na_swiece)
                             if b["t"] + k * zrodlo < until)
            out.append(b)
        return out
    return load_bars


IDS = {rola: i for i, rola in enumerate(ROLES, start=1)}
ROLA_ID = {i: rola for rola, i in IDS.items()}


def policz(d, h, until, monkeypatch) -> dict[tuple[str, datetime], float]:
    monkeypatch.setattr(features, "load_bars", _load_bars(d))
    cur = KursorCech(d)
    since = T - 5 * _krok(h)
    features.compute_prices(cur, 1, h, IDS, since, until)
    features.compute_funding(cur, 1, h, IDS, since, until)
    features.compute_positioning(cur, 1, h, IDS, since, until)
    return {(ROLA_ID[fid], t): v for fid, _iid, t, v, _ing in cur.wiersze}


@pytest.mark.parametrize("h", [H1H, H4H], ids=lambda h: h.bar)
@pytest.mark.parametrize("rola", ROLE_PYTHON)
def test_cecha_w_T_nie_zalezy_od_przyszlosci(rola, h, monkeypatch):
    d = dane(h)
    granica = T + _krok(h)
    pelna = T + 40 * _krok(h)
    a = policz(d, h, pelna, monkeypatch)
    b = policz(zmien_przyszlosc(d, granica), h, pelna, monkeypatch)
    c = policz(utnij_przyszlosc(d, granica), h, znane_od(T, h), monkeypatch)

    assert (rola, T) in a, f"{rola}: brak wartości w T — test niczego by nie sprawdził"
    przeszlosc = [t for (r, t) in a if r == rola and t <= T]
    zmienione = [t for t in przeszlosc if b.get((rola, t)) != a[(rola, t)]]
    assert not zmienione, (
        f"PRZECIEK {rola} ({h.bar}): zmiana danych od {granica:%m-%d %H:%M} "
        f"zmieniła cechę w {[f'{t:%m-%d %H:%M}' for t in zmienione]}")
    assert c.get((rola, T)) == a[(rola, T)], (
        f"{rola} ({h.bar}): na żywo w chwili znane_od(T) wychodzi "
        f"{c.get((rola, T))}, z pełnej historii {a[(rola, T)]} — silnik "
        f"decyduje na innej liczbie, niż uczy się trener")
    assert any(b.get((r, t)) != v for (r, t), v in a.items() if r == rola and t > T), (
        f"{rola}: zmiana przyszłości nie zmieniła cechy także PO T — kanarek "
        f"martwy, test nie dowodzi niczego")


# ------------------------------------------------------------------ tor z bazą

TEST_DB_URL = os.environ.get("TEST_DB_URL")
wymaga_bazy = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="ustaw TEST_DB_URL na jednorazową bazę z zastosowanymi migracjami")

# Ranking przekrojowy potrzebuje co najmniej MIN_UNIVERSE instrumentów naraz.
N_INSTRUMENTOW = features.MIN_UNIVERSE


def _polacz():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(TEST_DB_URL, row_factory=dict_row)


def _zasiej(cur, h, d_per_inst):
    """Instrumenty, świece źródłowe, funding/pozycjonowanie (z perpetuala),
    migawki arkusza i newsy. Zwraca (ids instrumentów, id perpetuala)."""
    cur.execute("SELECT id FROM ref.venue LIMIT 1")
    venue = cur.fetchone()["id"]
    sufiks = uuid.uuid4().hex[:8]
    cur.execute(
        "INSERT INTO ref.instrument (venue_id, symbol, engine, quote_ccy, kind,"
        " active, tradeable) VALUES (%s,%s,'crypto','USDC','perp',true,false)"
        " RETURNING id", (venue, f"PRZ{sufiks}PERP"))
    perp = cur.fetchone()["id"]
    ids = []
    for k in range(N_INSTRUMENTOW):
        cur.execute(
            "INSERT INTO ref.instrument (venue_id, symbol, engine, quote_ccy, kind,"
            " active, tradeable, meta) VALUES (%s,%s,'crypto','USDC','spot',true,"
            " true, %s::jsonb) RETURNING id",
            (venue, f"PRZ{sufiks}{k}USDC", '{"funding_from": %d}' % perp))
        ids.append(cur.fetchone()["id"])
    cur.execute("INSERT INTO news.feed (name, url) VALUES (%s,%s) RETURNING id",
                (f"przeciek {sufiks}", f"https://przeciek.test/{sufiks}"))
    feed = cur.fetchone()["id"]
    zrodlo = timedelta(minutes=h.bar_minutes // h.swiec_zrodlowych_na_swiece)
    for iid, d in zip(ids, d_per_inst):
        wiersze = []
        for b in d["swiece"]:
            # Świeca horyzontu rozpisana na świece źródłowe o tym samym
            # zamknięciu: agregat (open pierwszej, close ostatniej) jest wtedy
            # dokładnie świecą `b`.
            for j in range(h.swiec_zrodlowych_na_swiece):
                ostatnia = j == h.swiec_zrodlowych_na_swiece - 1
                wiersze.append((iid, h.source_timeframe, b["t"] + j * zrodlo,
                                b["open"] if j == 0 else b["close"],
                                b["high"], b["low"],
                                b["close"] if ostatnia else b["open"],
                                b["volume"] / h.swiec_zrodlowych_na_swiece))
        cur.executemany(
            "INSERT INTO market.candle (instrument_id, timeframe, event_time,"
            " open, high, low, close, volume) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            wiersze)
        # 30 migawek arkusza na świecę z ostatnich 10 świec przed i po T.
        migawki = []
        for b in d["swiece"][-50:]:
            for m in range(30):
                t = b["t"] + m * _krok(h) / 30
                mid = b["close"]
                migawki.append((iid, t, mid, mid * 0.0004 * (1 + (m % 7) / 10),
                                1.0 + m, 2.0 + (m % 5), ((m % 11) - 5) / 10))
        cur.executemany(
            "INSERT INTO market.book_snapshot (instrument_id, event_time, mid,"
            " spread, bid_volume, ask_volume, imbalance)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s)", migawki)
        # Artykuł na świecę, analiza godzinę po publikacji; połowa z kierunkiem.
        for n, b in enumerate(d["swiece"][-50:]):
            pub = b["t"] + timedelta(minutes=10)
            cur.execute(
                "INSERT INTO news.article (feed_id, url, title, content_hash, event_time)"
                " VALUES (%s,%s,'t',%s,%s) RETURNING id",
                (feed, f"https://przeciek.test/{sufiks}/{iid}/{n}",
                 uuid.uuid4().bytes, pub))
            art = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO news.article_instrument (article_id, instrument_id,"
                " relevance) VALUES (%s,%s,0.8)", (art, iid))
            cur.execute(
                "INSERT INTO news.article_analysis (article_id, model, prompt_version,"
                " direction, magnitude, horizon, confidence, novelty, raw, event_time,"
                " ingested_at) VALUES (%s,'m','p',%s,0.6,'hours',0.7,0.9,'{}',%s,%s)",
                (art, ("bullish", "bearish", "neutral")[n % 3], pub,
                 pub + timedelta(hours=1)))
    d0 = d_per_inst[0]
    cur.executemany(
        "INSERT INTO market.funding_rate (instrument_id, event_time, rate)"
        " VALUES (%s,%s,%s)", [(perp, t, r) for t, r in d0["funding"]])
    cur.executemany(
        "INSERT INTO market.positioning (instrument_id, event_time, long_ratio,"
        " open_interest) VALUES (%s,%s,%s,%s)",
        [(perp, t, lr, oi) for t, lr, oi in d0["pozycjonowanie"]])
    return ids, perp


def _zmien_przyszlosc_w_bazie(cur, ids, perp, granica):
    # Mnożnik ZALEŻNY OD INSTRUMENTU: ten sam dla wszystkich nie przestawiłby
    # rankingu i `rel_strength` po T wyszłoby bez zmian (martwy kanarek).
    cur.execute(
        "UPDATE market.candle SET open=open*m, high=high*m, low=low*m,"
        " close=close*m, volume=volume*3"
        " FROM (SELECT unnest(%s::int[]) AS iid,"
        "              1.37 + 0.11 * generate_series(0, %s - 1) AS m) k"
        " WHERE instrument_id = k.iid AND event_time >= %s",
        (ids, len(ids), granica))
    cur.execute(
        "UPDATE market.funding_rate SET rate = -5*rate + 0.001"
        " WHERE instrument_id=%s AND event_time >= %s", (perp, granica))
    cur.execute(
        "UPDATE market.positioning SET long_ratio = 1 - long_ratio,"
        " open_interest = open_interest*2 WHERE instrument_id=%s AND event_time >= %s",
        (perp, granica))
    cur.execute(
        "UPDATE market.book_snapshot SET imbalance = -imbalance, spread = spread*5"
        " WHERE instrument_id = ANY(%s) AND event_time >= %s", (ids, granica))
    # Przyszłość newsów to i artykuły z przyszłości, i SPÓŹNIONA analiza
    # artykułu starego — obie nie mogą ruszyć cechy w T.
    cur.execute(
        "UPDATE news.article_analysis an SET direction='bullish', magnitude=1,"
        " confidence=1, novelty=1 FROM news.article_instrument ai"
        " WHERE ai.article_id = an.article_id AND ai.instrument_id = ANY(%s)"
        "   AND (an.event_time >= %s OR an.ingested_at >= %s)", (ids, granica, granica))
    cur.execute(
        "INSERT INTO news.article_analysis (article_id, model, prompt_version,"
        " direction, magnitude, horizon, confidence, novelty, raw, event_time,"
        " ingested_at)"
        " SELECT a.id, 'm', 'spozniona', 'bearish', 1, 'hours', 1, 1, '{}',"
        "        a.event_time, %s"
        "   FROM news.article a JOIN news.article_instrument ai ON ai.article_id=a.id"
        "  WHERE ai.instrument_id = ANY(%s) AND a.event_time < %s",
        (granica + timedelta(minutes=1), ids, granica))


def _usun_przyszlosc_w_bazie(cur, ids, perp, granica):
    for tabela, kol in (("market.candle", "instrument_id"),
                        ("market.book_snapshot", "instrument_id")):
        cur.execute(f"DELETE FROM {tabela} WHERE {kol} = ANY(%s) AND event_time >= %s",
                    (ids, granica))
    for tabela in ("market.funding_rate", "market.positioning"):
        cur.execute(f"DELETE FROM {tabela} WHERE instrument_id=%s AND event_time >= %s",
                    (perp, granica))
    cur.execute(
        "DELETE FROM news.article_analysis an USING news.article_instrument ai"
        " WHERE ai.article_id = an.article_id AND ai.instrument_id = ANY(%s)"
        "   AND (an.event_time >= %s OR an.ingested_at >= %s)", (ids, granica, granica))


def _przebieg(cur, h, ids, since, until, feat_ids):
    for iid in ids:
        features.compute_prices(cur, iid, h, feat_ids, since, until)
        features.compute_news(cur, iid, h, feat_ids["news_sent"], since, until)
        features.compute_orderbook(cur, iid, h, feat_ids, since, until)
        features.compute_funding(cur, iid, h, feat_ids, since, until)
        features.compute_positioning(cur, iid, h, feat_ids, since, until)
    features.compute_cross_section(cur, h, feat_ids, since, until, ids, engine="crypto")


def _odczyt(cur, ids, feat_ids):
    cur.execute(
        "SELECT feature_def_id, instrument_id, event_time, value FROM feat.feature_value"
        " WHERE instrument_id = ANY(%s) AND feature_def_id = ANY(%s)",
        (ids, list(feat_ids.values())))
    rola = {v: k for k, v in feat_ids.items()}
    return {(rola[r["feature_def_id"]], r["instrument_id"], r["event_time"]): r["value"]
            for r in cur.fetchall()}


@pytest.fixture(scope="module")
def wyniki_z_bazy():
    """Trzy przebiegi na jednej transakcji, wycofanej na końcu: pełna historia,
    przyszłość zmieniona, przyszłość usunięta (na żywo w chwili znane_od(T))."""
    h = H4H
    granica = T + _krok(h)
    with _polacz() as conn:
        try:
            cur = conn.cursor()
            ids, perp = _zasiej(cur, h, [dane(h, seed=s) for s in range(N_INSTRUMENTOW)])
            cur.execute("SELECT symbol FROM ref.instrument WHERE id = ANY(%s)", (ids,))
            feat_ids = features.ensure_definitions(
                cur, h, [r["symbol"] for r in cur.fetchall()])
            since, pelna = T - 5 * _krok(h), T + 40 * _krok(h)

            cur.execute("DELETE FROM feat.feature_value WHERE instrument_id = ANY(%s)", (ids,))
            _przebieg(cur, h, ids, since, pelna, feat_ids)
            a = _odczyt(cur, ids, feat_ids)

            _zmien_przyszlosc_w_bazie(cur, ids, perp, granica)
            _przebieg(cur, h, ids, since, pelna, feat_ids)
            b = _odczyt(cur, ids, feat_ids)

            _usun_przyszlosc_w_bazie(cur, ids, perp, granica)
            cur.execute("DELETE FROM feat.feature_value WHERE instrument_id = ANY(%s)", (ids,))
            _przebieg(cur, h, ids, since, znane_od(T, h), feat_ids)
            c = _odczyt(cur, ids, feat_ids)
        finally:
            conn.rollback()
    return a, b, c


@wymaga_bazy
@pytest.mark.parametrize("rola", sorted(ROLES))
def test_PRAWDZIWA_BAZA_cecha_w_T_nie_zalezy_od_przyszlosci(rola, wyniki_z_bazy):
    a, b, c = wyniki_z_bazy
    w_t = {k: v for k, v in a.items() if k[0] == rola and k[2] == T}
    assert w_t, f"{rola}: brak wartości w T — test niczego by nie sprawdził"
    zmienione = [k for k, v in a.items()
                 if k[0] == rola and k[2] <= T and b.get(k) != v]
    assert not zmienione, f"PRZECIEK {rola}: zmiana przyszłości zmieniła {zmienione[:3]}"
    for k, v in w_t.items():
        assert c.get(k) == v, (
            f"{rola}: na żywo w chwili znane_od(T) {c.get(k)}, z pełnej historii {v}")
    assert any(b.get(k) != v for k, v in a.items() if k[0] == rola and k[2] > T), (
        f"{rola}: zmiana przyszłości nie zmieniła cechy także PO T — kanarek martwy")
