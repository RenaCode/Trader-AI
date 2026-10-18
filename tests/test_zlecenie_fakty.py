"""Wiersz `exec.order` ma trzymać FAKTY, a nie echo tego, co sami zapisaliśmy.

Trzy różne wady, jedna wspólna przyczyna: zapis rozstrzygnięcia nadpisywał albo
przemilczał stan, którego nikt inny już nie odtworzy.

  * cena i ilość WYSŁANA na giełdę (znalezisko 2) — jedyne, względem czego da
    się zmierzyć jakość wypełnienia,
  * prowizja pobrana w aktywie bazowym (znalezisko 4) — bez niej czujka
    porównuje netto z brutto i alarmuje przy każdym wejściu,
  * status po anulowaniu reszty (znalezisko 5) — `partial` opisuje zlecenie,
    którego reszty już nie ma,
  * zlecenie żyjące na giełdzie mimo odmowy sprawdzenia (znalezisko 7).
"""
import asyncio
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal as D

from services.engine.gielda_exec import posprzataj_zlecenia, rozstrzygnij_plan, zloz_plan
from services.engine.loop import zapisz_wejscie
from services.engine.zlecenia import Filtry
from services.exchanges.base import ExchangeError, StanZlecenia

TERAZ = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
SOLUSDC = Filtry(krok_ilosci=D("0.01"), krok_ceny=D("0.01"),
                 min_wartosc=D("5"), min_ilosc=D("0.01"))


class Kursor:
    """Zapamiętuje SQL, oddaje z góry zadane wiersze — w kolejności zapytań."""

    def __init__(self, wiersze=None):
        self.sql = []
        self._wiersze = list(wiersze or [])
        self._odp = None

    def execute(self, sql, params=None):
        jedno = " ".join(sql.split())
        oczekiwane = jedno.count("%s")
        podane = 0 if params is None else len(params)
        assert oczekiwane == podane, (
            f"zapytanie ma {oczekiwane} miejsc na parametry, a dostało {podane}: "
            f"{jedno}")
        self.sql.append((jedno, tuple(params or ())))
        if jedno.startswith("SELECT"):
            self._odp = self._wiersze.pop(0) if self._wiersze else None

    def fetchone(self):
        return self._odp

    def fetchall(self):
        return [self._odp] if self._odp else []

    def zapytania(self, fragment):
        return [(s, p) for s, p in self.sql if fragment in s]


class Gielda:
    def __init__(self, *, stan=None, blad_anulowania=None, odniesienie=None):
        self._stan = stan
        self._blad = blad_anulowania
        self._odniesienie = odniesienie
        self.anulowane = []

    async def stan_zlecenia(self, id_wlasne, kategoria="spot"):
        return self._stan

    async def anuluj_zlecenie(self, **kw):
        if self._blad:
            raise self._blad
        self.anulowane.append(kw)
        return True

    async def cena_odniesienia(self, symbol, kategoria=None):
        return self._odniesienie

    async def fetch_order_book(self, symbol, depth=50, *, ksiega_egzekucji=True):
        """Arkusz NIEODCZYTANY — i to jest w tym pliku decyzja, nie brak.

        `zloz_plan` czyta arkusz księgi egzekucji, żeby cena limitu pochodziła
        z chwili wysyłki. Ten plik mierzy co innego (POWÓD odmowy giełdy),
        a podstawienie arkusza podmieniałoby cenę wysyłaną i mieszało dwa
        pomiary w jednej asercji. Atrapa MUSI jednak tę metodę mieć: bez niej
        produkcyjne wywołanie kończy się `AttributeError`, który wygląda jak
        błąd testu, a nie jak luka w atrapie.
        """
        raise ExchangeError("arkusz nieczytelny (atrapa)")


# ========================================= 2. cena i ilość wysłana zostają

