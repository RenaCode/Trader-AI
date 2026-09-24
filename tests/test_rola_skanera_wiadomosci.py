"""Skaner wiadomości i klucze giełdy: czy najbardziej wystawiony proces może je odczytać.

CO TEN PLIK PILNUJE

`trader-news` jest jedynym procesem w systemie, który przetwarza treść od
obcych: parsuje dziesięć kanałów RSS (XML i HTML z niekontrolowanych źródeł)
i obsługuje odpowiedzi zewnętrznych API modeli językowych. Do migracji 0052
chodził rolą `trader_silnik`, a ta rola ma `SELECT` na `app.api_credential` —
i jednocześnie pod dostawał w środowisku `SECRETS_KEY`. Szyfrogram kluczy
giełdy i klucz, którym się go odszyfrowuje, w jednej przestrzeni adresowej:
wykonanie kodu w tym procesie dawało klucz giełdy w jawnej postaci bez drugiego
kroku.

Skaner nie handluje. W całym `services/ingest/` nie ma ani jednego odwołania do
`app.api_credential` — te tabele czyta wyłącznie silnik (`loop.py`,
`uzgodnienie.py`, `waluta.py`). Rola z dostępem do nich była mu nadana przy
okazji, a nie z potrzeby.

DLACZEGO TEST, A NIE SAMA MIGRACJA

Migracja 0052 zakłada rolę i nadaje jej wąski zestaw praw, ale nie broni się
przed niczym: rozjazd wraca przy pierwszym `dbRole` dopisanym bez namysłu albo
pierwszym `GRANT`, który „odblokuje coś, co nie działa". Trzy twierdzenia
poniżej są tym, co ten rozjazd wyłapuje:

  1. żadna migracja nie daje `trader_news` dostępu do `app.api_credential`,
  2. rola, którą chart wpina workerowi newsów, nie ma tego dostępu ŻADNĄ drogą,
  3. lista schematów i tabel w zapytaniach workera mieści się w jego nadaniach.

Twierdzenie (2) jest tu najważniejsze i celowo nie wymienia roli z nazwy: pilnuje
WŁASNOŚCI, a nie literówki. Wpięcie workerowi `dbRole: silnik` z powrotem — albo
jakiejkolwiek innej roli widzącej szyfrogram — łamie je tak samo.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

KORZEN = Path(__file__).resolve().parents[1]
MIGRACJE = KORZEN / "db" / "migrations"
MIGRACJA = MIGRACJE / "0052_rola_skanera_wiadomosci.sql"
VALUES = KORZEN / "charts" / "trader" / "values.yaml"

ROLA = "trader_news"
# Nazwa workera w `.Values.workers`. Tą samą nazwą chart tworzy Deployment
# `trader-news` i tą samą worker melduje się w `risk.worker_heartbeat`.
WORKER = "news"

# Pliki, z których składa się proces skanera. `puls.py` jest tu, bo to on pisze
# `risk.worker_heartbeat` — nadanie na tę tabelę musi pokryć zapytanie, które
# fizycznie wykonuje pod `trader-news`, a nie tylko to, co widać w jego module.
ZRODLA_SKANERA = [
    KORZEN / "services" / "ingest" / "news_worker.py",
    KORZEN / "services" / "ingest" / "llm.py",
    KORZEN / "services" / "ingest" / "puls.py",
]

# Tabele z zaszyfrowanymi poświadczeniami giełdy. To one są stawką całego pliku.
KLUCZE_GIELDY = "app.api_credential"


# ------------------------------------------------------------- model nadań

def _bez_komentarzy(sql: str) -> str:
    return "\n".join(l.split("--")[0] for l in sql.splitlines())


def _zdania(sql: str) -> list[str]:
    return [" ".join(z.split()) for z in _bez_komentarzy(sql).split(";") if z.split()]


def _role(fragment: str) -> set[str]:
    return {r.strip().strip('"') for r in fragment.split(",") if r.strip()}


def role_z_dostepem_do(obiekt: str) -> set[str]:
    """Role, które po WSZYSTKICH migracjach mają cokolwiek na danym obiekcie.

    Czytane z całego katalogu migracji w kolejności numerów, a nie z jednego
    pliku. Nadanie dopisane w przyszłej migracji ma tu być widoczne — inaczej
    ten plik opisywałby stan sprzed niej i przechodziłby, gdy rozdział już
    przestał obowiązywać. To jest ta sama pułapka, którą `MIGRACJE_NADAN`
    w tests/test_role_bazodanowe.py rozwiązuje wyliczeniem; tu wyliczenia nie
    ma celowo, bo twierdzenie jest NEGATYWNE i pominięty plik znaczyłby ciszę
    zamiast awarii.
    """
    ma: set[str] = set()
    wzor = re.compile(
        r"^(GRANT|REVOKE)\s+(.+?)\s+ON\s+(.+?)\s+(?:TO|FROM)\s+(.+)$", re.I)
    for plik in sorted(MIGRACJE.glob("*.sql")):
        for z in _zdania(plik.read_text(encoding="utf-8")):
            m = wzor.match(z)
            if not m:
                continue
            cele = {c.strip().strip('"') for c in m.group(3).split(",")}
            if obiekt not in cele:
                continue
            if m.group(1).upper() == "GRANT":
                ma |= _role(m.group(4))
            else:
                ma -= _role(m.group(4))
    return ma


class NadaniaRoli:
    """Co migracja 0052 daje roli `trader_news` — schematy, tabele, widoki."""

    def __init__(self, sql: str):
        self.schematy: set[str] = set()
        self.obiekty: dict[str, set[str]] = {}
        self.wszystkie_tabele: dict[str, set[str]] = {}
        self._czytaj(sql)

    def _czytaj(self, sql: str) -> None:
        for z in _zdania(sql):
            m = re.match(r"(GRANT|REVOKE)\s+(.+?)\s+ON\s+(.+?)\s+(?:TO|FROM)\s+(.+)$",
                         z, re.I)
            if not m or ROLA not in _role(m.group(4)):
                continue
            nadaj = m.group(1).upper() == "GRANT"
            prawa = {p.upper() for p in re.findall(
                r"\b(SELECT|INSERT|UPDATE|DELETE|TRUNCATE|USAGE|EXECUTE|ALL)\b",
                m.group(2), re.I)}
            cel = m.group(3).strip()

            m_schemat = re.match(r"SCHEMA\s+(.+)$", cel, re.I)
            if m_schemat:
                for s in _role(m_schemat.group(1)):
                    self.schematy.add(s) if nadaj else self.schematy.discard(s)
                continue

            m_all = re.match(r"ALL TABLES IN SCHEMA\s+(\w+)$", cel, re.I)
            if m_all:
                wpis = self.wszystkie_tabele.setdefault(m_all.group(1).lower(), set())
                wpis.update(prawa) if nadaj else wpis.difference_update(prawa)
                continue

            if re.match(r"(ALL SEQUENCES|ALL FUNCTIONS|DATABASE)\b", cel, re.I):
                continue

            for obiekt in _role(cel):
                wpis = self.obiekty.setdefault(obiekt.lower(), set())
                wpis.update(prawa) if nadaj else wpis.difference_update(prawa)

    def prawa(self, obiekt: str) -> set[str]:
        """Prawa na obiekcie, z nadania własnego ALBO z `ALL TABLES IN SCHEMA`."""
        schemat = obiekt.split(".")[0]
        return (self.obiekty.get(obiekt, set())
                | self.wszystkie_tabele.get(schemat, set()))


@pytest.fixture(scope="module")
def nadania() -> NadaniaRoli:
    return NadaniaRoli(MIGRACJA.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def values() -> dict:
    return yaml.safe_load(VALUES.read_text(encoding="utf-8"))


# ============================================================================
# 1. WŁASNOŚĆ, DLA KTÓREJ POWSTAŁ TEN PLIK
# ============================================================================

def test_kanarek_ktos_te_klucze_jednak_czyta():
    """Bez tego wszystkie twierdzenia niżej byłyby prawdziwe z braku danych.

    Gdyby `app.api_credential` nie występowało w migracjach w ogóle — bo ktoś
    zmienił nazwę tabeli albo parser przestał łapać zdania GRANT — zbiór ról
    z dostępem byłby pusty i KAŻDE „rola X tego nie widzi" przechodziłoby,
    także dla roli, która widzi wszystko.
    """
    czytajace = role_z_dostepem_do(KLUCZE_GIELDY)
    assert "trader_silnik" in czytajace, (
        "parser nie widzi nadania, o którym wiadomo, że istnieje (0034 daje "
        "`trader_silnik` SELECT na app.api_credential) — twierdzenia w tym "
        "pliku mierzą pustkę")


def test_rola_skanera_NIE_WIDZI_zaszyfrowanych_kluczy_gieldy():
    """Klucz i szyfrogram nie mogą być w jednym procesie.

    Ten proces ma `SECRETS_KEY` w środowisku i ma go z własnego powodu
    (odszyfrowanie klucza dostawcy analizy). Dopóki nie widzi szyfrogramu
    kluczy giełdy, ten klucz nie otwiera nic poza tym, co temu procesowi
    przysługuje.
    """
    assert ROLA not in role_z_dostepem_do(KLUCZE_GIELDY), (
        f"{ROLA} dostała nadanie na {KLUCZE_GIELDY} — proces przetwarzający "
        f"treść od obcych odzyskał klucze giełdy w jawnej postaci")


def test_worker_newsow_NIE_CHODZI_rola_widzaca_klucze_gieldy(values):
    """Twierdzenie o WŁASNOŚCI, nie o nazwie roli.

    Migracja może być bez zarzutu, a worker i tak dostanie klucze giełdy — bo
    rolę wpina chart, a nie migracja. Ten test bierze rolę, którą chart NAPRAWDĘ
    wpina workerowi newsów, i sprawdza, czy ta rola ma gdziekolwiek nadanie na
    `app.api_credential`. Złamie go tak samo `dbRole: silnik`, jak wypisanie
    `trader_news` w jakiejś przyszłej migracji obok silnika.

    Pamiętaj o domyśle: worker bez `dbRole` dostaje `silnik` (workers.yaml),
    więc SAMO POMINIĘCIE tego pola jest tu awarią, a nie brakiem deklaracji.
    """
    w = values["workers"][WORKER]
    nazwa_roli = w.get("dbRole", "silnik")
    assert nazwa_roli in values["db"]["role"], (
        f"worker {WORKER} deklaruje dbRole={nazwa_roli!r}, którego nie ma "
        f"w .Values.db.role — `helm template` zatrzyma się na tym, ale dopiero "
        f"przy wdrożeniu")
    uzytkownik = values["db"]["role"][nazwa_roli]["user"]

    assert uzytkownik not in role_z_dostepem_do(KLUCZE_GIELDY), (
        f"worker {WORKER} chodzi rolą {uzytkownik}, która ma nadanie na "
        f"{KLUCZE_GIELDY}. To JEDYNY proces przetwarzający treść od obcych "
        f"i ma w środowisku SECRETS_KEY — szyfrogram kluczy giełdy i klucz "
        f"do niego są znów w jednej przestrzeni adresowej.")


def test_skaner_nie_siega_do_tabeli_sekretow_tylko_do_widoku(nadania):
    """`app.secret` jest opisany jako „sekrety konfiguracyjne aplikacji".

    Dziś mieszczą się w nim same klucze dostawców analizy — ale to jest STAN,
    a nie gwarancja. Nadanie na całą tabelę znaczyłoby, że pierwszy sekret
    dotyczący pieniędzy wpisany tam przez kogokolwiek jest dla skanera czytelny
    od chwili powstania, a `SECRETS_KEY` w tym podzie pasuje do niego tak samo
    jak do kluczy LLM.
    """
    assert not nadania.prawa("app.secret"), (
        "skaner dostał nadanie na całą tabelę app.secret — widok app.secret_llm "
        "przestał być granicą, a każdy przyszły sekret jest stąd czytelny")
    assert "SELECT" in nadania.prawa("app.secret_llm"), (
        "skaner nie odczyta klucza dostawcy analizy — zostanie mu tylko ścieżka "
        "zapasowa ze zmiennej środowiskowej, czyli klucz wymieniany redeployem")


def test_widok_oddaje_DOKLADNIE_te_sekrety_ktorych_worker_szuka():
    """Widok i kod są w dwóch plikach, a rozjazd między nimi jest CICHY.

    Dostawca dopisany do `ZRODLA_KLUCZY` bez dopisania go do widoku nie wywoła
    błędu: `_klucz()` dostanie pusty wynik, spadnie na zmienną środowiskową
    i worker będzie pracował dalej — tyle że kluczem, którego nie da się
    wymienić z interfejsu. Odwrotny rozjazd (nazwa w widoku, której nikt nie
    czyta) jest tańszy, ale znaczy szyfrogram udostępniony bez powodu.
    """
    from services.ingest.news_worker import ZRODLA_KLUCZY

    tresc = _bez_komentarzy(MIGRACJA.read_text(encoding="utf-8"))
    m = re.search(r"CREATE OR REPLACE VIEW app\.secret_llm AS(.+?)(?=;)",
                  tresc, re.S | re.I)
    assert m, "migracja nie zakłada widoku app.secret_llm"
    w_widoku = set(re.findall(r"'([a-z0-9_]+)'", m.group(1)))
    w_kodzie = {nazwa for nazwa, _, _ in ZRODLA_KLUCZY}

    assert w_widoku == w_kodzie, (
        f"widok oddaje {sorted(w_widoku)}, a worker pyta o {sorted(w_kodzie)}. "
        f"Brakujące w widoku spadną po cichu na zmienną środowiskową; zbędne "
        f"to szyfrogram udostępniony procesowi, który go nie potrzebuje.")


def test_worker_czyta_widok_a_nie_tabele():
    """Nadanie bez zmiany zapytania zatrzymałoby skaner na `permission denied`,
    a zmiana zapytania bez nadania — tak samo. Oba pliki muszą się zgadzać."""
    tresc = (KORZEN / "services" / "ingest" / "news_worker.py").read_text(
        encoding="utf-8")
    assert "FROM app.secret_llm" in tresc, \
        "news_worker.py nie czyta widoku app.secret_llm"
    assert "FROM app.secret " not in tresc and "FROM app.secret\n" not in tresc, \
        "news_worker.py nadal czyta tabelę app.secret z pominięciem widoku"


# ============================================================================
# 2. KANARKI — samo odebranie wszystkiego też przeszłoby powyższe
# ============================================================================

def test_skaner_zapisuje_wyniki_swojej_pracy(nadania):
    """Rola bez prawa zapisu w `news` jest rolą, na której worker nie zrobi nic
    — a testy wyżej byłyby wtedy zielone najmocniej jak się da."""
    for prawo in ("SELECT", "INSERT", "UPDATE"):
        assert prawo in nadania.prawa("news.article"), (
            f"skaner nie ma {prawo} na news.article — nie zapisze ani nie "
            f"zdeduplikuje artykułu")


def test_skaner_odczyta_numer_migracji(nadania):
    """Bramka startowa `czekaj-na-migracje` chodzi tą samą wąską rolą co worker.

    Bez `SELECT` na `public.schema_migrations` kończy się na „permission denied"
    — w kontenerze startowym, w pętli ponawiania, gdzie wygląda dokładnie jak
    „baza jeszcze nie wstała".
    """
    assert nadania.prawa("public.schema_migrations") == {"SELECT"}, (
        "skaner ma na tabeli migracji co innego niż sam odczyt — zapis do niej "
        "otwierałby mu własną bramkę")


def test_skaner_bije_wlasny_puls(nadania):
    """Puls (0035) jest jedynym sygnałem odróżniającym workera ZAWIESZONEGO od
    pracującego. Rola bez `INSERT`/`UPDATE` na tej tabeli wywraca cykl przy
    każdym obrocie — i to jest zamierzone zachowanie `puls.bij`, więc worker
    stanąłby całkowicie."""
    prawa = nadania.prawa("risk.worker_heartbeat")
    assert {"SELECT", "INSERT", "UPDATE"} <= prawa
    assert "DELETE" not in prawa and "TRUNCATE" not in prawa, (
        "skaner może skasować puls — TRUNCATE zdjąłby puls WSZYSTKICH workerów "
        "naraz, czyli uciszył czujkę żywotności dla całego potoku danych")


def test_skaner_NIE_MA_dostepu_do_bezpiecznika(nadania):
    """Rola silnika dawała mu `SELECT` na limitach i na dzienniku zatrzymań.
    Proces przetwarzający treść od obcych nie musi wiedzieć, przy jakim progu
    obsunięcia system się zatrzymuje — a tym bardziej nie może tam pisać."""
    for tabela in ("risk.limit_def", "risk.killswitch_event",
                   "risk.engine_heartbeat", "risk.dozorca_heartbeat"):
        assert not nadania.prawa(tabela), (
            f"skaner dostał prawa na {tabela}")


@pytest.mark.parametrize("schemat", ["exec", "journal", "learn", "feat", "sig",
                                     "market", "notify"])
def test_skaner_nie_dostaje_schematow_ktorych_nie_dotyka(nadania, schemat):
    """Rola silnika dawała zapis do ośmiu schematów dziedzinowych. Skaner sięga
    do jednego. USAGE na schemacie jest w Postgresie warunkiem koniecznym
    dostępu do czegokolwiek w nim, więc jego brak jest tu granicą, a nie
    formalnością."""
    assert schemat not in nadania.schematy, (
        f"skaner dostał USAGE na schemacie {schemat}, do którego nie sięga "
        f"ani jednym zapytaniem")


# ============================================================================
# 3. ZAPYTANIA WORKERA MIESZCZĄ SIĘ W NADANIACH
# ============================================================================

def obiekty_w_kodzie() -> dict[str, set[str]]:
    """(schemat.obiekt) -> operacje, których kod skanera na nim używa.

    Przez AST, a nie przez czytanie pliku jako tekstu: zapytania są posklejane
    z kilku literałów, więc w surowym źródle między `FROM` a nazwą tabeli stoją
    cudzysłowy i wcięcia. Parser Pythona skleja je za nas.

    Lista wpisana tu na sztywno byłaby DRUGĄ KOPIĄ zapytań i rozjechałaby się
    po cichu przy pierwszym nowym `SELECT` — a brakujące nadanie objawia się
    dopiero przy wykonaniu, czyli na produkcji.
    """
    out: dict[str, set[str]] = {}
    wzor = re.compile(r"\b(FROM|INTO|UPDATE|JOIN)\s+([a-z_]+)\.([a-z_]+)", re.I)
    for plik in ZRODLA_SKANERA:
        for wezel in ast.walk(ast.parse(plik.read_text(encoding="utf-8"))):
            if not (isinstance(wezel, ast.Constant)
                    and isinstance(wezel.value, str)):
                continue
            if not re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", wezel.value, re.I):
                continue
            for slowo, schemat, tabela in wzor.findall(wezel.value):
                operacja = {"FROM": "SELECT", "JOIN": "SELECT",
                            "INTO": "INSERT", "UPDATE": "UPDATE"}[slowo.upper()]
                out.setdefault(f"{schemat.lower()}.{tabela.lower()}",
                               set()).add(operacja)
    return out


def test_kanarek_zapytania_workera_sa_w_ogole_widoczne():
    """Gdyby parser przestał znajdować zapytania, test niżej przechodziłby dla
    dowolnie wąskiej roli — łącznie z rolą bez jednego nadania."""
    znalezione = obiekty_w_kodzie()
    for obowiazkowe in ("news.article", "ref.instrument", "risk.worker_heartbeat",
                        "app.secret_llm"):
        assert obowiazkowe in znalezione, (
            f"parser nie widzi zapytań do {obowiazkowe} — twierdzenie o zasięgu "
            f"roli mierzy pustkę")


def test_kazde_zapytanie_skanera_miesci_sie_w_jego_nadaniach(nadania):
    """Klasa błędu, nie jedna literówka: kod sięga poza granicę własnej roli.

    Ten projekt zapłacił już za ten rozjazd 15 godzin martwego bezpiecznika —
    `check_account()` dozorcy sięgało po `journal.pl`, na który `trader_dozorca`
    nie ma USAGE, a pętla łapała wyjątek i spała 60 s. Z zewnątrz: `Running`,
    `restartCount 0`, `Healthy`.

    Tu skutek jest inny, ale ta sama klasa: cykl skanera przerwany na
    `permission denied` nie odświeża pulsu, więc awaria JEST widoczna — ale
    dopiero po dwóch nieodpytanych kanałach, a RSS nie odda jutro tego, czego
    dziś nie pobraliśmy.
    """
    braki = []
    for obiekt, operacje in sorted(obiekty_w_kodzie().items()):
        schemat = obiekt.split(".")[0]
        if schemat not in nadania.schematy:
            braki.append(f"{obiekt}: brak USAGE na schemacie {schemat}")
            continue
        brakujace = operacje - nadania.prawa(obiekt)
        if brakujace:
            braki.append(f"{obiekt}: brak {sorted(brakujace)}")
    assert not braki, (
        "kod skanera sięga poza nadania roli " + ROLA + ":\n  "
        + "\n  ".join(braki))


# ============================================================================
# 4. POŚWIADCZENIA — rozdział uprawnień jest wart tyle, ile rozdział haseł
# ============================================================================

def test_migracja_zaklada_role_idempotentnie():
    """Migracja idzie na bazę z danymi i musi przejść także wtedy, gdy rolę
    założono wcześniej ręcznie."""
    tresc = MIGRACJA.read_text(encoding="utf-8")
    assert re.search(r"IF NOT EXISTS \(SELECT 1 FROM pg_roles WHERE rolname = "
                     r"'trader_news'\)", tresc), \
        "CREATE ROLE bez osłony — druga migracja padnie na istniejącej roli"
    assert "CREATE OR REPLACE VIEW app.secret_llm" in tresc, \
        "widok zakładany bez OR REPLACE — nie przejdzie po ponownym zastosowaniu"


def test_migrator_ustawia_haslo_roli_skanera():
    """Rola bez hasła nie zaloguje się w ogóle i pod nie wstanie.

    To jest zachowanie ZAMIERZONE (0034): pod, który nie wstał, widać
    w `kubectl get pods`; proces chodzący na cudzych poświadczeniach nie widać
    wcale. Brak wpisu tutaj znaczyłby natomiast awarię BEZ powodu widocznego
    w konfiguracji — hasło byłoby w sekrecie i nigdy nie trafiałoby do bazy.
    """
    from db.migrate import ROLE_HASLA
    assert ROLE_HASLA.get(ROLA) == "NEWS_DB_PASSWORD", (
        "db/migrate.py nie ustawi hasła roli skanera — worker nie połączy się "
        "z bazą nawet wtedy, gdy klucz jest w sekrecie")


def test_skaner_ma_wlasne_haslo_a_nie_pozyczone(values):
    """Rozdział uprawnień jest wart tyle, ile rozdział POŚWIADCZEŃ: wspólny
    klucz w sekrecie znaczyłby, że proces o węższych nadaniach po prostu łączy
    się tą rolą, która ma szersze."""
    role = values["db"]["role"]
    assert WORKER in role, (
        f"chart nie definiuje roli {WORKER!r} w .Values.db.role — dopisz "
        f"{{user: {ROLA}, secretKey: NEWS_DB_PASSWORD}}")
    assert role[WORKER]["user"] == ROLA
    assert role[WORKER]["secretKey"] == "NEWS_DB_PASSWORD"
    klucze = [r["secretKey"] for r in role.values()]
    assert len(set(klucze)) == len(klucze), "dwie role dzielą klucz w sekrecie"


def test_skaner_NADAL_dostaje_SECRETS_KEY():
    """Kanarek do całej reszty pliku, i odpowiedź na pytanie „czy ten klucz jest
    tam w ogóle potrzebny".

    JEST. `_klucz()` czyta `app.secret_llm.value_enc` i odszyfrowuje go
    `SecretBox`-em — bez `SECRETS_KEY` zostaje wyłącznie ścieżka zapasowa ze
    zmiennej środowiskowej, czyli klucz dostawcy wymieniany redeployem zamiast
    z interfejsu. Usunięcie go stąd „dla bezpieczeństwa" cofnęłoby działającą
    funkcję i niczego nie kupiło: po tej migracji ten klucz nie pasuje już do
    żadnego szyfrogramu, którego skaner nie ma prawa odczytać.
    """
    tresc = (KORZEN / "charts" / "trader" / "templates" / "workers.yaml").read_text(
        encoding="utf-8")
    assert "SECRETS_KEY" in tresc, (
        "workery straciły SECRETS_KEY — skaner nie odszyfruje klucza dostawcy "
        "analizy i przestanie analizować bez jednego słowa w konfiguracji")
