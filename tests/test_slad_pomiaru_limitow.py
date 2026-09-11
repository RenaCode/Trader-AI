"""Sprawdzenie limitów konta musi ZOSTAWIAĆ ŚLAD — inaczej ochrona kapitału
jest założeniem, a nie faktem.

STAN ZASTANY, ZMIERZONY NA PRODUKCJI 2026-09-09

`RiskKernel.check_account()` liczyło zużycie każdego limitu konta w każdym
cyklu (300 s) i nie zapisywało niczego. Jedynym pisarzem do `risk.limit_usage`
była ścieżka wejścia w pozycję (`loop.py`, `risk.record_usage(verdict.usage)`),
więc ostatni wiersz w tabeli pochodził z dnia ostatniego WEJŚCIA — 2026-09-06.
Przez trzy doby silnik pracował, oceniał limity co cykl i nie zostawiał po tym
ani jednego wiersza.

To jest ta sama rodzina wad, co „kill-switch nie miał kodu wyjścia" i „worker
zawieszony wygląda jak zdrowy": SYSTEM DZIAŁAJĄCY I SYSTEM NIEDZIAŁAJĄCY
WYGLĄDAJĄ IDENTYCZNIE. Gdyby `check_account` przestało być wołane albo zaczęło
się wywracać po cichu, dane wyglądałyby tak samo jak dziś.

DLACZEGO `min_equity` NIE MIAŁO ANI JEDNEGO WIERSZA W CAŁEJ HISTORII

Nie z powodu niedopasowania klucza w `record_usage` — nazwa w `usage` i klucz
w `_limits` to ta sama nazwa z `risk.limit_def`. Powód jest prostszy i gorszy:
sekcja `min_equity` powstała w `risk_rules.evaluate_account` 2026-09-07
(commit 4c5d8e6), czyli PO ostatnim wejściu w pozycję (2026-09-06). Jedyna
ścieżka, która cokolwiek zapisywała, od tego czasu ani razu się nie wykonała.
Limit istniał, był oceniany co cykl i nie miał jak zostawić śladu.

Poprawka usuwa oba objawy naraz: pomiar konta zapisuje się w stałym rytmie,
niezależnie od tego, czy silnik chce w cokolwiek wejść.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from services.engine.risk import (
    POMIAR_CO_S,
    BrakLimitow,
    RiskKernel,
)

# Komplet limitów koniecznych w postaci, w jakiej zwraca je `_load_limits`.
LIMITY = {
    "max_daily_loss": {"id": 1, "name": "max_daily_loss", "value": Decimal("2"),
                       "unit": "pct", "action": "killswitch"},
    "max_drawdown": {"id": 2, "name": "max_drawdown", "value": Decimal("10"),
                     "unit": "pct", "action": "killswitch"},
    "min_equity": {"id": 11, "name": "min_equity", "value": Decimal("90"),
                   "unit": "pct_wplat", "action": "killswitch"},
}


class Kursor:
    """Atrapa kursora: oddaje ZDROWE konto i zapamiętuje instrukcje zapisu.

    Nie udaje bazy — zna dokładnie tyle, ile potrzeba, żeby `check_account()`
    przeszło całą swoją ścieżkę i żeby dało się zobaczyć, co po sobie zostawiło.
    """

    def __init__(self, nav=Decimal("1.0"), wplaty=Decimal("10000")):
        self.nav = nav
        self.wplaty = wplaty
        self.zapisy: list[tuple[str, tuple]] = []
        self._wynik: list[dict] = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("INSERT"):
            self.zapisy.append((s, params))
            self._wynik = []
            return self
        if ("exec.position" in s or "risk.killswitch_event" in s
                or "exec.order" in s):
            self._wynik = []
            return self
        if "max(equity)" in s:
            self._wynik = [{"peak": self.wplaty * self.nav}]
            return self
        if "max(nav)" in s:
            self._wynik = [{"szczyt": self.nav}]
            return self
        self._wynik = [{"equity": self.wplaty * self.nav, "nav": self.nav,
                        "wplaty_netto": self.wplaty,
                        "event_time": None, "wiek_min": 0}]
        return self

    def fetchone(self):
        return self._wynik[0] if self._wynik else None

    def fetchall(self):
        return self._wynik

    # --------------------------------------------------------------- pomoc
    def wiersze_pomiaru(self) -> list[tuple[str, tuple]]:
        return [(s, p) for s, p in self.zapisy if "risk.limit_usage" in s]

    def zmierzone_limity(self) -> set[int]:
        """Identyfikatory limitów, dla których poszedł zapis pomiaru."""
        return {p[0] for _, p in self.wiersze_pomiaru()}


class Polaczenie:
    def __init__(self, kursor):
        self._kursor = kursor

    def cursor(self, *a, **k):
        return self._kursor


def kernel(kur, limity=None) -> RiskKernel:
    k = RiskKernel.__new__(RiskKernel)
    k.conn, k.engine, k.mode = Polaczenie(kur), "crypto", "demo"
    k._limits = dict(limity if limity is not None else LIMITY)
    return k


# ------------------------------------------------- 1. pomiar zostawia ślad

def test_KANAREK_sprawdzenie_limitow_konta_zostawia_slad():
    """Sedno całej zmiany.

    Przed poprawką `check_account()` nie wykonywało ŻADNEJ instrukcji INSERT do
    `risk.limit_usage` — tabela zapełniała się wyłącznie przy próbie wejścia
    w pozycję. Ten test pada, gdy ktoś usunie zapis z `check_account`.
    """
    kur = Kursor()
    v = kernel(kur).check_account()

    assert v.verdict == "allow", f"zdrowe konto ocenione jako {v.verdict}"
    assert kur.wiersze_pomiaru(), (
        "check_account() nie zostawiło ani jednego wiersza w risk.limit_usage — "
        "ochrona kapitału znów jest założeniem, nie faktem")


def test_KANAREK_min_equity_TEZ_trafia_do_tabeli():
    """`min_equity` to jeden z trzech filarów podłogi kapitału, a przez całą
    historię produkcji nie miał w `risk.limit_usage` ani jednego wiersza.

    Zapisujemy KOMPLET limitów koniecznych, nie „te, które akurat wyszły" —
    brakujący jeden z trzech nie odróżnia się od kompletu żadną liczbą zbiorczą.
    """
    kur = Kursor()
    kernel(kur).check_account()

    zmierzone = kur.zmierzone_limity()
    assert LIMITY["min_equity"]["id"] in zmierzone, (
        f"min_equity (id {LIMITY['min_equity']['id']}) bez zapisu; "
        f"zmierzone limity: {sorted(zmierzone)}")
    assert zmierzone == {1, 2, 11}, (
        f"komplet limitów koniecznych to 3 wiersze, jest {sorted(zmierzone)}")


def test_zapis_ma_RYTM_a_nie_jest_bezwarunkowy():
    """Cykl trwa 300 s, a dozorca ocenia to samo konto co 60 s. Bez rytmu
    kosztem rządziłby najszybszy z procesów, a wiersze byłyby kopiami tej samej
    sekundy z dwóch źródeł.

    Rytm trzyma BAZA (`NOT EXISTS` po `event_time`), a nie licznik w pamięci
    procesu — licznik liczyłby czas od startu poda i restart co minutę omijałby
    go w całości.
    """
    kur = Kursor()
    kernel(kur).check_account()

    sql = [s for s, _ in kur.wiersze_pomiaru()]
    assert all("NOT EXISTS" in s for s in sql), (
        "zapis pomiaru konta bez warunku rytmu — przy dwóch procesach "
        "oceniających to samo konto to kopie tej samej sekundy")
    assert all("clock_timestamp()" in s for s in sql), (
        "now() to czas POCZĄTKU TRANSAKCJI: dwa pomiary tego samego limitu "
        "w jednej transakcji dostają ten sam klucz główny i drugi znika "
        "przez ON CONFLICT DO NOTHING — po cichu")
    assert any(POMIAR_CO_S in p for _, p in kur.wiersze_pomiaru()), \
        "rytm zapisu nie pochodzi z POMIAR_CO_S"


def test_sciezka_decyzyjna_zapisuje_BEZ_rytmu():
    """Pomiar towarzyszący konkretnej decyzji nie może zostać pominięty przez
    rytm — inaczej w dzienniku jest decyzja bez liczb, na których ją podjęto."""
    kur = Kursor()
    k = kernel(kur)
    k.record_usage([{"name": "max_drawdown", "used": 1.0, "threshold": 10.0}])

    sql = [s for s, _ in kur.wiersze_pomiaru()]
    assert sql and all("NOT EXISTS" not in s for s in sql), (
        "ścieżka decyzyjna dostała rytm — pomiar może wtedy zniknąć, "
        "choć decyzja zostaje")


# -------------------------------------------- 2. nietrafiona nazwa jest głośna

def test_nazwa_bez_odpowiednika_w_limitach_NIE_JEST_pomijana_po_cichu():
    """`continue` zamieniało literówkę w nazwie limitu w brak wierszy dla tego
    jednego limitu: tabela wyglądała zdrowo, a mierzony byłby o jeden filar
    mniej, niż sądzimy. To jest dokładnie ten kształt ciszy, który ta zmiana
    usuwa gdzie indziej — nie ma powodu, żeby zostawiać go tutaj."""
    kur = Kursor()
    k = kernel(kur)
    with pytest.raises(BrakLimitow) as exc:
        k.record_usage([{"name": "min_eqiuty", "used": 1.0, "threshold": 2.0}])
    assert "min_eqiuty" in str(exc.value)


def test_zdrowy_komplet_NIE_rzuca():
    """Kanarek na kanarka: gdyby powyższy wyjątek leciał zawsze, poprzedni test
    przechodziłby, a silnik nie zrobiłby ani jednego cyklu."""
    kur = Kursor()
    assert kernel(kur).check_account().verdict == "allow"


# ============================================================================
# 3. CISZA POMIARU JEST WIDOCZNA BEZ CZYTANIA LOGU PODA
#
# Sam zapis pomiaru nie wystarcza. Gdyby `check_account()` przestało być wołane
# — bo pętla się zawiesiła, bo dozorca wywraca się na wyjątku, bo ktoś usunął
# wywołanie — tabela po prostu przestałaby rosnąć, i nikt by tego nie zobaczył.
# Kryterium jest jedno: „bezpiecznik nie raportował od X" ma być rozpoznawalne
# bez zaglądania do logu poda.
# ============================================================================

import asyncio  # noqa: E402

from services.engine.dozor import (  # noqa: E402
    POMIAR_CISZA_S,
    ocen_cisze_pomiaru,
    sprawdz_ryzyko,
)


# --------------------------------------------------------- czysta funkcja

def test_swiezy_i_kompletny_pomiar_to_CISZA_CZUJKI():
    """Czujka, która odzywa się zawsze, jest tak samo bezużyteczna jak ta,
    która milczy zawsze."""
    assert ocen_cisze_pomiaru(wiek_s=120, nigdy=None,
                              od_zgloszenia_min=None) is None


def test_KANAREK_pomiar_starszy_niz_trzy_rytmy_to_ALARM():
    """Stan zmierzony na produkcji 2026-09-09: ostatni wiersz w
    `risk.limit_usage` sprzed trzech dób, silnik cały czas pracuje."""
    w = ocen_cisze_pomiaru(wiek_s=3 * 24 * 3600, nigdy=None,
                           od_zgloszenia_min=None)
    assert w is not None, "trzy doby ciszy pomiaru przeszły jako stan zdrowy"
    assert w["zglos"] is True
    assert "nie raportuje" in w["powod"]


def test_jedno_spoznienie_to_NIE_alarm():
    """Próg leży na trzech rytmach — jedno spóźnienie to wahanie, nie sygnał.
    Czujka, która miga, uczy się odklikiwać."""
    assert ocen_cisze_pomiaru(wiek_s=POMIAR_CISZA_S - 1, nigdy=None,
                              od_zgloszenia_min=None) is None


def test_KANAREK_limit_bez_ANI_JEDNEGO_pomiaru_to_alarm_mimo_swiezosci():
    """DOKŁADNIE STAN `min_equity` NA PRODUKCJI.

    Dwa z trzech filarów podłogi kapitału mierzą się co pięć minut, trzeci nie
    ma ani jednego wiersza w całej historii. Każda liczba zbiorcza — średnia,
    maksimum, „ostatni pomiar" — pokazuje wtedy stan znakomity.
    """
    w = ocen_cisze_pomiaru(wiek_s=120, nigdy=["min_equity"],
                           od_zgloszenia_min=None)
    assert w is not None, (
        "limit bez ani jednego pomiaru przeszedł jako stan zdrowy, bo pozostałe "
        "mierzą się na czas")
    assert "min_equity" in w["powod"]


def test_pusta_tabela_to_alarm_a_nie_spokoj():
    """Brak pomiaru nie jest stanem „wszystko w porządku", tylko stanem „nie
    wiadomo" — ta sama zasada, co przy braku limitów w risk kernelu."""
    w = ocen_cisze_pomiaru(wiek_s=None, nigdy=list(LIMITY), od_zgloszenia_min=None)
    assert w is not None and "ANI JEDNEGO" in w["powod"]


