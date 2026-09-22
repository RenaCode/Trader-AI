"""Plany należą do silnika, który je zrodził — i do trybu tej decyzji.

DWIE STRONY TEJ SAMEJ ASYMETRII

`journal.planned_order` nie ma kolumn `engine` ani `mode`
(db/migrations/0005_decisions_and_execution.sql:68). Kto chce wiedzieć, czyj
jest plan, musi dojść do `journal.decision`. `RiskKernel._open_exposure` tak
właśnie robi (services/engine/risk.py:393-402), a trzy funkcje egzekucji
pytały wyłącznie o `p.status='pending'`.

Asymetria była JEDNOSTRONNA i dlatego groźna: limit ekspozycji widział
wyłącznie własne plany, a wykonanie brało wszystkie. Dopóki pętla jest jedna
(values.yaml:298), nie ma to skutku — ale w repozytorium leży już adapter
Alpaki i `news_worker --silniki crypto,equity`, więc druga pętla zapaliłaby to
przy pierwszym wspólnym cyklu: zlecenia silnika `equity` poszłyby na rachunek
`crypto`, a ich pozycje zaksięgowałyby się jako `crypto`/`demo`, bo oba te
słowa `execute_paper` bierze z argumentów pętli, nie z planu.

Druga część pliku pilnuje ścieżki papierowej po zadziałaniu kill-switcha.
Ścieżka giełdowa zdejmuje wtedy zlecenia wejścia z arkusza (`anuluj_wejscia`),
papierowa nie robiła NIC — więc rachunek papierowy otwierał pozycje po tym, jak
system uznał, że handlować nie wolno. Rozjazd szedł w stronę „symulacja wygląda
lepiej", a to właśnie symulacja jest wzorcem kalibracji dla trenera.

CZEGO TE TESTY NIE UDAJĄ. Atrapa nie wykonuje `WHERE`, więc nie da się nią
pokazać, że filtr COŚ odsiewa. Pokazujemy dwie rzeczy, które dają się pokazać
bez bazy i które razem wyczerpują tę poprawkę: że instrukcja niesie ten sam
predykat, co `_open_exposure` (więc nie powstała druga reguła obok istniejącej),
i że wiąże pod niego silnik oraz tryb, a nie tylko wypisuje go w tekście.
"""
import asyncio
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from services.engine.gielda_exec import rozstrzygnij_oczekujace, zloz_oczekujace
from services.engine.horizons import get as get_horizon
from services.engine.loop import rozstrzygnij_plany
from tests.atrapa_bazy import Kursor

H = get_horizon("5m")

# Świeca rozstrzygająca musi być DOMKNIETA — `zakres_swiecy` odmawia oceny
# świecy trwającej, więc wszystkie znaczniki leżą w przeszłości wobec zegara.
BAR = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
SPRAWDZANA = BAR + timedelta(minutes=H.bar_minutes)
TERAZ = BAR + timedelta(hours=1)

SWIECA = {"high": D("110"), "low": D("90"), "close": D("100"), "n": 5}
POZYCJA = {"id": 77, "side": "long", "qty": D("10"), "avg_price": D("95")}
SKAD_POZYCJA = {"engine": "crypto", "mode": "demo", "instrument_id": 3,
                "side": "long", "quote_ccy": "USDC"}
ID_ZLECENIA = {"id": 91}


def plan(**nadpisz):
    """Plan `pending`, którego limit ŚWIECA TRAFIA.

    Klucze są DOKŁADNIE te, które pobiera zapytanie w `rozstrzygnij_plany` —
    patrz `_sprawdz_zgodnosc_kolumn`. Wiersz bogatszy niż SELECT dałby test
    przechodzący z fałszywego powodu: na produkcji kod sięgałby po klucz,
    którego w wierszu nie ma.
    """
    p = {"id": 42, "decision_id": 7, "instrument_id": 3, "side": "buy",
         "qty": D("10"), "limit_price": D("100"), "symbol": "SOLUSDC",
         "quote_ccy": "USDC",
         "valid_until": SPRAWDZANA + timedelta(minutes=H.bar_minutes),
         "trigger": {"rodzaj": "wejscie", "bar": BAR.isoformat()}}
    p.update(nadpisz)
    return p


