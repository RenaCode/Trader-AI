"""Spread w koszcie rundy: POMIAR Z BAZY, nie stala wpisana w chart.

CO BYLO NIE TAK

`charts/trader/values.yaml` mial `trainerCron.spreadBps: 5.85` - jedna liczba
wpisana 2026-09-25 i podawana trenerowi przez `--spread-bps`. Liczba byla
pomiarem, ale nikt nie zapisal zapytania, ktore ja dalo: nie ma jej ani
w docs/03, ani w docs/08, ani w zadnym tescie. Nie dalo sie jej wiec odtworzyc
ani zauwazyc, ze przestala opisywac rynek - a przestanie sama, bo uniwersum
decyzyjne zmienia sie migracja (24 pary USDT -> 10 par USDC), a plynnosc
i cennik gieldy zmieniaja sie bez zadnej migracji.

Najblizsza rekonstrukcja tamtej liczby (wlasna ksiega, 24-25.09, srednia
z median per instrument) daje 5,46 pb dla 1h i 5,69 pb dla 4h - podobnie, ale
NIE tyle samo. Przepisu nie da sie odtworzyc, i to jest caly powod tej zmiany.

CO PILNUJA TE TESTY

1. ZNACZENIE CECHY. `book_spread_bps` to PELNY spread w punktach bazowych,
   czyli dokladnie koszt rundy - "polowa na noge razy dwie nogi" jest juz
   w srodku. Podwojenie zawyzyloby doplate dwukrotnie i nie wywrocilo niczego.
2. STATYSTYKA. Srednia ze srednich per instrument, a nie mediana (zanizylaby
   runde o czynnik 1,93 na zmierzonych danych) i nie srednia ze zlaczonej
   proby (waga instrumentu zalezalaby wtedy od kompletnosci jego danych).
3. UNIWERSUM I OKNO. Reguly zyja w SQL-u, wiec testy nizej pytaja PRAWDZIWEGO
   Postgresa. Atrapa, ktora sama odtwarza filtr `instrument_id = ANY(...)`,
   nie dowodzi, ze filtr jest w zapytaniu.
4. BRAMKA SPOJNOSCI. Kwotowanie porownywalne z calym zakresem high-low swiecy
   NIE JEST cena, po ktorej ktos handluje - tak wygladaja dane equity z feedu
   IEX (mediana ilorazu 0,84; crypto 0,033). Taki pomiar zostaje odrzucony,
   a nie wpisany do karty kosztu.
5. CZYJA KSIEGA. Okno siegajace przed 2026-09-24 albo instrument
   `tradeable = false` oznacza arkusz z hosta globalnego - karta ZOSTAJE
   NIEPELNA i `--spread-obca-ksiega` zachowuje swoja semantyke.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from services.engine.koszty import INNY_RYNEK, POMIAR, karta_rundy
from services.engine.spread import (
    DATA_WLASNEJ_KSIEGI, MIN_INSTRUMENTOW, MIN_SWIEC_NA_INSTRUMENT,
    PROG_SPOJNOSCI, Pomiar, opis_pomiaru, zmierz_spread,
)

KORZEN = Path(__file__).resolve().parents[1]
BASE = (KORZEN / "services" / "exchanges" / "base.py").read_text(encoding="utf-8")
FEATURES = (KORZEN / "services" / "ingest" / "features.py").read_text(encoding="utf-8")
WORKER = (KORZEN / "services" / "ingest" / "market_worker.py").read_text(encoding="utf-8")
VALUES = yaml.safe_load((KORZEN / "charts" / "trader" / "values.yaml").read_text(encoding="utf-8"))


# ================================================ ZNACZENIE CECHY
#
# Trzy ogniwa lancucha, kazde w innym pliku. Dopiero razem odpowiadaja na
# pytanie "czy razy dwie nogi jest juz w srodku".

def test_ksiega_oddaje_PELNY_spread_a_nie_polowe():
    """`OrderBook.spread` to ask minus bid - cala szerokosc, nie polowa."""
    assert "return self.asks[0].price - self.bids[0].price" in BASE, (
        "definicja spreadu w `services/exchanges/base.py` zmienila sie — "
        "cala interpretacja `book_spread_bps` (i koszt rundy) wisi na tym, "
        "ze jest to ask MINUS bid, czyli PELNA szerokosc arkusza")


def test_worker_zapisuje_spread_BEZ_przeliczania():
    """Miedzy arkuszem a baza nie ma zadnego mnoznika."""
    assert "book.mid, book.spread," in WORKER, (
        "market_worker przestal zapisywac `book.spread` wprost — jesli "
        "wstawil tu mnoznik, `book_spread_bps` znaczy co innego niz mysli "
        "`services/engine/spread.py`")


def test_cecha_to_ulamek_ceny_w_punktach_bazowych():
    """avg(spread / mid) * 10000 - punkty bazowe pelnego spreadu."""
    assert "avg(spread / NULLIF(mid, 0)) * 10000" in FEATURES, (
        "agregacja cechy `book_spread_bps` zmienila sie; jesli mnoznik nie "
        "jest 10000 albo licznik nie jest pelnym spreadem, liczba podana "
        "do `karta_rundy` nie jest kosztem rundy")


def test_karta_NIE_PODWAJA_podanego_spreadu():
    """Runda placi pol spreadu na wejsciu i pol na wyjsciu - razem jeden pelny.

    To jest ten blad, ktorego tu pilnujemy: dolozenie "razy dwie nogi" do
    liczby, ktora juz jest pelnym spreadem, zawyzylo by doplate dwukrotnie -
    i nic by sie nie wywrocilo.
    """
    k = karta_rundy("spot", maker=True, spread_bps=8.62)
    s = next(x for x in k.skladniki if x.nazwa == "spread")
    assert s.wartosc == pytest.approx(8.62e-4)
    assert s.wartosc != pytest.approx(2 * 8.62e-4)


# ================================================ ZNACZENIE CECHY
#
# Trzy ogniwa lancucha, kazde w innym pliku. Dopiero razem odpowiadaja na
# pytanie "czy razy dwie nogi jest juz w srodku".

def test_ksiega_oddaje_PELNY_spread_a_nie_polowe():
    """`OrderBook.spread` to ask minus bid - cala szerokosc, nie polowa."""
    assert "return self.asks[0].price - self.bids[0].price" in BASE, (
        "definicja spreadu w `services/exchanges/base.py` zmienila sie — "
        "cala interpretacja `book_spread_bps` (i koszt rundy) wisi na tym, "
        "ze jest to ask MINUS bid, czyli PELNA szerokosc arkusza")


def test_worker_zapisuje_spread_BEZ_przeliczania():
    """Miedzy arkuszem a baza nie ma zadnego mnoznika."""
    assert "book.mid, book.spread," in WORKER, (
        "market_worker przestal zapisywac `book.spread` wprost — jesli "
        "wstawil tu mnoznik, `book_spread_bps` znaczy co innego niz mysli "
        "`services/engine/spread.py`")


def test_cecha_to_ulamek_ceny_w_punktach_bazowych():
    """avg(spread / mid) * 10000 - punkty bazowe pelnego spreadu."""
    assert "avg(spread / NULLIF(mid, 0)) * 10000" in FEATURES, (
        "agregacja cechy `book_spread_bps` zmienila sie; jesli mnoznik nie "
        "jest 10000 albo licznik nie jest pelnym spreadem, liczba podana "
        "do `karta_rundy` nie jest kosztem rundy")


def test_karta_NIE_PODWAJA_podanego_spreadu():
    """Runda placi pol spreadu na wejsciu i pol na wyjsciu - razem jeden pelny.

    To jest ten blad, ktorego tu pilnujemy: dolozenie "razy dwie nogi" do
    liczby, ktora juz jest pelnym spreadem, zawyzyloby doplate dwukrotnie -
    i nic by sie nie wywrocilo.
    """
    k = karta_rundy("spot", maker=True, spread_bps=8.62)
    s = next(x for x in k.skladniki if x.nazwa == "spread")
    assert s.wartosc == pytest.approx(8.62e-4)
    assert s.wartosc != pytest.approx(2 * 8.62e-4)


# ================================================ CHART BEZ STALEJ

def test_chart_NIE_PODAJE_juz_stalej_spreadu():
    """Zaden tor nie ma czynnego `spreadBps` - domyslnie obowiazuje pomiar.

    Klucz zostaje w pliku jako ZAKOMENTOWANE nadpisanie awaryjne, wiec test
    pyta o rozstrzygnieta strukture YAML, a nie o tresc komentarzy.
    """
    tc = VALUES["trainerCron"]
    assert "spreadBps" not in tc, (
        "wrocila stala spreadu dla toru crypto — trener przestal liczyc ja "
        "z bazy i znow opisuje rynek z dnia, w ktorym ktos wpisal liczbe")
    assert "spreadBps" not in tc["equity"], (
        "tor equity dostal stala spreadu; dane z feedu IEX nie sa pomiarem "
        "naszego spreadu — patrz bramka spojnosci")
    assert "spreadObcaKsiega" not in tc, (
        "flaga obcej ksiegi wrocila do chartu — o pochodzeniu arkusza "
        "rozstrzyga POMIAR (data okna i `tradeable`), nie wpis w pliku")


def test_diagnostyka_pyta_o_ZMIERZONY_spread_a_nie_o_stala():
    """Proba P0-4 sprawdzala koszt progowy wobec liczby, ktora sama podawala."""
    tresc = (KORZEN / "charts" / "trader" / "templates"
             / "diagnostyka-trener-cron.yaml").read_text(encoding="utf-8")
    czynne = [l for l in tresc.splitlines()
              if "--spread-bps" in l and not l.strip().startswith("#")]
    assert not czynne, (
        "diagnostyka znow karmi trenera stala, wiec pytanie o koszt progowy "
        f"ma z gory ustalona prawa strone: {czynne}")


# ================================================ PRAWDZIWY POSTGRES
#
# Uniwersum i okno sa filtrami W ZAPYTANIU. Atrapa kursora, ktora sama
# odfiltrowalaby instrumenty spoza listy, przepuscilaby wyciecie calego
# warunku `instrument_id = ANY(...)` - wczoraj dokladnie tak zginela regula
# SQL i padl tylko test na napis.
#
#     docker run -d --name koszt-db -e POSTGRES_USER=trader \
#         -e POSTGRES_PASSWORD=ci -e POSTGRES_DB=trader -p 55470:5432 \
#         timescale/timescaledb-ha:pg17
#     DATABASE_URL=postgresql://trader:ci@localhost:55470/trader \
#         .venv/bin/python -B db/migrate.py
#     TEST_DB_URL=postgresql://trader:ci@localhost:55470/trader \
#         .venv/bin/python -B -m pytest -q tests/test_spread_z_bazy.py

TEST_DB_URL = os.environ.get("TEST_DB_URL")
wymaga_bazy = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="ustaw TEST_DB_URL na jednorazowa baze z zastosowanymi migracjami")

OD = DATA_WLASNEJ_KSIEGI + timedelta(days=1)
DO = OD + timedelta(days=40)


def _polacz():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(TEST_DB_URL, row_factory=dict_row)


def _instrument(cur, symbol: str, engine: str = "crypto",
                tradeable: bool = True) -> int:
    cur.execute("SELECT id FROM ref.venue LIMIT 1")
    venue = cur.fetchone()["id"]
    cur.execute(
        "INSERT INTO ref.instrument (venue_id, symbol, engine, quote_ccy, kind,"
        " active, tradeable) VALUES (%s,%s,%s,'USDC','spot',true,%s) RETURNING id",
        (venue, symbol, engine, tradeable))
    return cur.fetchone()["id"]


def _cecha(cur, bar: str = "4h") -> int:
    cur.execute(
        "INSERT INTO feat.feature_def (name, params, code_ref, active)"
        " VALUES (%s, %s::jsonb, 'features.microstructure:spread', true)"
        " RETURNING id",
        (f"book_spread_bps__{bar}_test",
         '{"role": "book_spread_bps", "bar": "' + bar + '"}'))
    return cur.fetchone()["id"]


def _zasiej(cur, feat_id: int, inst_id: int, wartosci: list[float],
            od: datetime = OD, zakres_bps: float = 400.0) -> None:
    """Swiece 4h ze spreadem `wartosci[i]` i zakresem high-low `zakres_bps`.

    Zakres jest domyslnie duzo szerszy od spreadu, zeby bramka spojnosci
    przechodzila - jej wlasny test podaje wartosci, przy ktorych nie przechodzi.
    """
    for i, v in enumerate(wartosci):
        t = od + timedelta(hours=4 * i)
        cur.execute(
            "INSERT INTO feat.feature_value (feature_def_id, instrument_id,"
            " event_time, value, ingested_at) VALUES (%s,%s,%s,%s,%s)",
            (feat_id, inst_id, t, v, t))
        # Cztery swiece zrodlowe 1h na jedna swiece 4h - tak jak liczy je
        # `features.compute_orderbook` i tak, jak agreguje je zapytanie.
        for j in range(4):
            srodek = 100.0
            polowa = srodek * zakres_bps / 2e4
            cur.execute(
                "INSERT INTO market.candle (instrument_id, timeframe, event_time,"
                " open, high, low, close, volume) VALUES (%s,'1h',%s,%s,%s,%s,%s,1)",
                (inst_id, t + timedelta(hours=j), srodek, srodek + polowa,
                 srodek - polowa, srodek))


@wymaga_bazy
def test_PRAWDZIWA_BAZA_agregat_to_SREDNIA_ZE_SREDNICH_per_instrument():
    """Trzy instrumenty o roznej liczbie swiec - i dlatego trzy rozne odpowiedzi.

    Srednia ze zlaczonej proby dawalaby instrumentowi o dwa razy dluzszym
    szeregu dwa razy wieksza wage, czyli o koszcie decydowalaby kompletnosc
    danych. Mediana odpowiadalaby na inne pytanie niz "ile placi transakcja".
    """
    with _polacz() as conn:
        cur = conn.cursor()
        f = _cecha(cur)
        a = _instrument(cur, "AAA_TEST")
        b = _instrument(cur, "BBB_TEST")
        c = _instrument(cur, "CCC_TEST")
        _zasiej(cur, f, a, [1.0] * 40)
        _zasiej(cur, f, b, [5.0] * 40)
        _zasiej(cur, f, c, [30.0] * 80)

        p = zmierz_spread(cur, "crypto", "4h", "1h", 240, [a, b, c], OD, DO)
        assert p.instrumentow == 3 and p.n == 160
        assert p.bps == pytest.approx((1.0 + 5.0 + 30.0) / 3)
        # Zlaczona srednia: (40*1 + 40*5 + 80*30) / 160 = 16.5
        assert p.bps != pytest.approx(16.5), (
            "agregat policzyl srednia ze ZLACZONEJ proby — instrument o dluzszym "
            "szeregu wazy wtedy wiecej, choc nie handlujemy nim czesciej")
        # Mediana po swiecach to jeszcze co innego: (5 + 30) / 2 = 17,5.
        assert p.rozklad["mediana"] == pytest.approx(17.5)
        assert p.bps != pytest.approx(p.rozklad["mediana"])
        conn.rollback()


@wymaga_bazy
def test_PRAWDZIWA_BAZA_instrument_spoza_uniwersum_NIE_WCHODZI():
    """Filtr uniwersum jest W ZAPYTANIU, a nie w kodzie po odczycie.

    Czwarty instrument ma spread 1000 pb i lezy w tym samym oknie. Jesli
    `instrument_id = ANY(...)` zniknie z SQL-a, agregat skoczy - i tylko ten
    test to zobaczy.
    """
    with _polacz() as conn:
        cur = conn.cursor()
        f = _cecha(cur)
        ids = [_instrument(cur, s) for s in ("AA_U", "BB_U", "CC_U")]
        obcy = _instrument(cur, "ZZ_U")
        for i in ids:
            _zasiej(cur, f, i, [4.0] * 40)
        _zasiej(cur, f, obcy, [1000.0] * 40, zakres_bps=40000.0)

        p = zmierz_spread(cur, "crypto", "4h", "1h", 240, ids, OD, DO)
        assert p.instrumentow == 3
        assert p.bps == pytest.approx(4.0), (
            "instrument spoza podanego uniwersum wszedl do agregatu — filtr "
            "`instrument_id = ANY(...)` nie dziala w zapytaniu")
        conn.rollback()


@wymaga_bazy
def test_PRAWDZIWA_BAZA_wartosc_POZA_OKNEM_nie_wchodzi():
    """Okno jest tym samym oknem, ktore trenuje `train()` - liczy je SQL."""
    with _polacz() as conn:
        cur = conn.cursor()
        f = _cecha(cur)
        ids = [_instrument(cur, s) for s in ("AA_W", "BB_W", "CC_W")]
        for i in ids:
            _zasiej(cur, f, i, [4.0] * 40)
            # Ten sam instrument, ale pol roku pozniej i ze spreadem 500 pb.
            _zasiej(cur, f, i, [500.0] * 40, od=DO + timedelta(days=180),
                    zakres_bps=40000.0)

        p = zmierz_spread(cur, "crypto", "4h", "1h", 240, ids, OD, DO)
        assert p.n == 120 and p.bps == pytest.approx(4.0), (
            "wartosci z poza okna treningowego weszly do agregatu")
        assert p.do < DO
        conn.rollback()

@wymaga_bazy
def test_PRAWDZIWA_BAZA_krotkie_pokrycie_wypada_z_agregatu():
    """Instrument o garstce swiec mowi o momencie pomiaru, nie o rynku."""
    with _polacz() as conn:
        cur = conn.cursor()
        f = _cecha(cur)
        ids = [_instrument(cur, s) for s in ("AA_P", "BB_P", "CC_P")]
        krotki = _instrument(cur, "ZZ_P")
        for i in ids:
            _zasiej(cur, f, i, [4.0] * 40)
        _zasiej(cur, f, krotki, [900.0] * (MIN_SWIEC_NA_INSTRUMENT - 1),
                zakres_bps=40000.0)

        p = zmierz_spread(cur, "crypto", "4h", "1h", 240,
                          ids + [krotki], OD, DO)
        assert p.instrumentow == 3 and p.pominietych == 1
        assert p.bps == pytest.approx(4.0)
        conn.rollback()


@wymaga_bazy
def test_PRAWDZIWA_BAZA_za_malo_instrumentow_to_BRAK_POMIARU():
    """Ponizej progu oddajemy None i powod, a nie liczbe z dwoch par."""
    with _polacz() as conn:
        cur = conn.cursor()
        f = _cecha(cur)
        ids = [_instrument(cur, s) for s in ("AA_M", "BB_M")]
        for i in ids:
            _zasiej(cur, f, i, [4.0] * 40)

        p = zmierz_spread(cur, "crypto", "4h", "1h", 240, ids, OD, DO)
        assert p.bps is None and str(MIN_INSTRUMENTOW) in p.powod
        assert "NIE ZMIERZONO" in opis_pomiaru(p)[0]
        # I najwazniejsze: karta kosztu zostaje z „nie zmierzono", a nie z zerem.
        k = karta_rundy("spot", maker=True, spread_bps=p.bps)
        assert "spread" in k.nieznane and not k.pelna
        conn.rollback()


# ================================================ BRAMKA SPOJNOSCI

@wymaga_bazy
def test_PRAWDZIWA_BAZA_kwotowanie_szersze_niz_swieca_jest_ODRZUCANE():
    """Tak wygladaja dane equity z feedu IEX - i nie wolno ich wpisac do karty.

    Zmierzone na produkcji 2026-09-27: dla 20 spolek uniwersum mediana ilorazu
    (spread swiecy / zakres high-low swiecy) to 0,84, a w 257 z 560 przypadkow
    kwotowanie bylo SZERSZE niz caly zakres. Dla crypto ta sama liczba to
    0,033. Kwotowanie, ktore bracketuje kazda transakcje wydrukowana w oknie,
    nie jest cena, po ktorej ktokolwiek handluje.

    KANAREK: po zdjeciu bramki ten test przechodzi na `bps` rownym 340 pb i do
    karty kosztu wchodzi 3,4% rundy - liczba, ktora zabija kazda strategie.
    """
    with _polacz() as conn:
        cur = conn.cursor()
        f = _cecha(cur)
        ids = [_instrument(cur, s) for s in ("AA_S", "BB_S", "CC_S")]
        for i in ids:
            # Zakres swiecy 400 pb, kwotowanie 340 pb - iloraz 0,85, jak equity.
            _zasiej(cur, f, i, [340.0] * 40, zakres_bps=400.0)

        p = zmierz_spread(cur, "crypto", "4h", "1h", 240, ids, OD, DO)
        assert p.bps is None, (
            "bramka spojnosci przepuscila kwotowanie szersze niz 0,5 zakresu "
            "swiecy — do karty kosztu weszlaby liczba z arkusza, ktory nie "
            "wypelnia zadnego zlecenia")
        assert "ODRZUCONY" in p.powod
        assert p.spojnosc is not None and p.spojnosc > PROG_SPOJNOSCI
        # Rozklad zostaje policzony, zeby dalo sie zobaczyc, CO odrzucono.
        assert p.rozklad["mediana"] == pytest.approx(340.0)
        conn.rollback()


@wymaga_bazy
def test_PRAWDZIWA_BAZA_dane_crypto_PRZECHODZA_te_sama_bramke():
    """Strona odwrotna: prog nie jest dobrany tak, zeby odrzucal wszystko.

    Bez tego testu bramka o progu 0,001 tez „dzialalaby" — i odrzucalaby takze
    pomiar, ktory jest poprawny.
    """
    with _polacz() as conn:
        cur = conn.cursor()
        f = _cecha(cur)
        ids = [_instrument(cur, s) for s in ("AA_C", "BB_C", "CC_C")]
        for i in ids:
            # Iloraz 0,033 - tyle wyszlo na prawdziwych danych crypto.
            _zasiej(cur, f, i, [13.2] * 40, zakres_bps=400.0)

        p = zmierz_spread(cur, "crypto", "4h", "1h", 240, ids, OD, DO)
        assert p.bps == pytest.approx(13.2)
        assert p.spojnosc == pytest.approx(0.033, abs=0.001)
        conn.rollback()


@wymaga_bazy
def test_PRAWDZIWA_BAZA_brak_swiec_to_BRAK_POMIARU_a_nie_pomiar_niesprawdzony():
    """Kontrola, ktorej nie dalo sie wykonac, nie jest kontrola zaliczona."""
    with _polacz() as conn:
        cur = conn.cursor()
        f = _cecha(cur)
        ids = [_instrument(cur, s) for s in ("AA_B", "BB_B", "CC_B")]
        for i in ids:
            for j in range(40):
                t = OD + timedelta(hours=4 * j)
                cur.execute(
                    "INSERT INTO feat.feature_value (feature_def_id,"
                    " instrument_id, event_time, value, ingested_at)"
                    " VALUES (%s,%s,%s,4.0,%s)", (f, i, t, t))

        p = zmierz_spread(cur, "crypto", "4h", "1h", 240, ids, OD, DO)
        assert p.bps is None and "spojnosci" in p.powod
        conn.rollback()


# ================================================ CZYJA KSIEGA

@wymaga_bazy
def test_PRAWDZIWA_BAZA_okno_sprzed_24_09_to_OBCA_ksiega():
    """1400 dni treningu siega daleko przed 2026-09-24 - i to jest regula.

    Wtedy arkusz KAZDEGO instrumentu szedl z hosta globalnego, wiec pomiar jest
    OSZACOWANIEM: wchodzi do sumy, ale karta zostaje niepelna, a raport podaje
    krotnosc spreadu, przy ktorej przewaga schodzi do zera.
    """
    przed = DATA_WLASNEJ_KSIEGI - timedelta(days=10)
    with _polacz() as conn:
        cur = conn.cursor()
        f = _cecha(cur)
        ids = [_instrument(cur, s) for s in ("AA_O", "BB_O", "CC_O")]
        for i in ids:
            _zasiej(cur, f, i, [4.0] * 40, od=przed)

        p = zmierz_spread(cur, "crypto", "4h", "1h", 240, ids, przed, DO)
        assert p.bps == pytest.approx(4.0)
        assert not p.nasza_ksiega, (
            "pomiar z okna siegajacego przed 2026-09-24 oglosil sie ksiega "
            "egzekucji — arkusz szedl wtedy z hosta globalnego")
        k = karta_rundy("spot", maker=True, spread_bps=p.bps,
                        spread_nasza_ksiega=p.nasza_ksiega)
        s = next(x for x in k.skladniki if x.nazwa == "spread")
        assert s.zrodlo == INNY_RYNEK and "spread" in k.nieznane and not k.pelna
        assert any("NIEPEŁNA" in l for l in opis_pomiaru(p))
        conn.rollback()


@wymaga_bazy
def test_PRAWDZIWA_BAZA_okno_po_24_09_na_tradeable_to_NASZA_ksiega():
    """Strona odwrotna. Bez niej flaga mogla by zawsze mowic „obca"."""
    with _polacz() as conn:
        cur = conn.cursor()
        f = _cecha(cur)
        ids = [_instrument(cur, s) for s in ("AA_N", "BB_N", "CC_N")]
        for i in ids:
            _zasiej(cur, f, i, [4.0] * 40)

        p = zmierz_spread(cur, "crypto", "4h", "1h", 240, ids, OD, DO)
        assert p.nasza_ksiega
        k = karta_rundy("spot", maker=True, spread_bps=p.bps,
                        spread_nasza_ksiega=p.nasza_ksiega)
        s = next(x for x in k.skladniki if x.nazwa == "spread")
        assert s.zrodlo == POMIAR and "spread" not in k.nieznane
        conn.rollback()