def test_rozstrzygniecie_wejscia_NIE_NADPISUJE_ceny_ani_ilosci_wyslanej():
    """`poslizg.cena_odniesienia` bierze w pierwszej kolejności
    `exec.order.limit_price` — cenę, KTÓRĄ GIEŁDA DOSTAŁA. Nadpisanie jej ceną
    wykonania (tą samą, która idzie do `exec.fill.price`) dawało poślizg
    każdego wejścia giełdowego dokładnie (x − x)/x = 0, czyli formalne
    potwierdzenie `POSLIZG_MAKER = 0` zbudowane z echa własnego UPDATE-a.

    Ten sam UPDATE kasował `qty` wysłaną, zastępując ją ilością netto — a
    `RiskKernel._open_exposure` wycenia nierozstrzygnięte zlecenia właśnie
    przez `o.qty * o.limit_price`."""
    cur = Kursor()
    zapisz_wejscie(cur, "crypto", "demo", 3, 7, "buy", D("9.99"), D("101.5"),
                   D("0.25"), TERAZ, "USDC", {"execution": "gielda"},
                   order_id=5)

    (sql, params), = cur.zapytania("UPDATE exec.order SET status='filled'")
    assert "limit_price" not in sql, (
        "cena wysłana została nadpisana ceną wykonania — poślizg wejść "
        "gieldowych wychodzi wtedy zerowy z konstrukcji")
    assert " qty=" not in sql, (
        "ilość wysłana została nadpisana ilością netto — znika jedyny zapis "
        "tego, ile naprawdę zamówiliśmy")
    assert D("101.5") not in params and D("9.99") not in params

    # Cena i ilość WYKONANIA mają swoje miejsce i nadal tam trafiają.
    (_, fill), = cur.zapytania("INSERT INTO exec.fill")
    assert D("9.99") in fill and D("101.5") in fill


# ================================ 5. status po anulowaniu reszty jest prawdziwy

def _rozstrzygnij_wejscie(stan, gielda=None):
    plan = {"id": 42, "decision_id": 7, "instrument_id": 3, "side": "buy",
            "qty": D("10"), "limit_price": D("100"), "symbol": "SOLUSDC",
            "quote_ccy": "USDC", "base_ccy": "SOL",
            "trigger": {"rodzaj": "wejscie", "bar": "x"}}
    cur = Kursor([{"id": 1, "qty": D("10")}])
    g = gielda or Gielda(stan=stan)
    rodzaj, msg = asyncio.run(rozstrzygnij_plan(
        cur, g, plan, SOLUSDC, engine="crypto", mode="demo", kategoria="spot",
        teraz=TERAZ, kind="spot"))
    return cur, g, rodzaj, msg


def test_po_zdjeciu_reszty_zlecenie_jest_CANCELLED_a_nie_PARTIAL():
    """`stan.status` odczytujemy PRZED anulowaniem. Wpisanie go z powrotem po
    udanym zdjęciu reszty zostawiało `partial`, więc `_open_exposure` liczyło
    ten sam kapitał dwa razy: raz jako otwartą pozycję, raz jako „zlecenie
    złożone, jeszcze nierozstrzygnięte" (`status IN ('new','partial','unknown')`).
    Ekspozycja była wtedy około dwukrotnie zawyżona i `max_total_exposure`
    blokował wejścia, których nic nie blokuje."""
    stan = StanZlecenia(id_wlasne="tai-p42", id_gieldy="G1", status="partial",
                        ilosc=D("10"), wypelniono=D("6"), srednia_cena=D("100"),
                        prowizja=D("0.006"), prowizja_w="SOL")
    cur, g, rodzaj, _ = _rozstrzygnij_wejscie(stan)

    assert rodzaj == "czesciowe"
    assert g.anulowane, "reszta zlecenia nie została zdjęta z arkusza"
    # OSTATNI zapis statusu jest tym, co zostaje w bazie.
    statusy = [p[0] for _, p in cur.zapytania("UPDATE exec.order SET status=%s")]
    assert statusy[-1] == "cancelled", (
        f"w bazie zostaje status {statusy[-1]!r} sprzed anulowania — ekspozycja "
        f"liczy tę samą pozycję drugi raz jako żywe zlecenie")


def test_NIEUDANE_anulowanie_zostawia_status_z_gieldy():
    """Kanarek w drugą stronę: gdy zdjęcie reszty NIE wyszło, reszta naprawdę
    żyje i wiersz ma o tym mówić — inaczej sprzątanie po nią nie wróci."""
    from services.exchanges.base import ExchangeError

    stan = StanZlecenia(id_wlasne="tai-p42", id_gieldy="G1", status="partial",
                        ilosc=D("10"), wypelniono=D("6"), srednia_cena=D("100"),
                        prowizja=D("0.006"), prowizja_w="SOL")
    cur, _, _, _ = _rozstrzygnij_wejscie(
        stan, gielda=Gielda(stan=stan, blad_anulowania=ExchangeError("timeout")))
    statusy = [p[0] for _, p in cur.zapytania("UPDATE exec.order SET status=%s")]
    assert statusy[-1] == "partial"


# ============================ 7. odmowa przed wysłaniem a żywe zlecenie u giełdy

