"""Budżet na pozycję i łączna ekspozycja — na rachunku INNYM NIŻ 100 000.

DLACZEGO TEN PLIK ISTNIEJE

Właściciel zapowiedział wpłatę 10 000 USD. Przy tej kwocie system angażował
w rynek 50% rachunku, choć limit łącznej ekspozycji wynosi 15%. Składały się na
to DWIE wady i żadna z nich sama nie wystarczała:

  (A) BUDŻET LICZONY OD STAŁEJ. `loop.run_once` wołało `position_budget` bez
      kapitału, a funkcja spadała wewnątrz na START_EQUITY = 100 000. Budżet
      wychodził 15% * 100 000 / 10 = 1500 — dokładnie tyle, ile na rachunku
      10 000 wynosi CAŁY limit łącznej ekspozycji.

  (B) EKSPOZYCJA NIE KUMULOWAŁA SIĘ W CYKLU. `_open_exposure` czytało wyłącznie
      `exec.position`, a produkcja chodzi z `--limit`: pozycja powstaje dopiero
      przy wypełnieniu w następnej świecy. Każde kolejne z dziesięciu zleceń
      widziało więc tę samą ekspozycję sprzed cyklu.

Efekt złożony: dziesięć zleceń po 1500, każde obcięte przez
`max_position_notional` do 5% * 10 000 = 500, każde przepuszczone przez kontrolę
łącznej ekspozycji — razem 5000 przy limicie 1500.

NA RACHUNKU 100 000 TEGO NIE WIDAĆ i to jest najgroźniejsza cecha tej wady:
kapitał demo (100 063) był przypadkiem równy stałej, więc profil ryzyka trzymał
się na ZBIEŻNOŚCI DWÓCH LICZB, a nie na kontroli. Dlatego wszystkie testy w tym
pliku liczą na 10 000, a osobny sprawdza, że na 100 000 nic się nie zmieniło —
gdyby liczyły tylko na 100 000, przeszłyby także na kodzie sprzed poprawki.

CZEGO TEN PLIK NIE SPRAWDZA, A CO ZNALEZIONO PRZY OKAZJI: `min_equity` jest
w `risk.limit_def` zapisane KWOTOWO jako 95 000. Na rachunku 10 000 ten próg
uruchamia kill-switch w pierwszym cyklu, zanim którakolwiek z opisanych wyżej
wad zdąży się objawić. Testy niżej podają więc próg przeskalowany do rachunku,
żeby mierzyć to, co mierzą — ale sam zapis limitu jest osobną wadą i osobnym
zadaniem.
"""
from __future__ import annotations

import ast
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from services.engine.loop import wycen
from services.engine.risk import RiskKernel
from services.engine.risk_rules import (
    DEFAULT_LIMITS, START_EQUITY, position_budget, wielkosc_pozycji,
    wartosc_limitu,
)

KORZEN = Path(__file__).resolve().parents[1]
LOOP = KORZEN / "services" / "engine" / "loop.py"

KAPITAL = Decimal("10000")
INSTRUMENTOW = 10
CENA = Decimal("100")

# Limity jak w `risk.limit_def`, z jedną zmianą: `min_equity` przeskalowane do
# tego rachunku. Patrz ostatni akapit docstringu modułu.
LIMITY = DEFAULT_LIMITS | {
    "min_equity": {"value": Decimal("9500"), "unit": "quote_ccy",
                   "action": "killswitch"},
}


# ============================================================== (A) BUDŻET

def test_budzet_na_rachunku_10000_to_jedna_dziesiata_limitu():
    """Sedno wady (A), policzone wprost.

    Budżet bazowy ma być udziałem w limicie łącznej ekspozycji, a nie liczbą
    równą całemu limitowi.
    """
    baza = position_budget(LIMITY, INSTRUMENTOW, KAPITAL)
    limit = wartosc_limitu(LIMITY["max_total_exposure"], KAPITAL)

    assert limit == Decimal("1500.00")
    assert baza == limit / INSTRUMENTOW == Decimal("150")
    assert baza * INSTRUMENTOW == limit, (
        "suma budżetów wszystkich instrumentów ma się równać limitowi łącznemu; "
        f"jest {baza * INSTRUMENTOW} przy limicie {limit}")


