"""Puls procesów zbierających dane: zawieszony worker przestaje wyglądać na żywy.

CO TE TESTY PILNUJĄ

Licznik pięciu awarii, który mają wszystkie cztery workery, broni przed
procesem, który **pada**. Nie broni przed procesem, który **stoi**: zawieszony
cykl nigdy się nie kończy, więc nigdy nie zwiększa licznika, nie kończy procesu
i nie restartuje poda. `kubectl get pods` pokazuje `Running` bez końca.

Puls (`risk.worker_heartbeat`) zamyka tę dziurę, ale sam z siebie potrafi ją
tylko przenieść w inne miejsce. Dwie własności decydują o tym, czy jest wart
czegokolwiek, i obie są tutaj testowane wprost:

  1. puls bije PO UDANYM CYKLU — nigdy przy starcie procesu ani po pierwszym
     z kilku horyzontów;
  2. cykl zakończony błędem NIE ODŚWIEŻA `last_beat_at` — puls „żyję" przy
     pięciu awariach pod rząd jest gorszy niż jego brak, bo mówi czujce
     dokładnie to, co czujka chce usłyszeć, w chwili gdy jest najbardziej
     nieprawdziwe.

Trzecia rzecz to próg. Czujka, która miga, jest tak samo bezużyteczna jak
fałszywa — obie uczą odbiorcę, że można je pomijać — więc próg musi wynikać
z interwału KONKRETNEGO workera (`market` 60 s, `history` 1200 s), a nie być
jedną liczbą dla wszystkich.
"""
import asyncio
import re
import sys
from pathlib import Path

import pytest
import yaml

from services.engine.dozor import (
    CYKLI_ZWLOKI, MIN_PROG_S, ocen_puls_workerow,
)
from services.ingest import puls

KORZEN = Path(__file__).resolve().parents[1]
MIGRACJA = (KORZEN / "db" / "migrations" / "0035_puls_workerow.sql").read_text(
    encoding="utf-8")
SZABLONY = KORZEN / "charts" / "trader" / "templates"
WORKERY_YAML = (SZABLONY / "workers.yaml").read_text(encoding="utf-8")
CRON_YAML = (SZABLONY / "zywotnosc-cron.yaml").read_text(encoding="utf-8")
VALUES = yaml.safe_load((KORZEN / "charts" / "trader" / "values.yaml").read_text())


def _bez_komentarzy_sql(sql: str) -> str:
    """Uzasadnienie w komentarzu nie jest nadaniem uprawnienia."""
    return "\n".join(l.split("--")[0] for l in sql.splitlines())


MIGRACJA_SQL = _bez_komentarzy_sql(MIGRACJA)


def wiersz(worker="market", interwal_s=60, wiek_s=0, proba_wiek_s=None,
           awarii=0, blad=None):
    """Wiersz `risk.worker_heartbeat` w kształcie, w jakim czyta go czujka."""
    return {"worker": worker, "interwal_s": interwal_s, "wiek_s": wiek_s,
            "proba_wiek_s": wiek_s if proba_wiek_s is None else proba_wiek_s,
            "awarii_pod_rzad": awarii, "ostatni_blad": blad,
            "last_beat_at": None}


# --------------------------------------------------------------------- próg

def test_kanarek_alarm_w_ogole_dziala():
    """Bez tego wszystkie testy „cisza" niżej przechodziłyby także wtedy, gdyby
    funkcja zwracała pustą listę zawsze."""
    powody = ocen_puls_workerow([wiersz("history", 1200, wiek_s=86_400)])
    assert len(powody) == 1
    assert powody[0][0] == "history"


def test_prog_wynika_z_INTERWALU_workera_a_nie_z_jednej_liczby():
    """Ten sam wiek pulsu znaczy co innego przy różnych workerach.

    `history` chodzi co 1200 s, `features` co 900 s. Pięćdziesiąt minut ciszy
    to przy pierwszym dwa i pół cyklu (norma), a przy drugim ponad trzy
    (awaria). Jedna wspólna liczba musiałaby albo krzyczeć na historii, albo
    milczeć przy cechach — a czujka, która robi jedno z tych dwóch, nie jest
    czujką.
    """
    wiek = 3000                                   # 50 minut
    assert ocen_puls_workerow([wiersz("history", 1200, wiek_s=wiek)]) == []
    milczy = ocen_puls_workerow([wiersz("features", 900, wiek_s=wiek)])
    assert [p[0] for p in milczy] == ["features"]