def test_odmowa_przed_wyslaniem_NIE_PORZUCA_zlecenia_zywego_u_gieldy():
    """Cykl N wysyła plan (PostOnly leży w arkuszu), proces ginie przed
    `conn.commit()`, więc wiersz `exec.order` nie powstaje. Cykl N+1 widzi plan
    nadal `pending` i woła `zloz_plan` ponownie — a sprawdzenie wstępne biegnie
    PRZED zapytaniem giełdy i tym razem odmawia. Plan szedł na `rejected`,
    wiersz zlecenia na `rejected`, a ŻYWE zlecenie zostawało na giełdzie poza
    zasięgiem obu sprzątaczy (`status IN ('new','partial','unknown')`)."""
    plan = {"id": 77, "decision_id": 7, "instrument_id": 3, "side": "buy",
            "qty": D("0.001"), "limit_price": D("100"), "symbol": "SOLUSDC",
            "quote_ccy": "USDC", "base_ccy": "SOL", "atr_pct": None,
            "trigger": {"rodzaj": "wejscie", "bar": "x"}}
    zyje = StanZlecenia(id_wlasne="tai-p77", id_gieldy="G9", status="new",
                        ilosc=D("0.001"), wypelniono=D("0"), srednia_cena=None,
                        prowizja=D("0"), symbol="SOLUSDC", strona="buy")
    cur = Kursor()
    ok, msg = asyncio.run(zloz_plan(cur, Gielda(stan=zyje), plan, SOLUSDC,
                                    engine="crypto", mode="demo",
                                    kategoria="spot"))

    assert ok is True and "giełda zna to zlecenie" in msg
    # PLAN ZOSTAJE 'pending', bo tylko po nim rozstrzyganie wróci do zlecenia.
    assert not cur.zapytania("UPDATE journal.planned_order"), (
        "plan oznaczony jako rozstrzygnięty zrywa jedyną nitkę prowadzącą "
        "do żywego zlecenia")
    (sql, params), = cur.zapytania("INSERT INTO exec.order")
    assert "'rejected'" not in sql
    assert "new" in params, "wiersz zlecenia nie dostał statusu z giełdy"


def test_odmowa_przy_gieldzie_NIEZNAJACEJ_zlecenia_nadal_odrzuca_plan():
    """Kanarek: gdy giełda tego zlecenia nie zna, odmowa jest normalną ścieżką
    i ma zostawić ślad dokładnie taki jak dotąd."""
    plan = {"id": 78, "decision_id": 7, "instrument_id": 3, "side": "buy",
            "qty": D("0.001"), "limit_price": D("100"), "symbol": "SOLUSDC",
            "quote_ccy": "USDC", "base_ccy": "SOL", "atr_pct": None,
            "trigger": {"rodzaj": "wejscie", "bar": "x"}}
    cur = Kursor()
    ok, msg = asyncio.run(zloz_plan(cur, Gielda(stan=None), plan, SOLUSDC,
                                    engine="crypto", mode="demo",
                                    kategoria="spot"))
    assert ok is False
    assert cur.zapytania("UPDATE journal.planned_order SET status='rejected'")
    (sql, params), = cur.zapytania("INSERT INTO exec.order")
    assert "'rejected'" in sql and msg in params


# ================= 4. sprzątanie porównuje BRUTTO z BRUTTO, a nie netto z brutto

class KursorSQLite:
    """Wykonuje PRAWDZIWY SQL sprzątania na miniaturze bazy.

    Atrapa, która sama policzyłaby `zaksiegowane` w Pythonie, testowałaby
    siebie: to właśnie w treści tego podzapytania siedzi cała poprawka.
    Tłumaczenie ogranicza się do nazw schematów, rzutowań `::text`
    i znacznika parametru — `CASE`, `upper()`, `coalesce` i podzapytania
    skorelowane SQLite rozumie tak samo.
    """

    TABELE = ("exec.order", "exec.fill", "ref.instrument", "journal.planned_order")

    def __init__(self, db):
        self.db = db
        self.sql = []
        self._odp = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.sql.append((s, tuple(params or ())))
        if not s.startswith("SELECT"):
            return
        for t in self.TABELE:
            s = s.replace(t, t.replace(".", "_"))
        s = s.replace("::text", "").replace("%s", "?")
        self._odp = [dict(r) for r in
                     self.db.execute(s, tuple(params or ())).fetchall()]

    def fetchall(self):
        return self._odp

    def fetchone(self):
        return self._odp[0] if self._odp else None