def test_rytm_przypomnien_jest_TEN_SAM_co_przy_kill_switchu():
    """Nie druga kopia liczby: alarm o niedziałającym bezpieczniku opisuje stan
    trwały, tak samo jak aktywny kill-switch, i powtarzany co dziesięć minut
    nauczyłby się odklikiwać."""
    from services.engine.dozor import odstep_przypomnienia_ks

    w = ocen_cisze_pomiaru(wiek_s=4 * 3600, nigdy=None, od_zgloszenia_min=30)
    assert w is not None and w["zglos"] is False, "przypomnienie poza rytmem"
    assert w["odstep_h"] == odstep_przypomnienia_ks(4 * 60)

    poza = ocen_cisze_pomiaru(wiek_s=4 * 3600, nigdy=None,
                              od_zgloszenia_min=w["odstep_h"] * 60)
    assert poza["zglos"] is True, "po upływie odstępu przypomnienie jednak idzie"


# ------------------------------------------------------------ cała ścieżka

class AtrapaCzujki:
    """Atrapa bazy dla `sprawdz_ryzyko`. Rozpoznaje zapytania po fragmencie."""

    def __init__(self, zdarzenia=(), wiek_s=120, nigdy=None,
                 od_zgloszenia_pomiar_min=None, status="running",
                 puls_silnika_min=1, puls_dozorcy_min=0):
        self.zdarzenia = list(zdarzenia)
        self.wiek_s = wiek_s
        self.nigdy = nigdy
        self.od_zgloszenia_pomiar_min = od_zgloszenia_pomiar_min
        self.status = status
        self.puls_silnika_min = puls_silnika_min
        self.puls_dozorcy_min = puls_dozorcy_min

    async def fetch_all(self, sql, params=None):
        if "risk.killswitch_event" in sql:
            return list(self.zdarzenia)
        if "risk.limit_usage" in sql:
            return [{"engine": "crypto", "mode": "demo",
                     "wiek_s": self.wiek_s, "nigdy": self.nigdy,
                     "status": self.status,
                     "puls_silnika_min": self.puls_silnika_min,
                     "puls_dozorcy_min": self.puls_dozorcy_min}]
        if "notify.notification" in sql:
            if "risk.pomiar_zamilkl" in sql:
                return [{"od_zgloszenia_min": self.od_zgloszenia_pomiar_min}]
            return [{"od_zgloszenia_min": None}]
        if 'app."user"' in sql:
            return [{"id": 1}, {"id": 2}]
        raise AssertionError(f"nieoczekiwane zapytanie: {sql}")


