"""Dozorca ryzyka: martwy bezpiecznik, który przez 15 godzin wyglądał na żywy.

CO SIĘ WYDARZYŁO NA PRODUKCJI (2026-09-11 14:03 — 2026-09-12 05:11)

`trader-dozorca` — jedyny proces, który ocenia limity całego konta niezależnie
od silnika — nie domknął ANI JEDNEGO cyklu przez 15 godzin. 900 przebiegów,
900 wyjątków, zero pulsów. Pierwsza porażka trzy sekundy po starcie kontenera.

    [14:03:30] cykl dozorcy przerwany: InsufficientPrivilege:
               permission denied for schema journal
    LINE 1: ...ce, 0)) AS notional FROM exec.order o LEFT JOIN journal.pl...

Z zewnątrz: `Running 1/1`, `restartCount 0`, ArgoCD `Healthy`. Ostatni zapisany
pomiar limitów konta miał ponad sześć dni.

DWIE OSOBNE WADY, DWIE OSOBNE GRUPY TESTÓW W TYM PLIKU

1. `check_account()` sięgało po dane spoza granicy roli `trader_dozorca` —
   i sięgało po nie NIEPOTRZEBNIE, bo policzonej tam ekspozycji nie czytała
   żadna z reguł konta.

2. Pętla dozorcy łapała wyjątek i spała 60 s, więc 900 porażek pod rząd
   wyglądało dokładnie tak samo jak praca.

Naprawa jednej bez drugiej zostawia połowę awarii: bez (1) bezpiecznik nadal
nie działa, bez (2) następna taka awaria znów będzie niewidoczna.
"""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal
from pathlib import Path

import pytest

from services.engine import dozorca
from services.engine.risk import RiskKernel

MIGRACJE = Path(__file__).resolve().parents[1] / "db" / "migrations"


# ============================================================================
# 1. GRANICA ROLI — `check_account()` nie ma prawa wyjść poza nadania dozorcy
# ============================================================================

def _bez_komentarzy(sql: str) -> str:
    return "\n".join(l.split("--")[0] for l in sql.splitlines())


def schematy_bazy() -> set[str]:
    """Wszystkie schematy, jakie zakładają migracje."""
    return {m.group(1)
            for p in sorted(MIGRACJE.glob("*.sql"))
            for m in re.finditer(r"CREATE SCHEMA IF NOT EXISTS (\w+)",
                                 _bez_komentarzy(p.read_text(encoding="utf-8")))}


def schematy_dozorcy() -> set[str]:
    """Schematy, na których `trader_dozorca` ma USAGE — czytane Z MIGRACJI.

    Lista wpisana tutaj na sztywno byłaby DRUGĄ KOPIĄ nadań i rozjechałaby się
    po cichu przy pierwszej nowej migracji — a rozjazd tego rodzaju kosztował
    już ten projekt 21 godzin martwych cech (patrz nagłówek 0035).
    """
    ma: set[str] = set()
    for p in sorted(MIGRACJE.glob("*.sql")):
        kod = _bez_komentarzy(p.read_text(encoding="utf-8"))
        for m in re.finditer(r"GRANT\s+USAGE\s+ON\s+SCHEMA\s+([^;]+?)\s+TO\s+([^;]+);",
                             kod, re.IGNORECASE | re.DOTALL):
            role = {r.strip() for r in m.group(2).replace("\n", " ").split(",")}
            if "trader_dozorca" in role:
                ma |= {s.strip() for s in m.group(1).split(",")}
    return ma


class BrakPrawa(Exception):
    """Odpowiednik `psycopg.errors.InsufficientPrivilege` — ten sam komunikat."""