def plan_wyjscia(**nadpisz):
    return plan(id=43, side="sell",
                trigger={"rodzaj": "wyjscie", "bar": BAR.isoformat(),
                         "position_id": 77},
                **nadpisz)


class KursorDyspozytor(Kursor):
    """Atrapa dobierająca odpowiedzi PO ZAPYTANIU, nie po kolejności.

    Bazowa atrapa oddaje z `fetchall` całą podłożoną resztę, a rozstrzyganie
    wykonuje po drodze kilka odczytów (świeca, pozycja, identyfikator zlecenia).
    Kolejka po numerze odczytu wiązałaby test z kolejnością instrukcji w kodzie;
    dopasowanie po fragmencie SQL wiąże go z tym, o co kod PYTA.

    Sprawdza przy okazji, że każda kolumna podłożonego wiersza występuje
    w zapytaniu, które ten wiersz dostaje. To nie jest pełna kontrola (nazwa
    krótka jak `id` trafia w `decision_id`), ale wyłapuje przypadek, który
    w tym projekcie już raz dał zielony test na błędnym kodzie: atrapa oddająca
    kolumnę, której SELECT w ogóle nie pobiera.
    """

    def __init__(self, odpowiedzi):
        super().__init__()
        self._odpowiedzi = list(odpowiedzi)
        self._biezace = []

    def execute(self, sql, params=None):
        super().execute(sql, params)
        plaski = " ".join(sql.split())
        self._biezace = []
        for fragment, wiersze in self._odpowiedzi:
            if fragment not in plaski:
                continue
            for w in wiersze:
                for kolumna in w:
                    assert kolumna in plaski, (
                        f"atrapa oddaje kolumnę {kolumna!r}, której zapytanie "
                        f"nie pobiera: {plaski[:120]}…")
            self._biezace = list(wiersze)
            break

    def fetchall(self):
        return list(self._biezace)

    def fetchone(self):
        return self._biezace[0] if self._biezace else None


class Gielda:
    """Giełda, której w tej części nie pytamy o nic poza istnieniem."""

    async def stan_zlecenia(self, id_wlasne, kategoria="spot"):
        return None

    async def cena_odniesienia(self, symbol, kategoria=None):
        return None

    async def zloz_zlecenie(self, **kw):
        raise AssertionError("ten test nie powinien nic składać")

    async def anuluj_zlecenie(self, **kw):
        return True


# ======================================================= A: silnik i tryb

ZRODLO_RISK = Path("services/engine/risk.py").read_text(encoding="utf-8")
ZRODLO_LOOP = Path("services/engine/loop.py").read_text(encoding="utf-8")


def _znormalizuj(sql: str) -> str:
    """Tekst SQL bez wpływu białych znaków i spacji wokół `=`."""
    return " ".join(sql.split()).replace(" = ", "=").lower()


def predykat_wzorcowy() -> str:
    """Fragment zapytania `_open_exposure`, który ustala CZYJ jest plan.

    WZORZEC CZYTAMY Z `risk.py`, a nie przepisujemy tutaj. Kopia w pliku
    testowym rozjechałaby się przy pierwszej zmianie reguły i rozjazd byłby
    niewidoczny — czyli powielilibyśmy o piętro wyżej dokładnie ten błąd,
    który ten plik ma tępić.
    """
    i = ZRODLO_RISK.index('"  FROM journal.planned_order p"')
    j = ZRODLO_RISK.index('" GROUP BY p.instrument_id"', i)
    sklejone = _znormalizuj(" ".join(re.findall(r'"([^"]*)"', ZRODLO_RISK[i:j])))
    poczatek = sklejone.index("join journal.decision")
    koniec = sklejone.index("p.status='pending'") + len("p.status='pending'")
    return sklejone[poczatek:koniec]


def test_wzorzec_z_risk_py_JEST_TYM_CZYM_MYSLIMY():
    """Gdyby `_open_exposure` przestało filtrować po silniku, ten plik cicho
    przestałby czegokolwiek pilnować — wzorzec byłby pusty i pasowałby wszędzie."""
    wzorzec = predykat_wzorcowy()

    assert "join journal.decision d on d.id=p.decision_id" in wzorzec
    assert "d.engine=%s" in wzorzec and "d.mode=%s" in wzorzec