def test_prog_to_TRZY_cykle_a_nie_dwa():
    """Ta sama liczba co SWIEC_ZWLOKI i co MARTWY_PO_MIN (15 min = 3 × 300 s).

    Cykl trwa niezerowo i zmiennie, więc próg dwóch cykli leżałby na górnej
    granicy normy i migał kilka razy dziennie.
    """
    assert CYKLI_ZWLOKI == 3
    tuz_pod = 3 * 1200 - 60
    tuz_nad = 3 * 1200 + 60
    assert ocen_puls_workerow([wiersz("history", 1200, wiek_s=tuz_pod)]) == []
    assert ocen_puls_workerow([wiersz("history", 1200, wiek_s=tuz_nad)])


def test_podloga_progu_chroni_workera_o_krotkim_cyklu():
    """`market` chodzi co 60 s, więc trzy cykle to trzy minuty.

    Zadanie czujki chodzi co 10 minut, a pobranie obrazu i pierwszy pełny cykl
    po restarcie poda trwają minuty. Bez podłogi KAŻDE wdrożenie kończyłoby się
    alarmem o martwym workerze — czyli czujka migałaby przy normalnej pracy.
    """
    assert 3 * 60 < MIN_PROG_S, "podłoga przestała cokolwiek podnosić"
    assert ocen_puls_workerow([wiersz("market", 60, wiek_s=600)]) == []


def test_podloga_nie_jest_wyciszeniem_szybkiego_workera():
    """Kanarek do poprzedniego: podłoga podniesiona bez opamiętania uciszyłaby
    `market` na tyle długo, że puls przestałby cokolwiek wnosić ponad
    świeżość danych."""
    assert MIN_PROG_S <= 900
    assert ocen_puls_workerow([wiersz("market", 60, wiek_s=MIN_PROG_S + 60)])


def test_prog_na_zadanie_wywoluje_SCIEZKE_ALARMOWA():
    """Bez tego jedynym sposobem sprawdzenia, czy alarm działa, byłoby zabicie
    workera na produkcji — czyli ścieżka, która ma zadziałać raz na rok
    w najgorszym momencie, nie byłaby przetestowana nigdy."""
    zdrowy = wiersz("history", 1200, wiek_s=60)
    assert ocen_puls_workerow([zdrowy]) == []
    assert ocen_puls_workerow([zdrowy], prog_s=1)


# ------------------------------------------------- „stoi" kontra „nie daje rady"

def test_odroznia_proces_ZAWIESZONY_od_procesu_ktory_PADA():
    """Dwa stany nie do odróżnienia po samym `last_beat_at`, a wymagające
    dwóch różnych reakcji: „zajrzyj, czy pod żyje" i „giełda odmawia od
    godziny"."""
    stoi = wiersz("features", 900, wiek_s=7200)
    pada = wiersz("history", 1200, wiek_s=7200, proba_wiek_s=30, awarii=4,
                  blad="funding: ExchangeError: 429 Too Many Requests")

    (_, tytul_stoi, tresc_stoi), = ocen_puls_workerow([stoi])
    (_, tytul_pada, tresc_pada), = ocen_puls_workerow([pada])

    assert "zamilkł" in tytul_stoi
    assert "ZAWIESZONY" in tresc_stoi

    assert "nie domyka cykli" in tytul_pada
    assert "429 Too Many Requests" in tresc_pada, \
        "opis awarii nie doszedł do powiadomienia — zostaje sam licznik"
    assert "4" in tresc_pada


def test_jedna_awaria_to_JEDEN_alarm():
    """Kuszący drugi warunek na `awarii_pod_rzad` zgłaszałby ten sam problem
    dwa razy: worker kończący cykle błędem z definicji nie odświeża
    `last_beat_at`, więc i tak wpada w warunek wieku. Dwa powiadomienia
    o jednej awarii uczą odbiorcę, że można je pomijać."""
    pada = wiersz("news", 300, wiek_s=7200, proba_wiek_s=10, awarii=4,
                  blad="httpx.ConnectError")
    assert len(ocen_puls_workerow([pada])) == 1


def test_tresc_mowi_KTORY_proces_zamilkl():
    """Cała przewaga nad `--co swiezosc`: sygnał z `feat.feature_value` jest
    ten sam niezależnie od tego, czy stanął `market`, `history` czy
    `features`."""
    powody = ocen_puls_workerow([
        wiersz("market", 60, wiek_s=7200),
        wiersz("news", 300, wiek_s=60),
    ])
    assert [p[0] for p in powody] == ["market"]
    assert "market" in powody[0][1]