def przebieg(atrapa, **kw):
    wyslane = []

    async def notify(user_id, code, tytul, tresc, **reszta):
        wyslane.append({"user_id": user_id, "code": code, "tytul": tytul,
                        "tresc": tresc, **reszta})

    kod = asyncio.run(sprawdz_ryzyko(atrapa.fetch_all, notify, **kw))
    return kod, wyslane


def zdarzenie_ks(engine="crypto", mode="demo"):
    from datetime import datetime, timezone
    return {"id": 7, "engine": engine, "mode": mode,
            "reason": "Obsunięcie 12% przekroczyło limit 10%",
            "triggered_at": datetime(2026, 9, 9, 3, 0, tzinfo=timezone.utc),
            "wiek_min": 30}


def test_KANAREK_cisza_pomiaru_daje_NIEZEROWY_kod_wyjscia_bez_kill_switcha():
    """SEDNO KRYTERIUM: „bezpiecznik nie raportował od X" musi być widoczne
    w `kubectl get jobs`, a nie tylko w logu poda.

    Bez kill-switcha `--co ryzyko` kończyło się ZERO zawsze — także wtedy, gdy
    ocena limitów nie zostawiała po sobie niczego od trzech dni.
    """
    kod, wyslane = przebieg(AtrapaCzujki(wiek_s=3 * 24 * 3600))
    assert kod == 1, (
        "pomiar limitów milczy od trzech dób, a zadanie kończy się powodzeniem")
    assert [w["code"] for w in wyslane] == ["risk.pomiar_zamilkl"] * 2
    assert "Bezpiecznik przestał raportować" in wyslane[0]["tytul"]


