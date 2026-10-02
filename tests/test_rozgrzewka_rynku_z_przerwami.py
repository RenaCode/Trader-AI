"""Rozgrzewka cech liczy się w ŚWIECACH, nie w minutach zegara.

DLACZEGO TO JEST WŁASNY PLIK

`compute_prices` potrzebuje 205 świec rozgrzewki (najdłuższe okno + zapas),
zanim policzy cokolwiek. Do 2026-09-24 wołający brał je, cofając `since`
o `bar_minutes * 205` MINUT — co jest tym samym tylko na rynku notowanym bez
przerwy.

Giełda amerykańska daje SIEDEM świec 1h na dzień handlowy i zero przez ~17 h
nocy oraz ~65 h weekendu. W oknie 205 godzin leży tam około 55 świec, więc
`len(bars) <= lookback` było prawdą ZAWSZE, a funkcja zwracała zero.

Awaria nie miała objawu i nie mogła go mieć:

  * cykl kończył się kodem 0 i bił puls, bo żaden wyjątek nie padł,
  * log pokazywał „0 cen" dla każdego instrumentu — tak samo jak spokojny
    cykl, w którym po prostu nie przybyło nowych świec,
  * ręczny `--backfill --days 90` liczył się normalnie (okno 90 dni mieści
    z zapasem 205 świec), więc sprawdzenie „czy potok działa" wychodziło
    pozytywnie i zasypywało dziurę, której nie było widać w pracy ciągłej.

Zmierzone na produkcji 2026-09-24 w oknie 205 godzin: BTCUSDC 228 świec
(liczy się normalnie), AAPL 55 (nie policzyłoby się nigdy).

Te testy pilnują obu połówek naraz: że rynek z przerwami DOSTAJE cechy oraz
że rynek ciągły dostaje DOKŁADNIE te same co wcześniej.
"""
from datetime import datetime, timedelta, timezone

from services.engine.horizons import H1H, H4H
from services.ingest import features

# Czwartek. Dzień tygodnia ma znaczenie: siatka sesji omija soboty i niedziele.
TERAZ = datetime(2026, 9, 24, 18, 0, tzinfo=timezone.utc)

# Siatka sesji giełdowej odczytana z produkcji: 13:00-19:00 UTC, pon-pt.
GODZINY_SESJI = range(13, 20)


def swiece_gieldowe(dni: int) -> list[datetime]:
    """Znaczniki świec 1h giełdy USA — z przerwą nocną i weekendową."""
    t = (TERAZ - timedelta(days=dni)).replace(hour=0, minute=0,
                                              second=0, microsecond=0)
    out = []
    while t < TERAZ:
        if t.weekday() < 5 and t.hour in GODZINY_SESJI:
            out.append(t)
        t += timedelta(hours=1)
    return out


def swiece_ciagle(dni: int) -> list[datetime]:
    """Znaczniki świec 1h rynku notowanego bez przerwy."""
    t = TERAZ - timedelta(days=dni)
    out = []
    while t < TERAZ:
        out.append(t)
        t += timedelta(hours=1)
    return out