class KursorZaGranica:
    """Atrapa kursora, która ODMAWIA tak jak PostgreSQL: po schemacie w zapytaniu.

    Zdrowe konto bez pozycji i bez zleceń — czyli dokładnie stan produkcji
    z zaciągniętym kill-switchem. Każde sięgnięcie do schematu, na którym
    `trader_dozorca` nie ma USAGE, kończy się wyjątkiem, a nie pustym wynikiem:
    atrapa oddająca ciszę zamiast błędu przepuściłaby tę awarię tak samo jak
    testy sprzed 2026-09-12.
    """

    def __init__(self, wolno: set[str], wszystkie: set[str]):
        self.wolno, self.wszystkie = wolno, wszystkie
        self.dotkniete: set[str] = set()
        self._wynik: list[dict] = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        for schemat in re.findall(r"\b([a-z_]+)\.\w+", s):
            if schemat not in self.wszystkie:
                continue
            self.dotkniete.add(schemat)
            if schemat not in self.wolno:
                raise BrakPrawa(f"permission denied for schema {schemat}")
        if s.startswith("INSERT"):
            self._wynik = []
        elif "max(equity)" in s:
            self._wynik = [{"peak": Decimal("10000")}]
        elif "max(nav)" in s:
            self._wynik = [{"szczyt": Decimal("1.0")}]
        elif "exec.position" in s or "exec.order" in s or "killswitch_event" in s:
            self._wynik = []
        else:
            self._wynik = [{"equity": Decimal("10000"), "nav": Decimal("1.0"),
                            "wplaty_netto": Decimal("10000"), "wiek_min": 0}]
        return self

    def fetchone(self):
        return self._wynik[0] if self._wynik else None

    def fetchall(self):
        return self._wynik


class Polaczenie:
    def __init__(self, kursor):
        self._kursor = kursor

    def cursor(self, *a, **k):
        return self._kursor

    def commit(self):
        pass


LIMITY = {
    "max_daily_loss": {"id": 1, "name": "max_daily_loss", "value": Decimal("2"),
                       "unit": "pct", "action": "killswitch"},
    "max_drawdown": {"id": 2, "name": "max_drawdown", "value": Decimal("10"),
                     "unit": "pct", "action": "killswitch"},
    "min_equity": {"id": 11, "name": "min_equity", "value": Decimal("90"),
                   "unit": "pct_wplat", "action": "killswitch"},
}


def kernel(kur) -> RiskKernel:
    k = RiskKernel.__new__(RiskKernel)
    k.conn, k.engine, k.mode = Polaczenie(kur), "crypto", "demo"
    k._limits = dict(LIMITY)
    return k


def test_KANAREK_check_account_miesci_sie_w_nadaniach_dozorcy():
    """PADA NA KODZIE SPRZED POPRAWKI, z tym samym komunikatem co produkcja.

    To jest cała treść pierwszej wady: nie „zapytanie jest niepoprawne", tylko
    „zapytanie sięga tam, gdzie ta rola świadomie nie ma wstępu".
    """
    kur = KursorZaGranica(schematy_dozorcy(), schematy_bazy())
    v = kernel(kur).check_account()
    assert v.verdict == "allow"
    assert kur.dotkniete, "atrapa nie zobaczyła ani jednego zapytania"


def test_dozorca_NADAL_nie_widzi_uzasadnien_decyzji():
    """Granica z migracji 0034 ma zostać granicą.

    Komentarz przy `GRANT USAGE ON SCHEMA exec, ref TO trader_dozorca` mówi
    wprost: nie ma tu `journal` ani `learn`, bo nadzorca, który czyta
    uzasadnienia decyzji, zaczyna je oceniać. Ten test pada, gdy ktoś naprawi
    awarię bezpiecznika przez rozszczelnienie granicy zamiast przez usunięcie
    zapytania, którego wynik i tak nie był czytany.
    """
    ma = schematy_dozorcy()
    assert "journal" not in ma and "learn" not in ma, (
        f"dozorca dostał wstęp do {sorted(ma & {'journal', 'learn'})} — jeśli "
        "to decyzja, a nie przeoczenie, trzeba zmienić uzasadnienie w 0034")