def test_KANAREK_brak_pomiaru_jednego_limitu_tez_konczy_sie_jedynka():
    kod, wyslane = przebieg(AtrapaCzujki(wiek_s=120, nigdy=["min_equity"]))
    assert kod == 1
    assert "min_equity" in wyslane[0]["tresc"] or "min_equity" in wyslane[0]["data"]["nigdy"]


def test_swiezy_pomiar_bez_kill_switcha_to_ZERO():
    """Czujka musi umieć zamilknąć, inaczej czerwień przestaje cokolwiek
    znaczyć."""
    assert przebieg(AtrapaCzujki()) == (0, [])


def test_przy_aktywnym_kill_switchu_cisza_pomiaru_NIE_ALARMUJE_drugi_raz():
    """`evaluate_account` przy aktywnym kill-switchu wraca w punkcie 1, PRZED
    jakimkolwiek pomiarem — cisza jest wtedy spodziewana, a nie awarią.

    Drugi alarm o systemie, który już alarmuje, uczy odklikiwać oba. Stan i tak
    jest widoczny: kill-switch trzyma kod wyjścia niezerowy.
    """
    kod, wyslane = przebieg(AtrapaCzujki([zdarzenie_ks()], wiek_s=3 * 24 * 3600))
    assert kod == 1, "kill-switch aktywny, a zadanie zielone"
    kody = {w["code"] for w in wyslane}
    assert "risk.pomiar_zamilkl" not in kody, (
        f"drugi alarm o zatrzymanym systemie: {kody}")


