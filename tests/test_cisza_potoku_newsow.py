"""Cykl, który NIE WYPRODUKOWAŁ ANI JEDNEJ ANALIZY, nie ma bić zdrowego pulsu.

CO SIĘ DZIAŁO NA PRODUKCJI 09.09–22.09.2026 — TRZYNAŚCIE DNI

`puls.bij` bezwarunkowo ustawia `last_beat_at = now()`, a
`dozor.ocen_puls_workerow` alarmuje WYŁĄCZNIE wiekiem tego znacznika — mówi to
wprost jego własny docstring („Alarmuje wylacznie wiek `last_beat_at`").
`news_worker.main` wołał `puls.bij` po każdym cyklu zakończonym bez wyjątku.

Cykl, w którym obaj dostawcy byli odcięci (albo limit dobowy był wyczerpany),
kończy się bez wyjątku: artykuły są zapisywane, `przeanalizowane = 0`, `odciety`
wypełniony. Puls był więc świeży co pięć minut, `kubectl get pods` pokazywał
`Running`, czujka milczała — a `news.article_analysis` nie dostawało ANI JEDNEGO
wiersza. Jedynym śladem była linia `⚠ BEZ ANALIZ` na stdout poda, którego nikt
nie czyta.

DRUGA POŁOWA TEGO PLIKU: TO SAMO OSTRZEŻENIE, ODPALANE FAŁSZYWIE

`nadrob_zaleglosci` zlicza swoje analizy wyłącznie w `stats["zaleglosci"]`,
a `opis_ciszy` patrzył tylko na `stats["przeanalizowane"]`. Cykl, w którym kanały
nie dały nic nowego, ale nadrabianie przeanalizowało 12 zaległych artykułów,
kończył się linią:

    ⚠ BEZ ANALIZ (brak artykułów do analizy); ostatnia 0 h temu

Zdaniem wewnętrznie sprzecznym — i nieprawdziwym. Ostrzeżenie pojawiające się
przy ZDROWEJ pracy uczy czytać je nieuważnie akurat wtedy, gdy dotyczy prawdziwej
ciszy z pierwszej połowy tego pliku. Fałszywy alarm psuje czujkę skuteczniej niż
jej brak.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from services.ingest import news_worker

KORZEN = Path(__file__).resolve().parents[1]
ZRODLO = (KORZEN / "services/ingest/news_worker.py").read_text(encoding="utf-8")


def _stats(**nadpisz) -> dict:
    """Liczniki cyklu Z WORKERA, nie przepisane tutaj.

    Własna kopia rozjechałaby się przy pierwszym nowym liczniku i test
    pilnowałby kształtu, którego produkcja już nie ma.
    """
    return dict(news_worker.puste_stats(), **nadpisz)


# ============================================ puls: cisza nie jest udanym cyklem

def test_CISZA_gdy_artykuly_czekaja_a_zaden_nie_przeszedl():
    """Stan z 09.09–22.09: dostawca odcięty, 40 artykułów czeka, zero analiz."""
    s = _stats(czekaja=40, przeanalizowane=0, zaleglosci=0,
               odciety="brak środków na koncie (HTTP 400)")

    powod = news_worker.cisza_zamiast_pracy(s, analizuj=True)

    assert powod, "cykl bez ani jednej analizy przy 40 czekających to nie jest udany cykl"
    assert "czeka 40" in powod
    assert "brak środków" in powod, "powód ma być w alarmie, nie tylko w logu poda"


def test_CISZA_gdy_zatrzymal_potok_LIMIT_DOBOWY():
    s = _stats(czekaja=12, limit_powod="limit dobowy 1500 żądań osiągnięty (1500)")

    powod = news_worker.cisza_zamiast_pracy(s, analizuj=True)

    assert "limit dobowy" in powod


def test_SPOKOJNA_DOBA_BEZ_NEWSOW_nie_jest_awaria():
    """Warunek jest wąski CELOWO.

    Alarm o „braku analiz" w dobie, w której kanały nie dały nic nowego, byłby
    fałszywy — a fałszywy alarm psuje czujkę skuteczniej niż jej brak. Potok ma
    mieć CO ROBIĆ, żeby jego bezczynność coś znaczyła.
    """
    s = _stats(czekaja=0, przeanalizowane=0, zaleglosci=0, kanaly=5, pobrane=100)

    assert news_worker.cisza_zamiast_pracy(s, analizuj=True) == ""


def test_TRYB_BEZ_KLUCZA_nie_jest_awaria():
    """Brak klucza dostawcy to stan ZADANY (`--bez-analizy` albo pusty klucz),
    a nie awaria — i ma własną widoczność w logu („Tryb: wyłącznie pobieranie")."""
    s = _stats(czekaja=40, przeanalizowane=0)

    assert news_worker.cisza_zamiast_pracy(s, analizuj=False) == ""


def test_CYKL_KTORY_NADROBIL_ZALEGLOSCI_JEST_UDANY():
    """`nadrob_zaleglosci` liczy swoje analizy w `zaleglosci`, nie
    w `przeanalizowane`. Cykl, w którym powstało 12 analiz zaległych, wyprodukował
    12 wierszy w `news.article_analysis` — to jest praca, nie cisza."""
    s = _stats(czekaja=30, przeanalizowane=0, zaleglosci=12)

    assert news_worker.cisza_zamiast_pracy(s, analizuj=True) == ""


def _wola(wezel: ast.AST, nazwa: str) -> bool:
    return (isinstance(wezel, ast.Call)
            and getattr(wezel.func, "attr", None) == nazwa
            and getattr(getattr(wezel.func, "value", None), "id", None) == "puls")


def _zawiera_wywolanie(ciala: list, nazwa: str) -> bool:
    return any(_wola(w, nazwa)
               for galaz in ciala for w in ast.walk(galaz))


def test_MAIN_NIE_BIJE_PULSU_BEZWARUNKOWO():
    """Kanarek strukturalny — i to jest cała poprawka, a nie sama funkcja wyżej.

    Funkcja `cisza_zamiast_pracy` poprawnie rozpoznająca ciszę jest bezużyteczna,
    dopóki `main` wywołuje `puls.bij` bez patrzenia na nią. Czytamy więc drzewo
    składniowe i wymagamy, żeby `puls.bij` leżał w gałęzi `else` warunku
    opartego o ciszę, a `puls.zglos_awarie` — w gałęzi zajętej.

    Test czyta ŹRÓDŁO, bo `main` to pętla z połączeniem do bazy i klientem HTTP:
    uruchomienie jej w teście wymagałoby atrapy całego świata, a sprawdzana
    własność jest własnością KOLEJNOŚCI I WARUNKU, nie wartości zwracanej.
    """
    drzewo = ast.parse(ZRODLO)
    for w in ast.walk(drzewo):
        if not isinstance(w, ast.If):
            continue
        if "cisza" not in ast.dump(w.test):
            continue
        if (_zawiera_wywolanie(w.body, "zglos_awarie")
                and _zawiera_wywolanie(w.orelse, "bij")):
            return
    pytest.fail(
        "`puls.bij` w `news_worker.main` nie jest uzależniony od ciszy potoku — "
        "cykl bez ani jednej analizy odświeża `last_beat_at`, a "
        "`dozor.ocen_puls_workerow` alarmuje WYŁĄCZNIE jego wiekiem. Tak "
        "wyglądało 13 dni ciszy 09.09–22.09.2026.")


# ================================================ opis w logu: bez fałszywego ⚠

def test_OPIS_CISZY_MILCZY_po_cyklu_ktory_nadrobil_zaleglosci():
    """Linia, która pojawiała się przy zdrowej pracy:

        ⚠ BEZ ANALIZ (brak artykułów do analizy); ostatnia 0 h temu

    „brak artykułów do analizy" przy 12 właśnie przeanalizowanych zaległych
    i „ostatnia 0 h temu" w tej samej linii — zdanie zaprzeczające samo sobie.
    """
    s = _stats(przeanalizowane=0, zaleglosci=12, czekaja=0,
               od_ostatniej_analizy_h=0.0)

    assert news_worker.opis_ciszy(s, analizuj=True) == ""


def test_OPIS_CISZY_ODZYWA_SIE_gdy_naprawde_nic_nie_powstalo():
    """Granica poprawki. Warunek miał być węższy, a nie wyłączony."""
    s = _stats(przeanalizowane=0, zaleglosci=0, czekaja=40,
               odciety="brak środków na koncie", od_ostatniej_analizy_h=312.0)

    opis = news_worker.opis_ciszy(s, analizuj=True)

    assert "BEZ ANALIZ" in opis
    assert "312 h temu" in opis


# ======================================================= deduplikacja po adresie

def test_NORMALIZACJA_SKLEJA_TEN_SAM_ARTYKUL_Z_ROZNYCH_KANALOW():
    """Ten sam tekst przychodzi z różnych kanałów z różnymi ogonkami."""
    wzorzec = news_worker.normalizuj_url("https://przyklad.test/news/fed-podniosl")
    for wariant in (
        "http://przyklad.test/news/fed-podniosl",
        "https://www.przyklad.test/news/fed-podniosl",
        "https://przyklad.test/news/fed-podniosl/",
        "https://przyklad.test/news/fed-podniosl#lead",
        "https://przyklad.test/news/fed-podniosl?utm_source=rss&utm_medium=feed",
        "https://przyklad.test/news/fed-podniosl?fbclid=abc123",
    ):
        assert news_worker.normalizuj_url(wariant) == wzorzec, wariant


def test_NORMALIZACJA_NIE_TNIE_PARAMETROW_KTORE_SA_ADRESEM_ARTYKULU():
    """Świadoma granica: reszta parametrów zostaje.

    `?id=4471` i `?page=2` potrafią być całym adresem artykułu. Obcięcie ich
    skleiłoby ze sobą teksty, które nie mają ze sobą nic wspólnego — a błąd
    w tę stronę jest GORSZY niż duplikat: duplikat tylko zawyża cechę,
    sklejenie KASUJE materiał.
    """
    a = news_worker.normalizuj_url("https://przyklad.test/art.php?id=4471")
    b = news_worker.normalizuj_url("https://przyklad.test/art.php?id=4472")
    assert a != b
    assert "id=4471" in a

    # `ref` i `source` NIE są na liście, bo `refresh`/`source_id` bywają
    # częścią adresu. Przedrostkowe dopasowanie zabrałoby je razem z nimi.
    z_ref = news_worker.normalizuj_url("https://przyklad.test/a?ref=xyz")
    assert "ref=xyz" in z_ref


def test_NORMALIZACJA_ODDAJE_PUSTY_NAPIS_TAM_GDZIE_NIE_MA_CZEGO_POROWNYWAC():
    """Pusty wynik znaczy „ten wpis nie bierze udziału w dedupie po URL".

    To jest ważniejsze, niż wygląda: `run_cycle` podstawia `feed["url"]` tam,
    gdzie wpis nie ma własnego `link`. Gdyby ta wartość szła do porównania,
    WSZYSTKIE artykuły takiego kanału miałyby jeden adres i kanał oddałby
    w całym swoim życiu jeden artykuł.
    """
    for nic in ("", "   ", "/wzgledny/adres", "nie-adres"):
        assert news_worker.normalizuj_url(nic) == "", repr(nic)


class _BazaZAdresami:
    """Tyle bazy, ile dotyka zapis artykułu — z PAMIĘCIĄ ADRESÓW.

    Odtwarza dokładnie dwa warunki, o które chodzi w tym teście: `UNIQUE
    (content_hash)` i wyszukiwanie po `url`. Reszta zapytań oddaje `None`,
    czyli „nic nie znaleziono" — i to jest jawne, nie milczące.
    """

    def __init__(self):
        self.artykuly: list[dict] = []
        self.sql: list[tuple[str, tuple]] = []
        self._odpowiedz = None

    def cursor(self, row_factory=None):
        return self

    def commit(self):
        pass

    def execute(self, sql, params=None):
        plaski = " ".join(sql.split())
        self.sql.append((plaski, params))
        self._odpowiedz = self._wykonaj(plaski, params)

    def fetchone(self):
        w, self._odpowiedz = self._odpowiedz, None
        return w[0] if isinstance(w, list) and w else (None if isinstance(w, list) else w)

    def fetchall(self):
        w, self._odpowiedz = self._odpowiedz, None
        return w if isinstance(w, list) else ([] if w is None else [w])

    def _wykonaj(self, sql, params):
        if sql.startswith("SELECT 1 FROM news.article WHERE content_hash"):
            return {"?column?": 1} if any(a["h"] == params[0]
                                          for a in self.artykuly) else None
        if sql.startswith("SELECT 1 FROM news.article WHERE url"):
            return {"?column?": 1} if any(a["url"] == params[0]
                                          for a in self.artykuly) else None
        if sql.startswith("INSERT INTO news.article ("):
            feed_id, url, title, summary, h, event_time = params
            if any(a["h"] == h for a in self.artykuly):
                return None                    # ON CONFLICT (content_hash)
            self.artykuly.append({"id": len(self.artykuly) + 1, "url": url,
                                  "title": title, "h": h})
            return {"id": self.artykuly[-1]["id"]}
        return None


def _zapisz(baza, link: str, tytul: str, zajawka: str = "zajawka") -> bool:
    """Odtwarza DOKŁADNIE decyzję `run_cycle` o tym, czy artykuł jest nowy.

    Wywołanie całego `run_cycle` wymagałoby atrapy kanałów, dostawcy, budżetu
    i instrumentów — a sprawdzana tu własność mieści się w trzech krokach:
    hash, adres, zapis. Kolejność i treść zapytań biorą się ze źródła (patrz
    `test_RUN_CYCLE_pyta_o_adres_przed_zapisem` niżej), więc ta funkcja nie
    może się rozjechać z produkcją po cichu.
    """
    h = news_worker.content_hash(tytul, zajawka)
    baza.execute("SELECT 1 FROM news.article WHERE content_hash = %s", (h,))
    if baza.fetchone():
        return False
    url = news_worker.normalizuj_url(link)
    if url:
        baza.execute(
            "SELECT 1 FROM news.article WHERE url = %s"
            "   AND ingested_at > now() - make_interval(hours => %s)",
            (url, news_worker.OKNO_DEDUP_URL_H))
        if baza.fetchone():
            return False
    baza.execute(
        "INSERT INTO news.article (feed_id, url, title, summary, content_hash,"
        " event_time, ingested_at) VALUES (%s,%s,%s,%s,%s,%s, now())"
        " ON CONFLICT DO NOTHING RETURNING id",
        (1, url or link, tytul, zajawka, h, None))
    return baza.fetchone() is not None


def test_REDAKCYJNA_POPRAWKA_TYTULU_nie_tworzy_drugiego_platnego_artykulu():
    """Wydawca po dwudziestu minutach zdejmuje prefiks „BREAKING:".

    Ten sam URL, nowy `content_hash` — do 2026-09-23 artykuł wchodził jako nowy:
    drugi płatny triaż, druga płatna analiza, a `features.compute_news` SUMUJE
    po `news.article_instrument`, więc jedno zdarzenie wchodziło do cechy DWA
    RAZY z pełną wagą.
    """
    baza = _BazaZAdresami()
    adres = "https://przyklad.test/news/fed-podniosl-stopy?utm_source=rss"

    assert _zapisz(baza, adres, "BREAKING: Fed podniósł stopy o 25 pb")
    assert not _zapisz(baza, adres, "Fed podniósł stopy o 25 punktów bazowych",
                       zajawka="przeredagowany lead"), (
        "ten sam adres z poprawionym tytułem wszedł jako nowy artykuł — "
        "drugi płatny triaż i podwójna waga w cesze `news_sent`")
    assert len(baza.artykuly) == 1


def test_KANAL_BEZ_ADRESOW_WE_WPISACH_dalej_oddaje_wszystkie_artykuly():
    """Kanarek na nadgorliwość — i na awarię gorszą od naprawianej.

    Wpis bez `link` dostaje w zapisie adres kanału. Gdyby ta wartość brała
    udział w deduplikacji, drugi artykuł takiego kanału byłby już „duplikatem"
    pierwszego i kanał oddałby w całym swoim życiu jeden tekst.
    """
    baza = _BazaZAdresami()

    assert _zapisz(baza, "", "Pierwszy artykuł bez odnośnika")
    assert _zapisz(baza, "", "Drugi artykuł bez odnośnika"), (
        "kanał bez `link` we wpisach przestał oddawać artykuły — "
        "deduplikacja po URL złapała adres kanału zamiast adresu artykułu")
    assert len(baza.artykuly) == 2


def test_TEN_SAM_ADRES_PO_OKNIE_nie_jest_zaslaniany_na_zawsze():
    """Okno jest skończone i to jest decyzja, nie przeoczenie.

    Adres wracający po miesiącu to najczęściej nowy materiał pod starym slugiem;
    zasłanianie go byłoby gorsze niż przepuszczenie. Okno trafia przy okazji
    w indeks `article_ingested_at_idx` zamiast przeglądać całą tabelę.
    """
    assert 0 < news_worker.OKNO_DEDUP_URL_H <= 24 * 7


def test_RUN_CYCLE_pyta_o_adres_przed_zapisem():
    """Kanarek wiążący test wyżej z produkcją.

    `_zapisz` odtwarza kolejność kroków `run_cycle`. Gdyby produkcja przestała
    pytać o adres (albo zaczęła pytać PO wstawieniu wiersza), tamten test dalej
    by przechodził, pilnując zgodności atrapy z samą sobą. Ten test czyta
    źródło i wymaga, żeby oba zapytania w `run_cycle` naprawdę były — i żeby
    pytanie o adres poprzedzało `INSERT`.
    """
    zrodlo_cyklu = ZRODLO[ZRODLO.index("async def run_cycle"):]
    po_url = zrodlo_cyklu.find("FROM news.article WHERE url = %s")
    po_insert = zrodlo_cyklu.find("INSERT INTO news.article (")
    assert po_url > 0, "`run_cycle` nie szuka duplikatu po adresie"
    assert po_insert > po_url, (
        "pytanie o adres pada PO wstawieniu wiersza — czyli nigdy nie trafia")
    assert "normalizuj_url(" in zrodlo_cyklu, (
        "`run_cycle` porównuje adres surowy; adresy w bazie są znormalizowane, "
        "więc taki dedup nie trafiłby nigdy")


def test_ZAPIS_ARTYKULU_ZNOSI_KAZDE_ograniczenie_unikalnosci():
    """Kanarek na awarię GORSZĄ od duplikatu.

    `cur.execute` z `INSERT INTO news.article` nie leży w żadnym `try` wewnątrz
    `run_cycle`. `ON CONFLICT (content_hash) DO NOTHING` obsługuje DOKŁADNIE
    JEDNO ograniczenie — każde inne wraca jako `IntegrityError`, przewraca cykl
    i wprowadza transakcję w stan `aborted`. Przepadają wtedy wszystkie pozostałe
    artykuły partii i wszystkie kolejne kanały, a RSS ich jutro nie odda: ta sama
    kategoria straty co limit dobowy osiągnięty o 14:00, tylko wywołana
    duplikatem.

    Dziś `news.article` nie ma innego ograniczenia niż `UNIQUE (content_hash)`,
    więc to jeszcze nie strzela. Unikalny indeks na `url` jest natomiast pierwszą
    rzeczą, którą ktoś tu dołoży po tej poprawce — i wtedy strzeli.
    Klauzula bez wskazania ograniczenia obejmuje wszystkie naraz, więc pułapka
    nie może się zazbroić niezależnie od tego, co zrobi migracja.
    """
    zrodlo_cyklu = ZRODLO[ZRODLO.index("async def run_cycle"):]
    insert = zrodlo_cyklu[zrodlo_cyklu.index("INSERT INTO news.article ("):]
    insert = insert[:insert.index("RETURNING id")]

    assert "ON CONFLICT DO NOTHING" in " ".join(insert.split()), (
        "zapis artykułu wskazuje konkretne ograniczenie w `ON CONFLICT` — "
        "każde inne naruszenie unikalności przewróci cały cykl zamiast pominąć "
        "jeden wiersz")
