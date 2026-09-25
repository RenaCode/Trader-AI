"""Ranking przekrojowy liczy się w obrębie JEDNEGO rynku — tak jak jego odcisk.

CO BYŁO NIE TAK

`rel_strength` to percentyl zwrotu instrumentu w rankingu CAŁEGO zbioru, a sens
tego sygnału polega na ODJĘCIU wspólnego czynnika rynku: gdy rośnie wszystko,
ranking dalej mówi, kto rośnie mocniej od reszty.

Zbiór, o którym ta wartość mówi, nazywa odcisk w `params->>'uniwersum'`
(`services/engine/uniwersum.py`). Odcisk liczy się z zapytania filtrującego
`engine = %s AND active AND tradeable AND kind <> 'perp'`
(`features.przebieg`). Samo zapytanie rankingu — `compute_cross_section` —
filtrowało do 2026-09-25 tylko `i.active AND i.tradeable`. Dwa różne zbiory
pod jedną nazwą.

DLACZEGO DZIŚ TO NIC NIE ZMIENIA, A JUTRO BĘDZIE NIEODWRACALNE

Dziś `tradeable = true` mają wyłącznie dziesięć par USDC Bybita: migracja 0023
zdjęła tę flagę parom USDT, perpetuale jej nigdy nie miały, a migracja 0047
ustawiła `tradeable = false` wszystkim 21 akcjom Alpaki. Oba zapytania zwracają
więc DOKŁADNIE ten sam zbiór — i pierwszy test niżej to mierzy, zamiast
zapewniać.

`0047_instrumenty_alpaca.sql` zapowiada jednak wprost osobną migrację włączającą
akcje do uniwersum. Od tej chwili zapytanie bez `engine` liczyłoby percentyl
w rankingu 31 instrumentów naraz — 21 akcji i 10 par krypto — czyli sygnał
odejmujący czynnik rynku mierzyłby „czy krypto rośnie szybciej niż akcje".
Wartości wyszłyby poprawne składniowo, zapisane pod nazwą z odciskiem
dziesięciu par, i nie byłoby jak ich odróżnić od wartości policzonych dobrze.

DLACZEGO ATRAPA WYKONUJE PRAWDZIWY SQL

Przepisanie warunków do Pythona testowałoby moje przepisanie, a cała poprawka
siedzi w treści zapytania — to ten sam powód, dla którego tak zrobiono
w `test_licznik_zlecen_w_cyklu.py`. Zapytanie bierzemy stąd, gdzie stoi:
wołamy `compute_cross_section`, a kursor-atrapa tłumaczy SQL na dialekt sqlite
i wykonuje go na tabelach wypełnionych realnym składem `ref.instrument`.
Wersja SPRZED poprawki jest odtwarzana przez usunięcie z TEJ SAMEJ treści
zapytania dwóch dopisanych warunków, więc obie strony porównania pochodzą
z jednego źródła.

Żadne zapytanie nie idzie do produkcyjnej bazy ani do giełdy.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from services.ingest.features import MIN_UNIVERSE, compute_cross_section
from services.engine.horizons import H1H

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)

# SKŁAD `ref.instrument` ODTWORZONY ZE STANU BAZY, nie wymyślony.
#
#   * 10 par USDC Bybita — całe dzisiejsze uniwersum decyzyjne (`tradeable`),
#   * 10 odpowiadających par USDT — `active`, ale `tradeable = false` od
#     migracji 0023; ten sam aktyw w bazie DWA RAZY,
#   * 4 perpetuale — `kind = 'perp'`, wpisywane przez `funding.ensure_perp`,
#   * 21 akcji Alpaki — `active`, `tradeable = false` (migracja 0047).
#
# 34 wiersze crypto i 21 equity to liczby, które panel pokazuje dziś jako
# „Bybit — 34 instrumentów" i „Alpaca — 21".
USDC = ["BTCUSDC", "ETHUSDC", "XRPUSDC", "SOLUSDC", "DOGEUSDC",
        "ADAUSDC", "LINKUSDC", "ARBUSDC", "SUIUSDC", "NEARUSDC"]
USDT = [s.replace("USDC", "USDT") for s in USDC]
PERPY = ["BTCUSDT.P", "ETHUSDT.P", "SOLUSDT.P", "XRPUSDT.P"]
AKCJE = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO",
         "NFLX", "JPM", "V", "UNH", "XOM", "JNJ", "WMT", "PG", "MA", "HD",
         "COST", "ABBV", "SPY"]


def _instrumenty(equity_tradeable: bool,
                 perp_tradeable: bool = False) -> list[tuple]:
    """(id, symbol, engine, kind, active, tradeable) — jak w `ref.instrument`.

    DOMYSLNE WARTOSCI TO STAN BAZY NA 2026-09-25, nie wygodny przypadek:
    pary USDC handlowe, pary USDT aktywne i niehandlowe (0023), perpetuale
    aktywne i NIEhandlowe (0020), akcje aktywne i niehandlowe (0047).

    `perp_tradeable=True` to hipotetyczny skutek punktu 11b audytu: `ensure_perp`
    nie podaje kolumny `tradeable`, a DEFAULT to `true`. Od migracji 0020 nie
    powstal ani jeden taki wiersz, wiec dzis tego przypadku w bazie NIE MA -
    i dlatego nie wolno go wliczac do dowodu "poprawka nic nie zmienia".
    """
    wiersze: list[tuple] = []
    nid = 0
    for s in USDC:
        nid += 1
        wiersze.append((nid, s, "crypto", "spot", 1, 1))
    for s in USDT:
        nid += 1
        wiersze.append((nid, s, "crypto", "spot", 1, 0))
    for s in PERPY:
        nid += 1
        # `active=1`, choc `ensure_perp` wpisuje `false`: gdyby perpetual byl tu
        # nieaktywny, warunek `i.kind <> 'perp'` nie mialby czego odrzucac
        # i test mierzylby `i.active`, a nie to, co mierzyc ma.
        wiersze.append((nid, s, "crypto", "perp", 1, 1 if perp_tradeable else 0))
    for s in AKCJE:
        nid += 1
        wiersze.append((nid, s, "equity", "stock", 1, 1 if equity_tradeable else 0))
    return wiersze


def _na_sqlite(sql: str) -> str:
    """Prawdziwy SQL z modułu, przetłumaczony na dialekt sqlite.

    Tłumaczymy wyłącznie SKŁADNIĘ, nigdy warunków — inaczej test mierzyłby
    tłumaczenie. `percent_rank() OVER (...)`, `count(*)`, CTE i `JOIN ... ON`
    sqlite obsługuje bez zmian.

    KOMENTARZE `--` LECĄ PIERWSZE. Zwinięcie zapytania w jeden wiersz PRZED ich
    usunięciem zamienia pierwszy komentarz w koniec zapytania — cicho, bo sqlite
    mówi na to tylko „incomplete input".
    """
    s = " ".join(w.split("--")[0] for w in sql.splitlines())
    s = " ".join(s.split())
    s = s.replace("feat.feature_value", "feat_feature_value")
    s = s.replace("ref.instrument", "ref_instrument")
    # `i.engine::text` -> `i.engine`: w Postgresie `engine` jest typem
    # wyliczeniowym i rzutowanie jest tam konieczne, w sqlite kolumna jest
    # tekstem. Sam warunek zostaje bez zmian.
    s = s.replace("i.engine::text", "i.engine")
    # Lista instrumentow: w tym tescie jest zawsze NULL-em (uniwersum decyzyjne),
    # a `x = ANY(NULL)` w Postgresie daje NULL, czyli falsz - tak samo jak
    # `x = NULL` w sqlite. Tlumaczenie zachowuje wiec wynik obu galezi.
    s = s.replace("%s::int[] IS NULL", "? IS NULL")
    s = s.replace("= ANY(%s)", "= ?")
    return s.replace("%s", "?")


def _bez_nowych_warunkow(sql: str) -> str:
    """Wersja SPRZED poprawki, wycięta z TEJ SAMEJ treści zapytania.

    Druga, ręcznie przepisana kopia zapytania rozjechałaby się z pierwszą i test
    porównywałby wtedy dwie własne wersje, a nie kodu z produkcją.
    """
    bez = re.sub(r"AND\s+i\.engine\s*=\s*\?\s*", "", sql)
    bez = re.sub(r"AND\s+i\.kind\s*<>\s*'perp'\s*", "", bez)
    assert bez != sql, "nie ma czego usuwać — poprawka zniknęła z zapytania"
    return bez


class Kursor:
    """Kursor wykonujący SQL na sqlite i zapamiętujący, co dostał.

    `executemany` (używane przez `insert_values`) tylko liczy wiersze: zapis
    cech nie jest tu przedmiotem pomiaru, a tabela docelowa i tak jest atrapą.
    """

    def __init__(self, db, przycinanie=None):
        self.db = db
        self.przycinanie = przycinanie
        self.sql_ostatni = ""
        self.zapisane: list[tuple] = []
        self._wiersze: list[sqlite3.Row] = []

    def execute(self, sql, params=None):
        self.sql_ostatni = _na_sqlite(sql)
        do_wykonania = (self.przycinanie(self.sql_ostatni) if self.przycinanie
                        else self.sql_ostatni)
        p = list(params or ())
        if self.przycinanie:
            # Usunięcie warunku `i.engine = ?` zabiera JEDEN parametr — ten sam,
            # który poprawka dodała na początku listy.
            p = p[1:]
        # `timestamptz` -> ISO 8601. Ten sam format, w którym atrapa zapisała
        # `event_time`, więc porównanie tekstowe daje ten sam wynik co czasowe.
        p = [x.isoformat() if isinstance(x, datetime) else x for x in p]
        self._wiersze = self.db.execute(do_wykonania, tuple(p)).fetchall()

    def fetchall(self):
        # `event_time` wraca z sqlite jako TEKST; Postgres oddaje `timestamptz`,
        # a `znane_od()` liczy na nim arytmetykę czasu. Konwersja z powrotem
        # należy do tłumaczenia dialektu, nie do mierzonego zachowania.
        wynik = []
        for w in self._wiersze:
            d = dict(w)
            if isinstance(d.get("event_time"), str):
                d["event_time"] = datetime.fromisoformat(d["event_time"])
            wynik.append(d)
        return wynik

    def executemany(self, sql, rows):
        self.zapisane.extend(rows)


def _baza(equity_tradeable: bool, perp_tradeable: bool = False,
          swiec: int = 3):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE ref_instrument (id INTEGER, symbol TEXT, engine TEXT,
            kind TEXT, active INTEGER, tradeable INTEGER);
        CREATE TABLE feat_feature_value (feature_def_id INTEGER,
            instrument_id INTEGER, event_time TEXT, value REAL);
    """)
    db.executemany("INSERT INTO ref_instrument VALUES (?,?,?,?,?,?)",
                   _instrumenty(equity_tradeable, perp_tradeable))
    # `ret_mid` policzone dla KAŻDEGO aktywnego instrumentu - bo potok liczy
    # cechy szerzej, niż handlujemy. Wartości rosną z id, żeby percentyle były
    # jednoznaczne; ich treść nie jest tu mierzona, liczy się LICZNOŚĆ zbioru.
    wiersze = []
    for k in range(swiec):
        t = (T0 + timedelta(hours=k)).isoformat()
        for (nid, *_rest) in _instrumenty(equity_tradeable, perp_tradeable):
            wiersze.append((11, nid, t, float(nid)))
    db.executemany("INSERT INTO feat_feature_value VALUES (?,?,?,?)", wiersze)
    return db