def test_dozorca_nie_ma_wstepu_do_cen_wiec_NIE_MOZE_ich_wyceniac():
    """Druga, nieujawniona jeszcze noga tej samej wady.

    Krok (1) `_open_exposure` wycenia otwarte pozycje po `market.candle`,
    a `trader_dozorca` nie ma USAGE także na `market`. Na produkcji nie było
    tego widać WYŁĄCZNIE dlatego, że przy zatrzymanym handlu nie ma ani jednej
    otwartej pozycji, więc `cena_odniesienia` nie jest w ogóle wołana. Sam
    `GRANT` na `journal` przesunąłby awarię tutaj — przy pierwszej pozycji.
    """
    assert "market" not in schematy_dozorcy()


def test_reguly_konta_NIE_CZYTAJA_ekspozycji():
    """Dowód, że usunięcie pomiaru z `check_account` niczego nie zaniża.

    Gdyby którakolwiek reguła konta sięgnęła po ekspozycję, pominięcie
    `journal.planned_order` ZANIŻAŁOBY ją o zlecenia zaplanowane i jeszcze
    niewysłane, czyli rozluźniało bezpiecznik. Nie sięga żadna: stan bez
    zmierzonej ekspozycji przechodzi przez `evaluate_account` bez potknięcia.
    """
    from services.engine.risk_rules import RiskState, evaluate_account
    stan = RiskState(equity=Decimal("10000"), peak=Decimal("10000"),
                     day_ago=Decimal("10000"), exposure=None,
                     per_instrument=None, orders_last_hour=0)
    assert evaluate_account(stan, LIMITY).verdict == "allow"


def test_ocena_ZLECENIA_bez_zmierzonej_ekspozycji_KRZYCZY():
    """`None` nie ma się zamienić w ciche zero na ścieżce, która tę liczbę czyta.

    Zero jest poprawną zmierzoną wartością, więc domyśl `Decimal(0)` byłby
    limitem `max_total_exposure`, który przepuszcza wszystko i wygląda przy tym
    jak limit działający.
    """
    from services.engine.risk_rules import RiskState, evaluate_limits
    stan = RiskState(equity=Decimal("10000"), peak=Decimal("10000"),
                     day_ago=Decimal("10000"), exposure=None,
                     per_instrument=None, orders_last_hour=0)
    z_ekspozycja = LIMITY | {
        "max_total_exposure": {"id": 3, "name": "max_total_exposure",
                               "value": Decimal("15"), "unit": "pct_equity",
                               "action": "block"}}
    with pytest.raises(ValueError):
        evaluate_limits(stan, z_ekspozycja, instrument_id=1,
                        notional=Decimal("100"), qty=Decimal("1"))


def test_ocena_ZLECENIA_nadal_liczy_plany_z_journal():
    """Ścieżka silnika ma zostać nietknięta.

    `check()` chodzi na roli `trader_silnik`, która ma pełny dostęp do
    `journal`, i tam pominięcie planów `pending` NAPRAWDĘ zaniżałoby
    ekspozycję — o zlecenia zaplanowane w bieżącym cyklu, czyli dokładnie te,
    dla których `max_total_exposure` przestawał się kumulować przed 2026-09-09.
    """
    zrodlo = (Path(__file__).resolve().parents[1] / "services" / "engine"
              / "risk.py").read_text(encoding="utf-8")
    ciało = zrodlo[zrodlo.index("def _open_exposure"):zrodlo.index("def _orders_last_hour")]
    assert "journal.planned_order" in ciało and "journal.decision" in ciało


# ============================================================================
# 2. CICHA AWARIA — seria porażek ma przestać wyglądać jak praca
# ============================================================================

class PolaczenieBezBazy:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, *a, **k):
        raise AssertionError("ten test nie dotyka bazy")

    def rollback(self):
        pass


@pytest.fixture
def petla_bez_bazy(monkeypatch):
    """Pętla dozorcy odcięta od bazy, poczty i zegara. Zwraca dziennik pulsów."""
    import services.api.db as db

    async def nic(*a, **k):
        return None

    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa/atrapa")
    monkeypatch.setattr(db, "open_pool", nic, raising=False)
    monkeypatch.setattr(db, "close_pool", nic, raising=False)
    monkeypatch.setattr(dozorca.psycopg, "connect",
                        lambda *a, **k: PolaczenieBezBazy())
    monkeypatch.setattr(dozorca, "wznow", nic)
    monkeypatch.setattr(dozorca, "INTERWAL_S", 0)

    pulsy: list[dict] = []

    async def puls(execute, engine, mode, detal):
        pulsy.append(detal)

    monkeypatch.setattr(dozorca, "bij_heartbeat", puls)
    return pulsy


