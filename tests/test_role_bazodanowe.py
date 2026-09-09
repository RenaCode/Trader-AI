"""Rozdział uprawnień bazodanowych: czy silnik NAPRAWDĘ nie może zwolnić sobie
kill-switcha.

DLACZEGO OSOBNY PLIK

Do migracji 0034 silnik, dozorca ryzyka, czujka żywotności i próba pożarowa
łączyły się do bazy jedną rolą `trader_app`. Cała architektura bezpiecznika
(docs/06) stoi na zdaniu „nadzorca nie może dzielić losu z nadzorowanym" —
a dopóki nadzorowany ma poświadczenia nadzorcy, to zdanie jest komentarzem,
nie własnością systemu. Dokumentacja mówiła o tym wprost:

    Silnik i dozorca chodzą na tej samej roli bazodanowej, więc nic nie WYMUSZA,
    że silnik nie zwolni sobie kill-switcha. Dziś nie robi tego, bo nie ma
    takiego kodu — nie dlatego, że nie może.

„Nie ma takiego kodu" to stan, który znika przy pierwszym `UPDATE` dopisanym
w pośpiechu o trzeciej w nocy. Ten plik sprawdza, że znika już bezskutecznie.

DWIE WARSTWY SPRAWDZENIA

1. MODEL STATYCZNY (zawsze). Migracja jest parsowana do modelu uprawnień
   (rola, tabela) -> zbiór nadań, i to on jest odpytywany. To nie jest szukanie
   napisów: przeciwstawne nadanie dopisane gdziekolwiek w pliku zmienia model
   i test pada.

2. PRAWDZIWA BAZA (gdy jest). Przy ustawionym TEST_DB_URL testy łączą się do
   Postgresa, przełączają na każdą z ról i PRÓBUJĄ zwolnić kill-switcha.
   Model uprawnień jest tylko modelem; to sprawdzenie mierzy samego Postgresa.
   Wszystko dzieje się w transakcji zakończonej ROLLBACK.

   Uruchomienie (baza jednorazowa, NIGDY produkcyjna):

       docker run -d --name trader-role-test -e POSTGRES_DB=trader \\
           -e POSTGRES_USER=trader -e POSTGRES_PASSWORD=trader_dev \\
           -p 55432:5432 timescale/timescaledb-ha:pg17
       DATABASE_URL=postgresql://trader:trader_dev@localhost:55432/trader \\
           .venv/bin/python -B db/migrate.py
       TEST_DB_URL=postgresql://trader:trader_dev@localhost:55432/trader \\
           .venv/bin/python -B -m pytest -q tests/test_role_bazodanowe.py
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

KORZEN = Path(__file__).resolve().parents[1]
MIGRACJA = KORZEN / "db" / "migrations" / "0034_role_procesow.sql"
ROLA_APLIKACJI = KORZEN / "db" / "migrations" / "0013_rola_aplikacji.sql"
MIGRACJE = KORZEN / "db" / "migrations"
VALUES = KORZEN / "charts" / "trader" / "values.yaml"
SZABLONY = KORZEN / "charts" / "trader" / "templates"

# Tabele schematu `risk` znane w chwili pisania tej migracji. Lista jest
# sprawdzana z rzeczywistością (test niżej), więc nowa tabela w tym schemacie
# ZŁAMIE testy dopóki ktoś świadomie nie zdecyduje, kto ma do niej prawo.
# To jest zamierzone: schemat `risk` to miejsce, w którym „domyślne nadanie"
# jest tym samym co brak decyzji.
TABELE_RISK = {
    "limit_def", "limit_usage", "violation", "killswitch_event",
    "engine_heartbeat", "dozorca_heartbeat",
    # Puls workerów danych (0035). Nadania NIE są w 0034, tylko w tamtej
    # migracji — model niżej czyta wyłącznie 0034, więc pilnuje ich osobny
    # zestaw twierdzeń w tests/test_puls_workerow.py.
    "worker_heartbeat",
}

PRAWA = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES",
         "TRIGGER", "USAGE", "CONNECT", "EXECUTE", "ALL")
RE_PRAWO = re.compile(r"\b(" + "|".join(PRAWA) + r")\b\s*(\(([^)]*)\))?", re.I)
RE_DO = re.compile(r"\bDO\s+\$\$.*?\$\$", re.S | re.I)


# ---------------------------------------------------------------- model nadań

def _bez_komentarzy(sql: str) -> str:
    return "\n".join(l.split("--")[0] for l in sql.splitlines())


def _prawa(fragment: str) -> list[tuple[str, frozenset[str]]]:
    """Lista (prawo, kolumny). Pusty zbiór kolumn = nadanie na całą tabelę."""
    out = []
    for m in RE_PRAWO.finditer(fragment):
        kolumny = frozenset(
            k.strip().strip('"') for k in (m.group(3) or "").split(",") if k.strip())
        out.append((m.group(1).upper(), kolumny))
    return out


def _nazwy(fragment: str) -> list[str]:
    return [n.strip().strip('"').replace('"', "") for n in fragment.split(",") if n.strip()]


class Nadania:
    """Model uprawnień odtworzony z pliku migracji.

    Świadomie NIE jest pełną implementacją semantyki GRANT-a Postgresa — jest
    modelem tego, co plik nadaje i odbiera, czytanym w kolejności zapisu.
    Prawdziwą semantykę mierzą testy integracyjne na końcu pliku; ten model
    istnieje po to, żeby awaria była widoczna także bez bazy.
    """

    def __init__(self, sql: str):
        self.tabela: dict[tuple[str, str], set[str]] = {}
        self.kolumny: dict[tuple[str, str], dict[str, set[str]]] = {}
        self.domyslne: list[tuple[str, str, str]] = []   # (schemat, rola, prawo)
        self._stan_po_0013()
        self._czytaj(sql)

    def _stan_po_0013(self) -> None:
        """Stan zastany: co `trader_app` miało PRZED tą migracją.

        Bez tego kroku model zaczynałby od pustki, a każde twierdzenie o roli
        API byłoby prawdziwe z braku danych — test „API nie może już wstawić
        zatrzymania" przechodziłby także wtedy, gdyby migracja 0034 w ogóle
        niczego nie odbierała. Uprawnienia API pochodzą z 0013 i to je 0032
        zawęża, więc model musi je znać.
        """
        for t in TABELE_RISK:
            self.tabela[("trader_app", f"risk.{t}")] = {"SELECT", "INSERT", "UPDATE"}

    def _zastosuj(self, rola: str, obiekt: str, prawo: str,
                  kolumny: frozenset[str], nadaj: bool) -> None:
        klucz = (rola, obiekt)
        if kolumny:
            wpis = self.kolumny.setdefault(klucz, {}).setdefault(prawo, set())
            wpis.update(kolumny) if nadaj else wpis.difference_update(kolumny)
            return
        wpis = self.tabela.setdefault(klucz, set())
        if nadaj:
            wpis.add(prawo)
        else:
            wpis.discard(prawo)
            # REVOKE na całą tabelę zdejmuje także nadania kolumnowe — tak
            # działa Postgres i tak musi działać model, inaczej „odebrane"
            # znaczyłoby w teście co innego niż w bazie.
            self.kolumny.get(klucz, {}).pop(prawo, None)

    def _czytaj(self, sql: str) -> None:
        for zdanie in sql.split(";"):
            z = " ".join(zdanie.split())
            if not z:
                continue

            m = re.match(r"ALTER DEFAULT PRIVILEGES\s+(?:FOR ROLE \w+\s+)?"
                         r"IN SCHEMA (\w+)\s+GRANT (.+?) ON (TABLES|SEQUENCES|FUNCTIONS)"
                         r" TO (.+)$", z, re.I)
            if m:
                for prawo, _ in _prawa(m.group(2)):
                    for rola in _nazwy(m.group(4)):
                        self.domyslne.append((m.group(1).lower(), rola, prawo))
                continue

            m = re.match(r"(GRANT|REVOKE) (.+?) ON (.+?) (?:TO|FROM) (.+)$", z, re.I)
            if not m:
                continue
            nadaj = m.group(1).upper() == "GRANT"
            cel = m.group(3).strip()

            m_all = re.match(r"ALL (TABLES|SEQUENCES|FUNCTIONS) IN SCHEMA (\w+)$",
                             cel, re.I)
            if m_all:
                if m_all.group(1).upper() != "TABLES":
                    continue
                obiekty = [f"{m_all.group(2).lower()}.{t}"
                           for t in sorted(TABELE_RISK)] \
                    if m_all.group(2).lower() == "risk" else []
            elif re.match(r"(SCHEMA|DATABASE)\b", cel, re.I):
                continue
            else:
                obiekty = _nazwy(cel)

            for prawo, kolumny in _prawa(m.group(2)):
                for rola in _nazwy(m.group(4)):
                    for obiekt in obiekty:
                        self._zastosuj(rola, obiekt, prawo, kolumny, nadaj)

    # ------------------------------------------------------------- odpytywanie
    def na_tabeli(self, rola: str, obiekt: str) -> set[str]:
        return set(self.tabela.get((rola, obiekt), set()))

    def kolumnowe(self, rola: str, obiekt: str, prawo: str) -> set[str]:
        return set(self.kolumny.get((rola, obiekt), {}).get(prawo, set()))


@pytest.fixture(scope="module")
def sql() -> str:
    return _bez_komentarzy(MIGRACJA.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def nadania(sql: str) -> Nadania:
    return Nadania(RE_DO.sub("", sql))


@pytest.fixture(scope="module")
def values() -> dict:
    return yaml.safe_load(VALUES.read_text(encoding="utf-8"))


# ------------------------------------------------- warunek poprawności modelu

def test_petla_nadan_nie_dotyka_schematow_krytycznych(sql):
    """Bez tego cały ten plik czytałby połowę prawdy.

    Nadania dla schematów z danymi idą pętlą `DO $$ ... EXECUTE format(...)`,
    której model statyczny nie rozwija. Dopóki pętla omija `risk` i `app`,
    wszystkie nadania krytyczne są w pliku wypisane dosłownie i dają się
    przeczytać. Gdyby ktoś dopisał 'risk' do tablicy schematów, silnik dostałby
    UPDATE na tabeli zatrzymań przez pętlę — a testy niżej nadal by przechodziły,
    bo nie widzą wnętrza pętli. Dlatego ten test jest pierwszy.
    """
    for blok in RE_DO.findall(sql):
        for schemat in ("'risk'", "'app'"):
            assert schemat not in blok, (
                f"pętla nadań objęła schemat {schemat} — nadania krytyczne "
                f"przestały być czytelne w pliku, a model uprawnień w testach "
                f"przestał być pełny")


def test_stan_zastany_zgadza_sie_z_migracja_0013():
    """Model zaczyna od tego, co rola API dostała w 0013. Gdyby tamta migracja
    nadawała co innego, wszystkie twierdzenia o zawężeniu roli API mierzyłyby
    wymyślony punkt wyjścia."""
    tresc = ROLA_APLIKACJI.read_text(encoding="utf-8")
    assert "'risk'" in tresc, "0013 nie obejmuje już schematu risk"
    assert "GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA %I TO trader_app" \
        in tresc, "0013 nadaje roli API co innego niż zakłada model"


def test_lista_tabel_risk_zgadza_sie_ze_schematem():
    """Nowa tabela w schemacie `risk` ma wymusić decyzję, kto ma do niej prawo.

    Ten test pada przy jej dodaniu — i to jest jego cel. `risk` to jedyny
    schemat, w którym „nikt nie pomyślał o uprawnieniach" znaczy „bezpiecznik
    jest zapisywalny przez to, co ma zatrzymywać".
    """
    znalezione = set()
    for plik in sorted(MIGRACJE.glob("*.sql")):
        znalezione |= set(re.findall(
            r"CREATE TABLE (?:IF NOT EXISTS )?risk\.(\w+)",
            plik.read_text(encoding="utf-8"), re.I))
    assert znalezione == TABELE_RISK, (
        f"schemat risk się zmienił: doszło {znalezione - TABELE_RISK}, "
        f"zniknęło {TABELE_RISK - znalezione}. Uzupełnij TABELE_RISK i nadaj "
        f"(albo świadomie NIE nadaj) uprawnienia w migracji.")


# ------------------------------------------------------ czego nie może silnik

def test_silnik_NIE_MOZE_zwolnic_killswitcha(nadania):
    """Własność, dla której powstała migracja 0034.

    Bez niej proces, który właśnie traci pieniądze, może sam sobie zdjąć
    hamulec — jedną linijką, bez śladu innego niż wiersz, którego nikt nie
    czyta w nocy.
    """
    prawa = nadania.na_tabeli("trader_silnik", "risk.killswitch_event")
    assert "UPDATE" not in prawa, "silnik może przepisać wiersz zatrzymania"
    assert "DELETE" not in prawa, "silnik może skasować wiersz zatrzymania"
    assert "TRUNCATE" not in prawa
    assert not nadania.kolumnowe("trader_silnik", "risk.killswitch_event", "UPDATE"), (
        "silnik dostał UPDATE na kolumnie tabeli zatrzymań — nadanie kolumnowe "
        "na `released_at` jest zwolnieniem kill-switcha tak samo jak tabelowe")


def test_silnik_NADAL_moze_zazadac_zatrzymania(nadania):
    """Kanarek do poprzedniego: odebranie wszystkiego też przeszłoby tamten
    test, a silnik ocenia limity w każdym cyklu (obrona w głąb, docs/06)
    i musi umieć zażądać zatrzymania. Rola bez INSERT-u zamieniłaby
    zabezpieczenie w wyjątek w logu."""
    prawa = nadania.na_tabeli("trader_silnik", "risk.killswitch_event")
    assert "SELECT" in prawa, "silnik nie odczyta, czy ma wstrzymać wejścia"
    assert "INSERT" in prawa, "silnik nie zażąda zatrzymania"


def test_silnik_NIE_MOZE_zmienic_limitu(nadania):
    """Podniesienie progu obsunięcia z 10% na 90% zatrzymuje bezpiecznik
    skuteczniej niż zwolnienie zdarzenia — i, w odróżnieniu od niego, nie
    zostawia wiersza w dzienniku ani nie wysyła powiadomienia."""
    assert nadania.na_tabeli("trader_silnik", "risk.limit_def") == {"SELECT"}
    assert not nadania.kolumnowe("trader_silnik", "risk.limit_def", "UPDATE")


def test_silnik_NIE_MOZE_bic_pulsu_dozorcy(nadania):
    """Puls dozorcy jest jedynym dowodem, że nadzorca żyje. Gdyby silnik mógł
    go odświeżać, martwy dozorca wyglądałby dla czujki dokładnie jak żywy."""
    assert not nadania.na_tabeli("trader_silnik", "risk.dozorca_heartbeat")
    assert not nadania.kolumnowe("trader_silnik", "risk.dozorca_heartbeat", "UPDATE")


def test_domyslne_uprawnienia_w_risk_daja_wylacznie_odczyt(nadania):
    """Rozdział, który trzyma się do najbliższej migracji, nie jest rozdziałem.

    ALTER DEFAULT PRIVILEGES z prawem zapisu oznaczałby, że KAŻDY przyszły
    mechanizm bezpieczeństwa w tym schemacie dostaje procesowi tła prawo zapisu
    automatycznie — i że nikt się o tym nie dowie.
    """
    zapisy = [(r, p) for s, r, p in nadania.domyslne
              if s == "risk" and p in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALL")]
    assert not zapisy, f"domyślne prawo zapisu w schemacie risk: {zapisy}"


# ------------------------------------------------------- dozorca: co i tylko co

def test_dozorca_zwalnia_ale_NIE_PRZEPISUJE_historii(nadania):
    """Dziennik zatrzymań jest jedynym źródłem odpowiedzi na pytanie „czy
    bezpiecznik działał". Proces, który potrafi przepisać `reason`
    i `triggered_at`, przestaje być dowodem — staje się narracją."""
    kol = nadania.kolumnowe("trader_dozorca", "risk.killswitch_event", "UPDATE")
    assert kol == {"released_at", "released_auto", "release_note"}, (
        f"dozorca może zapisać {kol} — zwolnienie to trzy kolumny, nie wiersz")
    assert "UPDATE" not in nadania.na_tabeli("trader_dozorca", "risk.killswitch_event")


def test_dozorca_NIE_MOZE_zmienic_limitu(nadania):
    """Wznowienie ma iść „na dowodzie, że warunek ustał". Najprostszym sposobem
    spełnienia warunku jest przesunięcie progu — i wtedy dowód jest własnym
    produktem dozorcy."""
    assert nadania.na_tabeli("trader_dozorca", "risk.limit_def") == {"SELECT"}
    assert not nadania.kolumnowe("trader_dozorca", "risk.limit_def", "UPDATE")


def test_dozorca_NIE_MOZE_handlowac(sql):
    """Rozdział działa w obie strony. Gdyby nadzorca mógł pisać do exec.*, błąd
    w nim mógłby zamienić się w zlecenie — mechanizm powołany do ograniczania
    ryzyka stałby się jego źródłem."""
    assert re.search(
        r"REVOKE\s+INSERT,\s*UPDATE,\s*DELETE\s+ON\s+exec\.order[^;]*?"
        r"FROM\s+trader_dozorca", sql, re.I | re.S), \
        "brak jawnego odebrania zapisu na zleceniach dla dozorcy"
    assert not re.search(
        r"GRANT[^;]*?(INSERT|UPDATE)[^;]*?ON[^;]*?exec\.[^;]*?TO[^;]*?trader_dozorca",
        sql, re.I | re.S), "dozorca dostał prawo zapisu w schemacie exec"


def test_dozorca_NIE_MA_dostepu_do_kluczy_gieldy(sql):
    """docs/06 odrzuca awaryjne domykanie pozycji przez dozorcę, bo „wymaga
    kluczy giełdy w kolejnym procesie, co poszerza powierzchnię ataku". Skoro
    taka jest decyzja, egzekwuje ją baza, a nie dokumentacja."""
    assert not re.search(
        r"GRANT[^;]*?ON[^;]*?app\.api_credential[^;]*?TO[^;]*?trader_dozorca",
        sql, re.I | re.S)


# --------------------------------------------------------------- próba pożarowa

def test_proba_rusza_WARTOSC_ale_nie_wylacza_limitu(nadania):
    """Podniesiona wartość wraca w bloku `finally`, więc przeżywa błąd
    i przerwanie procesu. `enabled = false` ustawione przez proces, który zginął,
    nie wróci nigdy — a wyłączony limit wygląda dokładnie jak limit
    niespełniony, więc nikt tego nie zauważy do chwili, w której byłby
    potrzebny."""
    kol = nadania.kolumnowe("trader_proba", "risk.limit_def", "UPDATE")
    assert kol == {"value", "updated_at"}, f"próba może zapisać {kol}"
    assert "UPDATE" not in nadania.na_tabeli("trader_proba", "risk.limit_def")


def test_proba_NIE_MOZE_wstawic_wlasnego_zatrzymania(nadania):
    """Próba ma czekać, aż SILNIK sam wykryje przekroczenie. Gdyby mogła wstawić
    wiersz sama, mierzyłaby własny INSERT zamiast łańcucha ogniw 1-2."""
    assert "INSERT" not in nadania.na_tabeli("trader_proba", "risk.killswitch_event")


def _kolumny_zapisywane(plik: Path, tabela: str) -> set[str]:
    """Kolumny, które kod TEGO pliku ustawia w `UPDATE <tabela> SET ...`.

    Przez AST, nie przez czytanie pliku jako tekstu: zapytania są w źródle
    posklejane z kilku literałów, więc w surowym tekście między `SET` a nazwą
    kolumny stoją cudzysłowy i wcięcia. Parser Pythona skleja je za nas.
    """
    import ast as _ast

    kolumny: set[str] = set()
    for wezel in _ast.walk(_ast.parse(plik.read_text(encoding="utf-8"))):
        if not (isinstance(wezel, _ast.Constant) and isinstance(wezel.value, str)):
            continue
        for m in re.finditer(rf"UPDATE\s+{re.escape(tabela)}\s+SET\s+(.*?)"
                             rf"(?:\s+WHERE\b|$)", wezel.value, re.I | re.S):
            kolumny |= {k.strip('" ') for k in
                        re.findall(r"([\w\"]+)\s*=", m.group(1))}
    return kolumny


def test_proba_zapisuje_WYLACZNIE_kolumny_na_ktore_ma_nadanie(nadania):
    """Klasa błędu, nie jedna literówka: kod pisze do kolumny, której rola nie
    ma prawa ruszyć.

    Rozdział uprawnień z 0034 jest tu ostrzejszy niż zdrowy rozsądek autora
    zapytania — i o to chodzi. Ale nadanie i kod są w dwóch różnych plikach,
    a Postgres mówi o rozjeździe dopiero przy WYKONANIU, czyli w środku
    prawdziwego zadziałania kill-switcha. Zapytanie odrzucone w tym miejscu
    zostawia zdarzenie NIEZWOLNIONE, czyli zatrzymany handel: próba pożarowa
    sama staje się awarią, przed którą miała ostrzegać.

    `released_by` jest tu kluczowe. To bigint z kluczem obcym do app."user";
    nadanie go pomija świadomie, bo próba nie jest osobą i nie ma ID, którym
    mogłaby się podpisać bez kłamstwa.
    """
    zapisywane = _kolumny_zapisywane(
        KORZEN / "services" / "engine" / "proba_killswitcha.py",
        "risk.killswitch_event")
    assert zapisywane, "nie znaleziono w próbie żadnego zapisu do zdarzenia"
    wolno = nadania.kolumnowe("trader_proba", "risk.killswitch_event", "UPDATE")
    assert zapisywane <= wolno, (
        f"próba zapisuje {sorted(zapisywane - wolno)}, a 0034 daje jej "
        f"UPDATE tylko na {sorted(wolno)} — zapytanie padnie na produkcji "
        f"w środku zadziałania kill-switcha")

    # To samo dla limitu: podniesienie progu i jego przywrócenie.
    prog = _kolumny_zapisywane(
        KORZEN / "services" / "engine" / "proba_killswitcha.py", "risk.limit_def")
    assert prog and prog <= nadania.kolumnowe(
        "trader_proba", "risk.limit_def", "UPDATE"), \
        f"próba zapisuje w limicie {sorted(prog)} — poza nadaniem"


# ---------------------------------------------------------------- rola API

def test_api_zwalnia_recznie_ale_NIE_udaje_automatu(nadania):
    """`released_auto` świadomie poza nadaniem dla API: zwolnienie przez
    człowieka ma zostać zwolnieniem przez człowieka. Gdyby API mogło ustawić tę
    flagę, pytanie „czy system wrócił do handlu sam" przestałoby być
    rozstrzygalne z bazy."""
    kol = nadania.kolumnowe("trader_app", "risk.killswitch_event", "UPDATE")
    assert kol == {"released_at", "released_by", "release_note"}, \
        f"API może zapisać {kol}"
    assert "released_auto" not in kol


def test_api_stracilo_zbedny_zapis_na_ryzyku(nadania):
    """API jest jedyną z tych ról wystawioną na internet, więc jedyną, w której
    zbędne uprawnienie ma czym zostać wykorzystane. Nie wstawia zatrzymań i nie
    zmienia limitów — więc nie ma do tego prawa."""
    assert "INSERT" not in nadania.na_tabeli("trader_app", "risk.killswitch_event")
    assert not nadania.kolumnowe("trader_app", "risk.limit_def", "UPDATE")
    assert "UPDATE" not in nadania.na_tabeli("trader_app", "risk.limit_def")


# ----------------------------------------------------- zgodność chartu z bazą

def test_kazdy_proces_ma_wlasna_role_i_wlasny_sekret(values):
    """Rozdział uprawnień jest wart tyle, ile rozdział poświadczeń: wspólne
    hasło znaczyłoby, że proces o węższych nadaniach po prostu łączy się tą
    rolą, która ma szersze."""
    role = values["db"]["role"]
    uzytkownicy = [r["user"] for r in role.values()]
    klucze = [r["secretKey"] for r in role.values()]
    assert len(set(uzytkownicy)) == len(uzytkownicy), "dwa procesy dzielą rolę"
    assert len(set(klucze)) == len(klucze), "dwa procesy dzielą hasło"
    assert values["db"]["appUsername"] not in uzytkownicy, \
        "proces tła chodzi rolą API"


def test_role_z_chartu_istnieja_w_migracji(values, sql):
    for rola in (r["user"] for r in values["db"]["role"].values()):
        assert re.search(rf"'{rola}'", sql), \
            f"chart używa roli {rola}, której migracja nie tworzy"


def test_migrator_ustawia_haslo_kazdej_roli(values):
    """Rola bez hasła się nie zaloguje. Brak tu wpisu znaczy pod, który nie
    wstaje — awarię widoczną, ale zupełnie niepowiązaną z przyczyną."""
    from db.migrate import ROLE_HASLA
    for rola in values["db"]["role"].values():
        assert ROLE_HASLA.get(rola["user"]) == rola["secretKey"], (
            f"db/migrate.py nie ustawi hasła roli {rola['user']} "
            f"ze zmiennej {rola['secretKey']}")


def test_dozorca_NIE_chodzi_rola_silnika(values):
    """Nadzorca zwalniający kill-switcha poświadczeniami nadzorowanego nie jest
    nadzorcą — jest tą samą awarią o jeden poziom wyżej."""
    assert values["workers"]["dozorca"]["dbRole"] == "dozorca"
    for nazwa, w in values["workers"].items():
        if nazwa != "dozorca":
            assert w.get("dbRole", "silnik") != "dozorca", (
                f"worker {nazwa} dostał poświadczenia dozorcy, więc mógłby "
                f"zwolnić kill-switcha")


@pytest.mark.parametrize("plik,rola", [
    ("workers.yaml", None),            # rola per worker, domyślnie silnik
    ("proba-cron.yaml", "proba"),
    ("zywotnosc-cron.yaml", "czujka"),
    ("trainer-cron.yaml", "silnik"),
])
def test_szablony_uzywaja_rol_procesow(plik, rola):
    """Każdy pod bierze poświadczenia ze swojej roli. Zapomniany szablon wraca
    na `trader_app` i cicho odzyskuje prawo zwolnienia kill-switcha."""
    tresc = (SZABLONY / plik).read_text(encoding="utf-8")
    assert "trader.rolaDbEnv" in tresc, f"{plik} nie używa roli procesu"
    assert "trader.appDatabaseUrl" not in tresc, \
        f"{plik} nadal łączy się rolą API"
    assert "APP_DB_PASSWORD" not in tresc, f"{plik} nadal bierze hasło roli API"
    if rola:
        assert f'"rola" "{rola}"' in tresc, f"{plik} nie używa roli {rola}"


def test_domysl_dla_workera_idzie_w_strone_wezszych_uprawnien():
    """Worker, który zapomni zadeklarować rolę, ma dostać tę BEZ prawa
    zwolnienia kill-switcha. Domyślnie szersze uprawnienie znaczyłoby, że
    zabezpieczenie znika przez przeoczenie, a nie przez decyzję."""
    tresc = (SZABLONY / "workers.yaml").read_text(encoding="utf-8")
    assert 'default "silnik"' in tresc


# ============================================================================
# TESTY NA PRAWDZIWEJ BAZIE
#
# Model uprawnień wyżej jest modelem. Poniższe testy pytają samego Postgresa —
# przełączają się na rolę procesu (SET ROLE, więc bez rozdawania haseł) i
# PRÓBUJĄ zrobić to, czego nie wolno. Wszystko w transakcji z ROLLBACK.
# ============================================================================

TEST_DB_URL = os.environ.get("TEST_DB_URL")
wymaga_bazy = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="ustaw TEST_DB_URL na jednorazową bazę z zastosowanymi migracjami")


@pytest.fixture
def jako():
    """Wykonuje SQL jako wskazana rola i zwraca None albo nazwę błędu.

    ROLLBACK na końcu: te testy piszą do tabel ryzyka, więc nie mogą zostawić
    po sobie ani wiersza. Baza i tak ma być jednorazowa.
    """
    import psycopg
    with psycopg.connect(TEST_DB_URL) as conn:
        def wykonaj(rola: str, sql: str):
            with conn.transaction(force_rollback=True):
                conn.execute(f"SET LOCAL ROLE {rola}")
                try:
                    conn.execute(sql)
                    return None
                except psycopg.errors.InsufficientPrivilege as e:
                    return type(e).__name__
        yield wykonaj


@pytest.fixture
def ma_prawo():
    """Pyta katalog Postgresa wprost, czy rola ma dane prawo do tabeli.

    Potrzebne obok prób zapisu, bo próba może paść z INNEGO powodu niż ten,
    o który pytamy — `INSERT INTO exec.order` odbija się o brak prawa do
    sekwencji `order_id_seq`, zanim dojdzie do sprawdzenia prawa do tabeli.
    Test opierający się wyłącznie na wyniku próby byłby wtedy zielony także
    wtedy, gdyby ktoś nadał dozorcy INSERT na zleceniach. Sprawdzone kanarkiem:
    samo `GRANT INSERT ON exec.order` nie łamało testu, dopóki nie doszło to
    pytanie.
    """
    import psycopg
    with psycopg.connect(TEST_DB_URL) as conn:
        def pytaj(rola: str, obiekt: str, prawo: str) -> bool:
            return conn.execute("SELECT has_table_privilege(%s, %s, %s)",
                                (rola, obiekt, prawo)).fetchone()[0]
        yield pytaj


ZATRZYMANIE = ("INSERT INTO risk.killswitch_event (engine, mode, reason, limit_name)"
               " VALUES ('crypto','demo','test roli','min_equity')")
ZWOLNIENIE = ("UPDATE risk.killswitch_event SET released_at = now(),"
              " released_auto = true, release_note = 'test' WHERE released_at IS NULL")


@wymaga_bazy
def test_BAZA_silnik_zada_zatrzymania_ale_go_nie_zwolni(jako):
    """Najważniejsze zdanie tej migracji, sprawdzone na Postgresie."""
    assert jako("trader_silnik", ZATRZYMANIE) is None, \
        "silnik nie może zażądać zatrzymania — bezpiecznik jest martwy"
    assert jako("trader_silnik", ZWOLNIENIE) == "InsufficientPrivilege", \
        "SILNIK ZWOLNIŁ SOBIE KILL-SWITCHA"
    assert jako("trader_silnik", "DELETE FROM risk.killswitch_event") \
        == "InsufficientPrivilege"


@wymaga_bazy
@pytest.mark.parametrize("opis,sql", [
    ("podniesienie progu", "UPDATE risk.limit_def SET value = 999999"),
    ("wyłączenie limitu", "UPDATE risk.limit_def SET enabled = false"),
    ("zmiana akcji limitu", "UPDATE risk.limit_def SET action = 'block'"),
    ("skasowanie limitu", "DELETE FROM risk.limit_def"),
])
def test_BAZA_silnik_nie_rusza_limitow(jako, opis, sql):
    """Każdy z tych zapisów wyłącza bezpiecznik ciszej niż zwolnienie
    zdarzenia — bez wiersza w dzienniku i bez powiadomienia."""
    assert jako("trader_silnik", sql) == "InsufficientPrivilege", opis


@wymaga_bazy
def test_BAZA_silnik_nie_udaje_pulsu_dozorcy(jako):
    assert jako("trader_silnik",
                "INSERT INTO risk.dozorca_heartbeat (nazwa, last_beat_at)"
                " VALUES ('fejk', now())") == "InsufficientPrivilege"


@wymaga_bazy
def test_BAZA_silnik_nie_czyta_hasel(jako):
    """Skaner wiadomości i pobieranie danych rynkowych chodzą tą rolą i dotykają
    treści z internetu. Hash hasła właściciela jest najdroższą możliwą
    konsekwencją błędu w parsowaniu cudzej treści."""
    assert jako("trader_silnik", 'SELECT password_hash FROM app."user"') \
        == "InsufficientPrivilege"
    assert jako("trader_silnik", 'SELECT id FROM app."user"') is None


@wymaga_bazy
def test_BAZA_dozorca_zwalnia_ale_nie_przepisuje_powodu(jako):
    assert jako("trader_dozorca", ZWOLNIENIE) is None, \
        "dozorca nie może zwolnić — autonomiczne wznowienie przestało działać"
    assert jako("trader_dozorca",
                "UPDATE risk.killswitch_event SET reason = 'przepisane'") \
        == "InsufficientPrivilege"
    assert jako("trader_dozorca",
                "UPDATE risk.killswitch_event SET triggered_at = now()") \
        == "InsufficientPrivilege"


@wymaga_bazy
@pytest.mark.parametrize("tabela", ["exec.order", "exec.fill", "exec.position",
                                    "exec.equity_point"])
@pytest.mark.parametrize("prawo", ["INSERT", "UPDATE", "DELETE"])
def test_BAZA_dozorca_nie_handluje(ma_prawo, tabela, prawo):
    """Rozdział działa w obie strony: nadzorca, który potrafi zapisać zlecenie
    albo pozycję, może zamienić własny błąd w transakcję."""
    assert not ma_prawo("trader_dozorca", tabela, prawo), \
        f"dozorca ma {prawo} na {tabela} — nadzorca może handlować"


@wymaga_bazy
def test_BAZA_dozorca_nie_zapisze_zlecenia_ani_pozycji(jako):
    """To samo od strony próby, nie katalogu — bo liczy się skutek, a nie
    tylko wpis w pg_class."""
    assert jako("trader_dozorca",
                "INSERT INTO exec.order (engine, mode, instrument_id,"
                " client_order_id, side, order_type, qty, status)"
                " VALUES ('crypto','demo',1,'test','buy','market',1,'new')") \
        == "InsufficientPrivilege"
    assert jako("trader_dozorca", "UPDATE exec.position SET qty = 0") \
        == "InsufficientPrivilege"


@wymaga_bazy
def test_BAZA_dozorca_nie_widzi_kluczy_gieldy(jako, ma_prawo):
    """docs/06 odrzuca awaryjne domykanie pozycji przez dozorcę, bo wymagałoby
    kluczy giełdy w kolejnym procesie. Baza egzekwuje tę decyzję."""
    assert not ma_prawo("trader_dozorca", "app.api_credential", "SELECT")
    assert jako("trader_dozorca", "SELECT key_enc FROM app.api_credential") \
        == "InsufficientPrivilege"


@wymaga_bazy
def test_BAZA_czujka_nie_zapisuje_niczego_w_ryzyku(jako):
    """Diagnoza, która potrafi zmienić stan, prędzej czy później go zmieni —
    i wtedy mierzy siebie zamiast systemu."""
    assert jako("trader_czujka", "SELECT count(*) FROM risk.killswitch_event") is None
    for sql in (ZATRZYMANIE, ZWOLNIENIE,
                "UPDATE risk.engine_heartbeat SET status = 'halted'"):
        assert jako("trader_czujka", sql) == "InsufficientPrivilege"


@wymaga_bazy
def test_BAZA_proba_podnosi_prog_ale_nie_wylacza_limitu(jako):
    assert jako("trader_proba",
                "UPDATE risk.limit_def SET value = 1, updated_at = now()") is None
    assert jako("trader_proba", "UPDATE risk.limit_def SET enabled = false") \
        == "InsufficientPrivilege"
    assert jako("trader_proba", ZATRZYMANIE) == "InsufficientPrivilege"


@wymaga_bazy
def test_BAZA_api_zwalnia_recznie_i_nie_udaje_automatu(jako):
    assert jako("trader_app",
                "UPDATE risk.killswitch_event SET released_at = now(),"
                " release_note = 'reczne'") is None
    assert jako("trader_app",
                "UPDATE risk.killswitch_event SET released_auto = true") \
        == "InsufficientPrivilege"
    assert jako("trader_app", ZATRZYMANIE) == "InsufficientPrivilege"