def _zapytanie_o_plany(cur):
    (sql, params), = [(s, p) for s, p in cur.sql
                      if "FROM journal.planned_order p" in " ".join(s.split())]
    return _znormalizuj(sql), params


def test_rachunek_papierowy_bierze_TYLKO_swoje_plany():
    """`rozstrzygnij_plany` księguje pozycje z `engine`/`mode` PĘTLI, nie planu.

    Cudzy plan zaksięgowałby się więc jako nasza pozycja, a `_open_exposure`
    i tak by go nie zobaczył — limit ekspozycji liczyłby się z niepełnego stanu.
    """
    cur = KursorDyspozytor([])
    rozstrzygnij_plany(cur, "crypto", "demo", H, TERAZ, "spot", verbose=False)
    sql, params = _zapytanie_o_plany(cur)

    assert predykat_wzorcowy() in sql, (
        "rozstrzyganie papierowe bierze KAŻDY plan 'pending' w bazie")
    assert params == ("crypto", "demo")


def test_skladanie_na_gielde_bierze_TYLKO_swoje_plany():
    """Najdroższy z tych trzech: cudzy plan poszedłby NA NASZ RACHUNEK."""
    cur = KursorDyspozytor([])
    asyncio.run(zloz_oczekujace(cur, Gielda(), engine="crypto", mode="demo",
                                kategoria="spot", h=H, verbose=False))
    sql, params = _zapytanie_o_plany(cur)

    assert predykat_wzorcowy() in sql
    # Nazwa cechy idzie PRZED silnikiem, bo podzapytanie ATR stoi w SELECT.
    assert params == (H.name("atr_pct"), "crypto", "demo", False)


def test_rozstrzyganie_gieldowe_bierze_TYLKO_swoje_plany():
    """Domknięcie pary: gdyby filtrowało tylko składanie, cudzy plan złożony
    wcześniej wciąż księgowałby się tutaj jako nasza pozycja."""
    cur = KursorDyspozytor([])
    asyncio.run(rozstrzygnij_oczekujace(cur, Gielda(), engine="crypto",
                                        mode="demo", h=H, now=TERAZ,
                                        kategoria="spot", kind="spot",
                                        verbose=False))
    sql, params = _zapytanie_o_plany(cur)

    assert predykat_wzorcowy() in sql
    assert params == ("crypto", "demo")


def test_denormalizacja_engine_mode_JEST_OPISANA_jako_osobna_decyzja():
    """JOIN przy każdym zapytaniu jest kosztem i ktoś będzie chciał go znieść
    kolumnami w `planned_order`. To migracja z backfillem i zgodnością
    z dzisiejszymi wierszami, a nie poprawka przy okazji — więc powód, dla
    którego jej tu NIE ma, musi stać w kodzie, nie w cudzej pamięci."""
    gielda = Path("services/engine/gielda_exec.py").read_text(encoding="utf-8")

    for zrodlo, nazwa in ((ZRODLO_LOOP, "loop.py"), (gielda, "gielda_exec.py")):
        assert "enormalizacja" in zrodlo, (
            f"{nazwa}: brak notatki, dlaczego engine/mode nie są kolumnami planu")


# ============================================ B: kill-switch na papierze

def test_kill_switch_ZDEJMUJE_plan_wejscia_na_rachunku_papierowym():
    """Ścieżka giełdowa zdejmuje wtedy zlecenia wejścia z arkusza. Papierowa
    rozstrzygała je dalej i otwierała pozycje — do `len(instrumenty)` nowych —
    już PO tym, jak system uznał, że handlować nie wolno."""
    cur = KursorDyspozytor([("FROM journal.planned_order p", [plan()]),
                            ("FROM market.candle", [SWIECA]),
                            ("RETURNING id", [ID_ZLECENIA])])
    st = rozstrzygnij_plany(cur, "crypto", "demo", H, TERAZ, "spot", verbose=False,
                            tylko_wyjscia=True)

    assert st["anulowane_wejscia"] == 1
    assert st["wypelnione"] == 0
    assert not any("INSERT INTO exec.position" in s for s, _ in cur.sql), (
        "pozycja otwarta mimo działającego kill-switcha")
    assert not any("INSERT INTO exec.fill" in s for s, _ in cur.sql)