def _baza_sprzatania(*, qty_wypelnienia, prowizja, prowizja_ccy):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE exec_order (id INTEGER, client_order_id TEXT, status TEXT,
            engine TEXT, mode TEXT, instrument_id INTEGER, planned_order_id INTEGER,
            -- `venue_order_id` NADAJE GIELDA przy przyjeciu zlecenia, wiec jego
            -- brak jest jedynym dowodem, ze zlecenie do niej nie dotarlo.
            -- Sprzatanie czyta go po to, zeby zostawic ten slad w `raw`
            -- i w liczniku czujki; atrapa bez tej kolumny sprawdzalaby
            -- schemat, ktorego baza nie ma.
            venue_order_id TEXT);
        CREATE TABLE exec_fill (order_id INTEGER, qty REAL, fee REAL, fee_ccy TEXT);
        CREATE TABLE ref_instrument (id INTEGER, symbol TEXT, lot_size REAL,
            base_ccy TEXT);
        CREATE TABLE journal_planned_order (id INTEGER, status TEXT);
    """)
    db.execute("INSERT INTO exec_order VALUES (1,'tai-p42','partial',"
               "'crypto','demo',3,NULL,'G1')")
    db.execute("INSERT INTO ref_instrument VALUES (3,'ADAUSDC',0.1,'ADA')")
    db.execute("INSERT INTO exec_fill VALUES (1,?,?,?)",
               (float(qty_wypelnienia), float(prowizja), prowizja_ccy))
    db.commit()
    return db


def _posprzataj(db, wypelniono):
    stan = StanZlecenia(id_wlasne="tai-p42", id_gieldy="G1", status="filled",
                        ilosc=D("2000"), wypelniono=wypelniono,
                        srednia_cena=D("0.35"), prowizja=D("2"), prowizja_w="ADA")
    cur = KursorSQLite(db)
    st = asyncio.run(posprzataj_zlecenia(cur, Gielda(stan=stan),
                                         engine="crypto", mode="demo",
                                         kategoria="spot", verbose=False))
    return cur, st


def test_prowizja_w_aktywie_bazowym_NIE_JEST_pozycja_poza_dziennikiem():
    """`exec.fill.qty` dla wejścia jest NETTO, a `stan.wypelniono` to
    `cumExecQty` giełdy, czyli BRUTTO. Różnica wynosi dokładnie prowizję
    w ADA: przy zleceniu na 2000 ADA to ~2 ADA przy progu 0,05 — warunek
    „pozycja poza dziennikiem" był spełniony ZAWSZE. Czujka, która przy każdym
    wejściu melduje nieistniejącą pozycję poza dziennikiem, uczy odbiorcę, że
    można ją pomijać."""
    db = _baza_sprzatania(qty_wypelnienia=D("1998"), prowizja=D("2"),
                          prowizja_ccy="ADA")
    cur, st = _posprzataj(db, wypelniono=D("2000"))

    assert st["wypelnione_po_fakcie"] == 0, (
        "prowizja pobrana w kupionym aktywie została zgłoszona jako pozycja "
        "poza dziennikiem — fałszywy alarm przy KAŻDYM wejściu")
    assert st["domkniete"] == 1
    assert not any("poza dziennikiem" in str(p) for _, p in cur.sql)


def test_PRAWDZIWE_wypelnienie_poza_dziennikiem_nadal_jest_zglaszane():
    """Kanarek: poprawka ma uciszyć prowizję, a nie czujkę. 100 ADA ponad to,
    co zaksięgowaliśmy, to ekspozycja, której risk kernel nie policzył."""
    db = _baza_sprzatania(qty_wypelnienia=D("1998"), prowizja=D("2"),
                          prowizja_ccy="ADA")
    cur, st = _posprzataj(db, wypelniono=D("2100"))

    assert st["wypelnione_po_fakcie"] == 1
    assert any("poza dziennikiem" in str(p) for _, p in cur.sql)


def test_prowizja_w_walucie_kwotowanej_NIE_JEST_doliczana_do_ilosci():
    """Przy sprzedaży prowizja wychodzi w walucie kwotowanej i z ilością nie ma
    nic wspólnego. Doliczenie jej zawyżałoby to, co uznajemy za zaksięgowane —
    czyli uciszałoby czujkę o wartość prowizji w USDC."""
    db = _baza_sprzatania(qty_wypelnienia=D("2000"), prowizja=D("0.7"),
                          prowizja_ccy="USDC")
    _, st = _posprzataj(db, wypelniono=D("2000.7"))
    assert st["wypelnione_po_fakcie"] == 1