def test_rytm_ciszy_pomiaru_nie_wysyla_i_NIE_PODNOSI_kodu_wyjscia(capsys):
    """Kod wyjścia idzie za tłumieniem powiadomień — dokładnie jak przy
    kill-switchu i z tego samego powodu (zmierzonego na produkcji 2026-09-10:
    144 padnięte Joby na dobę o jednym znanym zdarzeniu).

    Cisza dotyczy jednak WYŁĄCZNIE kodu wyjścia. Znalezisko zostaje wypisane
    wielkimi literami przy każdym przebiegu, razem z powodem, dla którego
    zadanie mimo to kończy się zerem — zielone zadanie BEZ śladu w logu byłoby
    stanem nieodróżnialnym od spokoju w obu kanałach naraz.
    """
    kod, wyslane = przebieg(
        AtrapaCzujki(wiek_s=3 * 24 * 3600, od_zgloszenia_pomiar_min=30))
    assert (kod, wyslane) == (0, [])
    log = capsys.readouterr().out
    assert "BEZPIECZNIK NIE RAPORTUJE crypto/demo" in log
    assert "bezpiecznik nie raportuje" in log.split("BEZPIECZNIK NIE RAPORTUJE")[-1], \
        "log nie mówi, DLACZEGO kod wyjścia jest zerem mimo znaleziska"


def test_NAWROT_ciszy_pomiaru_NIE_wpada_w_rytm_poprzedniego_zgloszenia():
    """Pomiar zamilkł, poszedł alarm, ktoś to naprawił — i po godzinie zamilkł
    ZNOWU. To druga, osobna awaria bezpiecznika.

    Sam rytm przypominania widzi tu „zgłoszone 90 minut temu, cisza" i tłumi ją.
    Dopóki kod wyjścia był niezależny od tłumienia, wada była zamaskowana:
    zadanie i tak świeciło się na czerwono. Po związaniu obu kanałów jednym
    warunkiem nawrót przepadłby w obu naraz — czyli cisza NIEODRÓŻNIALNA od
    spokoju o bezpieczniku, który przestał mierzyć.

    Rozpoznanie nie wymaga nowej kolumny ani stanu: cisza zaczęła się `wiek_s`
    temu, więc zgłoszenie STARSZE od niej opisywało ciszę POPRZEDNIĄ. Ten sam
    chwyt, którym `engine.down` porównuje się z `last_beat_at`, a kill-switch
    z `triggered_at`.
    """
    w = ocen_cisze_pomiaru(wiek_s=20 * 60, nigdy=None, od_zgloszenia_min=90)
    assert w is not None and w["nawrot"] is True
    assert w["zglos"] is True, "druga awaria bezpiecznika uciszona przez alarm o pierwszej"