def test_KANAREK_na_100000_budzet_sie_NIE_zmienil():
    """Kanarek do poprzedniego, i to on pilnuje, że poprawka niczego nie ruszyła
    tam, gdzie mierzono wynik.

    Gdyby ten plik liczył wyłącznie na 100 000, przeszedłby także na kodzie
    sprzed poprawki — bo tam stała i kapitał były tą samą liczbą.
    """
    assert position_budget(LIMITY, INSTRUMENTOW,
                           Decimal(str(START_EQUITY))) == Decimal("1500")


def test_ten_sam_UDZIAL_rachunku_niezaleznie_od_wplaty():
    """Właściwa niezmienna własność: budżet jest ułamkiem rachunku, nie kwotą."""
    maly = position_budget(LIMITY, INSTRUMENTOW, KAPITAL)
    duzy = position_budget(LIMITY, INSTRUMENTOW, Decimal("250000"))
    assert maly / KAPITAL == duzy / Decimal("250000")


@pytest.mark.parametrize("kapital", [None, Decimal("0"), Decimal("-1")])
def test_brak_kapitalu_NIE_spada_na_stala(kapital):
    """Najważniejszy pojedynczy warunek tej poprawki.

    Domyślna wartość parametru była tu groźniejsza niż jego brak: wywołanie bez
    kapitału wyglądało na poprawne i nie zostawiało śladu. None znaczy „nie da
    się policzyć" i wołający ma na to jedną odpowiedź — nie wchodzić.
    """
    assert position_budget(LIMITY, INSTRUMENTOW, kapital) is None


def test_brak_limitu_ekspozycji_tez_nie_daje_liczby():
    """Ta sama nieobecność wyłącza w `evaluate_limits` całą kontrolę ekspozycji
    (`if lim:`), więc stała odesłana stąd byłaby wielkością pozycji, której nic
    już nie ogranicza."""
    bez = {k: v for k, v in LIMITY.items() if k != "max_total_exposure"}
    assert position_budget(bez, INSTRUMENTOW, KAPITAL) is None


def test_position_budget_WYMAGA_kapitalu_jawnie():
    """Kanarek na sygnaturę: gdyby ktoś przywrócił domyślną wartość, wywołanie
    bez kapitału znów przechodziłoby po cichu i cała wada (A) by wróciła."""
    with pytest.raises(TypeError):
        position_budget(LIMITY, INSTRUMENTOW)      # 3. argument wymagany


# ==================================================== atrapa bazy dla kernela

#
# ATRAPA WYKONUJE PRAWDZIWY SQL, i to jest w niej najważniejsze.
#
# Pierwsza wersja rozpoznawała zapytania po fragmencie tekstu i sama, w Pythonie,
# powtarzała ich warunki: filtr statusów, odsianie planów już złożonych, wybór
# samych wejść. Kanarki natychmiast to zdemaskowały — po zepsuciu WHERE
# w `_open_exposure` trzy z nich nie padły, bo atrapa i tak filtrowała po
# swojemu. Testowała więc siebie, a nie kod.
#
# Trzy zapytania, o które w tym pliku chodzi, idą teraz do SQLite-a i wykonują
# się naprawdę. Tłumaczenie ogranicza się do nazw schematów (`exec.order` →
# `exec_order`) i znacznika parametru; `->>`, `COALESCE`, `NOT EXISTS`
# i agregaty SQLite rozumie tak samo. Reszta zapytań kernela dotyka `now()`,
# interwałów i krzywej kapitału — te zostają atrapami, bo nie są przedmiotem
# tych testów.

TABELE = ["exec.order", "exec.position", "exec.equity_point",
          "journal.planned_order", "journal.decision", "ref.instrument"]


def _na_sqlite(sql: str) -> str:
    s = " ".join(sql.split())
    for t in TABELE:
        s = s.replace(t, t.replace(".", "_"))
    # OKNO GODZINNE TLUMACZYMY, A NIE OMIJAMY. `_orders_last_hour` dolicza
    # plany `pending` z BIEZACEGO cyklu i robi to w oknie godzinnym - gdyby
    # atrapa to okno wycinala, test przechodzilby takze dla zapytania bez
    # niego, czyli dla wersji zliczajacej plany sprzed doby.
    s = s.replace("now() - interval '1 hour'", "datetime('now', '-1 hour')")
    return s.replace("%s", "?")