# ------------------------------------------------------ kształt zapisu pulsu

def test_awaria_NIE_RUSZA_last_beat_at():
    """Własność, dla której cały ten mechanizm ma sens.

    Gdyby zapis awarii dotykał `last_beat_at`, worker padający pięć razy pod
    rząd meldowałby czujce zdrowie — dokładnie w chwili, w której jest
    najbardziej chory.
    """
    assert "last_beat_at" not in puls._AWARIA
    assert "last_beat_at = now()" in puls._UPSERT


def test_awaria_NIE_ZAKLADA_wiersza():
    """Wiersz zakłada dopiero PIERWSZY UDANY cykl.

    Gdyby awaria potrafiła go założyć, trzeba by czymś wypełnić `last_beat_at`,
    a każda możliwa wartość jest kłamstwem: `now()` twierdzi, że cykl się udał,
    a znacznik z przeszłości alarmuje o workerze, który dopiero wstaje.
    """
    assert puls._AWARIA.lstrip().upper().startswith("UPDATE")
    assert "INSERT" not in puls._AWARIA.upper()


def test_zapis_awarii_nie_przykrywa_pierwotnego_bledu():
    """Jesteśmy na ścieżce awaryjnej: wyjątek z zapisu szczegółów zasłoniłby
    wyjątek, który ma zobaczyć człowiek."""

    class Zepsute:
        def rollback(self):
            raise RuntimeError("połączenie zerwane")

    puls.zglos_awarie(Zepsute(), "market", 60, 1, "cokolwiek")   # nie wolno rzucić


def test_bij_commituje_od_razu():
    """Cykl, który po pulsie wywróci się na czymś innym i cofnie transakcję,
    cofnąłby razem z nią dowód na to, że dane jednak doszły."""

    class Atrapa:
        def __init__(self):
            self.kolejnosc = []

        def execute(self, sql, params=None):
            self.kolejnosc.append("execute")

        def commit(self):
            self.kolejnosc.append("commit")

    c = Atrapa()
    puls.bij(c, "market", 60, {"a": 1})
    assert c.kolejnosc == ["execute", "commit"]


def test_blad_zapisu_pulsu_NIE_JEST_polykany():
    """Połknięty błąd zapisu = wiersz starzeje się przy zdrowym workerze
    i czujka zgłasza śmierć procesu, który żyje. Fałszywy alarm psuje czujkę
    skuteczniej niż jej brak."""

    class Zepsute:
        def execute(self, sql, params=None):
            raise RuntimeError("InsufficientPrivilege")

    with pytest.raises(RuntimeError):
        puls.bij(Zepsute(), "market", 60)


# ------------------------------------------------------- czujka: cała ścieżka

def _czujka(wiersze, *, wyslane_po=None, **kw):
    """Uruchamia `sprawdz_puls_workerow` na atrapie bazy. Zwraca (kod, powiadomienia).

    `wyslane_po` udaje powiadomienie zapisane już po ostatnim udanym cyklu —
    czyli awarię ZGŁOSZONĄ.
    """
    from services.engine.dozor import sprawdz_puls_workerow

    wyslane = wyslane_po or set()
    poszly = []

    async def fetch_all(sql, params=None):
        if "risk.worker_heartbeat" in sql:
            return wiersze
        if "app." in sql and "is_active" in sql:
            return [{"id": 1}]
        if "notify.notification" in sql:
            return [{"x": 1}] if params[0] in wyslane else []
        raise AssertionError(f"nieoczekiwane zapytanie: {sql}")

    async def notify(user_id, code, tytul, tresc, **kwargs):
        poszly.append((code, tytul, kwargs))

    kod = asyncio.run(sprawdz_puls_workerow(fetch_all, notify, **kw))
    return kod, poszly


def test_czujka_przekazuje_PROG_NA_ZADANIE_do_werdyktu():
    """Bez tego `--prog-min` byłby flagą MARTWĄ: przyjmowaną przez argparse
    i nigdzie nie czytaną — dokładnie tym, czym było `--once` w komendzie cech.

    Ścieżka alarmowa, którą da się wywołać tylko przez zabicie workera na
    produkcji, nie jest przetestowana nigdy.
    """
    zdrowy = [wiersz("history", 1200, wiek_s=60, blad=None)]
    assert _czujka(zdrowy)[0] == 0
    kod, poszly = _czujka(zdrowy, prog_min=0)
    assert kod == 1 and len(poszly) == 1


