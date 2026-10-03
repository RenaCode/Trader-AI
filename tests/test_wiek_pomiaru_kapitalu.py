"""Ile lat ma liczba, na której stoi cały bezpiecznik.

DLACZEGO OSOBNY PLIK

`exec.equity_point` pisze WYŁĄCZNIE `loop.update_equity`, na końcu cyklu
decyzyjnego. Czytają ją trzej: risk kernel, dozorca ryzyka i usługa wznowienia
— i do 2026-09-09 żaden z nich ani razu nie spojrzał na `event_time` bieżącego
punktu. Zapytanie „ostatni punkt krzywej" odpowiada tak samo chętnie dla punktu
sprzed pięciu minut, jak dla punktu sprzed trzech dni.

Skutek jest cichy w najgorszy możliwy sposób. Zamrożona krzywa NIE MA
obsunięcia, NIE MA straty dziennej i NIE SCHODZI do progu kapitału — więc
wszystkie trzy limity krytyczne meldują `allow`, a zawieszona pętla wygląda
dokładnie tak jak spokojny rynek. Na produkcji zdarzyło się to dwa razy w ciągu
trzynastu dni (przerwy 88 i 83 minuty, zmierzone `lag()` po `event_time`)
i nikt tego nie odnotował.

Te testy pilnują jednego zdania: STARY POMIAR NIE JEST POMIAREM.
"""
from pathlib import Path

import pytest

from services.engine.risk import (
    POMIAR_STARY_PO_MIN, RiskKernel, ocen_wiek_pomiaru,
)
from services.engine.risk_rules import DEFAULT_LIMITS

# Limity takie jak na produkcji, tylko z `id` — `record_usage` pisze po nim
# do risk.limit_usage, a DEFAULT_LIMITS opisuje same progi, nie wiersze bazy.
LIMITY = [{"id": i, "name": n, "value": d["value"], "unit": d.get("unit"),
           "action": d["action"], "instrument_id": None}
          for i, (n, d) in enumerate(DEFAULT_LIMITS.items(), start=1)]


class Kursor:
    """Atrapa bazy: zdrowe konto 100 000, jedna otwarta pozycja 15 000.

    Odpowiedzi rozstrzygane po treści zapytania, a nie po kolejności wywołań —
    inaczej każda zmiana w `_equity()` czy `_wynik()` przestawiałaby ten test
    o jedno pytanie i psuła go bez związku z tym, co on sprawdza.
    """

    def __init__(self, wiek_min, killswitch=None, equity="100000"):
        self.wiek_min = wiek_min
        self.killswitch = killswitch
        self.equity = equity
        self.zapytania: list[str] = []
        self._one = None
        self._all: list = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.zapytania.append(s)
        self._one, self._all = None, []
        if "max(event_time)" in s:
            # Pusta tabela zwraca w Postgresie wiersz z NULL-em, a nie brak
            # wiersza — atrapa musi kłamać tak samo jak baza.
            self._one = {"wiek_min": self.wiek_min}
        elif "risk.limit_def" in s:
            self._all = [dict(r) for r in LIMITY]
        elif "killswitch_event" in s:
            self._one = self.killswitch
        elif "market.candle" in s:
            # Cena świeża: badamy wiek POMIARU KAPITAŁU, a nie wiek ceny —
            # gdyby i cena była stara, wstrzymanie miałoby drugi powód i test
            # przechodziłby, nie sprawdzając tego, po co powstał.
            self._one = {"close": "15000", "wiek_min": 1.0}
        elif "FROM exec.position" in s:
            self._all = [{"id": 1, "instrument_id": 1, "side": "long",
                          "qty": "1", "avg_price": "15000", "symbol": "BTCUSDT"}]
        elif "exec.order" in s or "planned_order" in s:
            self._all = []
        elif "max(equity)" in s:
            self._one = {"peak": self.equity}
        elif "max(nav)" in s:
            self._one = {"szczyt": "1.0", "bez_nav": 0}
        elif "SELECT nav, wplaty_netto" in s:
            self._one = {"nav": "1.0", "wplaty_netto": "100000"}
        elif "SELECT nav FROM" in s:
            self._one = {"nav": "1.0"}
        elif "SELECT equity FROM" in s:
            self._one = {"equity": self.equity}
        return self

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class Polaczenie:
    def __init__(self, kursor):
        self.kursor = kursor
        self.commity = 0

    def cursor(self, *a, **k):
        return self.kursor

    def commit(self):
        self.commity += 1

    def rollback(self):
        pass


def kernel(wiek_min, killswitch=None, equity="100000") -> RiskKernel:
    return RiskKernel(Polaczenie(Kursor(wiek_min, killswitch, equity)),
                      "crypto", "demo")


# --------------------------------------------------- stary pomiar to nie pomiar

