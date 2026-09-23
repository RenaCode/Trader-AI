"""`max_orders_per_hour` ma wiązać TAKŻE w obrębie jednego cyklu.

Produkcja chodzi z `--limit`, więc wiersze `exec.order` powstają dopiero
w `zloz_oczekujace`, czyli na KOŃCU przebiegu. Wszystkie wejścia jednego cyklu
czytały przez to tę samą, sprzed cyklu, liczbę zleceń:

    19 zleceń w poprzedniej godzinie, limit 20, dziesięć instrumentów w cyklu
    → każde widzi `19 < 20` i przechodzi → po cyklu jest ich 29 przy limicie 20

Jest to dokładnie ten kształt wady, który 2026-09-09 naprawiono dla
`max_total_exposure` (`_open_exposure` dolicza zlecenia i plany) — poprawka nie
objęła wtedy licznika zleceń. Rozjazd był przy tym jednostronny i dotyczył
kalibracji: `portfolio.simulate_portfolio` inkrementuje `orders_h` w obrębie
świecy, więc backtest mierzył limit, którego produkcja nie miała.

ATRAPA WYKONUJE PRAWDZIWY SQL. Powtórzenie warunków zapytania w Pythonie
testowałoby atrapę — a cała poprawka siedzi właśnie w treści zapytania.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

from services.engine.risk import RiskKernel

TABELE = ("exec.order", "journal.planned_order", "journal.decision")


def _na_sqlite(sql: str) -> str:
    s = " ".join(sql.split())
    for t in TABELE:
        s = s.replace(t, t.replace(".", "_"))
    # Okno godzinne TŁUMACZYMY, a nie wycinamy — bez niego test przechodziłby
    # także dla wersji zliczającej plany sprzed doby.
    s = s.replace("now() - interval '1 hour'", "datetime('now', '-1 hour')")
    return s.replace("%s", "?")


class Kursor:
    def __init__(self, db):
        self.db = db
        self._odp = None

    def execute(self, sql, params=None):
        wiersze = self.db.execute(_na_sqlite(sql), tuple(params or ())).fetchall()
        self._odp = dict(wiersze[0]) if wiersze else None

    def fetchone(self):
        return self._odp


class Polaczenie:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return Kursor(self.db)


def _kernel(db) -> RiskKernel:
    k = RiskKernel.__new__(RiskKernel)
    k.conn, k.engine, k.mode = Polaczenie(db), "crypto", "demo"
    k._limits = {}
    return k


def _baza(*, zlozonych=0, planow=0, planow_starych=0, planow_ze_zleceniem=0):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE exec_order (id INTEGER, planned_order_id INTEGER,
            engine TEXT, mode TEXT, submitted_at TEXT);
        CREATE TABLE journal_decision (id INTEGER, engine TEXT, mode TEXT);
        CREATE TABLE journal_planned_order (id INTEGER, decision_id INTEGER,
            status TEXT, created_at TEXT);
        INSERT INTO journal_decision VALUES (1,'crypto','demo');
    """)
    nid = 0
    for _ in range(zlozonych):
        nid += 1
        db.execute("INSERT INTO exec_order VALUES (?,NULL,'crypto','demo',"
                   "datetime('now'))", (nid,))
    pid = 0
    for _ in range(planow):
        pid += 1
        db.execute("INSERT INTO journal_planned_order VALUES "
                   "(?,1,'pending',datetime('now'))", (pid,))
    for _ in range(planow_starych):
        pid += 1
        stary = (datetime.now(timezone.utc) - timedelta(hours=3))
        db.execute("INSERT INTO journal_planned_order VALUES (?,1,'pending',?)",
                   (pid, stary.strftime("%Y-%m-%d %H:%M:%S")))
    for _ in range(planow_ze_zleceniem):
        pid += 1
        nid += 1
        db.execute("INSERT INTO journal_planned_order VALUES "
                   "(?,1,'pending',datetime('now'))", (pid,))
        db.execute("INSERT INTO exec_order VALUES (?,?,'crypto','demo',"
                   "datetime('now'))", (nid, pid))
    db.commit()
    return db


def test_plany_biezacego_cyklu_SA_LICZONE():
    """Dziesięć instrumentów jednego cyklu zostawia dziesięć planów i ani
    jednego wiersza `exec.order` — dopóki pętla nie dojdzie do
    `zloz_oczekujace`. To właśnie ten stan przepuszczał wszystko."""
    k = _kernel(_baza(zlozonych=19, planow=10))
    assert k._orders_last_hour() == 29, (
        "licznik zleceń widzi wyłącznie stan sprzed cyklu, więc dziesiąte "
        "wejście przechodzi tak samo jak pierwsze")


def test_bez_planow_licznik_sie_NIE_ZMIENIA():
    """Kanarek: doliczenie planów nie ma podbijać liczby tam, gdzie planów
    nie ma — inaczej limit blokowałby handel bez powodu."""
    assert _kernel(_baza(zlozonych=19))._orders_last_hour() == 19


def test_plan_ktory_ma_juz_wiersz_zlecenia_NIE_JEST_liczony_dwa_razy():
    """Po `zloz_oczekujace` plan zostaje `pending`, a wiersz zlecenia już
    istnieje. Liczenie obu dawałoby podwójny licznik zaraz po złożeniu."""
    k = _kernel(_baza(zlozonych=0, planow_ze_zleceniem=5))
    assert k._orders_last_hour() == 5


def test_plan_sprzed_trzech_godzin_wypada_z_OKNA():
    """Okno jest godzinne i dotyczy też planów. Bez niego restart po dłuższym
    postoju zastawałby stertę planów `pending` i blokował handel limitem
    częstotliwości, choć w ostatniej godzinie nie poszło ani jedno zlecenie."""
    k = _kernel(_baza(zlozonych=0, planow=2, planow_starych=25))
    assert k._orders_last_hour() == 2