def test_czujka_wysyla_NAZWE_workera_w_danych():
    """`notify.notification.engine` ma typ `ref.engine`, a „market" nie jest
    silnikiem handlowym. Nazwa musi więc jechać w `data` — i musi tam być,
    bo po niej tłumi się powtórki."""
    kod, poszly = _czujka([wiersz("market", 60, wiek_s=7200)])
    assert kod == 1
    (code, tytul, kwargs), = poszly
    assert code == "engine.down"
    assert kwargs["severity"] == "critical"
    assert kwargs["data"]["worker"] == "market"
    assert "engine" not in kwargs, \
        "nazwa workera poszła jako `engine` — to enum silników handlowych"


def test_czujka_MILCZY_o_awarii_juz_zgloszonej():
    """`engine.down` jest zdarzeniem `always_on`, więc omija zwykły cooldown.
    Bez tłumienia czujka krzyczałaby co dziesięć minut, aż ktoś wyciszy kanał —
    a wyciszony kanał to gorszy stan niż brak czujki."""
    kod, poszly = _czujka([wiersz("market", 60, wiek_s=7200)],
                          wyslane_po={"market"})
    assert poszly == []
    assert kod == 0, (
        "awaria już zgłoszona, a zadanie znowu pada — worker martwy przez "
        "tydzień dałby 1008 padniętych Jobów o jednym zgłoszeniu; stan opisuje "
        "linia „MILCZY ...' w logu, wypisywana przy każdym przebiegu")


def test_awaria_juz_zgloszona_NIE_ZNIKA_z_logu(capsys):
    """Cisza w kodzie wyjścia nie ma prawa zabrać ze sobą logu.

    Zadanie zielone I log bez śladu to stan nieodróżnialny od spokoju w obu
    kanałach naraz — czyli dokładnie ta cisza, którą ten plik ma likwidować,
    tyle że wpuszczona drugimi drzwiami.
    """
    _czujka([wiersz("market", 60, wiek_s=7200)], wyslane_po={"market"})
    log = capsys.readouterr().out
    assert "MILCZY" in log and "market" in log, \
        "zadanie kończy się zerem i nie zostawia śladu o martwym workerze"
    assert "worker bez pulsu" in log, \
        "log nie mówi, DLACZEGO kod wyjścia jest zerem mimo znaleziska"


def test_NAWROT_po_powrocie_workera_znowu_maluje_zadanie_na_czerwono():
    """Worker, który wstał i zamilkł ponownie, to zdarzenie NOWE.

    Rozróżnienie jest tu za darmo i bez drugiego mechanizmu: tłumienie pyta
    o powiadomienie młodsze niż OSTATNI UDANY CYKL, a ten się przesunął — stare
    zgłoszenie przestaje więc dotyczyć tej awarii. Gdyby tłumienie pytało tylko
    „czy kiedykolwiek zgłoszono", druga awaria przepadłaby teraz w obu kanałach
    naraz, bo od 2026-09-10 kod wyjścia idzie za tym samym warunkiem.
    """
    kod, poszly = _czujka([wiersz("market", 60, wiek_s=7200)], wyslane_po=set())
    assert kod == 1 and len(poszly) == 1


def test_czujka_bez_wierszy_nie_udaje_ze_sprawdzila():
    """Cisza jest wynikiem, więc musi być widoczna — inaczej nie da się
    odróżnić czujki, która nic nie znalazła, od czujki, która nie działa."""
    assert _czujka([])[0] == 0


# ------------------------------------------------------------ workery: runtime

class AtrapaKursora:
    def __init__(self, wiersze=None):
        self.wiersze = wiersze or []

    def execute(self, *a, **kw):
        return self

    def executemany(self, *a, **kw):
        return self

    def fetchall(self):
        return self.wiersze

    def fetchone(self):
        return self.wiersze[0] if self.wiersze else None


class AtrapaPolaczenia:
    """Zapisuje zapytania puszczone PROSTO z połączenia — tak leci puls."""

    def __init__(self, wiersze=None):
        self.zapytania = []
        self.wiersze = wiersze or []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.zapytania.append((sql, params))
        return AtrapaKursora()

    def cursor(self, *a, **kw):
        return AtrapaKursora(self.wiersze)

    def commit(self):
        pass

    def rollback(self):
        pass