def test_ta_SAMA_cisza_nie_jest_nawrotem_bo_wtedy_nawrot_znaczylby_zawsze():
    """Druga strona tego samego warunku. Zgłoszenie MŁODSZE niż początek ciszy
    dotyczy tej ciszy, więc obowiązuje rytm — inaczej czujka alarmowałaby przy
    każdym z 144 przebiegów doby i nauczyłaby się ją przewijać."""
    w = ocen_cisze_pomiaru(wiek_s=3 * 24 * 3600, nigdy=None, od_zgloszenia_min=90)
    assert w["nawrot"] is False and w["zglos"] is False


def test_limit_bez_ANI_JEDNEGO_pomiaru_nie_jest_nawrotem_przy_kazdym_przebiegu():
    """KANAREK NA NAJPROSTSZY SPOSÓB ZEPSUCIA TEGO WARUNKU.

    Pomiar świeży (2 minuty), ale jeden limit konieczny nie ma pomiaru w całej
    historii. Gdyby nawrót liczył się z `wiek_s` także tutaj, wychodziłoby
    „zgłoszenie starsze niż dwie minuty", czyli alarm co dziesięć minut bez
    końca o stanie, który trwa od zawsze i nie ma czego nawracać.
    """
    w = ocen_cisze_pomiaru(wiek_s=120, nigdy=["min_equity"], od_zgloszenia_min=90)
    assert w["nawrot"] is False, "stan trwający od zawsze zgłoszony jako nawrót"
    assert w["zglos"] is False, "alarm o tym samym braku przy każdym przebiegu"


def test_tryb_suchy_wylacza_WYSYLKE_a_nie_stan():
    kod, wyslane = przebieg(AtrapaCzujki(wiek_s=3 * 24 * 3600), sucho=True)
    assert (kod, wyslane) == (1, [])


# ============================================================================
# 4. NADANIE, BEZ KTÓREGO CAŁA ZMIANA COFA SIĘ W GORSZY STAN
#
# `check_account()` wołają DWA procesy na DWÓCH rolach bazodanowych: pętla
# decyzyjna (`trader_silnik`, ma INSERT od 0034) i dozorca ryzyka
# (`trader_dozorca`, nie miał). Bez nadania dozorca wywraca się na pierwszym
# cyklu — a jego pętla łapie wyjątek i idzie dalej, więc `wznow()` i
# `bij_heartbeat()` NIE WYKONUJĄ SIĘ (leżą za rzuconym wyjątkiem w tym samym
# `try`). Objawem byłby „martwy dozorca" w czujce żywotności, a przyczyną brak
# jednego GRANT-a. To jest ten sam kształt cichej awarii, którą ta zmiana usuwa.
# ============================================================================

from pathlib import Path  # noqa: E402

MIGRACJA = (Path(__file__).resolve().parents[1] / "db" / "migrations"
            / "0043_slad_pomiaru_limitow.sql")


def test_dozorca_MOZE_zapisac_pomiar_ktory_sam_wykonal():
    sql = MIGRACJA.read_text(encoding="utf-8")
    kod = "\n".join(l.split("--")[0] for l in sql.splitlines())
    assert "GRANT INSERT ON risk.limit_usage TO trader_dozorca;" in kod, (
        "dozorca ocenia limity co 60 s i od tej zmiany zapisuje pomiar — bez "
        "INSERT-a wywraca się przed heartbeatem i przed wznowieniem")


def test_dozorca_NIE_MOZE_przepisac_wlasnego_pomiaru():
    """Proces, który potrafi zmienić własny pomiar, nie jest już dowodem na to,
    że mierzył."""
    kod = "\n".join(l.split("--")[0]
                    for l in MIGRACJA.read_text(encoding="utf-8").splitlines())
    assert "REVOKE UPDATE, DELETE, TRUNCATE ON risk.limit_usage" in kod