FEAT_IDS = {"ret_mid": 11, "rel_strength": 22}


def _policz(engine: str, equity_tradeable: bool, *, sprzed_poprawki: bool,
            perp_tradeable: bool = False):
    db = _baza(equity_tradeable, perp_tradeable)
    kursor = Kursor(db, przycinanie=_bez_nowych_warunkow if sprzed_poprawki else None)
    n = compute_cross_section(kursor, H1H, FEAT_IDS, T0,
                              T0 + timedelta(hours=24), None, engine=engine)
    # Liczba instrumentów w rankingu JEDNEJ świecy — to ona nadaje sens
    # percentylowi i to ona jest przedmiotem poprawki.
    na_swiece = {}
    for wiersz in kursor.zapisane:
        na_swiece.setdefault(wiersz[2], 0)
        na_swiece[wiersz[2]] += 1
    return n, sorted(set(na_swiece.values()))


# ------------------------------------------------------- dowód: dziś bez zmian

def test_DZIS_oba_zapytania_zwracaja_dokladnie_tyle_samo():
    """Poprawka jest odwracalna dopóki uniwersum equity jest puste — i tu jest
    na to dowód, a nie zapewnienie.

    Gdyby liczności się różniły, byłaby to zmiana wartości cechy wstecz i wtedy
    poprawka wymagałaby przeliczenia historii, czyli decyzji właściciela.
    """
    po, po_swiece = _policz("crypto", equity_tradeable=False, sprzed_poprawki=False)
    przed, przed_swiece = _policz("crypto", equity_tradeable=False,
                                  sprzed_poprawki=True)
    assert po == przed, (
        f"zapytanie z filtrem zwraca {po} wartości, bez filtru {przed} — "
        f"poprawka ZMIENIA dzisiejsze wartości, więc nie jest odwracalna")
    assert po_swiece == przed_swiece == [len(USDC)], (
        f"ranking idzie po {po_swiece} instrumentach (z filtrem) i {przed_swiece} "
        f"(bez) — spodziewane {len(USDC)} par USDC w obu")