def _pulsy(conn) -> list[tuple[str, tuple]]:
    """(rodzaj, parametry) dla każdego zapisu do risk.worker_heartbeat."""
    out = []
    for sql, params in conn.zapytania:
        if "risk.worker_heartbeat" not in sql:
            continue
        out.append(("udany" if sql.lstrip().upper().startswith("INSERT")
                    else "awaria", params))
    return out


def test_history_bije_puls_dopiero_po_udanym_cyklu(monkeypatch):
    from services.ingest import history_worker

    conn = AtrapaPolaczenia()

    async def swiece_ok(*a, **kw):
        return 7

    async def stawki_ok(*a, **kw):
        return 3, 2, []

    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(history_worker.psycopg, "connect", lambda url, **kw: conn)
    monkeypatch.setattr(history_worker.swiece, "przebieg", swiece_ok)
    monkeypatch.setattr(history_worker.stawki, "przebieg", stawki_ok)
    monkeypatch.setattr(sys, "argv",
                        ["history_worker.py", "--once", "--interval", "1200", "--quiet"])

    assert asyncio.run(history_worker.main()) == 0
    zapisy = _pulsy(conn)
    assert [r for r, _ in zapisy] == ["udany"], f"zapisy pulsu: {zapisy}"
    assert zapisy[0][1][0] == "history"
    assert zapisy[0][1][1] == 1200, \
        "worker nie zadeklarował własnego interwału — czujka nie ma z czego " \
        "policzyć progu i musiałaby trzymać drugą kopię tej liczby"


def test_history_NIE_BIJE_pulsu_po_cyklu_z_bledem(monkeypatch):
    """Puls „żyję" przy nieudanym cyklu jest gorszy niż jego brak."""
    from services.ingest import history_worker

    conn = AtrapaPolaczenia()

    async def swiece_ok(*a, **kw):
        return 7

    async def stawki_z_bledem(*a, **kw):
        return 0, 3, ["BTCUSDT.P: Symbol Invalid"]

    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(history_worker.psycopg, "connect", lambda url, **kw: conn)
    monkeypatch.setattr(history_worker.swiece, "przebieg", swiece_ok)
    monkeypatch.setattr(history_worker.stawki, "przebieg", stawki_z_bledem)
    monkeypatch.setattr(sys, "argv", ["history_worker.py", "--once", "--quiet"])

    assert asyncio.run(history_worker.main()) == 1
    zapisy = _pulsy(conn)
    assert [r for r, _ in zapisy] == ["awaria"], f"zapisy pulsu: {zapisy}"
    assert "Symbol Invalid" in zapisy[0][1][2]


def test_cechy_bija_puls_RAZ_po_wszystkich_horyzontach(monkeypatch):
    """Bicie po pierwszym horyzoncie meldowałoby zdrowie procesu, który od
    tygodnia wywala się na drugim — dokładnie ta awaria kosztowała 2026-09-04
    dwadzieścia jeden godzin martwych cech 1h."""
    from services.ingest import features

    conn = AtrapaPolaczenia()

    def przebieg_ok(c, h, **kw):
        return 100

    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(features.psycopg, "connect", lambda url, **kw: conn)
    monkeypatch.setattr(features, "przebieg", przebieg_ok)
    monkeypatch.setattr(sys, "argv",
                        ["features.py", "--bar", "4h,1h", "--once",
                         "--interval", "900", "--quiet"])

    assert features.main() == 0
    zapisy = _pulsy(conn)
    assert [r for r, _ in zapisy] == ["udany"], \
        f"puls bity raz na horyzont zamiast raz na cykl: {zapisy}"
    assert zapisy[0][1][:2] == ("features", 900)


def test_cechy_nie_bija_pulsu_gdy_pada_DRUGI_horyzont(monkeypatch):
    """Awaria jednego horyzontu nie pomija pozostałych w tym samym cyklu — ale
    cyklem udanym już nie jest."""
    from services.ingest import features

    conn = AtrapaPolaczenia()

    def przebieg(c, h, **kw):
        if h.bar == "1h":
            raise RuntimeError("brak świec 1h")
        return 100

    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(features.psycopg, "connect", lambda url, **kw: conn)
    monkeypatch.setattr(features, "przebieg", przebieg)
    monkeypatch.setattr(sys, "argv",
                        ["features.py", "--bar", "4h,1h", "--once", "--quiet"])

    assert features.main() == 1
    assert [r for r, _ in _pulsy(conn)] == ["awaria"]