def test_czujka_NIE_MOZE_dopisac_dowodu_ktorego_pilnuje():
    kod = "\n".join(l.split("--")[0]
                    for l in MIGRACJA.read_text(encoding="utf-8").splitlines())
    assert "FROM trader_czujka" in kod, (
        "obserwator z prawem zapisu do risk.limit_usage mógłby wyprodukować "
        "dowód, na który sam patrzy")


def test_typ_zdarzenia_istnieje_i_NIE_DA_SIE_go_wylaczyc():
    """`always_on` — alarmu o niedziałającym bezpieczniku nie wyłącza się
    w preferencjach. Kod zdarzenia musi się zgadzać z tym, którego używa czujka;
    rozjazd tutaj kończy się `ValueError: nieznany typ zdarzenia` w środku
    przebiegu, czyli ciszą zamiast alarmu o ciszy."""
    sql = MIGRACJA.read_text(encoding="utf-8")
    assert "'risk.pomiar_zamilkl'" in sql
    wiersz = sql[sql.index("('risk.pomiar_zamilkl'"):]
    wiersz = wiersz[:wiersz.index("ON CONFLICT")]
    assert "'ryzyko', 'critical', true, 0" in wiersz, (
        "typ zdarzenia nie jest krytyczny albo daje się wyłączyć")

    zrodlo = (Path(__file__).resolve().parents[1]
              / "services" / "engine" / "dozor.py").read_text(encoding="utf-8")
    assert '"risk.pomiar_zamilkl"' in zrodlo, \
        "czujka używa innego kodu zdarzenia niż zasiany w migracji"


# ============================================================================
# 5. CZUJKA MUSI ODRÓŻNIĆ „NIKT NIE MIERZY" OD „NIE MA KOMU MIERZYĆ"
#
# `risk.limit_def` ma włączone limity także dla silnika `equity`, który stoi ze
# statusem `halted` od 2026-08-27 i nie ma ANI JEDNEGO pomiaru w całej historii.
# Bez tego rozróżnienia czujka alarmowałaby o nim przy każdym przebiegu, bez
# końca — a alarm o rzeczy zatrzymanej świadomie uczy przewijać także ten
# o awarii.
# ============================================================================

def test_KANAREK_silnik_zatrzymany_swiadomie_NIE_alarmuje_bez_konca():
    """Stan produkcji 2026-09-09: `equity` halted, puls sprzed 13 dni, zero
    pomiarów w historii. To nie jest awaria bezpiecznika, tylko brak silnika."""
    kod, wyslane = przebieg(AtrapaCzujki(
        wiek_s=None, nigdy=list(LIMITY), status="halted",
        puls_silnika_min=18708, puls_dozorcy_min=None))
    assert (kod, wyslane) == (0, []), (
        "czujka alarmuje o silniku, którego świadomie nie ma — taki alarm "
        "przy każdym przebiegu uczy przewijać także ten prawdziwy")


def test_zawieszony_silnik_przy_ZYWYM_dozorcy_to_PRAWDZIWE_znalezisko():
    """Do zapisu pomiaru wystarczy jeden proces. Jeśli dozorca bije, a wierszy
    nie ma, to nie jest „nie ma komu mierzyć" — to jest awaria pomiaru."""
    kod, wyslane = przebieg(AtrapaCzujki(
        wiek_s=3 * 24 * 3600, status="running", puls_silnika_min=600,
        puls_dozorcy_min=0))
    assert kod == 1 and wyslane, (
        "dozorca żyje i nie zostawia pomiaru — to jest dokładnie ten stan, "
        "którego `--co żywotność` nie widzi")


def test_martwy_dozorca_przy_ZYWYM_silniku_tez_jest_znaleziskiem():
    kod, _ = przebieg(AtrapaCzujki(
        wiek_s=3 * 24 * 3600, status="running", puls_silnika_min=2,
        puls_dozorcy_min=None))
    assert kod == 1


def test_oba_pulsy_martwe_to_sprawa_czujki_ZYWOTNOSCI_a_nie_tej():
    """Powtarzanie tu `engine.down` byłoby echem, nie informacją — tamten alarm
    jest krytyczny i zawsze włączony."""
    kod, wyslane = przebieg(AtrapaCzujki(
        wiek_s=3 * 24 * 3600, status="running", puls_silnika_min=600,
        puls_dozorcy_min=600))
    assert (kod, wyslane) == (0, [])