def test_stary_punkt_kapitalu_NIE_daje_zgody_na_handel():
    """Dokładnie stan zmierzony na produkcji: punkt sprzed trzech dni, otwarta
    pozycja 15 000, wszystkie limity procentowe „w normie" — bo liczą się
    z liczby, która stoi."""
    v = kernel(wiek_min=3 * 24 * 60).check_account()

    assert v.verdict != "allow", (
        "krzywa kapitału sprzed trzech dni nadal daje zgodę na wejście — "
        "limity liczą się wtedy z liczby, której nikt nie zmierzył")
    assert "pomiar kapitału" in (v.reason or ""), (
        f"powód nie mówi, że problemem jest WIEK pomiaru: {v.reason!r}")


def test_KANAREK_ta_sama_krzywa_bez_bramki_wieku_dawala_allow(monkeypatch):
    """Kanarek do testu wyżej: bez bramki wieku ten sam stan przechodzi.

    Gdyby atrapa blokowała z jakiegokolwiek innego powodu (literówka w limitach,
    zerowy kapitał, wyzwolony kill-switch), test wyżej przechodziłby, nie
    sprawdzając niczego z tego, po co powstał. Tu wyłączamy JEDNĄ rzecz —
    ocenę wieku — i stan musi wrócić do `allow`, czyli do zachowania sprzed
    poprawki.
    """
    monkeypatch.setattr("services.engine.risk.ocen_wiek_pomiaru",
                        lambda *a, **k: None)
    v = kernel(wiek_min=3 * 24 * 60).check_account()

    assert v.verdict == "allow", (
        f"atrapa blokuje z innego powodu niż wiek pomiaru ({v.reason!r}) — "
        f"test starego punktu nie dowodzi wtedy niczego")


def test_swiezy_pomiar_przechodzi():
    """Drugi kanarek, w przeciwną stronę. Bramka, która blokuje zawsze, spełnia
    test wyżej i zatrzymuje handel na stałe — czyli jest awarią, nie ochroną."""
    v = kernel(wiek_min=5.0).check_account()
    assert v.verdict == "allow", f"świeży pomiar zablokowany: {v.reason!r}"


def test_normalne_opoznienie_cyklu_NIE_wstrzymuje_handlu():
    """Zdrowy odczyt ma tu 5-6 minut, a nie zero: wiek sprawdzamy na POCZĄTKU
    cyklu, a punkt krzywej powstaje na jego KOŃCU. Zmierzone na produkcji
    2026-09-09: mediana odstępu 300 s, p95 300 s, p99 360 s.

    Próg, który miga, kończy tak samo jak czujka, która miga: ktoś ją podniesie,
    żeby przestała przeszkadzać."""
    assert ocen_wiek_pomiaru(6.0) is None, "p99 produkcyjny wpada w próg"
    assert ocen_wiek_pomiaru(float(POMIAR_STARY_PO_MIN)) is None
    assert ocen_wiek_pomiaru(POMIAR_STARY_PO_MIN + 0.1) is not None


def test_prog_to_TRZY_cykle_petli_decyzyjnej():
    """Ta sama zasada i ta sama liczba co CENA_STARA_PO_MIN i MARTWY_PO_MIN:
    jedno spóźnienie to wahanie, trzy to sygnał. Rozjazd tych trzech progów
    znaczyłby, że system ma trzy różne zdania o tym, kiedy dane są martwe."""
    from services.engine.dozor import MARTWY_PO_MIN
    from services.engine.loop import CENA_STARA_PO_MIN

    CYKL_MIN = 5  # `--interval 300` w charts/trader/values.yaml
    assert POMIAR_STARY_PO_MIN >= 3 * CYKL_MIN, (
        f"próg {POMIAR_STARY_PO_MIN} min to mniej niż trzy cykle pętli")
    assert POMIAR_STARY_PO_MIN == CENA_STARA_PO_MIN == MARTWY_PO_MIN, (
        "trzy progi tej samej zasady rozjechały się — świeżość ceny, "
        "świeżość pomiaru kapitału i żywotność silnika mierzą to samo okno")


def test_BRAK_krzywej_to_nie_jest_zgoda():
    """Brak pomiaru nie jest stanem „wszystko w porządku", tylko stanem
    „nie wiadomo" — ta sama zasada, co przy braku limitów (`sprawdz_komplet`)."""
    assert ocen_wiek_pomiaru(None) is not None

    v = kernel(wiek_min=None).check_account()
    assert v.verdict != "allow", "pusty exec.equity_point przepuścił wejście"


# ------------------------------------------- wstrzymanie, a nie kill-switch

def test_stary_pomiar_WSTRZYMUJE_zamiast_zatrzymywac_na_stale():
    """Zamrożona krzywa wraca sama razem z pętlą. Kill-switch wymaga ręcznego
    zwolnienia, więc żądanie go tutaj znaczyłoby budzenie właściciela o trzeciej
    w nocy do awarii, która minęła — dokładnie ten sam wybór i z tego samego
    powodu zrobiono dla pozycji bez świeżej ceny."""
    v = kernel(wiek_min=3 * 24 * 60).check_account()
    assert not v.trigger_killswitch, (
        "stary pomiar zażądał kill-switcha — zatrzymanie chwilowe zamieniłoby "
        "się w trwałe, wymagające człowieka")