def test_KANAREK_seria_porazek_KONCZY_proces(petla_bez_bazy, monkeypatch):
    """PADA NA KODZIE SPRZED POPRAWKI — tam pętla kręci się bez końca.

    Wstrzykujemy dokładnie produkcyjny przypadek: każdy cykl wywraca się na
    braku prawa do schematu `journal`. Przed poprawką `petla()` nie kończy się
    NIGDY i nie ma kodu wyjścia; po poprawce kończy się kodem 1 po
    `POD_RZAD_MAX` porażkach, czyli pod wstaje, `restartCount` rośnie,
    a przy trwałej przyczynie widać CrashLoopBackOff.
    """
    def wybuch(conn, engine, mode):
        raise BrakPrawa("permission denied for schema journal")

    monkeypatch.setattr(dozorca, "ocen_konto", wybuch)

    async def przebieg():
        # Limit czasu, a nie liczby obrotów: przed poprawką pętla nie ma
        # warunku wyjścia i test bez niego wisiałby tak, jak wisiała produkcja.
        return await asyncio.wait_for(dozorca.petla("crypto", "demo"), 5)

    try:
        kod = asyncio.run(przebieg())
    except asyncio.TimeoutError:
        pytest.fail(
            "pętla dozorcy kręci się mimo serii porażek — 900 przerwanych "
            "cykli pod rząd wygląda z zewnątrz dokładnie tak samo jak praca")

    assert kod == 1, (
        "dozorca przeżył serię porażek i dalej wygląda na żywy — to jest stan, "
        "w którym produkcja stała 15 godzin")
    assert petla_bez_bazy == [], (
        "puls bity mimo nieudanego cyklu: czujka usłyszałaby dokładnie to, "
        "co chce usłyszeć, w chwili gdy jest to najbardziej nieprawdziwe")


def test_porazka_i_sukces_na_przemian_NIE_koncza_procesu(petla_bez_bazy, monkeypatch):
    """Licznik liczy porażki POD RZĄD, nie porażki w ogóle.

    Dozorca nie może ginąć od chwilowego zerwania połączenia z bazą — inaczej
    lekarstwo byłoby tą samą awarią co choroba, tyle że z drugiej strony.
    """
    stan = {"n": 0}

    def co_drugi(conn, engine, mode):
        stan["n"] += 1
        if stan["n"] % 2:
            raise BrakPrawa("chwilowy błąd połączenia")
        return "allow", None, 1.0

    monkeypatch.setattr(dozorca, "ocen_konto", co_drugi)
    # Nie jest to kanarek na kod sprzed poprawki (tam pętla nie ginie nigdy),
    # tylko zabezpieczenie przed licznikiem, który strzela za chętnie.
    monkeypatch.setattr(dozorca, "POD_RZAD_MAX", 3, raising=False)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(asyncio.wait_for(dozorca.petla("crypto", "demo"), 0.5))

    assert len(petla_bez_bazy) > 3, "pętla przestała pracować mimo udanych cykli"


def test_jeden_cykl_zglasza_porazke_KODEM_WYJSCIA(petla_bez_bazy, monkeypatch):
    """`--raz` zwracało 0 także po wyjątku.

    Jedyne narzędzie do ręcznego sprawdzenia dozorcy meldowało więc sukces
    dokładnie wtedy, gdy dozorca nie działał.
    """
    def wybuch(conn, engine, mode):
        raise BrakPrawa("permission denied for schema journal")

    monkeypatch.setattr(dozorca, "ocen_konto", wybuch)
    assert asyncio.run(dozorca.petla("crypto", "demo", raz=True)) == 1

    monkeypatch.setattr(dozorca, "ocen_konto",
                        lambda conn, engine, mode: ("allow", None, 1.0))
    assert asyncio.run(dozorca.petla("crypto", "demo", raz=True)) == 0