def test_zbior_rankingu_to_dokladnie_uniwersum_decyzyjne():
    """Kanarek do poprzedniego: zapytanie zwracające zero przeszłoby tamten test.

    `tradeable` mają dziś tylko pary USDC, więc pary USDT (ten sam aktyw drugi
    raz), perpetuale i akcje muszą wypaść — a dziesięć par zostać.
    """
    n, na_swiece = _policz("crypto", equity_tradeable=False, sprzed_poprawki=False)
    assert n > 0, "ranking nie policzył niczego — test nie mierzy niczego"
    assert na_swiece == [len(USDC)], (
        f"w rankingu jednej świecy stoi {na_swiece} instrumentów, a uniwersum "
        f"decyzyjne ma {len(USDC)}")
    assert len(USDC) >= MIN_UNIVERSE, (
        "próg MIN_UNIVERSE wyciąłby uniwersum decyzyjne — test mierzyłby próg")


# ------------------------------- dowód: po włączeniu equity różnica jest realna

def test_PO_WLACZENIU_EQUITY_brak_filtru_miesza_dwa_rynki():
    """To jest ta wada, a nie hipoteza o niej.

    Po migracji włączającej akcje do uniwersum zapytanie bez `i.engine` liczy
    percentyl w rankingu 31 instrumentów: 21 akcji i 10 par krypto. Wartość dla
    BTC przestaje mówić „ile mocniej od reszty krypto", a zaczyna „ile mocniej
    od rynku akcji" — pod tą samą nazwą i tym samym odciskiem.
    """
    _, z_filtrem = _policz("crypto", equity_tradeable=True, sprzed_poprawki=False)
    _, bez_filtru = _policz("crypto", equity_tradeable=True, sprzed_poprawki=True)
    assert z_filtrem == [len(USDC)], (
        f"z filtrem ranking krypto ma {z_filtrem} instrumentów zamiast {len(USDC)}")
    assert bez_filtru == [len(USDC) + len(AKCJE)], (
        f"bez filtru ranking miał mieć {len(USDC) + len(AKCJE)} instrumentów "
        f"(10 par + 21 akcji), a ma {bez_filtru} — test nie odtwarza wady")