def test_zdjety_plan_wejscia_jest_CANCELLED_a_nie_EXPIRED():
    """`expired` znaczy w tej funkcji dokładnie jedno: świeca nie dotknęła
    limitu. Użyte tutaj wpisywałoby do dziennika, że limit nie został trafiony —
    czego nie sprawdziliśmy — i kłamałoby o przyczynie. `cancelled` to to samo
    słowo, którym ścieżka giełdowa opisuje to samo zdarzenie."""
    cur = KursorDyspozytor([("FROM journal.planned_order p", [plan()])])
    rozstrzygnij_plany(cur, "crypto", "demo", H, TERAZ, "spot", verbose=False,
                       tylko_wyjscia=True)

    assert cur.zawiera("UPDATE journal.planned_order SET status='cancelled'")
    assert not cur.zawiera("SET status='expired'")
    assert not cur.zawiera("SET status='triggered'")


def test_kill_switch_NIE_dotyka_planow_WYJSCIA():
    """Kill-switch ma wyprowadzać z ryzyka, a nie więzić w nim. Zdjęcie planu
    sprzedaży zostawiłoby otwartą pozycję bez czegokolwiek, co ją zamknie."""
    cur = KursorDyspozytor([("FROM journal.planned_order p", [plan_wyjscia()]),
                            ("FROM market.candle", [SWIECA]),
                            ("WHERE id=%s AND closed_at IS NULL", [POZYCJA]),
                            ("FROM exec.position p JOIN ref.instrument", [SKAD_POZYCJA]),
                            ("RETURNING id", [ID_ZLECENIA])])
    st = rozstrzygnij_plany(cur, "crypto", "demo", H, TERAZ, "spot", verbose=False,
                            tylko_wyjscia=True)

    assert st["anulowane_wejscia"] == 0
    assert st["wypelnione"] == 1
    assert cur.zawiera("UPDATE journal.planned_order SET status='triggered'")


def test_BEZ_kill_switcha_plan_wejscia_rozstrzyga_sie_normalnie():
    """Kontrola, bez której trzy testy wyżej przechodziłyby także wtedy, gdyby
    `rozstrzygnij_plany` przestało księgować cokolwiek."""
    cur = KursorDyspozytor([("FROM journal.planned_order p", [plan()]),
                            ("FROM market.candle", [SWIECA]),
                            ("RETURNING id", [ID_ZLECENIA])])
    st = rozstrzygnij_plany(cur, "crypto", "demo", H, TERAZ, "spot", verbose=False)

    assert st["anulowane_wejscia"] == 0
    assert st["wypelnione"] == 1
    assert any("INSERT INTO exec.position" in s for s, _ in cur.sql)


def test_licznik_zdjetych_wejsc_ISTNIEJE_zanim_kill_switch_zadziala():
    """Klucz pojawiający się dopiero przy zdarzeniu jest w heartbeacie nie do
    odróżnienia od klucza, którego nikt nie liczy."""
    st = rozstrzygnij_plany(KursorDyspozytor([]), "crypto", "demo", H, TERAZ,
                            "spot", verbose=False)

    assert st["anulowane_wejscia"] == 0


def test_PETLA_podaje_stan_kill_switcha_sciezce_papierowej():
    """Reguła żyje w `run_once`, którego testy jednostkowe nie obejmują — wymaga
    bazy. Sprawdzamy więc samo wywołanie, tak jak przy ścieżce giełdowej.

    KOMENTARZE SĄ ODFILTROWANE przed przeszukaniem: wcześniejszy test w tym
    projekcie przechodził, bo szukanego wzorca dostarczał komentarz nad kodem —
    a w tym pliku komentarz nad tym właśnie wywołaniem mówi dokładnie to, czego
    szukamy.
    """
    kod = "\n".join(w for w in ZRODLO_LOOP.splitlines()
                    if not w.lstrip().startswith("#"))
    i = kod.index("stats.update(rozstrzygnij_plany(")
    wywolanie = kod[i:kod.index("conn.commit()", i)]

    assert "tylko_wyjscia=wejscia_wstrzymane" in wywolanie, (
        "ścieżka papierowa rozstrzyga plany wejścia mimo kill-switcha")

    # WARUNEK GAŁĘZI MA ZOSTAĆ BEZ ZMIAN. Gdyby kill-switch trafił tutaj zamiast
    # do argumentu, plany WYJŚCIA przestałyby się rozstrzygać w ogóle — czyli
    # bezpiecznik uwięziłby pozycje zamiast je wypuścić.
    galaz = kod.rindex("elif limit:", 0, i)
    assert "wejscia_wstrzymane" not in kod[galaz:i].splitlines()[0]