class AtrapaGieldy:
    venue_code = "bybit"

    def __init__(self, *a, **kw):
        pass

    async def close(self):
        pass


def _market_argv(*reszta):
    return ["market_worker.py", "--once", "--interval", "60", *reszta]


def test_rynek_bije_puls_po_udanym_cyklu(monkeypatch):
    from services.ingest import market_worker

    conn = AtrapaPolaczenia([{"id": 1, "symbol": "BTCUSDT", "tradeable": True}])

    async def cykl_ok(*a, **kw):
        return {"swiece": 5, "orderbooki": 1, "bledy": 0, "bez_arkusza": 0}

    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(market_worker.psycopg, "connect", lambda url, **kw: conn)
    monkeypatch.setattr(market_worker, "adapter_danych", lambda conn, engine, mode="demo": AtrapaGieldy(api_key="", api_secret="", mode=mode))
    monkeypatch.setattr(market_worker, "cycle", cykl_ok)
    monkeypatch.setattr(sys, "argv", _market_argv())

    assert asyncio.run(market_worker.main()) == 0
    zapisy = _pulsy(conn)
    assert [r for r, _ in zapisy] == ["udany"]
    assert zapisy[0][1][:2] == ("market", 60)


def test_rynek_liczy_nieudane_pobranie_jako_awarie_cyklu(monkeypatch):
    """Ten worker jako jedyny nie miał ŻADNEGO licznika awarii.

    Bez tego instrument, którego giełda nie zna, mógłby milczeć miesiącami przy
    zerowym liczniku restartów — a puls meldowałby zdrowie przez cały ten czas.
    """
    from services.ingest import market_worker

    conn = AtrapaPolaczenia([{"id": 1, "symbol": "BTCUSDT", "tradeable": True}])

    async def cykl_z_bledem(*a, **kw):
        return {"swiece": 0, "orderbooki": 0, "bledy": 2, "bez_arkusza": 0}

    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(market_worker.psycopg, "connect", lambda url, **kw: conn)
    monkeypatch.setattr(market_worker, "adapter_danych", lambda conn, engine, mode="demo": AtrapaGieldy(api_key="", api_secret="", mode=mode))
    monkeypatch.setattr(market_worker, "cycle", cykl_z_bledem)
    monkeypatch.setattr(sys, "argv", _market_argv())

    assert asyncio.run(market_worker.main()) == 1, \
        "nieudane pobranie nie policzyło się jako awaria cyklu"
    assert [r for r, _ in _pulsy(conn)] == ["awaria"]


def test_rynek_konczy_proces_po_pieciu_awariach(monkeypatch, capsys):
    """Wpis w logu nie jest widocznością — logu poda nikt nie czyta o trzeciej
    w nocy. Restart JEST widoczny."""
    from services.ingest import market_worker

    conn = AtrapaPolaczenia([{"id": 1, "symbol": "BTCUSDT", "tradeable": True}])

    async def cykl_pada(*a, **kw):
        raise RuntimeError("giełda nie odpowiada")

    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(market_worker.psycopg, "connect", lambda url, **kw: conn)
    monkeypatch.setattr(market_worker, "adapter_danych", lambda conn, engine, mode="demo": AtrapaGieldy(api_key="", api_secret="", mode=mode))
    monkeypatch.setattr(market_worker, "cycle", cykl_pada)
    monkeypatch.setattr(sys, "argv", ["market_worker.py", "--interval", "0"])

    assert asyncio.run(market_worker.main()) == 1
    out = capsys.readouterr().out
    assert "kończę proces" in out
    assert "giełda nie odpowiada" in out
    assert [r for r, _ in _pulsy(conn)] == ["awaria"] * market_worker.POD_RZAD_MAX


def test_newsy_bija_puls_po_udanym_cyklu(monkeypatch):
    from services.ingest import news_worker

    conn = AtrapaPolaczenia()
    stat = {"kanaly": 4, "pobrane": 10, "nowe": 2, "przeszly_filtr": 0,
            "przeanalizowane": 0, "zapisane": 2, "zaleglosci": 0,
            "koszt": 0.0, "bledy": 1, "opisy_bledow": ["kanał X: 503"]}

    async def cykl_ok(*a, **kw):
        return stat

    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(news_worker.psycopg, "connect", lambda url, **kw: conn)
    monkeypatch.setattr(news_worker, "run_cycle", cykl_ok)
    monkeypatch.setattr(sys, "argv",
                        ["news_worker.py", "--once", "--bez-analizy",
                         "--interval", "300", "--quiet"])

    assert asyncio.run(news_worker.main()) == 0
    zapisy = _pulsy(conn)
    assert [r for r, _ in zapisy] == ["udany"]
    assert zapisy[0][1][:2] == ("news", 300)