def test_aktywny_killswitch_zostaje_POWODEM_mimo_starego_pomiaru():
    """Werdykt niekorzystny jest ważniejszy. Podmiana powodu „kill-switch"
    na „stary pomiar" zamieniałaby zatrzymanie wymagające człowieka
    w zatrzymanie, które mija samo."""
    v = kernel(wiek_min=3 * 24 * 60,
               killswitch={"reason": "obsunięcie 12%", "triggered_at": None}).check_account()
    assert v.verdict == "block"
    assert "Kill-switch" in (v.reason or ""), (
        f"stary pomiar przykrył powód zatrzymania: {v.reason!r}")


def test_petla_wstrzymuje_wejscia_ISTNIEJACA_sciezka():
    """Werdykt z `check_account()` wpada w `powody_wstrzymania` — tę samą
    ścieżkę, którą wstrzymuje pozycja bez świeżej ceny. Druga, równoległa
    ścieżka wstrzymania znaczyłaby dwa miejsca do pomylenia przy każdej
    następnej zmianie (i drugie miejsce, w którym można zapomnieć o wyjściach).
    """
    zrodlo = Path("services/engine/loop.py").read_text(encoding="utf-8")
    assert "powody_wstrzymania.append(konto.reason)" in zrodlo
    assert 'if action.startswith("enter") and wejscia_wstrzymane:' in zrodlo, (
        "wstrzymanie musi dotyczyć wyłącznie wejść — wyjścia z pozycji, "
        "której nie umiemy wycenić, zmniejszają to, czego nie mierzymy")


# ------------------------------------------------------------------ dozorca

def test_dozorca_WYPOWIADA_wiek_pomiaru_w_pulsie():
    """Puls dozorcy odpowiadał wyłącznie na pytanie „czy dozorca żyje".
    Zamrożona krzywa nie ruszała ani tej odpowiedzi, ani werdyktu — więc jedyny
    ślad wielodniowej ciszy w pomiarze leżał w kolumnie, o którą nikt nie pytał.
    """
    zrodlo = Path("services/engine/dozorca.py").read_text(encoding="utf-8")
    detal = zrodlo.split("await bij_heartbeat(execute")[1].split("except Exception")[0]
    assert "wiek_pomiaru_min" in detal, (
        "puls dozorcy nie niesie wieku pomiaru — krzywa stojąca od trzech dni "
        "wygląda w nim identycznie jak krzywa odświeżana co pięć minut")


def test_dozorca_ocenia_konto_TYM_SAMYM_kernelem_razem_z_wiekiem():
    """Druga implementacja reguł „na potrzeby dozorcy" byłaby gwarancją
    rozjazdu przy pierwszej zmianie progu."""
    import services.engine.dozorca as dozorca

    conn = Polaczenie(Kursor(wiek_min=3 * 24 * 60))
    werdykt, powod, wiek = dozorca.ocen_konto(conn, "crypto", "demo")

    assert werdykt != "allow", (
        "dozorca melduje `allow` na krzywej sprzed trzech dni — nadzorca, "
        "który tego nie widzi, nadzoruje własne echo")
    assert "pomiar kapitału" in (powod or "")
    assert wiek == pytest.approx(3 * 24 * 60), (
        "dozorca nie zna wieku pomiaru, więc nie ma czego zameldować")


def test_komentarz_przy_INTERWAL_S_nie_obiecuje_rozdzielczosci_ktorej_nie_ma():
    """Pętla dozorcy co 60 s NIE daje rozdzielczości 60 s: mierzona liczba
    powstaje raz na cykl pętli decyzyjnej, czyli co 300 s (3713 odstępów na
    produkcji, mediana 300 s). Okno ślepoty ma pięć minut, nie minutę —
    a komentarz, który obiecuje więcej, niż mechanizm daje, jest gorszy niż
    brak komentarza: buduje zaufanie do rozdzielczości, której nikt nie ma."""
    zrodlo = Path("services/engine/dozorca.py").read_text(encoding="utf-8")
    naglowek = zrodlo.split("INTERWAL_S = int(")[0].split("NAZWA")[0]
    fragment = naglowek[naglowek.rindex("# Pętla co minutę"):]
    assert "300" in fragment, (
        "komentarz przy INTERWAL_S milczy o rytmie źródła (300 s)")
    assert "dziesięciominutowe okno ślepoty" not in fragment, (
        "komentarz nadal sugeruje, że interwał pętli jest rozdzielczością "
        "pomiaru")


def test_dozorca_ODNOTOWUJE_ze_czyta_liczbe_nadzorowanego():
    """Docelowo dozorca powinien liczyć kapitał SAM z exec.position
    i market.candle. Dopóki tego nie robi, jest niezależny jako proces, ale nie
    jako pomiar — i to ograniczenie ma być zapisane tam, gdzie ktoś je przeczyta
    przed następną zmianą, a nie odkryte po raz drugi za pół roku."""
    zrodlo = Path("services/engine/dozorca.py").read_text(encoding="utf-8")
    assert "ZNANE OGRANICZENIE" in zrodlo
    assert "exec.position" in zrodlo and "market.candle" in zrodlo