# ============================================================================
# 3. POMIAR — liczba, na której stoi decyzja „usunąć zapytanie, nie nadawać praw"
#
# Wejścia WYŁĄCZNIE produkcyjne, odczytane z logów podów 2026-09-12 (read-only):
#
#   kapitał                100 063,09   trader-engine, komunikat kill-switcha
#   instrumentów w cyklu   10           „już rozstrzygnięte w tej świecy 10"
#   max_total_exposure     15 000 quote_ccy   db/migrations/0008_seed.sql
#   max_position_notional   5 000 quote_ccy   db/migrations/0008_seed.sql
#
# Pytanie brzmiało: czy pominięcie `journal.planned_order` ZANIŻA ekspozycję,
# a więc rozluźnia bezpiecznik? Odpowiedź jest RÓŻNA dla dwóch ścieżek i to
# jest cała treść tej sekcji.
# ============================================================================

KAPITAL_PROD = Decimal("100063.09")
N_INSTR = 10
LIMITY_PROD = LIMITY | {
    "max_position_notional": {"id": 3, "name": "max_position_notional",
                              "value": Decimal("5000"), "unit": "quote_ccy",
                              "action": "downsize"},
    "max_total_exposure": {"id": 4, "name": "max_total_exposure",
                           "value": Decimal("15000"), "unit": "quote_ccy",
                           "action": "block"},
    "max_orders_per_hour": {"id": 5, "name": "max_orders_per_hour",
                            "value": Decimal("20"), "unit": "count",
                            "action": "block"},
}


def test_POMIAR_werdykt_dozorcy_NIE_REAGUJE_na_ekspozycje():
    """ŚCIEŻKA DOZORCY: zaniżenie wynosi ZERO, bo liczby nikt nie czyta.

    To jest kanarek „czy miara reaguje na wejście", uruchomiony po to, żeby
    dostać odpowiedź NIE. Przemiatamy ekspozycję przez pełen zakres — od zera
    do dziesięciokrotności limitu — na czterech stanach konta (płaski, -0,5%
    na dobę, -2,5% na dobę, -12% od szczytu). Jeśli choć jeden werdykt, powód,
    ślad w `risk.limit_usage` albo żądanie kill-switcha drgnie, to znaczy, że
    dozorca JEDNAK tej liczby używa — i wtedy jej usunięcie byłoby zaniżeniem,
    a jedyną poprawną drogą wąski GRANT.

    Zmierzone: 0 różnic na 40 przebiegach, 0 pozycji o ekspozycji w śladzie.
    """
    from services.engine.risk_rules import RiskState, evaluate_account

    zakres = [Decimal(x) for x in
              (0, 1, 100, 1_000, 7_500, 14_999, 15_000, 15_001, 30_000, 150_000)]
    stany = [(Decimal("1.000"), Decimal("1.000"), Decimal("1.000")),
             (Decimal("0.995"), Decimal("1.000"), Decimal("1.000")),
             (Decimal("0.975"), Decimal("1.000"), Decimal("1.000")),
             (Decimal("0.880"), Decimal("0.885"), Decimal("1.000"))]

    roznych = przebiegow = sladow_o_ekspozycji = 0
    for nav, nav_doba, nav_szczyt in stany:
        odniesienie = None
        for e in zakres:
            st = RiskState(equity=KAPITAL_PROD, peak=KAPITAL_PROD,
                           day_ago=KAPITAL_PROD, exposure=e,
                           per_instrument={1: e}, orders_last_hour=0,
                           nav=nav, nav_peak=nav_szczyt, nav_day_ago=nav_doba,
                           wplaty_netto=Decimal("100000"))
            v = evaluate_account(st, LIMITY_PROD)
            odcisk = (v.verdict, v.reason, v.trigger_killswitch,
                      tuple(sorted((u["name"], round(u["used"], 6),
                                    round(u["threshold"], 6)) for u in v.usage)))
            sladow_o_ekspozycji += sum(1 for u in v.usage
                                       if "exposure" in u["name"]
                                       or "notional" in u["name"])
            przebiegow += 1
            if odniesienie is None:
                odniesienie = odcisk
            elif odcisk != odniesienie:
                roznych += 1

    assert przebiegow == 40
    assert roznych == 0, (
        f"{roznych} werdyktów konta drgnęło od zmiany ekspozycji — dozorca "
        "JEDNAK jej używa, więc usunięcie pomiaru ZANIŻA bezpiecznik i drogą "
        "poprawną jest wąski GRANT, nie usunięcie zapytania")
    assert sladow_o_ekspozycji == 0, (
        "ocena konta zostawia w risk.limit_usage ślad o ekspozycji — ta liczba "
        "jednak do czegoś służy")