def test_newsy_nie_bija_pulsu_po_przerwanym_cyklu(monkeypatch):
    from services.ingest import news_worker

    conn = AtrapaPolaczenia()

    async def cykl_pada(*a, **kw):
        raise RuntimeError("feedparser padł")

    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(news_worker.psycopg, "connect", lambda url, **kw: conn)
    monkeypatch.setattr(news_worker, "run_cycle", cykl_pada)
    monkeypatch.setattr(sys, "argv",
                        ["news_worker.py", "--once", "--bez-analizy", "--quiet"])

    assert asyncio.run(news_worker.main()) == 0
    zapisy = _pulsy(conn)
    assert [r for r, _ in zapisy] == ["awaria"], f"zapisy pulsu: {zapisy}"
    assert "feedparser padł" in zapisy[0][1][2]


def test_kazdy_wlaczony_worker_danych_bije_puls():
    """Kanarek na kompletność: piąty worker dopisany bez pulsu byłby dokładnie
    tym samym ślepym punktem, który te zmiany zamykają."""
    for nazwa in ("market", "history", "features", "news"):
        assert VALUES["workers"][nazwa]["enabled"], f"{nazwa} wyłączony"
    for plik in ("market_worker.py", "history_worker.py", "features.py",
                 "news_worker.py"):
        zrodlo = (KORZEN / "services" / "ingest" / plik).read_text(encoding="utf-8")
        assert "puls.bij(" in zrodlo, f"{plik} nie bije pulsu po udanym cyklu"
        assert "puls.zglos_awarie(" in zrodlo, f"{plik} nie zapisuje awarii cyklu"


# ------------------------------------------------------------------ migracja

def test_silnik_moze_zapisac_wlasny_puls():
    """Workery chodzą na roli `silnik`, a 0034 ustawił dla schematu `risk`
    domyślne nadanie samego SELECT-a. Bez jawnego GRANT-u pierwszy zapis pulsu
    kończyłby się InsufficientPrivilege."""
    assert re.search(
        r"GRANT[^;]*INSERT[^;]*ON risk\.worker_heartbeat[^;]*TO trader_silnik",
        MIGRACJA_SQL, re.I | re.S)
    assert re.search(
        r"GRANT[^;]*UPDATE[^;]*ON risk\.worker_heartbeat[^;]*TO trader_silnik",
        MIGRACJA_SQL, re.I | re.S)


def test_silnik_NIE_MOZE_skasowac_wiersza_pulsu():
    """Skasowanie wiersza jest cichszym sposobem uciszenia czujki niż jego
    sfałszowanie — nie zostawia nawet starego znacznika do porównania."""
    assert re.search(r"REVOKE[^;]*DELETE[^;]*ON risk\.worker_heartbeat"
                     r"[^;]*FROM trader_silnik", MIGRACJA_SQL, re.I | re.S)


def test_API_nie_moze_sfalszowac_pulsu():
    """0013 nadało roli API SELECT, INSERT i UPDATE na PRZYSZŁYCH tabelach
    schematu `risk`, więc bez tego odebrania nowa tabela byłaby zapisywalna
    z internetu. Sfałszowany puls to czujka meldująca zdrowie martwego potoku.
    """
    assert re.search(r"REVOKE[^;]*INSERT[^;]*ON risk\.worker_heartbeat"
                     r"[^;]*FROM trader_app", MIGRACJA_SQL, re.I | re.S)


def test_czujka_tylko_czyta_puls():
    """Proces diagnostyczny z prawem zapisu do obserwowanego stanu prędzej czy
    później zacznie mierzyć siebie zamiast systemu."""
    assert re.search(r"GRANT SELECT ON risk\.worker_heartbeat[^;]*TO trader_czujka",
                     MIGRACJA_SQL, re.I | re.S)
    assert re.search(r"REVOKE[^;]*(INSERT|UPDATE)[^;]*ON risk\.worker_heartbeat"
                     r"[^;]*FROM trader_czujka", MIGRACJA_SQL, re.I | re.S)