def test_tor_equity_dostaje_ranking_WLASNEGO_rynku():
    """Odwrotna strona tej samej poprawki: ranking equity nie może iść po krypto."""
    _, equity = _policz("equity", equity_tradeable=True, sprzed_poprawki=False)
    assert equity == [len(AKCJE)], (
        f"ranking equity idzie po {equity} instrumentach zamiast {len(AKCJE)}")


def test_perpetualy_wypadaja_po_RODZAJU_a_nie_przez_zbieg_okolicznosci():
    """`funding.ensure_perp` wpisywał perpetuale bez kolumny `tradeable`, a DEFAULT
    to `true` (punkt 11b audytu). Dziś nie boli wyłącznie dlatego, że od migracji
    0020 nie powstał ani jeden taki wiersz — zbieg okoliczności, nie zabezpieczenie.

    Ranking ma je odrzucać po `kind`, bo perpetual i spot tego samego aktywa mają
    niemal identyczny zwrot: policzone razem przesuwają percentyle całego zbioru.
    """
    _, z_filtrem = _policz("crypto", equity_tradeable=False, sprzed_poprawki=False,
                           perp_tradeable=True)
    _, bez_filtru = _policz("crypto", equity_tradeable=False, sprzed_poprawki=True,
                            perp_tradeable=True)
    assert z_filtrem == [len(USDC)]
    assert bez_filtru == [len(USDC) + len(PERPY)], (
        f"atrapa nie odtwarza perpetuala z `tradeable = true`: bez filtru wyszło "
        f"{bez_filtru}, spodziewane {len(USDC) + len(PERPY)}")


def test_engine_jest_argumentem_OBOWIAZKOWYM():
    """Wartość domyślna zamieniłaby tę poprawkę w zalecenie.

    Wołający, który o `engine` zapomni, ma dostać `TypeError` przy pierwszym
    uruchomieniu, a nie ranking policzony dla `crypto` w potoku equity.
    """
    with pytest.raises(TypeError):
        compute_cross_section(Kursor(_baza(False)), H1H, FEAT_IDS, T0,
                              T0 + timedelta(hours=24), None)
