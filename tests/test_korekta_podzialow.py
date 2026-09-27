"""Korekta swiec equity o podzialy akcji - czy mierzymy to, co mysmy mysleli.

CO SIE STALO

Adapter Alpaki pobieral swiece z `adjustment=raw`, czyli BEZ korekty o podzialy.
W `market.candle` leza trzy podzialy 10:1 (NVDA 2024-06-10, AVGO 2024-07-15,
NFLX 2025-11-17), a w szeregu zwrotow kazdy z nich wyglada jak spadek o 90%.
`ret_long` na horyzoncie 1h siega 168 swiec, rozgrzewka 200 - jedno zdarzenie
zatruwa okolo 670 swiec po sobie.

CZEGO PILNUJA TE TESTY - i dlaczego kazdy z nich pilnuje czegos, co moze zawiesc
CICHO, bez awarii i bez czerwonego testu:

1. PARAMETR MUSI DOCHODZIC DO DOSTAWCY. Stala `KOREKTA_SWIEC = "split"`, ktorej
   nikt nie wysyla, nie zmienia ani jednej liczby - a wyglada na zmiane. Dlatego
   testy patrza na `params["adjustment"]` w zapytaniu, nie na tresc stalej,
   i sprawdzaja, ze ROZNA wartosc daje ROZNE zapytanie.

2. PELNOSC ALBO NIC. Szereg, w ktorym czesc swiec jest skorygowana, a czesc nie,
   jest GORSZY niz nieskorygowany caly: nieciaglosc wypada w srodku okna
   treningowego i nic o niej nie mowi. Przepisanie brakujace choc jednej swiecy
   MUSI zostawic instrument nietkniety.

3. ZNACZNIK NIE MOZE WYPRZEDZAC DANYCH. Znacznik to stwierdzenie o danych;
   postawiony hurtem zamienia stan nieznany w potwierdzony i to jest jedyny
   sposob, w jaki bramka `train.py` moze zawiesc. Testy pilnuja, ze bez sladu
   przepisania go nie ma i ze rozjazd cech go zatrzymuje.

4. CECHY, NIE TYLKO SWIECE. Trener czyta `feat.feature_value`. Poprawienie swiec
   nie rusza policzonych wartosci, a `features-equity` chodzi z `--okno-dni 7`,
   wiec okien z 2024 nie dotknie NIGDY. Znacznik postawiony po samym przepisaniu
   swiec zdejmowalby bramke z danych, ktore trener faktycznie czyta i ktore
   nadal sa zatrute.

5. NASTEPNY PODZIAL. Alpaca koryguje wzgledem DZIS, wiec kolejny podzial
   uniewaznia cala zapisana historie, a znacznik nadal mowi `true`. Detektor
   w `backfill.py` porownuje osiem ostatnich swiec z tym, co oddaje dostawca,
   i po wykryciu PRZESKALOWANIA zdejmuje znacznik. Musi przy tym milczec na
   poprawce jednej swiecy i nie tykac crypto.

BAZA. Testy z `wymaga_bazy` pytaja PRAWDZIWEGO Postgresa, bo caly mechanizm
"albo wszystko, albo nic" zyje w transakcji - atrapa polaczenia, ktora sama
udaje rollback, nie dowodzi niczego o tym, co zostaje w tabeli.

    docker run -d --name podz-db -e POSTGRES_USER=trader -e POSTGRES_PASSWORD=ci \
        -e POSTGRES_DB=trader -p 55475:5432 timescale/timescaledb-ha:pg17
    DATABASE_URL=postgresql://trader:ci@127.0.0.1:55475/trader .venv/bin/python db/migrate.py
    TEST_DB_URL=postgresql://trader:ci@127.0.0.1:55475/trader .venv/bin/python -B -m pytest \
        --override-ini="addopts=" tests/test_korekta_podzialow.py
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services.engine.horizons import H1H
from services.engine.uniwersum import sql_uniwersum_decyzyjnego
from services.exchanges.alpaca import (
    KOREKTA_SWIEC, KOREKTY_DOPUSZCZALNE, AlpacaAdapter,
    ZNACZNIK_KOREKTY_PODZIALOW,
)
from services.exchanges.base import Candle
from services.ingest import backfill, korekta_podzialow as korekta

TEST_DB_URL = os.environ.get("TEST_DB_URL")
wymaga_bazy = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="ustaw TEST_DB_URL na jednorazowa baze z zastosowanymi migracjami")


# ======================================================= 1. parametr u dostawcy

class _Zapytania:
    """Podstawka pod `_get` adaptera: zapisuje parametry i oddaje jedna swiece."""

    def __init__(self):
        self.parametry = []

    async def __call__(self, path, params=None, *, host=None):
        self.parametry.append(params)
        bary = [dict(t="2026-09-25T13:00:00Z", o=1, h=2, l=1, c=2, v=10, n=3)]
        return dict(bars=dict(AAPL=bary))


def _adapter_z_podstawka():
    a = AlpacaAdapter("klucz", "sekret", mode="demo")
    szpieg = _Zapytania()
    a._get = szpieg
    return a, szpieg


def test_adapter_pyta_dostawce_o_split_a_nie_o_raw():
    """Sprawdzamy WARTOSC w zapytaniu, nie obecnosc klucza i nie tresc stalej.

    Stala ustawiona na `split`, ktorej nikt nie wysyla, nie zmienia ani jednej
    liczby w bazie - a w kodzie wyglada na zmiane.
    """
    a, szpieg = _adapter_z_podstawka()
    asyncio.run(a.fetch_candles("AAPL", "1h", limit=1))
    assert szpieg.parametry, "adapter nie wyslal zadnego zapytania o swiece"
    wyslana = szpieg.parametry[0].get("adjustment")
    assert wyslana == "split", (
        f"do Alpaca Data poszlo adjustment={wyslana!r}. `raw` to BRAK korekty "
        f"(domysl dostawcy), wiec swiece zapisywalyby sie dalej z podzialami "
        f"w szeregu, a znacznik korekty mowilby o nich `true`")


def test_wartosc_korekty_DOCHODZI_do_zapytania():
    """Dwa rozne parametry musza dac dwa rozne zapytania.

    Gdyby `adjustment` byl wpisany na sztywno, oba przebiegi wyslalyby to samo -
    i nie bylo by po czym poznac, ze parametr nie dochodzi. To ten sam kanarek,
    ktorym mierzy sie `--sucho --korekta raw` na produkcji.
    """
    a, szpieg = _adapter_z_podstawka()
    asyncio.run(a.fetch_candles("AAPL", "1h", limit=1, korekta="raw"))
    asyncio.run(a.fetch_candles("AAPL", "1h", limit=1, korekta="split"))
    wyslane = [p.get("adjustment") for p in szpieg.parametry]
    assert wyslane == ["raw", "split"], (
        f"parametr korekty nie dochodzi do zapytania: {wyslane}")


def test_nieznana_korekta_nie_idzie_do_dostawcy_wcale():
    """Literowka musi paść U NAS. Jak zachowuje sie Alpaca przy nieznanej
    wartosci, nie jest zmierzone - a odpowiedz "200 OK z cenami surowymi" byla by
    nierozpoznawalna od poprawnej."""
    a, szpieg = _adapter_z_podstawka()
    with pytest.raises(ValueError, match="korekta"):
        asyncio.run(a.fetch_candles("AAPL", "1h", limit=1, korekta="splitt"))
    assert szpieg.parametry == [], "poszlo zapytanie z nieznana korekta"


def test_wybrano_split_a_nie_all():
    """`all` koryguje TAKZE dywidendy, a wynik silnika liczy sie ze zmian ceny.

    Szereg z dywidendami pokazywalby zwrot, ktorego pozycja nie osiaga (krotka
    dywidende PLACI), i nic by sie przy tym nie wywrocilo - Sharpe po prostu
    wyszedlby wyzszy. To jest wybor, nie kosmetyka, wiec stoi w tescie.
    """
    assert KOREKTA_SWIEC == "split", (
        f"korekta swiec to {KOREKTA_SWIEC!r}; `all` zmienia DEFINICJE zwrotu "
        f"(wlicza dywidendy), `raw` to brak korekty")
    assert "all" in KOREKTY_DOPUSZCZALNE and "raw" in KOREKTY_DOPUSZCZALNE


# ============================================ 2. pelnosc i uniwersum, bez bazy

class _Kursor:
    def __init__(self, wyniki=None):
        self.sql = []
        self.wyniki = list(wyniki or [])

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        return self

    def fetchall(self):
        return self.wyniki.pop(0) if self.wyniki else []

    def fetchone(self):
        return None


def test_uniwersum_pyta_o_TEN_SAM_warunek_co_bramka_trenera():
    """Znacznik postawiony innemu zbiorowi niz pytany przez bramke znaczy albo
    bramke, ktora nie puszcza nigdy, albo znacznik na instrumencie, ktorego nikt
    nie przepisal. Warunek jest wiec jeden - z `uniwersum.py`."""
    kursor = _Kursor([[dict(id=7, symbol="NVDA", meta=dict())]])
    korekta.uniwersum(kursor, "equity", None)
    zapytanie = kursor.sql[0][0]
    assert zapytanie == sql_uniwersum_decyzyjnego("id, symbol, meta"), (
        f"skrypt korekty pyta o uniwersum WLASNYM zapytaniem: {zapytanie}")
    assert "tradeable" in zapytanie and "active" in zapytanie


def _swieca(t: datetime, close: str) -> Candle:
    return Candle(event_time=t, open=Decimal(close), high=Decimal(close),
                  low=Decimal(close), close=Decimal(close), volume=Decimal(1),
                  trades=1)


def _wiersz(t: datetime, close: str) -> dict:
    c = Decimal(close)
    return dict(event_time=t, open=c, high=c, low=c, close=c,
                volume=Decimal(1), trade_count=1)


def test_brak_choc_jednej_swiecy_u_dostawcy_to_NIEPELNE_pokrycie():
    """Definicja pelnosci: dostawca musi oddac KAZDA swiece, ktora mamy."""
    t0 = datetime(2024, 6, 3, 13, tzinfo=timezone.utc)
    czasy = [t0 + timedelta(hours=i) for i in range(3)]
    w_bazie = dict((t, _wiersz(t, "100")) for t in czasy)
    # dostawca gubi swiece srodkowa
    swiece = [_swieca(czasy[0], "10"), _swieca(czasy[2], "10")]
    r = korekta.zestaw("NVDA", 7, w_bazie, swiece)
    assert r.brakujacych == 1 and not r.pelny
    assert r.zmienionych == 2
    assert r.czynnik == Decimal("0.1")


def test_szereg_bez_podzialu_daje_zero_zmienionych():
    """Uczciwy wynik: korekta, ktora nic nie zmienia, ma pokazac ZERO, a nie
    liczbe przepisanych wierszy. Inaczej nie dalo by sie odpowiedziec na pytanie
    'czy w tym szeregu byl w ogole podzial'."""
    t0 = datetime(2024, 6, 3, 13, tzinfo=timezone.utc)
    czasy = [t0 + timedelta(hours=i) for i in range(3)]
    w_bazie = dict((t, _wiersz(t, "100")) for t in czasy)
    swiece = [_swieca(t, "100") for t in czasy]
    r = korekta.zestaw("AAPL", 8, w_bazie, swiece)
    assert (r.zmienionych, r.brakujacych, r.pelny) == (0, 0, True)
    assert r.czynnik is None


# ================================================ 3. baza: albo wszystko, albo nic

class _Gielda:
    """Atrapa dostawcy. Oddaje DOKLADNIE to, co jej podano - zeby test mogl
    opisac 'dostawca zgubil swiece' i 'dostawca przeskalowal historie'."""

    def __init__(self, swiece, pyta_wolno=True):
        self.swiece = list(swiece)
        self.wywolania = 0
        self.pyta_wolno = pyta_wolno

    async def fetch_candles(self, symbol, timeframe, limit=200, start=None,
                            end=None, *, korekta=None):
        self.wywolania += 1
        if not self.pyta_wolno:
            raise AssertionError(
                "dostawca zostal zapytany, choc instrument nie ma znacznika "
                "korekty - to zapytanie na kazdy instrument crypto, na kazdy cykl")
        wynik = [c for c in self.swiece
                 if (start is None or c.event_time >= start)
                 and (end is None or c.event_time < end)]
        return wynik[:limit]

    async def close(self):
        pass


def _polacz():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(TEST_DB_URL, row_factory=dict_row, autocommit=False)


def _zasiej_instrument(cur, symbol: str, engine: str = "equity") -> int:
    cur.execute("SELECT id FROM ref.venue WHERE code = 'alpaca'")
    venue = cur.fetchone()["id"]
    cur.execute(
        "INSERT INTO ref.instrument (venue_id, symbol, engine, quote_ccy, kind,"
        " active, tradeable) VALUES (%s,%s,%s,'USD','stock',true,true) RETURNING id",
        (venue, symbol, engine))
    return cur.fetchone()["id"]


def _zasiej_swiece(cur, iid: int, ile: int, od: datetime, cena0: float = 100.0,
                   timeframe: str = "1h") -> list[datetime]:
    czasy = []
    for i in range(ile):
        t = od + timedelta(hours=i)
        cena = Decimal(str(round(cena0 + (i % 17) - (i % 5) * 0.5, 4)))
        cur.execute(
            "INSERT INTO market.candle (instrument_id, timeframe, event_time,"
            " open, high, low, close, volume, trade_count, ingested_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (iid, timeframe, t, cena, cena + 1, cena - 1, cena,
             Decimal(1000 + i), 10, t))
        czasy.append(t)
    return czasy


def _sprzataj(cur, iid: int, def_ids_przed: set) -> None:
    cur.execute("DELETE FROM feat.feature_value WHERE instrument_id = %s", (iid,))
    cur.execute("DELETE FROM market.korekta_swiec WHERE instrument_id = %s", (iid,))
    cur.execute("DELETE FROM market.candle WHERE instrument_id = %s", (iid,))
    cur.execute("SELECT id FROM feat.feature_def")
    nowe = set(r["id"] for r in cur.fetchall()) - def_ids_przed
    if nowe:
        cur.execute("DELETE FROM feat.feature_value WHERE feature_def_id = ANY(%s)",
                    (list(nowe),))
        cur.execute("DELETE FROM feat.feature_def WHERE id = ANY(%s)", (list(nowe),))
    cur.execute("DELETE FROM ref.instrument WHERE id = %s", (iid,))


def _def_ids(cur) -> set:
    cur.execute("SELECT id FROM feat.feature_def")
    return set(r["id"] for r in cur.fetchall())


def _stan(cur, iid: int) -> tuple:
    """(ile swiec zmienilo cene na 10, ile sladow, czy znacznik) - jednym pytaniem
    o KAZDA z trzech rzeczy, ktore ta operacja dotyka."""
    cur.execute("SELECT count(*) AS n FROM market.candle"
                " WHERE instrument_id = %s AND close < 50", (iid,))
    skorygowane = cur.fetchone()["n"]
    cur.execute("SELECT zdarzenie FROM market.korekta_swiec"
                " WHERE instrument_id = %s ORDER BY id", (iid,))
    slady = [r["zdarzenie"] for r in cur.fetchall()]
    cur.execute("SELECT coalesce(meta->%s, 'false'::jsonb) = 'true'::jsonb AS ma"
                " FROM ref.instrument WHERE id = %s",
                (ZNACZNIK_KOREKTY_PODZIALOW, iid))
    return skorygowane, slady, cur.fetchone()["ma"]


@wymaga_bazy
def test_niepelne_pokrycie_NIE_ZAPISUJE_ani_jednego_wiersza():
    """Dostawca gubi jedna swiece - instrument zostaje NIETKNIETY.

    Przepisanie czesciowe jest gorsze od braku korekty: pol szeregu
    skorygowanego i pol nieskorygowanego to nieciaglosc x10 w SRODKU okna
    treningowego, o ktorej nic nie mowi.
    """
    od = datetime(2024, 6, 3, 13, tzinfo=timezone.utc)
    with _polacz() as conn:
        cur = conn.cursor()
        przed = _def_ids(cur)
        iid = _zasiej_instrument(cur, "ZZKOR1")
        czasy = _zasiej_swiece(cur, iid, 10, od)
        conn.commit()
        try:
            # dostawca oddaje wszystko poza jedna swieca, i to po podziale 10:1
            gielda = _Gielda([_swieca(t, "10") for t in czasy if t != czasy[4]])
            wiersze = [dict(id=iid, symbol="ZZKOR1", meta=dict())]
            raporty, odrzucone = asyncio.run(
                korekta.tryb_przepisania(conn, gielda, wiersze, "1h"))
            assert raporty == [] and len(odrzucone) == 1
            assert "nie oddal" in odrzucone[0]
            skorygowane, slady, znacznik = _stan(cur, iid)
            assert skorygowane == 0, (
                f"{skorygowane} swiec zostalo przepisanych, mimo ze pokrycie "
                f"bylo niepelne - w bazie siedzi teraz szereg mieszany")
            assert slady == [] and znacznik is False
        finally:
            _sprzataj(cur, iid, przed)
            conn.commit()


@wymaga_bazy
def test_udane_przepisanie_zapisuje_swiece_I_slad_ale_NIE_znacznik():
    """Swiece i slad jednym commitem; znacznik NIE, bo cechy jeszcze nie sa
    przeliczone i trener czyta wlasnie je."""
    od = datetime(2024, 6, 3, 13, tzinfo=timezone.utc)
    with _polacz() as conn:
        cur = conn.cursor()
        przed = _def_ids(cur)
        iid = _zasiej_instrument(cur, "ZZKOR2")
        czasy = _zasiej_swiece(cur, iid, 10, od)
        conn.commit()
        try:
            gielda = _Gielda([_swieca(t, "10") for t in czasy])
            wiersze = [dict(id=iid, symbol="ZZKOR2", meta=dict())]
            raporty, odrzucone = asyncio.run(
                korekta.tryb_przepisania(conn, gielda, wiersze, "1h"))
            assert odrzucone == [] and len(raporty) == 1
            skorygowane, slady, znacznik = _stan(cur, iid)
            assert skorygowane == 10, "szereg nie zostal przepisany w calosci"
            assert slady == ["swiece_przepisane"]
            assert znacznik is False, (
                "znacznik postawiony razem ze swiecami - bramka train.py "
                "puscilaby trening na cechach policzonych ze STARYCH swiec")
            cur.execute("SELECT adjustment, swiec_zmienionych, wspolczynnik"
                        " FROM market.korekta_swiec WHERE instrument_id = %s", (iid,))
            slad = cur.fetchone()
            assert slad["adjustment"] == "split"
            assert slad["swiec_zmienionych"] == 10
            assert abs(float(slad["wspolczynnik"]) - 0.1) < 0.01
        finally:
            _sprzataj(cur, iid, przed)
            conn.commit()


# ================================== 4. znacznik: dane, nie deklaracja czlowieka

def _slad_przepisania(cur, iid: int, symbol: str, ile: int) -> None:
    r = korekta.Raport(symbol=symbol, instrument_id=iid, w_bazie=ile,
                       pobranych=ile, zmienionych=ile)
    korekta.zapisz_slad(cur, r, "1h", "swiece_przepisane", KOREKTA_SWIEC)


@wymaga_bazy
def test_znacznik_odmawia_gdy_nikt_nie_przepisal_szeregu():
    """Bez sladu przepisania znacznik nie ma na czym stac. To jest ten sam blad,
    ktory bramka w `train.py` miala wylapac: stan nieznany wpisany jako
    potwierdzony."""
    od = datetime(2024, 6, 3, 13, tzinfo=timezone.utc)
    with _polacz() as conn:
        cur = conn.cursor()
        przed = _def_ids(cur)
        iid = _zasiej_instrument(cur, "ZZKOR3")
        _zasiej_swiece(cur, iid, 10, od)
        conn.commit()
        try:
            wiersze = [dict(id=iid, symbol="ZZKOR3", meta=dict())]
            postawione, odmowy = korekta.tryb_znacznika(conn, wiersze, "1h",
                                                       H1H, "equity")
            assert postawione == []
            assert any("brak sladu przepisania" in o for o in odmowy), odmowy
            assert _stan(cur, iid)[2] is False
        finally:
            _sprzataj(cur, iid, przed)
            conn.commit()


@wymaga_bazy
def test_znacznik_odmawia_gdy_ZAPISANE_CECHY_nie_zgadzaja_sie_ze_swiecami():
    """TO JEST NAJWAZNIEJSZY TEST W TYM PLIKU.

    Odtwarza dokladnie ten stan, ktory po korekcie swiec wyglada na zalatwiony:
    `market.candle` jest juz skorygowane, a `feat.feature_value` trzyma wartosci
    policzone ze STARYCH swiec - bo `features-equity` chodzi z `--okno-dni 7`
    i okna z 2024 nie dotknie nigdy. Trener czyta wlasnie te wartosci.
    """
    od = datetime(2024, 6, 3, 13, tzinfo=timezone.utc)
    with _polacz() as conn:
        cur = conn.cursor()
        przed = _def_ids(cur)
        iid = _zasiej_instrument(cur, "ZZKOR4")
        czasy = _zasiej_swiece(cur, iid, 300, od)
        conn.commit()
        try:
            from services.ingest import features
            feat_ids = features.ensure_definitions(cur, H1H, ["ZZKOR4"])
            conn.commit()
            features.compute_prices(cur, iid, H1H, feat_ids, czasy[0], czasy[-1])
            _slad_przepisania(cur, iid, "ZZKOR4", 300)
            conn.commit()
            cur.execute("SELECT count(*) AS n FROM feat.feature_value"
                        " WHERE instrument_id = %s", (iid,))
            assert cur.fetchone()["n"] > 0, "test nie zasial zadnej cechy"

            # PRZEPISANIE SWIEC bez przeliczenia cech - podzial 10:1 na pierwszej
            # polowie szeregu, dokladnie jak korekta u dostawcy.
            cur.execute(
                "UPDATE market.candle SET open = open/10, high = high/10,"
                " low = low/10, close = close/10 WHERE instrument_id = %s"
                "   AND event_time <= %s", (iid, czasy[150]))
            conn.commit()

            wiersze = [dict(id=iid, symbol="ZZKOR4", meta=dict())]
            postawione, odmowy = korekta.tryb_znacznika(conn, wiersze, "1h",
                                                       H1H, "equity")
            assert postawione == [], "znacznik postawiony na zatrutych cechach"
            assert any("nie zgadza sie ze swiecami" in o for o in odmowy), odmowy
            assert _stan(cur, iid)[2] is False
        finally:
            _sprzataj(cur, iid, przed)
            conn.commit()


@wymaga_bazy
def test_znacznik_STAJE_gdy_cechy_zgadzaja_sie_ze_swiecami():
    """Druga strona tego samego sprawdzenia - bez niej test wyzej przechodzilby
    takze wtedy, gdyby sprawdzenie ODMAWIALO ZAWSZE (i bramka nie puscilaby
    treningu nigdy, tez bez awarii)."""
    od = datetime(2024, 6, 3, 13, tzinfo=timezone.utc)
    with _polacz() as conn:
        cur = conn.cursor()
        przed = _def_ids(cur)
        iid = _zasiej_instrument(cur, "ZZKOR5")
        czasy = _zasiej_swiece(cur, iid, 300, od)
        conn.commit()
        try:
            from services.ingest import features
            feat_ids = features.ensure_definitions(cur, H1H, ["ZZKOR5"])
            conn.commit()
            features.compute_prices(cur, iid, H1H, feat_ids, czasy[0], czasy[-1])
            _slad_przepisania(cur, iid, "ZZKOR5", 300)
            conn.commit()

            wiersze = [dict(id=iid, symbol="ZZKOR5", meta=dict())]
            postawione, odmowy = korekta.tryb_znacznika(conn, wiersze, "1h",
                                                       H1H, "equity")
            assert odmowy == [], odmowy
            assert postawione == ["ZZKOR5"]
            skorygowane, slady, znacznik = _stan(cur, iid)
            assert znacznik is True
            assert slady == ["swiece_przepisane", "znacznik"], slady
        finally:
            _sprzataj(cur, iid, przed)
            conn.commit()


# ======================= 5. nastepny podzial nie moze byc cicha awaria

def _kursor_krotkowy(conn):
    """Ten sam ksztalt wiersza, co w `backfill_symbol` - tam kursor jest
    `tuple_row`, wiec detektor MUSI dzialac na krotkach."""
    from psycopg.rows import tuple_row
    return conn.cursor(row_factory=tuple_row)


def _postaw_znacznik(cur, iid: int) -> None:
    cur.execute(
        "UPDATE ref.instrument SET meta = coalesce(meta, jsonb_build_object())"
        " || jsonb_build_object(%s::text, true) WHERE id = %s",
        (ZNACZNIK_KOREKTY_PODZIALOW, iid))


@wymaga_bazy
def test_detektor_ZDEJMUJE_znacznik_gdy_dostawca_przeskalowal_historie():
    """Alpaca koryguje wzgledem DZIS, wiec kolejny podzial uniewaznia cala
    zapisana historie - a znacznik nadal mowi `true` i bramka `train.py`
    przepuszcza trening na szeregu z nieciagloscia x10 w srodku.

    Detektor porownuje osiem ostatnich swiec z tym, co oddaje dostawca, i po
    wykryciu STALEGO czynnika zdejmuje znacznik. Bramka znow zatrzymuje tor.
    """
    od = datetime(2024, 6, 3, 13, tzinfo=timezone.utc)
    with _polacz() as conn:
        cur = conn.cursor()
        przed = _def_ids(cur)
        iid = _zasiej_instrument(cur, "ZZKOR6")
        czasy = _zasiej_swiece(cur, iid, 10, od, cena0=1000.0)
        _postaw_znacznik(cur, iid)
        conn.commit()
        try:
            assert _stan(cur, iid)[2] is True, "test nie postawil znacznika"
            cur.execute("SELECT event_time, close FROM market.candle"
                        " WHERE instrument_id = %s ORDER BY event_time", (iid,))
            stare = [(r["event_time"], r["close"]) for r in cur.fetchall()]
            # dostawca oddaje TE SAME chwile, ceny podzielone przez dziesiec
            gielda = _Gielda([_swieca(t, str(c / 10)) for t, c in stare])
            krotki = _kursor_krotkowy(conn)
            czynnik = asyncio.run(backfill.sprawdz_ponowna_korekte(
                gielda, conn, krotki, iid, "ZZKOR6", "1h", timedelta(hours=1),
                czasy[-1], czasy[-1] + timedelta(hours=2)))
            assert czynnik is not None and abs(float(czynnik) - 0.1) < 0.001
            skorygowane, slady, znacznik = _stan(cur, iid)
            assert znacznik is False, (
                "znacznik zostal, mimo ze dostawca przeskalowal historie - "
                "bramka train.py puscilaby trening na szeregu z nieciagloscia")
            assert slady == ["uniewazniony"], slady
        finally:
            _sprzataj(cur, iid, przed)
            conn.commit()


@wymaga_bazy
def test_detektor_MILCZY_gdy_zmienila_sie_JEDNA_swieca():
    """Poprawka pojedynczej swiecy to nie podzial - a detektor, ktory zdejmuje
    znacznik przy kazdej roznicy, zatrzymywalby tor equity co kilka dni bez
    powodu. Warunek jest wiec mocniejszy: WSZYSTKIE porownane swiece musza byc
    przeskalowane TYM SAMYM czynnikiem."""
    od = datetime(2024, 6, 3, 13, tzinfo=timezone.utc)
    with _polacz() as conn:
        cur = conn.cursor()
        przed = _def_ids(cur)
        iid = _zasiej_instrument(cur, "ZZKOR7")
        czasy = _zasiej_swiece(cur, iid, 10, od, cena0=1000.0)
        _postaw_znacznik(cur, iid)
        conn.commit()
        try:
            cur.execute("SELECT event_time, close FROM market.candle"
                        " WHERE instrument_id = %s ORDER BY event_time", (iid,))
            stare = [(r["event_time"], r["close"]) for r in cur.fetchall()]
            # jedna swieca poprawiona o polowe, reszta bez zmian
            swiece = [_swieca(t, str(c / 2 if t == czasy[-3] else c))
                      for t, c in stare]
            krotki = _kursor_krotkowy(conn)
            czynnik = asyncio.run(backfill.sprawdz_ponowna_korekte(
                _Gielda(swiece), conn, krotki, iid, "ZZKOR7", "1h",
                timedelta(hours=1), czasy[-1], czasy[-1] + timedelta(hours=2)))
            assert czynnik is None
            skorygowane, slady, znacznik = _stan(cur, iid)
            assert znacznik is True, "znacznik zdjety przez poprawke jednej swiecy"
            assert slady == []
        finally:
            _sprzataj(cur, iid, przed)
            conn.commit()


@wymaga_bazy
def test_detektor_nie_pyta_gieldy_dla_instrumentu_BEZ_znacznika():
    """Bybit nie dzieli akcji. Detektor rozciagniety na crypto to dodatkowe
    zapytanie na kazdy instrument i na kazdy cykl - a `history` chodzi co 20
    minut po 21 instrumentach."""
    od = datetime(2024, 6, 3, 13, tzinfo=timezone.utc)
    with _polacz() as conn:
        cur = conn.cursor()
        przed = _def_ids(cur)
        iid = _zasiej_instrument(cur, "ZZKOR8", engine="crypto")
        czasy = _zasiej_swiece(cur, iid, 10, od)
        conn.commit()
        try:
            gielda = _Gielda([], pyta_wolno=False)
            krotki = _kursor_krotkowy(conn)
            czynnik = asyncio.run(backfill.sprawdz_ponowna_korekte(
                gielda, conn, krotki, iid, "ZZKOR8", "1h", timedelta(hours=1),
                czasy[-1], czasy[-1] + timedelta(hours=2)))
            assert czynnik is None
            assert gielda.wywolania == 0
        finally:
            _sprzataj(cur, iid, przed)
            conn.commit()


# ================================================== 6. pomiar nie jest zapisem

@wymaga_bazy
def test_sucho_nie_dotyka_ani_swiec_ani_sladu():
    """`--sucho` ma byc pomiarem, ktory wolno uruchomic na produkcji przed
    decyzja. Gdyby zapisywal, pierwsze uruchomienie bylo by juz operacja."""
    od = datetime(2024, 6, 3, 13, tzinfo=timezone.utc)
    with _polacz() as conn:
        cur = conn.cursor()
        przed = _def_ids(cur)
        iid = _zasiej_instrument(cur, "ZZKOR9")
        czasy = _zasiej_swiece(cur, iid, 10, od, cena0=1000.0)
        conn.commit()
        try:
            gielda = _Gielda([_swieca(t, "100") for t in czasy])
            wiersze = [dict(id=iid, symbol="ZZKOR9", meta=dict())]
            raporty = asyncio.run(
                korekta.tryb_pomiaru(conn, gielda, wiersze, "1h", None))
            assert len(raporty) == 1 and raporty[0].zmienionych == 10
            conn.rollback()
            cur.execute("SELECT count(*) AS n FROM market.candle"
                        " WHERE instrument_id = %s AND close < 500", (iid,))
            assert cur.fetchone()["n"] == 0, "pomiar przepisal swiece"
            assert _stan(cur, iid)[1] == [], "pomiar zapisal slad"
        finally:
            _sprzataj(cur, iid, przed)
            conn.commit()


@wymaga_bazy
def test_zadna_migracja_NIE_stawia_znacznika():
    """Baza po wszystkich migracjach nie moze miec ani jednego znacznika.

    Znacznik to stwierdzenie o TRESCI swiec; migracja swiec nie dotyka, wiec
    postawiony w migracji bylby klamstwem od pierwszej sekundy - i zdejmowalby
    bramke, ktora ma go pilnowac.
    """
    with _polacz() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT count(*) AS n FROM ref.instrument"
            " WHERE coalesce(meta->%s, 'false'::jsonb) = 'true'::jsonb",
            (ZNACZNIK_KOREKTY_PODZIALOW,))
        assert cur.fetchone()["n"] == 0, (
            "w bazie zlozonej z samych migracji stoi juz znacznik korekty")


# ============================================ 7. czym wolno zapisywac, a czym nie

def test_korekta_z_linii_polecen_dziala_TYLKO_z_sucho(monkeypatch):
    """`--korekta raw --przepisz` zapisalby do bazy szereg NIESKORYGOWANY,
    a slad i znacznik mowilyby `split`. Zapis idzie wiec zawsze ta korekta,
    ktora zadeklarowal adapter."""
    monkeypatch.setattr(
        "sys.argv",
        ["korekta_podzialow.py", "--przepisz", "--korekta", "raw"])
    with pytest.raises(SystemExit) as e:
        asyncio.run(korekta.main())
    assert "WYLACZNIE z --sucho" in str(e.value)


def test_trzeba_podac_dokladnie_jeden_tryb(monkeypatch):
    """Brak trybu nie moze znaczyc 'zrob najbardziej oczywista rzecz'."""
    monkeypatch.setattr("sys.argv", ["korekta_podzialow.py"])
    with pytest.raises(SystemExit) as e:
        asyncio.run(korekta.main())
    assert "DOKLADNIE jeden tryb" in str(e.value)