def test_puls_NIE_TRAFIL_do_tabeli_dozorcy():
    """0034 odebrał silnikowi wszelkie prawo do `risk.dozorca_heartbeat`, żeby
    martwy dozorca nie mógł wyglądać jak żywy. Dopisanie workerów do tamtej
    tabeli cofnęłoby tę własność — dla wygody zapisu w zupełnie innym
    mechanizmie."""
    assert "CREATE TABLE IF NOT EXISTS risk.worker_heartbeat" in MIGRACJA_SQL
    # Wzmianka w COMMENT ON jest uzasadnieniem; zmianą byłoby dopiero
    # zdanie wykonawcze dotykające tamtej tabeli.
    assert not re.search(
        r"\b(GRANT|REVOKE|ALTER|DROP|INSERT|UPDATE)\b[^;]*\bdozorca_heartbeat\b",
        MIGRACJA_SQL, re.I | re.S), \
        "migracja rusza puls dozorcy — to nie jest jej rola"


def test_interwal_jest_kolumna_a_nie_stala_w_czujce():
    """Gdyby próg brał się ze słownika w `dozor.py`, byłaby to druga kopia
    liczby z `--interval` w values.yaml. Rozjazd dwóch takich list zdarzył się
    już 2026-09-04 i kosztował 21 godzin martwych cech."""
    assert re.search(r"interwal_s\s+integer\s+NOT NULL", MIGRACJA_SQL, re.I), \
        "interwał przestał być kolumną wiersza pulsu"
    zrodlo = (KORZEN / "services" / "engine" / "dozor.py").read_text(encoding="utf-8")
    assert not re.search(r'"(market|history|features|news)"\s*:\s*\d+', zrodlo), \
        "czujka trzyma własny słownik interwałów workerów"


# --------------------------------------------------------------------- chart

def test_czujka_workerow_jest_URUCHAMIANA():
    """Czujka, której nikt nie odpala, jest równa jej brakowi — z tą różnicą,
    że wygląda na obecną."""
    assert "--co workery" in CRON_YAML


def test_workery_NIE_MAJA_livenessProbe():
    """DECYZJA, nie przeoczenie — uzasadnienie stoi w nagłówku workers.yaml.

    Probe restartuje kontener i NIKOMU o tym nie mówi, a zawieszenie na
    gnieździe bez limitu czasu wraca. Cicho leczony worker zostawia dziury
    w świecach i cechach, których nikt nie zbada — ten sam kształt co `|| true`
    w komendzie: awaria obsłużona i niewidoczna. Reakcją ma być powiadomienie
    z nazwą procesu, a nie milczący restart.

    Ten test pada, gdy ktoś probe dopisze — po to, żeby najpierw przeczytał
    powyższe i świadomie się z nim nie zgodził.
    """
    # Nagłówek `{{/* ... */}}` to uzasadnienie, nie manifest — patrzymy na to,
    # co naprawdę idzie do klastra.
    manifest = WORKERY_YAML.split("*/}}", 1)[1]
    assert "livenessProbe" not in manifest
    assert "DLACZEGO TU NIE MA `livenessProbe`" in WORKERY_YAML


def test_prog_ROSNIE_z_interwalem_workera_a_nie_jest_stala():
    """Jedna liczba dla wszystkich byłaby albo krzykliwa, albo bezużyteczna:
    `market` chodzi co 60 s, `history` co 1200 s — dwudziestokrotna różnica.

    Podłoga 900 s chroni przed alarmem przy każdym wdrożeniu (restart poda trwa
    minuty), ale POWYŻEJ niej próg musi iść za interwałem. Spłaszczenie go do
    samej podłogi zgłaszałoby `history` jako martwy po 15 minutach, czyli po
    niecałym jednym cyklu.
    """
    from services.engine.dozor import ocen_puls_workerow

    def wiersz(nazwa, interwal, wiek_s):
        return {"worker": nazwa, "interwal_s": interwal, "wiek_s": wiek_s,
                "awarii_pod_rzad": 0, "ostatni_blad": None,
                "proba_wiek_s": wiek_s}

    # 40 minut ciszy: dla `history` (co 1200 s) to niecale dwa cykle - norma.
    assert ocen_puls_workerow([wiersz("history", 1200, 2400)]) == []
    # Dla `market` (co 60 s) czterdziesci minut to czterdziesci cykli - awaria.
    assert len(ocen_puls_workerow([wiersz("market", 60, 2400)])) == 1

    # Ponizej podlogi milczymy nawet przy krotkim interwale: restart poda trwa
    # minuty, a czujka chodzi co dziesiec.
    assert ocen_puls_workerow([wiersz("market", 60, 600)]) == []