# ================================== C: pasmo ceny a horyzont

def test_nazwa_cechy_ATR_IDZIE_Z_HORYZONTU_a_nie_na_sztywno():
    """Wpisane wprost `atr_pct__4h` działało wyłącznie dla domyślnego bara.

    Przy `--bar 1h` podzapytanie oddawało NULL, `atr_pct` schodziło do None,
    a `zlecenia.prog_pasma` zwracało wtedy pasmo AWARYJNE (SUFIT_PASMA = 0,25)
    zamiast policzyć właściwe ~0,105. Pasmo przestawało wykluczać cokolwiek
    poniżej rzędu wielkości — czyli traciło swoją funkcję — i robiło to CICHO,
    bo brak cechy wygląda dokładnie tak samo jak zmienność nieznana.

    Ten plik pracuje na horyzoncie 5m właśnie po to, żeby sztywne `4h` nie
    mogło tu przypadkiem trafić.
    """
    cur = KursorDyspozytor([])
    asyncio.run(zloz_oczekujace(cur, Gielda(), engine="crypto", mode="demo",
                                kategoria="spot", h=H, verbose=False))
    _, params = _zapytanie_o_plany(cur)

    assert params[0] == H.name("atr_pct") == "atr_pct__5m", (
        "nazwa cechy ATR nie pochodzi z horyzontu — przy innym barze pasmo ceny "
        "po cichu rozszerza się do sufitu")


def test_horyzont_JEST_WYMAGANY_przy_skladaniu():
    """Wartość domyślna przywróciłaby dokładnie tę cichą awarię: wywołanie bez
    horyzontu przechodziłoby, a pasmo liczyłoby się dla cudzego bara."""
    import inspect

    from services.engine import gielda_exec

    p = inspect.signature(gielda_exec.zloz_oczekujace).parameters["h"]
    assert p.default is inspect.Parameter.empty, (
        "`h` ma wartość domyślną — brak horyzontu przestaje być błędem "
        "wywołania i znów staje się cichym poszerzeniem pasma")


def test_verbose_i_tylko_wyjscia_SA_TYLKO_NAZWANE():
    """Dwa bool-e obok siebie na końcu listy to pomyłka, której nic nie zgłasza.

    `..., "spot", False, True)` nie da się przeczytać bez zaglądania do
    definicji, a zamiana ich miejscami daje kill-switch, który przestaje
    zdejmować plany wejścia i JEDNOCZEŚNIE milknie — cichy bezpiecznik zamiast
    gadatliwego logu. `*` przed nimi zamienia tę pomyłkę w błąd wywołania.
    """
    import inspect

    from services.engine.loop import rozstrzygnij_plany

    p = inspect.signature(rozstrzygnij_plany).parameters
    for nazwa in ("verbose", "tylko_wyjscia"):
        assert p[nazwa].kind is inspect.Parameter.KEYWORD_ONLY, (
            f"`{nazwa}` da się podać pozycyjnie — zamiana go z sąsiednim "
            f"bool-em przechodzi bez słowa")


def test_nazwa_parametru_JEST_TA_SAMA_co_na_sciezce_gieldowej():
    """Dwie nazwy na ten sam stan rozjeżdżają się przy pierwszej zmianie, a przy
    czytaniu obu ścieżek obok siebie wyglądają jak dwa różne mechanizmy."""
    import inspect

    from services.engine import gielda_exec, loop

    assert "tylko_wyjscia" in inspect.signature(loop.rozstrzygnij_plany).parameters
    assert "tylko_wyjscia" in inspect.signature(
        gielda_exec.zloz_oczekujace).parameters
