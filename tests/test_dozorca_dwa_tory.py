"""Tor `equity` dostaje własnego dozorcę ryzyka — i nikt nikomu nie wchodzi w drogę.

CO BYŁO ZMIERZONE NA PRODUKCJI 2026-09-27, PRZED TĄ ZMIANĄ

  * `risk.limit_def`: SIEDEM włączonych limitów dla `equity/demo`, w tym wszystkie
    trzy kończące się kill-switchem (`max_daily_loss` 2%, `max_drawdown` 10%,
    `min_equity` 95 000),
  * `risk.dozorca_heartbeat`: DOKŁADNIE JEDEN wiersz, `dozorca-ryzyka:crypto:demo`,
  * `wznow()` — jedyna droga automatycznego zwolnienia kill-switcha — ma w całym
    repozytorium JEDNO miejsce wywołania: `services/engine/dozorca.py`,
  * `ref.instrument`: 20 handlowalnych instrumentów equity (migracja 0060),
    a `risk.engine_heartbeat` ma żywy wiersz `equity`.

Czyli: tor equity miał aktywne limity, żadnego niezależnego oceniającego i NIKOGO,
kto mógłby zwolnić kill-switcha poza człowiekiem w API. Dziś nieszkodliwe (krzywa
płaska, `--wymagaj-modelu` wstrzymuje handel), groźne od pierwszej transakcji —
`max(nav)` liczy się BEZ ograniczenia czasowego, więc kill-switch z obsunięcia
nie mija z dobą.

CZEGO PILNUJĄ TE TESTY — PIĘĆ OSOBNYCH PYTAŃ

  1. PREMISA. Czy naprawdę nie ma kto zwolnić kill-switcha i czy każda para
     z włączonymi limitami ma dziś swojego dozorcę.
  2. DWIE INSTANCJE. Czy puls, werdykt i rytm pomiaru rozdzielają się po silniku,
     zamiast nadpisywać się nawzajem. Tu jest kanarek „czy miara reaguje na
     wejście": dwa dozorcy czytający TĘ SAMĄ liczbę byliby jednym dozorcą
     uruchomionym dwa razy.
  3. ROLA. Czy `trader_dozorca` wystarcza dla equity NA BAZIE ZBUDOWANEJ Z TEGO
     REPOZYTORIUM — a nie na produkcji, która ma nadania spoza migracji.
  4. CZUJKI. Czy `swiezosc`, `instrumenty` i `poslizg` obejmują equity, i czy
     robią to bez alarmu co noc o stanie, który nie zmieni się sam.
  5. WIDOCZNOŚĆ BRAKU. Czy dozorca, który NIGDY nie wstał, jest dziś odróżnialny
     od spokoju. To pytanie jest tu najważniejsze: bez niego ta sama wada wróci
     przy trzecim silniku i znowu nikt jej nie zobaczy.

BAZA JEST WYMAGANA DO CZĘŚCI TESTÓW i to jest wybór, nie wygoda. Model uprawnień
czytany z plików migracji jest modelem; pytanie „czy dozorca domknie cykl dla
equity" ma odpowiedź wyłącznie w Postgresie.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

KORZEN = Path(__file__).resolve().parents[1]
CHART = Path(os.environ.get("CHART_TRADER", KORZEN / "charts/trader"))
VALUES = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
MIGRACJE = KORZEN / "db" / "migrations"
MODUL_DOZORCY = "services/engine/dozorca.py"

TEST_DB_URL = os.environ.get("TEST_DB_URL")
wymaga_bazy = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="ustaw TEST_DB_URL na jednorazową bazę z zastosowanymi migracjami")


def dozorcy_z_chartu() -> dict:
    """Wpis workera -> (silnik, tryb) dla każdego procesu uruchamiającego dozorcę.

    Rozpoznanie po KOMENDZIE, nie po nazwie wpisu: nazwa jest etykietą i wolno ją
    zmienić, a modułu uruchamianego nie da się pomylić.
    """
    wynik = {}
    for nazwa, w in VALUES["workers"].items():
        cmd = w["command"]
        if MODUL_DOZORCY not in " ".join(cmd) or not w.get("enabled"):
            continue
        silnik = cmd[cmd.index("--engine") + 1] if "--engine" in cmd else "crypto"
        tryb = cmd[cmd.index("--mode") + 1] if "--mode" in cmd else "demo"
        wynik[nazwa] = (silnik, tryb)
    return wynik


def polaczenie(rola=None):
    """Połączenie do bazy testowej, opcjonalnie przełączone na rolę procesu."""
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(TEST_DB_URL, row_factory=dict_row)
    if rola:
        conn.execute("SET ROLE " + rola)
    return conn


def _uruchom(korutyna):
    """Jedno miejsce na `asyncio.run` - czujki i dozorca sa asynchroniczne."""
    return asyncio.run(korutyna)


# ============================================================================
# 1. PREMISA — dlaczego drugi dozorca w ogóle jest potrzebny
# ============================================================================

def wolacze_funkcji(nazwa: str) -> set:
    """Pliki, w których ta funkcja jest NAPRAWDĘ WOŁANA. Po AST, nie po tekście.

    Szukanie po treści pliku dawało dwa rodzaje fałszywych trafień i oba są tu
    realne: `wznow()` pojawia się w komentarzach (`wznowienie.py`, `risk.py`)
    ORAZ w treści powiadomienia, które o tej funkcji opowiada (`dozor.py`).
    Komentarze da się zdjąć; napisu w f-stringu już nie, bo od kodu różni go
    tylko kontekst — a dokładnie taki test „przechodzący przez przypadek"
    złapaliśmy w tym repozytorium dziś dwa razy.

    Definicja funkcji nie jest jej wywołaniem, więc `ast.Call` odpowiada na
    właściwe pytanie bez żadnego odejmowania.
    """
    import ast

    wynik = set()
    for p in sorted((KORZEN / "services").rglob("*.py")):
        drzewo = ast.parse(p.read_text(encoding="utf-8"))
        for wezel in ast.walk(drzewo):
            if not isinstance(wezel, ast.Call):
                continue
            f = wezel.func
            czy = (isinstance(f, ast.Name) and f.id == nazwa) or (
                isinstance(f, ast.Attribute) and f.attr == nazwa)
            if czy:
                wynik.add(p.relative_to(KORZEN).as_posix())
    return wynik


def test_wznow_ma_JEDNO_miejsce_wywolania_i_jest_nim_dozorca():
    """Cała stawka tej zmiany w jednym zdaniu.

    Gdyby kill-switcha zwalniał też `loop.py` albo cokolwiek innego, brak
    dozorcy dla equity byłby niedogodnością. Zwalnia go WYŁĄCZNIE `wznow()`,
    a `wznow()` woła wyłącznie dozorca — więc tor bez dozorcy to tor, na
    którym zatrzymanie trwa do chwili, gdy człowiek kliknie w API.

    Ten test pada, gdy ktoś doda drugie miejsce wywołania: wtedy premisę
    opisaną w nagłówku tego pliku trzeba przeczytać od nowa.
    """
    wolacze = wolacze_funkcji("wznow")
    oczekiwane = {MODUL_DOZORCY, "services/engine/wznowienie.py"}
    assert wolacze == oczekiwane, (
        f"`wznow()` wołane z {sorted(wolacze)}, a nie z {sorted(oczekiwane)} — "
        f"jeśli to celowe, opis premisy w nagłówku tego pliku przestał być "
        f"prawdziwy")


@wymaga_bazy
def test_KANAREK_kazda_para_z_wlaczonymi_limitami_MA_dozorce():
    """PADA NA CHARCIE SPRZED ZMIANY — i to jest cała treść tego zadania.

    Para engine/mode z włączonym limitem konta to para, której limity ktoś MUSI
    oceniać niezależnie od pętli decyzyjnej. Lista oczekiwanych dozorców idzie
    więc Z BAZY, a nie z drugiej listy w teście: dopisanie limitów trzeciemu
    silnikowi zapali ten test samo, bez niczyjej pamięci.

    Zmierzone 2026-09-27: crypto/demo 7 limitów, equity/demo 7 limitów, a dozorca
    w charcie JEDEN, z --engine crypto.
    """
    with polaczenie() as conn:
        wiersze = conn.execute(
            "SELECT DISTINCT engine::text AS e, mode::text AS m"
            "  FROM risk.limit_def WHERE enabled").fetchall()
    pary = set()
    for r in wiersze:
        pary.add((r["e"], r["m"]))
    assert pary, "baza nie ma ani jednego włączonego limitu — nie ma czego mierzyć"
    maja = set(dozorcy_z_chartu().values())
    assert pary <= maja, (
        f"pary z włączonymi limitami: {sorted(pary)}; dozorcy w charcie: "
        f"{sorted(maja)}. Para bez dozorcy ma limity, których nie ocenia żaden "
        f"niezależny proces, i kill-switcha, którego nie ma kto zwolnić")


def test_kazdy_dozorca_ma_WLASNA_pare_silnik_tryb():
    """Dwa wpisy o tej samej parze byłyby dwiema replikami, nie dwoma dozorcami.

    Oceniałyby ten sam stan i mogłyby zwolnić to samo zdarzenie dwukrotnie —
    dokładnie to, przed czym `strategy: Recreate` i `replicas: 1` broni wewnątrz
    jednego Deploymentu.
    """
    pary = list(dozorcy_z_chartu().values())
    assert len(pary) == len(set(pary)), (
        f"dwa procesy dozorcy na tę samą parę engine/mode: {pary}")


# ============================================================================
# 2. DWIE INSTANCJE — czy nie wchodzą sobie w drogę
# ============================================================================

@wymaga_bazy
def test_puls_dwoch_dozorcow_to_DWA_WIERSZE():
    """Klucz pulsu zawiera silnik i tryb, więc instancje się nie nadpisują.

    Kanarek na wariant, który wyglądałby identycznie z zewnątrz: gdyby kluczem
    była sama `NAZWA`, druga instancja odświeżałaby wiersz pierwszej. Czujka
    żywotności widziałaby wtedy JEDEN żywy puls i meldowała zdrowie także po
    śmierci jednego z dozorców — puls martwego biłby cudzym sercem.
    """
    from services.engine.dozorca import NAZWA, bij_heartbeat

    with polaczenie() as conn:
        with conn.transaction(force_rollback=True):
            async def execute(sql, params=None):
                conn.execute(sql, params)

            _uruchom(bij_heartbeat(execute, "crypto", "demo", {"kto": "crypto"}))
            _uruchom(bij_heartbeat(execute, "equity", "demo", {"kto": "equity"}))
            wiersze = conn.execute(
                "SELECT nazwa, detail FROM risk.dozorca_heartbeat"
                " WHERE nazwa LIKE %s ORDER BY nazwa", (NAZWA + ":%",)).fetchall()
            czyj = {}
            for r in wiersze:
                czyj[r["nazwa"]] = r["detail"]["kto"]
            assert czyj == {NAZWA + ":crypto:demo": "crypto",
                            NAZWA + ":equity:demo": "equity"}, czyj


SQL_PUNKT = (
    "INSERT INTO exec.equity_point (engine, mode, event_time, equity, cash,"
    " nav, units, wplaty_netto, zrodlo) VALUES (%s, %s, now(), %s, %s, %s, %s,"
    " %s, NULL)")


def _zasiej_krzywa(conn, engine, equity, nav):
    """Jeden świeży punkt krzywej. Świeży, bo `check_account` odmawia na starym."""
    conn.execute(SQL_PUNKT, (engine, "demo", equity, equity, nav, 100000, 100000))


@wymaga_bazy
def test_KANAREK_werdykt_REAGUJE_na_silnik():
    """Czy parametr `--engine` dochodzi do MIARY, a nie tylko do nazwy procesu.

    Dwie instancje czytające tę samą liczbę byłyby jednym dozorcą uruchomionym
    dwa razy — i wyglądałyby z zewnątrz dokładnie tak, jak mają wyglądać: dwa
    pody, dwa pulsy, dwa zielone wiersze w czujce.

    Dajemy więc każdemu torowi INNY stan konta i żądamy INNEGO werdyktu. Gdyby
    werdykty wyszły identyczne, `engine` byłby etykietą, nie filtrem.
    """
    from services.engine.risk import RiskKernel

    with polaczenie() as conn:
        with conn.transaction(force_rollback=True):
            _zasiej_krzywa(conn, "crypto", 100000, 1.0)
            _zasiej_krzywa(conn, "equity", 50000, 0.5)
            conn.execute("SET LOCAL ROLE trader_dozorca")
            wynik = {}
            for engine in ("crypto", "equity"):
                v = RiskKernel(conn, engine, "demo").check_account()
                wynik[engine] = (v.verdict, v.reason)
    assert wynik["crypto"][0] == "allow", wynik["crypto"]
    assert wynik["equity"][0] == "block", wynik["equity"]
    assert "min_equity" in (wynik["equity"][1] or "") or "95000" in (
        wynik["equity"][1] or ""), wynik["equity"]
    assert wynik["crypto"] != wynik["equity"], (
        "werdykt nie drgnął przy dwóch różnych stanach kont — `engine` nie "
        "dochodzi do miary, więc drugi dozorca mierzy to samo co pierwszy")


@wymaga_bazy
def test_rytm_pomiaru_jednego_toru_NIE_TLUMI_drugiego():
    """`risk.limit_usage` ma rytm trzymany przez BAZĘ, a nie przez proces.

    Warunek rytmu to `NOT EXISTS (... WHERE limit_def_id = ...)`, a wiersze
    `risk.limit_def` są osobne dla każdego silnika — więc pomiar toru crypto nie
    może zabrać torowi equity jego jedynego dowodu, że bezpiecznik jeszcze liczy.

    Gdyby rytm był liczony globalnie (albo w pamięci procesu), drugi dozorca
    startujący w tej samej minucie nie zapisałby ANI JEDNEGO wiersza — i przez
    `dozor.py --co ryzyko` przeszłoby to jako „brak pomiaru limitów equity",
    czyli jako awaria, której nie ma.
    """
    from services.engine.risk import RiskKernel

    with polaczenie() as conn:
        with conn.transaction(force_rollback=True):
            _zasiej_krzywa(conn, "crypto", 100000, 1.0)
            _zasiej_krzywa(conn, "equity", 100000, 1.0)
            conn.execute("SET LOCAL ROLE trader_dozorca")
            for engine in ("crypto", "equity"):
                RiskKernel(conn, engine, "demo").check_account()
            wiersze = conn.execute(
                "SELECT d.engine::text AS e, count(*) AS n"
                "  FROM risk.limit_usage u JOIN risk.limit_def d"
                "    ON d.id = u.limit_def_id GROUP BY 1").fetchall()
            ile = {}
            for r in wiersze:
                ile[r["e"]] = r["n"]
    assert ile.get("crypto", 0) > 0 and ile.get("equity", 0) > 0, (
        f"pomiar zapisany tylko dla części torów: {ile}")


def test_dziennik_wznowienia_jest_stanem_PROCESU_wiec_pody_MUSZA_byc_osobne():
    """`wznowienie.DZIENNIK` tłumi powtórki w PAMIĘCI PROCESU i nie zna silnika.

    Klucz stanu to `wznowienie` oraz `zdarzenie:<id>` — pierwszy z nich nie ma
    w sobie silnika. Dwa tory obsługiwane w JEDNYM procesie dzieliłyby więc jeden
    dziennik: opis stanu jednego toru tłumiłby opis drugiego, a log przestałby
    odpowiadać na pytanie „co się dzieje z equity".

    Nie jest to wada `wznowienie.py` — to jest powód, dla którego dwa tory idą
    jako DWA Deploymenty, a nie jako dwa argumenty jednego procesu. Ten test
    wiąże jedno z drugim, żeby przyszłe „zrobimy jeden pod na oba silniki" padło
    tutaj, a nie na produkcji w postaci cichego logu.
    """
    from services.engine import wznowienie

    assert isinstance(wznowienie.DZIENNIK, wznowienie.Dziennik)
    dozorcy = dozorcy_z_chartu()
    silniki = set()
    for silnik, _ in dozorcy.values():
        silniki.add(silnik)
    assert len(dozorcy) == len(silniki), (
        "liczba procesów dozorcy nie zgadza się z liczbą silników")
    assert len(dozorcy) > 1 or len(silniki) == 1, (
        "dwa tory w jednym procesie dzieliłyby DZIENNIK wznowienia")


# ============================================================================
# 3. ROLA — czy `trader_dozorca` wystarcza dla equity NA BAZIE Z TEGO REPO
# ============================================================================

@wymaga_bazy
@pytest.mark.parametrize("rola", ["trader_dozorca", "trader_silnik"])
@pytest.mark.parametrize("engine", ["crypto", "equity"])
def test_KANAREK_ocena_konta_domyka_sie_na_WASKIEJ_ROLI(rola, engine):
    """PADA PRZED MIGRACJĄ 0063, z komunikatem produkcyjnym z 2026-09-11.

        psycopg.errors.InsufficientPrivilege:
            permission denied for table limit_usage

    `record_usage(..., co_najmniej_s=...)` trzyma rytm pomiaru w bazie przez
    `INSERT ... WHERE NOT EXISTS (SELECT 1 FROM risk.limit_usage ...)`, czyli
    CZYTA tę tabelę. Migracje nadawały obu rolom sam INSERT. Produkcja ma te
    SELECT-y, bo ktoś je nadał ręcznie — więc baza odtworzona z tego
    repozytorium dostawała dozorcę wywracającego się na KAŻDYM cyklu.

    Sprawdzamy OBIE role i OBA silniki, bo ta wada nie należy do equity: należy
    do ścieżki oceny konta, a equity tylko pokazało ją pierwsze.
    """
    from services.engine.risk import RiskKernel

    with polaczenie() as conn:
        with conn.transaction(force_rollback=True):
            _zasiej_krzywa(conn, engine, 100000, 1.0)
            conn.execute("SET LOCAL ROLE " + rola)
            v = RiskKernel(conn, engine, "demo").check_account()
    assert v.verdict in ("allow", "block")
    assert v.usage, (
        "ocena konta nie zostawiła ANI JEDNEGO pomiaru — pusta lista znaczy "
        "brak limitów, a te są w bazie")


@wymaga_bazy
@pytest.mark.parametrize("engine", ["crypto", "equity"])
def test_wznowienie_domyka_sie_na_roli_dozorcy_dla_OBU_torow(engine):
    """Druga połowa cyklu dozorcy: przegląd zatrzymań i ich zwolnienie.

    `wznow()` czyta `risk.killswitch_event`, `risk.limit_def`, krzywą kapitału
    i `app.user`, a zapisuje trzy kolumny zwolnienia. Wszystkie te nadania są
    w migracji 0034 BEZ podziału na silnik — ten test to sprawdza, zamiast
    zakładać, bo brak wiersza per silnik nie dowodzi jeszcze, że zapytanie
    przejdzie.
    """
    from services.engine.wznowienie import Dziennik, wznow

    with polaczenie() as conn:
        with conn.transaction(force_rollback=True):
            _zasiej_krzywa(conn, engine, 100000, 1.0)
            conn.execute(
                "INSERT INTO risk.killswitch_event (engine, mode, reason,"
                " limit_name, triggered_at) VALUES (%s, %s, %s, %s,"
                " now() - make_interval(mins => %s))",
                (engine, "demo", "próba", "max_drawdown", 180))
            conn.execute("SET LOCAL ROLE trader_dozorca")

            async def fetch_all(sql, params=None):
                return conn.execute(sql, params).fetchall()

            async def fetch_one(sql, params=None):
                return conn.execute(sql, params).fetchone()

            async def execute(sql, params=None):
                conn.execute(sql, params)

            poszly = []

            async def notify(*a, **k):
                poszly.append(a)
                return 1

            kod = _uruchom(wznow(fetch_all, fetch_one, execute, notify,
                                 engine=engine, mode="demo",
                                 dziennik=Dziennik(przypomnienie_s=0)))
            zwolnione = conn.execute(
                "SELECT count(*) AS n FROM risk.killswitch_event"
                " WHERE engine::text = %s AND released_auto", (engine,)
            ).fetchone()["n"]
    assert kod == 0
    assert zwolnione == 1, (
        "dozorca nie zwolnił zatrzymania, którego warunek ustał — na tym torze "
        "kill-switch czekałby na człowieka")


def test_migracja_0063_nadaje_TYLKO_odczyt():
    """Poprawka uprawnień nie może być okazją do rozszczelnienia granicy.

    Migracja 0034 postawiła granicę świadomie: dozorca nie handluje, nie widzi
    uzasadnień decyzji, nie ma kluczy giełdy i nie rusza limitów. 0063 dodaje
    ODCZYT tabeli, którą dozorca sam zapisuje — i nic więcej.
    """
    pliki = list(MIGRACJE.glob("0063_*.sql"))
    assert len(pliki) == 1, f"spodziewana dokładnie jedna migracja 0063: {pliki}"
    kod = "\n".join(
        w.split("--")[0] for w in pliki[0].read_text(encoding="utf-8").splitlines())
    nadania = re.findall(r"GRANT\s+([^;]+?)\s+ON\s+([^;]+?)\s+TO\s+([^;]+);",
                         kod, re.I | re.S)
    assert nadania, "migracja 0063 nie nadaje niczego"
    for prawa, tabela, role in nadania:
        assert prawa.strip().upper() == "SELECT", (
            f"0063 nadaje {prawa!r} — to ma być sam odczyt")
        assert tabela.strip() == "risk.limit_usage", (
            f"0063 dotyka {tabela!r} — zakres to tabela pomiaru, nic więcej")
    for zakazane in ("journal", "market", "app.api_credential", "learn"):
        assert zakazane not in kod, (
            f"0063 sięga do {zakazane} — granica z 0034 ma zostać granicą")


# ============================================================================
# 4. CZUJKI — equity przestaje być niewidoczne, ale nie budzi co noc
# ============================================================================

@wymaga_bazy
def test_czujka_ODROZNIA_rynek_z_sesjami_i_BRAK_odswiezacza():
    """Dwie odpowiedzi, które rozstrzygają o alarmie — obie Z BAZY, na roli czujki.

    `rynek_z_sesjami` czyta `ref.venue.is_24_7`, czyli TĘ SAMĄ kolumnę, którą
    rozstrzyga `sesja.stan_sesji`. `venue_bez_odswiezacza` porównuje giełdę
    silnika z `instrumenty.VENUE_ODSWIEZANY`. Gdyby którakolwiek z nich kłamała,
    poprawka zamieniłaby alarm co noc na ciszę albo odwrotnie — a oba te stany
    wyglądają z zewnątrz tak samo jak zdrowie.

    Pytamy NA ROLI CZUJKI, bo to ona uruchamia dozór: prawo odczytu `ref.venue`
    nie jest tu założeniem.
    """
    from services.engine.dozor import rynek_z_sesjami, venue_bez_odswiezacza

    with polaczenie("trader_czujka") as conn:
        async def fetch_all(sql, params=None):
            return conn.execute(sql, params).fetchall()

        odpowiedzi = {}
        for engine in ("crypto", "equity"):
            sesje = _uruchom(rynek_z_sesjami(fetch_all, engine))
            bez = _uruchom(venue_bez_odswiezacza(fetch_all, engine))
            odpowiedzi[engine] = (sesje, bez)
    assert odpowiedzi["crypto"] == (False, False), odpowiedzi
    assert odpowiedzi["equity"] == (True, True), odpowiedzi


def _stend_swiezosci(wiek_min, is_24_7):
    """Atrapa `fetch_all` dla `sprawdz_swiezosc` + lista wysłanych powiadomień."""
    wyslane = []

    async def fetch_all(sql, params=None):
        p = " ".join(sql.split())
        if "notify.notification" in p:
            return []
        if "app." in p and "is_active" in p:
            return [{"id": 1}]
        if "risk.engine_heartbeat" in p:
            return [{"poza": False}]
        if "ref.venue" in p:
            return [{"is_24_7": is_24_7}]
        if "market.candle" in p:
            swieca = {"co": "equity: market.candle", "minut_swiecy": 60,
                      "wiek_min": wiek_min, "ostatni": None}
            cecha = {"co": "equity: feat.feature_value", "minut_swiecy": 60,
                     "wiek_min": 5, "ostatni": None}
            return [swieca, cecha]
        raise AssertionError("atrapa nie zna zapytania: " + p)

    async def notify(user_id, kod, tytul, tresc, **kw):
        wyslane.append((kod, tytul))
        return 1

    return fetch_all, notify, wyslane


# Wiek najnowszej świecy 1h toru equity w chwili otwarcia sesji, ZMIERZONY.
#
# Siatka świec equity to 12:00..20:00 UTC (produkcja 2026-09-21..25, 7 pełnych
# świec na sesję), a sesja otwiera się 13:30 UTC. O 13:30 najnowsza świeca ma
# więc znacznik 20:00 z dnia poprzedniego: 17,5 h = 1050 min. Próg to trzy
# świece źródłowe, czyli 180 min.
WIEK_PRZY_OTWARCIU_MIN = 1050
PROG_1H_MIN = 180


def test_POMIAR_swiezosc_equity_przy_otwarciu_sesji_PRZEKRACZA_prog():
    """Liczba, dla której ta poprawka nie kończy się na dołożeniu --engine.

    Bez niej „dla rynku z sesjami nie alarmujemy" byłoby zdaniem z sufitu.
    `ocen_swiezosc` na wieku z chwili otwarcia sesji ZWRACA zastrzeżenie —
    czyli czujka dołożona naiwnie wysyłałaby `engine.down` o wadze `critical`
    każdego rana, przy potoku w pełni zdrowym, od 13:30 do dopisania pierwszej
    świecy dnia (około 14:20, `history-equity` co 1200 s).

    Tłumienie per źródło tego nie zasypuje: kluczem jest „powiadomienie młodsze
    niż ostatni wpis tego źródła", a ostatnim wpisem jest wczorajsze zamknięcie,
    więc wczorajszy alarm nie tłumi dzisiejszego. Wychodzi dokładnie jedno
    powiadomienie na źródło na sesję, czyli dwa dziennie.
    """
    from services.engine.dozor import SWIEC_ZWLOKI, ocen_swiezosc

    assert SWIEC_ZWLOKI * 60 == PROG_1H_MIN, (
        "próg świeżości przestał wynosić 180 min dla świecy 1h — przelicz "
        "okno fałszywego alarmu od nowa")
    wiersz = {"co": "equity: market.candle", "minut_swiecy": 60,
              "wiek_min": WIEK_PRZY_OTWARCIU_MIN}
    powody = ocen_swiezosc([wiersz])
    assert len(powody) == 1, powody
    assert "nieświeże" in powody[0][1], powody[0]


def test_swiezosc_rynku_Z_SESJAMI_mierzy_ale_NIE_WYSYLA_alarmu():
    """Sedno decyzji: pomiar w logu przy każdym przebiegu, zero powiadomień.

    Kod wyjścia 0, więc Job nie zapala się na czerwono. Log NIE MILCZY — wiek
    obu źródeł jest wypisywany, a dziś nie ma go tam wcale.
    """
    from services.engine.dozor import sprawdz_swiezosc

    fetch_all, notify, wyslane = _stend_swiezosci(WIEK_PRZY_OTWARCIU_MIN, False)
    kod = _uruchom(sprawdz_swiezosc(fetch_all, notify, bar="1h", engine="equity"))
    assert kod == 0, "czujka zapaliła zadanie na czerwono przy zdrowym potoku"
    assert wyslane == [], (
        f"poszło powiadomienie o rynku z sesjami: {wyslane} — to jest ten alarm "
        f"co noc, który uczy odklikiwać całą kategorię `engine.down`")


def test_KANAREK_swiezosc_rynku_CIAGLEGO_NADAL_alarmuje():
    """Druga strona tego samego przełącznika — bez niej cisza nic nie znaczy.

    Ten sam wiek danych, ta sama atrapa, jedyna różnica to `is_24_7`. Gdyby
    poprawka wyciszyła czujkę dla WSZYSTKICH torów, oba testy z tej pary
    przeszłyby przy zepsutym krypto — a krypto jest jedynym torem, który
    naprawdę handluje.
    """
    from services.engine.dozor import sprawdz_swiezosc

    fetch_all, notify, wyslane = _stend_swiezosci(WIEK_PRZY_OTWARCIU_MIN, True)
    kod = _uruchom(sprawdz_swiezosc(fetch_all, notify, bar="1h", engine="crypto"))
    assert kod != 0, "rynek ciągły ze stojącym potokiem nie zapalił zadania"
    assert wyslane, "rynek ciągły ze stojącym potokiem nie wysłał powiadomienia"
    assert wyslane[0][0] == "engine.down", wyslane


def _instrument(symbol, sprawdzony_h):
    return {"symbol": symbol, "tradeable": True, "status_gieldy": "Trading",
            "sprawdzony_h": sprawdzony_h, "pozycji": 0, "wartosc": 0}


def test_instrumenty_BEZ_odswiezacza_mowia_BRAK_POMIARU():
    """Tor equity: 20 instrumentów, zero odczytów statusu, ZERO powiadomień.

    `services/ingest/instrumenty.py` pyta wyłącznie giełdy `VENUE_ODSWIEZANY`
    i chodzi bez `--engine`, więc dla żadnej akcji nie odczytał statusu nigdy
    i nie odczyta, dopóki ktoś nie napisze odświeżacza dla Alpaki. Zmierzone
    2026-09-27: `status_sprawdzony_at` ustawione dla 0 z 20 instrumentów equity
    i dla 34 z 34 instrumentów crypto.

    Alarm o tym stanie szedłby przy każdym przebiegu (co 10 min), a
    `instrument.suspended` ma cooldown 3600 s, czyli 24 maile na dobę o rzeczy,
    która nie zmieni się sama NIGDY.
    """
    from services.engine.dozor import brak_odczytu_statusu, ocen_instrumenty

    wiersze = [_instrument("AAPL", None), _instrument("MSFT", None)]
    assert brak_odczytu_statusu(wiersze, True) is True
    assert ocen_instrumenty(wiersze, bez_odswiezacza=True) == []


def test_KANAREK_instrumenty_Z_odswiezaczem_NADAL_alarmuja():
    """Ta sama lista wierszy, jedyna różnica to istnienie odświeżacza.

    Dla krypto pusta data znaczy „zadanie `trader-instrumenty` przestało
    chodzić" — i to jest awaria, która 2026-09-24 przeleżała 10 godzin za
    `|| true`. Gdyby poprawka uciszyła oba przypadki, ten test przeszedłby
    przy zamrożonej kolumnie statusu całego uniwersum krypto.
    """
    from services.engine.dozor import brak_odczytu_statusu, ocen_instrumenty

    wiersze = [_instrument("BTCUSDC", None), _instrument("ETHUSDC", None)]
    assert brak_odczytu_statusu(wiersze, False) is False
    powody = ocen_instrumenty(wiersze, bez_odswiezacza=False)
    assert len(powody) == 1, powody
    assert powody[0][0] == "instrument.suspended"
    assert "nie był sprawdzany" in powody[0][1]


def test_MIESZANKA_dat_sprawdzenia_NADAL_alarmuje():
    """Druga granica ciszy: „ani jeden", a nie „ten jeden".

    Odświeżacz, który chodzi i POMIJA część uniwersum — nowy instrument, nowa
    kategoria, zmieniony symbol — zostawia mieszankę. To jest rozjazd, a nie
    brak mechanizmu, więc alarm idzie jak dotąd. Bez tego warunku pierwszy
    odświeżacz statusu dla Alpaki mógłby pomijać połowę spółek w ciszy.
    """
    from services.engine.dozor import brak_odczytu_statusu, ocen_instrumenty

    wiersze = [_instrument("AAPL", None), _instrument("MSFT", 2)]
    assert brak_odczytu_statusu(wiersze, True) is False
    assert ocen_instrumenty(wiersze, bez_odswiezacza=True)


def test_poslizg_NIE_UMIE_wyslac_powiadomienia():
    """Pomiar bez wypełnień mówi NIE ZMIERZONO i nie ma czym alarmować.

    `sprawdz_poslizg` nie przyjmuje `notify` — nie jest to konwencja, tylko
    niemożność: raport, który nie ma kanału, nie zacznie go używać przez
    przeoczenie. Tor equity nie ma ani jednego wypełnienia, więc bez tej
    własności dołożenie `--engine equity` byłoby trzecim alarmem co noc.
    """
    import inspect

    from services.engine.dozor import sprawdz_poslizg

    parametry = set(inspect.signature(sprawdz_poslizg).parameters)
    assert "notify" not in parametry, parametry
    assert "engine" in parametry and "mode" in parametry, parametry


# ============================================================================
# 5. WIDOCZNOŚĆ BRAKU — dozorca, którego NIGDY nie było
# ============================================================================

def test_nazwa_pulsu_dozorcy_jest_JEDNA():
    """Czujka i dozorca muszą składać ten klucz z tego samego napisu.

    Druga kopia rozjechałaby się po cichu, a wtedy `--co zywotnosc` twierdziłaby,
    że spodziewanego dozorcy nie ma (albo że jest) — w zależności od kierunku
    rozjazdu. Oba warianty są cichym fałszem.
    """
    from services.engine import dozor, dozorca

    assert dozor.DOZORCA_PULS is dozorca.NAZWA


def test_brakujacy_dozorcy_bierze_pary_Z_LIMITOW():
    """CZYSTA FUNKCJA. Oczekiwani dozorcy wynikają z danych, nie z listy w kodzie."""
    from services.engine.dozor import DOZORCA_PULS, brakujacy_dozorcy

    pary = [{"engine": "crypto", "mode": "demo"}, {"engine": "equity", "mode": "demo"}]
    pulsy = [{"nazwa": DOZORCA_PULS + ":crypto:demo"}]
    assert brakujacy_dozorcy(pary, pulsy) == [DOZORCA_PULS + ":equity:demo"]
    pelne = pulsy + [{"nazwa": DOZORCA_PULS + ":equity:demo"}]
    assert brakujacy_dozorcy(pary, pelne) == []
    assert brakujacy_dozorcy([], pulsy) == []


def _stend_zywotnosci(pary, pulsy, zgloszone_w_dobie=()):
    """Atrapa `fetch_all` dla `sprawdz_zywotnosc` + lista powiadomień."""
    poszly = []

    async def fetch_all(sql, params=None):
        p = " ".join(sql.split())
        if "notify.notification" in p:
            juz = params[0] in zgloszone_w_dobie
            return [{"x": 1}] if juz else []
        if "risk.limit_def" in p:
            return pary
        if "SELECT nazwa FROM risk.dozorca_heartbeat" in p:
            return pulsy
        if "risk.engine_heartbeat" in p:
            teraz = datetime.now(timezone.utc)
            zywy = {"engine": "crypto", "etykieta": "crypto", "mode": "demo",
                    "status": "running", "last_beat_at": teraz, "wiek_min": 0}
            return [zywy]
        if "is_active" in p:
            return [{"id": 1}]
        raise AssertionError("atrapa nie zna zapytania: " + p)

    async def notify(user_id, kod, tytul, tresc, **kw):
        poszly.append((kod, tytul, kw.get("data")))
        return 1

    return fetch_all, notify, poszly


def test_KANAREK_stan_produkcji_sprzed_zmiany_KRZYCZY():
    """PADA NA KODZIE SPRZED POPRAWKI — tam ten stan był ciszą.

    Wejście jest DOKŁADNIE produkcyjne z 2026-09-27: włączone limity dla
    crypto/demo i equity/demo, a w `risk.dozorca_heartbeat` jeden wiersz.
    Przed poprawką `sprawdz_zywotnosc` iterowała same istniejące wiersze, więc
    kończyła się kodem 0 i zerem powiadomień — czyli meldowała zdrowie toru,
    który nie miał ani oceniającego limitów, ani nikogo, kto zwolni
    kill-switcha.
    """
    from services.engine.dozor import DOZORCA_PULS, sprawdz_zywotnosc

    pary = [{"engine": "crypto", "mode": "demo"}, {"engine": "equity", "mode": "demo"}]
    pulsy = [{"nazwa": DOZORCA_PULS + ":crypto:demo"}]
    fetch_all, notify, poszly = _stend_zywotnosci(pary, pulsy)
    kod = _uruchom(sprawdz_zywotnosc(fetch_all, notify))
    assert kod != 0, (
        "tor z włączonymi limitami i bez dozorcy przeszedł przez czujkę na "
        "zielono — to jest stan, w którym produkcja stała od migracji 0060")
    assert len(poszly) == 1, poszly
    kodzik, tytul, dane = poszly[0]
    assert kodzik == "engine.down"
    assert DOZORCA_PULS + ":equity:demo" in tytul, tytul
    assert dane["proces"] == DOZORCA_PULS + ":equity:demo", dane


def test_komplet_dozorcow_NIE_wysyla_niczego():
    """Druga strona kanarka: pełny zestaw pulsów to cisza i kod 0.

    Bez tego test wyżej byłby zaspokojony czujką, która krzyczy zawsze.
    """
    from services.engine.dozor import DOZORCA_PULS, sprawdz_zywotnosc

    pary = [{"engine": "crypto", "mode": "demo"}, {"engine": "equity", "mode": "demo"}]
    pulsy = [{"nazwa": DOZORCA_PULS + ":crypto:demo"},
             {"nazwa": DOZORCA_PULS + ":equity:demo"}]
    fetch_all, notify, poszly = _stend_zywotnosci(pary, pulsy)
    assert _uruchom(sprawdz_zywotnosc(fetch_all, notify)) == 0
    assert poszly == []


def test_brak_dozorcy_przypominany_RAZ_NA_DOBE_a_nie_144_razy():
    """`engine.down` jest `always_on`, więc katalogowy cooldown go nie dotyczy.

    Bez własnego okna czujka wysyłałaby to powiadomienie co dziesięć minut
    i malowała 144 Joby na czerwono na dobę — dokładnie wzorzec zmierzony
    2026-09-12 przy kill-switchu, który uczy odklikiwać całą kategorię.

    Stan zostaje przy tym GŁOŚNY W LOGU: linia BRAK idzie przy każdym przebiegu,
    niezależnie od ciszy w kanale powiadomień.
    """
    from services.engine.dozor import DOZORCA_PULS, sprawdz_zywotnosc

    brakujacy = DOZORCA_PULS + ":equity:demo"
    pary = [{"engine": "equity", "mode": "demo"}]
    fetch_all, notify, poszly = _stend_zywotnosci(pary, [], (brakujacy,))
    kod = _uruchom(sprawdz_zywotnosc(fetch_all, notify))
    assert poszly == [], "powtórzone powiadomienie o tym samym braku"
    assert kod == 0, (
        "kod wyjścia niezerowy bez powiadomienia — to są te 144 czerwone Joby "
        "na dobę o jednym, już zgłoszonym stanie")


@wymaga_bazy
def test_zapytanie_o_pary_dziala_na_ROLI_CZUJKI():
    """Nowe zapytanie czujki musi się wykonać na roli, którą czujka naprawdę ma.

    Atrapa wyżej odpowiada na SQL, którego nikt nie wykonał w Postgresie —
    a `permission denied` wywróciłby czujkę dokładnie w chwili, w której ma
    krzyknąć. To jest ta sama wada, która 2026-09-12 zabrała alarm o martwym
    dozorcy razem z czujką.
    """
    with polaczenie("trader_czujka") as conn:
        pary = conn.execute(
            "SELECT DISTINCT engine::text AS engine, mode::text AS mode"
            "  FROM risk.limit_def WHERE enabled ORDER BY 1, 2").fetchall()
        pulsy = conn.execute("SELECT nazwa FROM risk.dozorca_heartbeat").fetchall()
    from services.engine.dozor import brakujacy_dozorcy

    assert len(pary) >= 2, pary
    assert isinstance(brakujacy_dozorcy(pary, pulsy), list)