class KursorSwiec:
    """Atrapa odtwarzająca SEMANTYKĘ zapytania `load_bars`, nie jego wynik.

    Atrapa oddająca gotową listę świec byłaby ślepa dokładnie na to, o co tu
    chodzi: na to, CZYM wyznacza się rozgrzewkę. Ta wykonuje oba warunki —
    `LIMIT` na świecach przed `since` i zakres czasu na oknie właściwym —
    więc wywróci się, gdy rozgrzewka wróci do liczenia zegarem.
    """

    def __init__(self, znaczniki: list[datetime]):
        # Cena rośnie monotonicznie o 1 — wartości cech nas tu nie obchodzą,
        # obchodzi nas, ILE świec trafia do panelu.
        self.swiece = [
            {"event_time": t, "open": 100.0 + i, "high": 101.0 + i,
             "low": 99.0 + i, "close": 100.5 + i, "volume": 1000.0 + i}
            for i, t in enumerate(znaczniki)
        ]
        self.zapisane: list[tuple] = []
        self._wynik: list[dict] = []

    def execute(self, sql, params=None):
        assert "market.candle" in sql, f"nieoczekiwane zapytanie: {sql}"
        assert "UNION ALL" in sql and "LIMIT" in sql, (
            "zapytanie o świece nie pobiera rozgrzewki po LICZBIE świec — "
            "wróciła rozgrzewka liczona zegarem, która na rynku z przerwami "
            "nie zbiera nigdy dość świec")
        _iid, _tf, since, limit = params[0], params[1], params[2], params[3]
        _, _, since2, until = params[4], params[5], params[6], params[7]
        assert since == since2, "obie połówki muszą dzielić tę samą granicę"

        rozgrzewka = [s for s in self.swiece if s["event_time"] < since][-limit:]
        okno = [s for s in self.swiece
                if since <= s["event_time"] < until]
        wiersze = rozgrzewka + okno

        if "time_bucket" in sql:
            wiersze = self._agreguj(wiersze, params[8])
        self._wynik = [
            {"t": s["event_time"], "open": s["open"], "high": s["high"],
             "low": s["low"], "close": s["close"], "volume": s["volume"]}
            for s in wiersze
        ]

    @staticmethod
    def _agreguj(wiersze: list[dict], interwal: str) -> list[dict]:
        minut = int(interwal.split()[0])
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        kubelki: dict[datetime, list[dict]] = {}
        for s in wiersze:
            n = int((s["event_time"] - epoch).total_seconds() // (minut * 60))
            kubelki.setdefault(epoch + timedelta(seconds=n * minut * 60),
                               []).append(s)
        out = []
        for t in sorted(kubelki):
            grupa = kubelki[t]
            out.append({"event_time": t, "open": grupa[0]["open"],
                        "high": max(s["high"] for s in grupa),
                        "low": min(s["low"] for s in grupa),
                        "close": grupa[-1]["close"],
                        "volume": sum(s["volume"] for s in grupa)})
        return out

    def fetchall(self):
        return self._wynik

    def executemany(self, sql, rows):
        assert "feat.feature_value" in sql
        self.zapisane.extend(rows)


IDS = {rola: i for i, rola in enumerate(features.PRICE_ROLES, start=1)}


def policz(znaczniki: list[datetime], h, okno_dni: int = 1) -> int:
    cur = KursorSwiec(znaczniki)
    return features.compute_prices(cur, 1, h, IDS,
                                   TERAZ - timedelta(days=okno_dni), TERAZ)


def test_gielda_dostaje_cechy_mimo_przerw_w_notowaniach():
    """Sedno. Przy rozgrzewce liczonej zegarem wychodziło tu zero."""
    n = policz(swiece_gieldowe(dni=400), H1H)
    assert n > 0, (
        "potok nie policzył ANI JEDNEJ cechy dla rynku z przerwami — "
        "rozgrzewka znów nie zbiera dość świec, a proces kończy się "
        "kodem 0 i bije puls, więc awaria nie ma objawu")


def test_rynek_ciagly_liczy_sie_dalej():
    """Poprawka dla giełdy nie może ruszyć krypto."""
    assert policz(swiece_ciagle(dni=400), H1H) > 0
    assert policz(swiece_ciagle(dni=400), H4H) > 0


def test_okno_zegarowe_NIE_zebraloby_dosc_swiec_na_gieldzie():
    """Pin pod samą przyczynę, żeby test wyżej nie przechodził przypadkiem.

    Gdyby siatka sesji w tym pliku rozjechała się z produkcją i zrobiła się
    gęsta, `test_gielda_dostaje_cechy...` przechodziłby także ze starym,
    zegarowym oknem — czyli przestałby czegokolwiek pilnować.
    """
    lookback = max(H1H.ret_long, H1H.range_window, H1H.volume_lookback,
                   H1H.ema_slow, H1H.warmup) + 5
    since = TERAZ - timedelta(days=1)
    stare_okno = since - timedelta(minutes=H1H.bar_minutes * lookback)
    w_oknie = [t for t in swiece_gieldowe(dni=400) if t >= stare_okno]
    assert len(w_oknie) <= lookback, (
        f"siatka sesji w tym teście daje {len(w_oknie)} świec w oknie "
        f"zegarowym przy progu {lookback} — jest za gęsta, żeby odtwarzać "
        f"giełdę, i test wyżej przestał dowodzić czegokolwiek")


def test_swieca_po_dlugiej_przerwie_dostaje_cechy_tylko_w_szerszym_oknie():
    """Dlaczego worker equity ma `--okno-dni 7`, a nie domyślną dobę.

    Do 02.10.2026 świeca dostawała cechy dopiero w cyklu, w którym istniała
    świeca PO niej (`compute_prices` pomijało ostatnią bezwarunkowo). Po
    piątkowym zamknięciu następna świeca przychodzi 65 h później, więc
    w oknie dobowym świeca piątkowa 19:00 nie dostawała cech NIGDY. Dziś
    liczy się w piątek po zamknięciu (tests/test_cechy_ostatniej_zamknietej_
    swiecy.py), ale okno 7 dni zostaje jako siatka bezpieczeństwa dla cyklu,
    który akurat wtedy nie przeszedł.
    """
    znaczniki = swiece_gieldowe(dni=400)
    piatek = [t for t in znaczniki if t.weekday() == 4 and t.hour == 19][-1]
    # Cykl w poniedziałek po tym piątku, w środku sesji.
    poniedzialek = piatek + timedelta(days=3, hours=-5)

    def cechy_piatkowej(okno_dni: int) -> int:
        cur = KursorSwiec([t for t in znaczniki if t <= poniedzialek])
        features.compute_prices(cur, 1, H1H, IDS,
                                poniedzialek - timedelta(days=okno_dni),
                                poniedzialek)
        return sum(1 for r in cur.zapisane if r[2] == piatek)

    assert cechy_piatkowej(1) == 0, (
        "test przestał odtwarzać dziurę weekendową — sprawdź siatkę sesji")
    assert cechy_piatkowej(7) == len(features.PRICE_ROLES), (
        f"świeca piątkowa {piatek} nie dostała kompletu cech nawet w oknie "
        f"siedmiodniowym — cechy giełdy mają systematyczną dziurę co tydzień")