@wymaga_bazy
def test_PRAWDZIWA_BAZA_instrument_NIETRADEABLE_psuje_wlasna_ksiege():
    """Od 2026-09-25 ksiegi egzekucji pytamy WYLACZNIE o `tradeable`.

    Pozostale instrumenty maja arkusz z hosta globalnego nadal i celowo, zeby
    szereg `book_spread_bps` byl ciagly. Pomiar rozciagniety na nie NIE jest
    wiec pomiarem naszej ksiegi - i flaga musi to zobaczyc.
    """
    with _polacz() as conn:
        cur = conn.cursor()
        f = _cecha(cur)
        ids = [_instrument(cur, s) for s in ("AA_T", "BB_T")]
        ids.append(_instrument(cur, "CC_T", tradeable=False))
        for i in ids:
            _zasiej(cur, f, i, [4.0] * 40)

        p = zmierz_spread(cur, "crypto", "4h", "1h", 240, ids, OD, DO)
        assert p.bps == pytest.approx(4.0)
        assert not p.nasza_ksiega, (
            "pomiar obejmujacy instrument `tradeable = false` oglosil sie "
            "ksiega egzekucji — jego arkusz idzie z hosta globalnego")
        conn.rollback()


@wymaga_bazy
def test_PRAWDZIWA_BAZA_equity_NIGDY_nie_jest_ksiega_egzekucji():
    """Alpaca ma jedno miejsce egzekucji, ale feed danych to IEX - jedna gielda.

    Nawet gdyby liczby przeszly bramke spojnosci, nie sa arkuszem, w ktorym
    staje nasze zlecenie. Dlatego karta equity zostaje niepelna z DWOCH powodow
    naraz i zaden z nich nie jest brakiem klucza w charcie.
    """
    with _polacz() as conn:
        cur = conn.cursor()
        f = _cecha(cur, bar="1h")
        ids = [_instrument(cur, s, engine="equity") for s in ("AA_E", "BB_E", "CC_E")]
        for i in ids:
            _zasiej(cur, f, i, [4.0] * 40)

        p = zmierz_spread(cur, "equity", "1h", "1h", 60, ids, OD, DO)
        assert not p.nasza_ksiega
        conn.rollback()