class Baza:
    """Stan bazy w miniaturze — dokładnie te tabele, o które pyta RiskKernel."""

    def __init__(self, *, equity: Decimal, limity: dict):
        self.equity = equity
        self.limity = limity
        self.ceny: dict[int, Decimal] = {}   # market.candle, ostatnie zamknięcie
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE exec_position (id INTEGER, engine TEXT, mode TEXT,
                instrument_id INTEGER, side TEXT, qty REAL, avg_price REAL,
                closed_at TEXT);
            CREATE TABLE ref_instrument (id INTEGER, symbol TEXT);
            CREATE TABLE journal_decision (id INTEGER, engine TEXT, mode TEXT);
            CREATE TABLE journal_planned_order (id INTEGER, decision_id INTEGER,
                instrument_id INTEGER, qty REAL, limit_price REAL,
                status TEXT, "trigger" TEXT, created_at TEXT);
            CREATE TABLE exec_order (id INTEGER, planned_order_id INTEGER,
                engine TEXT, mode TEXT, instrument_id INTEGER, qty REAL,
                limit_price REAL, status TEXT);
        """)

    # --- mutacje, którymi test odtwarza przebieg cyklu -------------------
    def zaplanuj(self, instrument_id: int, qty: Decimal, cena: Decimal,
                 rodzaj: str = "wejscie") -> int:
        c = self.db.cursor()
        did = c.execute("SELECT COALESCE(max(id), 0) + 1 FROM journal_decision"
                        ).fetchone()[0]
        c.execute("INSERT INTO journal_decision VALUES (?,?,?)",
                  (did, "crypto", "demo"))
        pid = c.execute("SELECT COALESCE(max(id), 0) + 1"
                        " FROM journal_planned_order").fetchone()[0]
        # `created_at` JEST W MIGRACJI 0005 i od niej zalezy okno godzinne
        # licznika zlecen — plan bez tej kolumny wypadalby z niego zawsze,
        # czyli atrapa mowilaby „limit nie kumuluje sie w cyklu" niezaleznie
        # od tego, co robi kod.
        c.execute("INSERT INTO journal_planned_order"
                  " VALUES (?,?,?,?,?,?,?,datetime('now'))",
                  (pid, did, instrument_id, float(qty), float(cena), "pending",
                   json.dumps({"rodzaj": rodzaj})))
        self.db.commit()
        return pid

    def zloz(self, plan_id: int, status: str = "new") -> None:
        """Plan poszedł na giełdę: powstaje wiersz w `exec.order`, a plan
        ZOSTAJE 'pending' — dokładnie tak, jak robi to `gielda_exec.zloz_plan`."""
        c = self.db.cursor()
        p = c.execute("SELECT * FROM journal_planned_order WHERE id=?",
                      (plan_id,)).fetchone()
        oid = c.execute("SELECT COALESCE(max(id), 0) + 1 FROM exec_order"
                        ).fetchone()[0]
        c.execute("INSERT INTO exec_order VALUES (?,?,?,?,?,?,?,?)",
                  (oid, plan_id, "crypto", "demo", p["instrument_id"],
                   p["qty"], p["limit_price"], status))
        self.db.commit()

    def otworz_pozycje(self, instrument_id: int, qty: Decimal,
                       avg_price: Decimal) -> None:
        c = self.db.cursor()
        pid = c.execute("SELECT COALESCE(max(id), 0) + 1 FROM exec_position"
                        ).fetchone()[0]
        c.execute("INSERT INTO exec_position VALUES (?,?,?,?,?,?,?,NULL)",
                  (pid, "crypto", "demo", instrument_id, "long",
                   float(qty), float(avg_price)))
        c.execute("INSERT INTO ref_instrument VALUES (?,?)",
                  (instrument_id, f"I{instrument_id}"))
        self.db.commit()

    def cursor(self):
        return Kursor(self)

    def commit(self):
        pass


class Kursor:
    # Zapytania, ktore MUSZA wykonac sie naprawde - to o nie chodzi w tym pliku.
    PRAWDZIWE = ("FROM exec.position p JOIN ref.instrument",
                 "FROM exec.order o LEFT JOIN journal.planned_order",
                 "FROM journal.planned_order p JOIN journal.decision")

    def __init__(self, baza: Baza):
        self.b = baza
        self._odp: list[dict] = []

    def execute(self, sql: str, params=None):
        s = " ".join(sql.split())
        b = self.b

        if any(f in s for f in self.PRAWDZIWE):
            wiersze = b.db.execute(_na_sqlite(s), tuple(params or ())).fetchall()
            self._odp = [
                {k: (Decimal(str(v)) if isinstance(v, float) else v)
                 for k, v in dict(r).items()}
                for r in wiersze]
        elif "SELECT kind FROM ref.instrument" in s:
            # RODZAJ INSTRUMENTU. `RiskKernel.check` pyta o niego od 2026-09-24,
            # bo od niego zalezy, czy limit dzwigni jest KONIECZNY
            # (`risk_rules.evaluate_limits`, punkt 7) - a brak wiersza znaczy
            # "nie wiadomo, czym handlujemy" i daje werdykt `block`.
            #
            # Atrapa oddaje 'spot' dla kazdego id, bo swiat tego pliku jest
            # spotowy: chodzi w nim o budzet i ekspozycje na rachunku 10 000,
            # a nie o dzwignie. Gdyby milczala, kazde `check()` tutaj
            # blokowaloby sie na braku instrumentu i testy budzetu mierzylyby
            # cos zupelnie innego, niz deklaruja.
            self._odp = [{"kind": "spot"}]
        elif "risk.limit_def" in s:
            self._odp = [dict(v, id=i, name=k)
                         for i, (k, v) in enumerate(b.limity.items(), start=1)]
        elif "market.candle" in s:
            cena = b.ceny.get(params[1])          # params: (as_of, iid, as_of)
            self._odp = [] if cena is None else [{"close": cena, "wiek_min": 0.0}]
        elif "count(*) AS n FROM exec.order" in s:
            self._odp = [{"n": 0}]
        elif "max(event_time)" in s:
            self._odp = [{"wiek_min": 0.0}]
        elif "max(equity)" in s:
            self._odp = [{"peak": b.equity}]
        elif "max(nav)" in s:
            self._odp = [{"szczyt": None, "bez_nav": 0}]
        elif "SELECT nav" in s:
            self._odp = []                # krzywa jednostki jeszcze nie liczona
        elif "exec.equity_point" in s:
            self._odp = [{"equity": b.equity}]
        else:
            self._odp = []                # killswitch_event i reszta
        return self

    def fetchone(self):
        return self._odp[0] if self._odp else None

    def fetchall(self):
        return list(self._odp)


# =============================================== (B) EKSPOZYCJA W CYKLU

def test_ekspozycja_widzi_PLAN_ktory_nie_jest_jeszcze_pozycja():
    """Wada (B) w najmniejszej postaci: jeden plan, zero pozycji."""
    b = Baza(equity=KAPITAL, limity=LIMITY)
    k = RiskKernel(b, "crypto", "demo")
    assert k._open_exposure() == (Decimal(0), {})

    b.zaplanuj(1, Decimal("1.5"), CENA)
    laczna, per = k._open_exposure()
    assert laczna == Decimal("150.0"), (
        "plan złożony w tym cyklu to zamiar zaangażowania pieniędzy; "
        "limit, który go nie widzi, mierzy przeszłość, a nie ryzyko")
    assert per == {1: Decimal("150.0")}


def test_ekspozycja_widzi_ZLECENIE_lezace_w_arkuszu():
    b = Baza(equity=KAPITAL, limity=LIMITY)
    k = RiskKernel(b, "crypto", "demo")
    pid = b.zaplanuj(1, Decimal("1.5"), CENA)
    b.zloz(pid, status="new")
    assert k._open_exposure()[0] == Decimal("150.0")


@pytest.mark.parametrize("status", ["new", "partial", "unknown"])
def test_kazdy_stan_zlecenia_otwartego_liczy_sie_do_ekspozycji(status):
    """'unknown' znaczy „odpowiedź giełdy nie dotarła" — najgroźniejszy z tych
    stanów, bo zlecenie mogło wejść. Pominięcie go byłoby uznaniem niewiedzy
    za zero."""
    b = Baza(equity=KAPITAL, limity=LIMITY)
    k = RiskKernel(b, "crypto", "demo")
    pid = b.zaplanuj(1, Decimal("1.5"), CENA)
    b.zloz(pid, status=status)
    assert k._open_exposure()[0] == Decimal("150.0")


def test_plan_i_powstale_z_niego_zlecenie_licza_sie_RAZ():
    """Plan zostaje 'pending' także po złożeniu, więc bez warunku
    `NOT EXISTS (... planned_order_id = p.id)` jedno zlecenie liczyłoby się
    dwa razy i limit zaciskałby się dwukrotnie za mocno."""
    b = Baza(equity=KAPITAL, limity=LIMITY)
    k = RiskKernel(b, "crypto", "demo")
    pid = b.zaplanuj(1, Decimal("1.5"), CENA)
    przed = k._open_exposure()[0]
    b.zloz(pid)
    assert k._open_exposure()[0] == przed == Decimal("150.0")


def test_plan_WYJSCIA_nie_zajmuje_limitu_ekspozycji():
    """Wyjście to zamiar ZMNIEJSZENIA zaangażowania. Doliczenie go blokowałoby
    wejścia tym mocniej, im więcej pozycji system właśnie zamyka."""
    b = Baza(equity=KAPITAL, limity=LIMITY)
    k = RiskKernel(b, "crypto", "demo")
    b.zaplanuj(1, Decimal("1.5"), CENA, rodzaj="wyjscie")
    assert k._open_exposure()[0] == Decimal(0)


# ================================================= (4) KOSZT CZY RYNEK

def test_pozycja_ktora_UROSLA_zajmuje_wiecej_limitu():
    """Rozstrzygnięcie kwestii z audytu: ekspozycję liczymy CENĄ RYNKOWĄ.

    Licząc kosztem (`qty * avg_price`), zwyżka cicho zwalniała miejsce pod
    kolejne zakupy — limit luzował się dokładnie wtedy, gdy zaangażowanie
    rosło. Drugi powód: `update_equity` zapisuje do `exec.equity_point.exposure`
    wielkość rynkową, więc liczba pokazywana właścicielowi i liczba egzekwowana
    przez bezpiecznik były dwiema różnymi wielkościami o tej samej nazwie.
    """
    b = Baza(equity=KAPITAL, limity=LIMITY)
    b.otworz_pozycje(1, Decimal("10"), avg_price=Decimal("100"))   # koszt 1000
    b.ceny[1] = Decimal("150")                                     # rynek 1500
    k = RiskKernel(b, "crypto", "demo")
    laczna, per = k._open_exposure()
    assert laczna == Decimal("1500"), (
        f"pozycja warta dziś 1500 ma zajmować 1500 limitu, nie 1000; jest {laczna}")
    assert per == {1: Decimal("1500")}


def test_pozycja_bez_swiecy_zostaje_wyceniona_ceną_wejscia():
    """Ta sama reguła co w `wycen()`: brak pomiaru nie jest pomiarem zerowym.
    Ekspozycja zostaje, bo kwota naprawdę jest zaangażowana."""
    b = Baza(equity=KAPITAL, limity=LIMITY)
    b.otworz_pozycje(1, Decimal("10"), avg_price=Decimal("100"))
    k = RiskKernel(b, "crypto", "demo")
    assert k._open_exposure()[0] == Decimal("1000")


def test_wycen_rozbija_ekspozycje_na_instrumenty():
    """`per_instrument` jest tą samą liczbą co `exposure`, tylko w rozbiciu —
    inaczej limit na instrument i limit łączny mierzyłyby dwie różne rzeczy."""
    w = wycen([
        {"instrument_id": 1, "side": "long", "qty": Decimal("2"),
         "avg_price": Decimal("100"), "cena": Decimal("110"), "swieza": True,
         "wiek_min": 0.0},
        {"instrument_id": 1, "side": "long", "qty": Decimal("1"),
         "avg_price": Decimal("100"), "cena": Decimal("110"), "swieza": True,
         "wiek_min": 0.0},
        {"instrument_id": 2, "side": "short", "qty": Decimal("4"),
         "avg_price": Decimal("50"), "cena": Decimal("40"), "swieza": True,
         "wiek_min": 0.0},
    ])
    assert w.per_instrument == {1: Decimal("330"), 2: Decimal("160")}
    assert sum(w.per_instrument.values()) == w.exposure


# ======================================== TEST GŁÓWNY: CAŁY CYKL NA 10 000

def test_CALY_CYKL_na_rachunku_10000_nie_przekracza_limitu():
    """Obie wady naraz, przez prawdziwy kernel, na rachunku 10 000.

    Odtwarza to, co robi `run_once`: budżet z kapitału, potem dziesięć decyzji,
    z których każda przechodzi przez `risk.check()` i — gdy dostanie zgodę —
    zostawia plan w `journal.planned_order`, tak jak `zaplanuj()`. Pozycje NIE
    powstają, bo produkcja chodzi z `--limit` i wypełnienie rozstrzyga się
    dopiero w następnej świecy. To właśnie ten stan przepuszczał wszystko.

    PRZED POPRAWKĄ: budżet 1500, dziesięć zleceń obciętych do 500, razem 5000
    przy limicie 1500 — przekroczenie 3,33x.
    """
    b = Baza(equity=KAPITAL, limity=LIMITY)
    k = RiskKernel(b, "crypto", "demo")

    baza = position_budget(k.limits, INSTRUMENTOW, k.kapital())
    limit = wartosc_limitu(LIMITY["max_total_exposure"], KAPITAL)
    assert baza is not None

    zlecone = Decimal(0)
    przepuszczone = 0
    for iid in range(1, INSTRUMENTOW + 1):
        notional, qty = wielkosc_pozycji(baza, 0.9, CENA)
        v = k.check(iid, notional, qty)
        if v.verdict == "block":
            continue
        faktyczne = v.qty * CENA
        b.zaplanuj(iid, v.qty, CENA)          # tak jak `loop.zaplanuj`
        zlecone += faktyczne
        przepuszczone += 1

    assert zlecone <= limit, (
        f"do rynku poszło {zlecone} przy limicie łącznej ekspozycji {limit} "
        f"— to jest {zlecone / KAPITAL:.0%} rachunku zamiast "
        f"{limit / KAPITAL:.0%}")
    assert przepuszczone == INSTRUMENTOW, (
        "limit ma być WYKORZYSTANY, a nie omijany: przy budżecie równym "
        f"udziałowi w limicie mieszczą się wszystkie {INSTRUMENTOW} wejścia, "
        f"weszło {przepuszczone}")
    assert zlecone == limit


def test_jedenaste_wejscie_zostaje_ZABLOKOWANE():
    """Kumulacja ma nie tylko mieścić dziesięć wejść, ale i zatrzymać jedenaste.
    Bez tego test wyżej przechodziłby także wtedy, gdyby limit nie działał wcale,
    a zlecenia były po prostu małe."""
    b = Baza(equity=KAPITAL, limity=LIMITY)
    k = RiskKernel(b, "crypto", "demo")
    baza = position_budget(k.limits, INSTRUMENTOW, k.kapital())

    for iid in range(1, INSTRUMENTOW + 1):
        b.zaplanuj(iid, baza / CENA, CENA)

    v = k.check(99, baza, baza / CENA)
    assert v.verdict == "block", (
        f"limit łącznej ekspozycji jest wyczerpany, werdykt: {v.verdict}")
    assert "kspozycja" in (v.reason or "")


def test_slad_limitu_ekspozycji_jest_W_PIENIADZACH():
    """Próg zapisywany do `risk.limit_usage` i `risk.violation` musi być w tej
    samej jednostce co `used`.

    `lim["value"]` przy `pct_equity` to 15 — liczba procentowa. Zapis „zużyto
    1350 z 15" daje wykorzystanie 9000% i czyni ten limit nieczytelnym
    dokładnie wtedy, gdy zaczął działać. `max_position_notional` liczył to
    poprawnie od początku, więc dwa sąsiednie wiersze tej samej tabeli znaczyły
    dwie różne rzeczy.
    """
    b = Baza(equity=KAPITAL, limity=LIMITY)
    k = RiskKernel(b, "crypto", "demo")
    for iid in range(1, 6):
        b.zaplanuj(iid, Decimal("1.5"), CENA)          # 5 x 150 = 750

    v = k.check(9, Decimal("150"), Decimal("1.5"))
    slad = {u["name"]: u for u in v.usage}
    assert slad["max_total_exposure"]["threshold"] == 1500.0, (
        "próg ma być kwotą (15% z 10 000), a nie liczbą 15")
    assert slad["max_total_exposure"]["used"] == 750.0
    assert slad["max_position_notional"]["threshold"] == 500.0


def test_KANAREK_bez_kumulacji_ten_sam_cykl_przekracza_limit():
    """Kanarek do testu głównego: pokazuje, ŻE liczy on właściwą rzecz.

    Powtarza cykl, ale karmi reguły ekspozycją liczoną po staremu — z samych
    pozycji, których w tym cyklu nie ma. Jeżeli ten test przestanie widzieć
    przekroczenie, znaczy to, że test główny przechodzi z innego powodu niż
    kumulacja (np. bo zlecenia zrobiły się małe) i przestał być dowodem.
    """
    from services.engine.risk_rules import RiskState, evaluate_limits

    baza = Decimal("1500")                 # budżet sprzed poprawki (A)
    zlecone = Decimal(0)
    for iid in range(1, INSTRUMENTOW + 1):
        stan = RiskState(equity=KAPITAL, peak=KAPITAL, day_ago=KAPITAL,
                         exposure=Decimal(0),      # (B): pozycji wciąż brak
                         per_instrument={}, orders_last_hour=0)
        v = evaluate_limits(stan, LIMITY, iid, baza, baza / CENA)
        assert v.verdict != "block"
        zlecone += v.qty * CENA

    limit = wartosc_limitu(LIMITY["max_total_exposure"], KAPITAL)
    assert zlecone == Decimal("5000"), zlecone
    assert zlecone / limit > Decimal("3"), (
        "kanarek przestał odtwarzać wadę — test główny nie dowodzi już niczego")


# ================================== WPIĘCIE W PĘTLĘ (test strukturalny)
#
# `run_once` woła giełdę, kernel i pomiar kapitału naraz; postawienie go na
# atrapie kosztowałoby więcej niż mechanizm, o który tu chodzi. Sprawdzamy więc
# SAMO WPIĘCIE: że budżet dostaje kapitał i że jego brak trafia do tej samej
# listy powodów, która wstrzymuje wejścia przy niepełnym pomiarze ceny.
# Detektory mają własne kanarki niżej — inaczej ich cisza znaczyłaby „parser nic
# nie zrozumiał", a nie „wpięte poprawnie".

def _wywolania_position_budget(zrodlo: str) -> list[ast.Call]:
    return [w for w in ast.walk(ast.parse(zrodlo))
            if isinstance(w, ast.Call) and isinstance(w.func, ast.Name)
            and w.func.id == "position_budget"]


def _powody_wstrzymania_dostaja_budzet(zrodlo: str) -> bool:
    """Czy w `run_once` gałąź `baza is None` dopisuje powód wstrzymania."""
    for wezel in ast.walk(ast.parse(zrodlo)):
        if not isinstance(wezel, ast.If):
            continue
        warunek = ast.unparse(wezel.test)
        if "baza" not in warunek or "None" not in warunek:
            continue
        if "powody_wstrzymania.append" in ast.unparse(wezel):
            return True
    return False


def test_petla_podaje_budzetowi_KAPITAL():
    wywolania = _wywolania_position_budget(LOOP.read_text())
    assert wywolania, "w loop.py nie ma wywołania position_budget"
    for w in wywolania:
        assert len(w.args) + len(w.keywords) >= 3, (
            "position_budget wołane bez kapitału — budżet znów liczyłby się "
            f"od stałej (linia {w.lineno})")
        podane = ast.unparse(w)
        assert "kapital" in podane, (
            f"trzeci argument nie jest kapitałem: {podane}")


def test_brak_budzetu_WSTRZYMUJE_wejscia_ta_sama_sciezka():
    assert _powody_wstrzymania_dostaja_budzet(LOOP.read_text()), (
        "gałąź `baza is None` nie dopisuje nic do `powody_wstrzymania` — "
        "brak pomiaru przeszedłby po cichu")


def test_KANAREK_detektory_LAPIA_kod_sprzed_poprawki():
    """Bez tego cisza dwóch testów wyżej niczego nie dowodzi."""
    przed = (
        "def run_once():\n"
        "    baza = position_budget(risk.limits, len(instrumenty))\n"
        "    powody_wstrzymania = []\n"
        "    if konto.verdict != 'allow':\n"
        "        powody_wstrzymania.append(konto.reason)\n"
    )
    wywolania = _wywolania_position_budget(przed)
    assert wywolania and len(wywolania[0].args) == 2, (
        "detektor nie rozpoznał wywołania sprzed poprawki")
    assert not _powody_wstrzymania_dostaja_budzet(przed), (
        "detektor gałęzi `baza is None` melduje ją w kodzie, w którym jej nie ma")