def test_POMIAR_sciezka_SILNIKA_bez_journal_zanizalaby_ekspozycje_o_13500():
    """ŚCIEŻKA SILNIKA: tam zaniżenie jest ogromne — i dlatego jej NIE RUSZAMY.

    `loop.py` woła `risk.check()` dla KAŻDEGO instrumentu (linia ~1847), a plany
    wysyła dopiero po całej pętli (`zloz_oczekujace`, linia ~1950). W trakcie
    cyklu plany leżą WYŁĄCZNIE w `journal.planned_order` — nie ma dla nich ani
    jednego wiersza w `exec.order`. Zapytanie do `journal` jest więc JEDYNYM
    źródłem kumulacji w obrębie cyklu.

    Zmierzone na rachunku, który trzyma już 9 pozycji z poprzednich cykli:
    ekspozycja końcowa 28 500 zamiast 15 000, czyli zaniżenie 13 500 USDC
    = 90% limitu = 13,5% rachunku, limit przekroczony 1,90x.

    Ten test pada, gdyby ktoś zastosował tę samą poprawkę do `check()`.
    """
    from services.engine.risk_rules import (
        RiskState, evaluate_limits, position_budget,
    )
    budzet = position_budget(LIMITY_PROD, N_INSTR, KAPITAL_PROD)
    cap = LIMITY_PROD["max_total_exposure"]["value"]
    assert budzet == Decimal("1500")

    def cykl(widzi_journal: bool, n_pozycji: int) -> Decimal:
        baza = budzet * n_pozycji
        plany: list[Decimal] = []
        for i in range(100, 100 + N_INSTR):
            w_cyklu = sum(plany, Decimal(0)) if widzi_journal else Decimal(0)
            per = {j: budzet for j in range(n_pozycji)}
            if widzi_journal:
                per |= {j: p for j, p in enumerate(plany, start=100)}
            st = RiskState(equity=KAPITAL_PROD, peak=KAPITAL_PROD,
                           day_ago=KAPITAL_PROD, exposure=baza + w_cyklu,
                           per_instrument=per, orders_last_hour=0,
                           nav=Decimal("1.0"), nav_peak=Decimal("1.0"),
                           nav_day_ago=Decimal("1.0"),
                           wplaty_netto=Decimal("100000"))
            v = evaluate_limits(st, LIMITY_PROD, instrument_id=i,
                                notional=budzet, qty=Decimal("1"))
            if v.verdict != "block":
                plany.append(budzet if v.qty is None else budzet * v.qty)
        return baza + sum(plany, Decimal(0))

    # Rachunek pusty to przypadek ZDEGENEROWANY: budżet = limit / N, więc
    # dziesięć wejść po 1500 wyczerpuje limit co do grosza i różnicy nie widać.
    # Mierzenie wyłącznie na nim dałoby fałszywe „journal nic nie zmienia".
    assert cykl(True, 0) == cykl(False, 0) == cap

    z_journal, bez_journal = cykl(True, 9), cykl(False, 9)
    assert z_journal == cap, "limit przestał wiązać na ścieżce zlecenia"
    assert bez_journal == Decimal("28500")
    assert bez_journal - z_journal == Decimal("13500"), (
        "zmierzone zaniżenie przestało się zgadzać — jeśli to skutek zmiany "
        "limitów, popraw liczbę; jeśli zmiany reguł, popraw reguły")
    assert bez_journal / cap == Decimal("1.9")
