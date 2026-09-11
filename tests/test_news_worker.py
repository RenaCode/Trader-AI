"""Testy odporności workera newsów na ZAWIESZENIE — i na MYLĄCY log.

Awaria, od której te testy powstały, nie była awarią w żadnym sensie, który
kod potrafił zauważyć: pod stał `Running`, nie rzucił wyjątku, nie zapisał
błędu — po prostu przez 41 minut nie zrobił nic przy interwale cyklu 300 s.
Cała obsługa błędów w pętli była na to ślepa, bo zawieszenie nie jest wyjątkiem.

Druga rodzina testów niżej pilnuje czegoś pokrewnego: awarii, którą kod
zauważył i OPISAŁ NIEPRAWDZIWIE. Log podpisany dostawcą, o którego prosiliśmy,
zamiast tym, który odmówił, kieruje szukanie awarii wprost na niewinnego —
kosztuje więc dokładnie to, co cisza, tylko z pozorem informacji.
"""
import asyncio

import pytest

from services.ingest import news_worker
from services.ingest.llm import (
    BladOdpowiedzi, Dostawca, DostawcaMieszany, Przeciazony, Wyczerpany,
)

# ===================================================== zawieszenie workera

def test_klient_anthropica_ma_OGRANICZONY_czas_oczekiwania():
    """Worker zamilkł 2026-09-08 na 41 minut przy interwale cyklu 300 s.

    Klient był tworzony bez timeoutu, a SDK domyślnie czeka 600 s i ponawia
    dwa razy — jedno wywołanie mogło więc zablokować proces na pół godziny.
    Zawieszenie NIE JEST WYJĄTKIEM, więc licznik awarii nie rósł, puls nie był
    bity, a pod stał `Running` i nie robił nic. Klient Gemini obok miał 90 s od
    początku; ta rozbieżność była przeoczeniem, nie decyzją.

    Test czyta ŹRÓDŁO, a nie gotowy obiekt, bo pakiet `anthropic` jest tylko
    w obrazie — test wymagający go pomijałby się po cichu na każdej maszynie
    bez tej zależności, czyli nie pilnowałby niczego tam, gdzie się go
    uruchamia.
    """
    import ast
    import pathlib as _p

    drzewo = ast.parse(_p.Path("services/ingest/llm.py").read_text(encoding="utf-8"))
    wywolania = [w for w in ast.walk(drzewo)
                 if isinstance(w, ast.Call)
                 and getattr(w.func, "id", None) == "AsyncAnthropic"]
    assert wywolania, "nie znaleziono konstrukcji klienta Anthropica"

    for w in wywolania:
        nazwane = {k.arg: k.value for k in w.keywords}
        assert "timeout" in nazwane, (
            "klient bez `timeout` czeka domyślne 600 s na wywołanie")
        limit = ast.literal_eval(nazwane["timeout"])
        assert limit <= 300, f"timeout {limit}s jest dłuższy niż cykl workera"
        assert "max_retries" in nazwane, (
            "bez `max_retries` SDK ponawia dwa razy, mnożąc timeout przez trzy")
        assert ast.literal_eval(nazwane["max_retries"]) <= 1


def test_zawieszony_cykl_jest_PRZERYWANY_a_nie_czekany_w_nieskonczonosc():
    """Ogólna warstwa obrony, niezależna od tego, co dokładnie stanęło.

    Timeout na kliencie HTTP chroni przed jednym zawieszonym zapytaniem. Ten
    limit chroni przed wszystkim innym, co może stanąć — w tym przed kodem,
    którego jeszcze nie ma. Bez niego każda przyszła blokująca operacja
    odtworzy tę samą cichą awarię.
    """
    import asyncio

    async def zawieszony():
        await asyncio.sleep(3600)

    async def przebieg():
        async with asyncio.timeout(0.05):
            await zawieszony()

    with pytest.raises(TimeoutError):
        asyncio.run(przebieg())


def test_worker_STOSUJE_limit_cyklu():
    """Sam fakt istnienia opcji nic nie daje, jeśli pętla jej nie używa —
    nastawa udająca, że działa."""
    import ast
    import pathlib

    zrodlo = pathlib.Path("services/ingest/news_worker.py").read_text(encoding="utf-8")
    drzewo = ast.parse(zrodlo)
    limity = [
        w for w in ast.walk(drzewo)
        if isinstance(w, ast.Call)
        and isinstance(w.func, ast.Attribute)
        and w.func.attr == "timeout"
        and any(isinstance(a, ast.Attribute) and a.attr == "limit_cyklu" for a in w.args)
    ]
    assert limity, "run_cycle nie jest opakowany w asyncio.timeout(args.limit_cyklu)"


# ============================ log, który podpisuje się WŁAŚCIWYM dostawcą
#
# Log produkcyjny (pod trader-news, 2026-09-10), ta sama linia 52-62 razy
# w każdym cyklu, cykl co 5 minut:
#
#   BŁĄD triaż gemini-3.5-flash-lite: BadRequestError: Error code: 400 -
#   {'type': 'error', 'error': {'type': 'invalid_request_error', 'message':
#   'Your credit balance is too low to access the Anthropic API...'}}
#
# Nazwa mówi „gemini", treść mówi „Anthropic". Odpowiedź przyszła od Anthropica,
# bo mieszanka zeszła na dostawcę zapasowego po wyczerpaniu dobowego limitu
# Gemini — a log nazwał tego, o którego PROSILIŚMY. Diagnoza poszła wtedy za
# nazwą, czyli w stronę dostawcy, który z tym błędem nie miał nic wspólnego.

class _Atrapa(Dostawca):
    """Dostawca-atrapa z prawdziwym cennikiem i jedną, ustaloną reakcją.

    `blad` jest FABRYKĄ, a nie gotowym wyjątkiem: wyjątek dostaje podpis
    wykonawcy, więc jedna instancja użyta dwa razy niosłaby podpis z pierwszego
    wywołania i test przechodziłby z niewłaściwego powodu.
    """

    def __init__(self, nazwa, triaz, analiza, cennik, blad=None):
        self.nazwa, self.model_triazu, self.model_analizy = nazwa, triaz, analiza
        self.CENNIK = cennik
        self._blad = blad
        self.pytania = []

    async def zapytaj(self, *, model, **kw):
        self.pytania.append(model)
        if self._blad is not None:
            raise self._blad()
        return ({"worth_analyzing": True, "reason": "istotne"},
                {"input": 1000, "output": 200, "cache_read": 0})


def _gemini(blad=None):
    return _Atrapa("gemini", "gemini-3.5-flash-lite", "gemini-3.5-flash",
                   {"gemini-3.5-flash-lite": (0.30, 2.50),
                    "gemini-3.5-flash": (1.50, 9.00)}, blad)


def _anthropic(blad=None):
    return _Atrapa("anthropic", "claude-haiku-4-5", "claude-opus-5",
                   {"claude-haiku-4-5": (1.0, 5.0),
                    "claude-opus-5": (5.0, 25.0)}, blad)


def _mieszanka_po_zejsciu(blad_zapasowego):
    """Mieszanka w stanie z produkcji: Gemini odbija triaż, odpowiada Anthropic."""
    gemini = _gemini(lambda: Przeciazony("gemini: limit tempa (HTTP 429)"))
    anthropic = _anthropic(blad_zapasowego)
    return DostawcaMieszany(triaz=gemini, analiza=anthropic), gemini, anthropic


class AtrapaBazy:
    """Połączenie i kursor w jednym — tyle bazy, ile dotyka nadrabianie zaległości.

    ŚWIADOMIE NIE WYKONUJE `WHERE`: oddaje ten jeden artykuł, o który chodzi
    w teście. Sprawdzamy tu, CO worker zapisuje, a nie które wiersze wybiera.
    """

    def __init__(self, artykuly):
        self.artykuly = artykuly
        self.sql = []

    def cursor(self, row_factory=None):
        return self

    def commit(self):
        pass

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.artykuly

    def fetchone(self):
        return None

    def wydatki(self):
        """Wiersze wstawione do `news.api_spend`, w kolejności zapisu."""
        return [p for s, p in self.sql
                if s.startswith("INSERT INTO news.api_spend")]

    def werdykty_triazu(self):
        return [p for s, p in self.sql
                if s.startswith("UPDATE news.article SET triage_verdict")]


def _zalegly():
    """Wiersz `news.article` czekający na triaż — bez żadnej oceny."""
    from datetime import datetime, timezone

    return {"id": 1, "title": "Fed podniósł stopy o 25 pb", "summary": "zajawka",
            "event_time": datetime.now(timezone.utc), "feed": "Kanał testowy",
            "triage_verdict": None, "triage_attempts": 0,
            "analysis_verdict": None, "analysis_attempts": 0}


def _nadrob(ai, baza):
    stats = {"koszt": 0.0, "bledy": 0, "opisy_bledow": [], "zaleglosci": 0,
             "odsiane": 0}
    asyncio.run(news_worker.nadrob_zaleglosci(baza, baza, ai, {}, [], 0.0,
                                              stats, verbose=False))
    return stats


def test_log_nazywa_dostawce_KTORY_ODMOWIL_a_nie_tego_o_ktorego_prosilismy():
    """Kanarek na log z produkcji 2026-09-10.

    Mieszanka zeszła na dostawcę zapasowego i to ON odmówił — komunikat ma
    nieść jego nazwę i jego model. Podpis modelem żądanym nie jest kosmetyką:
    to on skierował szukanie awarii na Gemini, podczas gdy pusty był rachunek
    u Anthropica.
    """
    ai, gemini, anthropic = _mieszanka_po_zejsciu(
        lambda: Wyczerpany("anthropic: brak środków na koncie (HTTP 400). "
                           "Your credit balance is too low"))
    baza = AtrapaBazy([_zalegly()])

    stats = _nadrob(ai, baza)

    assert anthropic.pytania == ["claude-haiku-4-5"], (
        "test nie odtworzył zejścia na zapasowego — nie ma czego sprawdzać")
    assert stats["bledy"] == 1
    podpis = stats["opisy_bledow"][0].split(":")[0]
    assert podpis == "triaż anthropic/claude-haiku-4-5", (
        f"log podpisany {podpis!r}, a odmówił anthropic/claude-haiku-4-5 — "
        f"diagnoza pójdzie za nazwą, czyli w stronę niewinnego dostawcy")
    assert "gemini" not in podpis
    # Ten sam model ma trafić do `news.article.triage_model`: kolumna mówi,
    # KTO ocenił artykuł, i jest jedynym śladem po zmianie dostawcy.
    assert baza.werdykty_triazu()[0][2] == "claude-haiku-4-5"


def test_dziennik_wydatkow_nazywa_model_KTORY_SPALIL_TOKENY():
    """`news.api_spend` jest jedynym oparciem limitu dobowego.

    Nieudane wywołanie też kosztuje, a po zejściu awaryjnym płaci za nie inny
    dostawca, po innej stawce. Wpisanie modelu żądanego dawałoby wiersz
    z tańszego cennika za pracę, której tamten model nie wykonał — limit
    dobowy liczyłby wtedy mniej, niż wynosi rachunek.
    """
    zuzycie = {"input": 1000, "output": 200, "cache_read": 0}
    ai, gemini, anthropic = _mieszanka_po_zejsciu(
        lambda: BladOdpowiedzi("odpowiedź ucięta na limicie tokenów", dict(zuzycie)))
    baza = AtrapaBazy([_zalegly()])

    _nadrob(ai, baza)

    wpisy = baza.wydatki()
    assert len(wpisy) == 1, "nieudane wywołanie ma zostawić wiersz w api_spend"
    model, cel, wejscie, cache, wyjscie, koszt, _aid, ok = wpisy[0]
    assert (cel, ok) == ("triage", False)
    assert model == "claude-haiku-4-5", (
        f"dziennik nazywa {model!r}, a tokeny spalił claude-haiku-4-5")
    # Stawka Anthropica (1,0 / 5,0), nie Gemini (0,30 / 2,50).
    assert koszt == pytest.approx(1000 / 1e6 * 1.0 + 200 / 1e6 * 5.0)


# Argument z NAZWĄ MODELU w wywołaniach, które zapisują ślad po pracy modelu.
ZAPISY_MODELU = {"record_spend": 1, "zapisz_analize": 2,
                 "zapisz_werdykt_triazu": 4, "zapisz_nieudane": 3}


def test_zaden_zapis_nie_bierze_modelu_ZADANEGO_wprost():
    """Granica, której dwa testy wyżej nie pilnują: przyszłe wywołania.

    `ai.model_triazu` i `ai.model_analizy` to modele, o które PROSIMY. Zapisane
    w dzienniku albo przy artykule wyglądają poprawnie do dnia, w którym
    mieszanka zejdzie na zapasowego — a wtedy milczą o tym, że coś się zmieniło.
    Model faktyczny daje `uzyty_model` (sukces) albo `kto_wykonal` (awaria).
    """
    import ast
    import pathlib

    drzewo = ast.parse(pathlib.Path(
        "services/ingest/news_worker.py").read_text(encoding="utf-8"))
    for w in ast.walk(drzewo):
        if not isinstance(w, ast.Call):
            continue
        i = ZAPISY_MODELU.get(getattr(w.func, "id", ""))
        if i is None or len(w.args) <= i:
            continue
        arg = w.args[i]
        assert not (isinstance(arg, ast.Attribute)
                    and arg.attr in ("model_triazu", "model_analizy")), (
            f"{w.func.id}(...) w linii {w.lineno} zapisuje `{arg.attr}` — model "
            f"ŻĄDANY, a nie ten, który wykonał wywołanie")


# ============ dostawca MUSI przeżyć cykl, ale MUSI zauważyć nowy klucz ============
#
# Log produkcyjny (pod trader-news, 2026-09-10):
#
#   [llm] triaz: gemini — wyczerpany limit DOBOWY; przechodze na anthropic
#   na 60 min
#
# wracał co ~6 minut zamiast raz na godzinę. Bezpiecznik jest STANEM w instancji
# dostawcy, a pętla budowała tę instancję od nowa w każdym cyklu — zwarcie „na
# 60 minut" trwało więc dokładnie tyle, co cykl, i każdy cykl odkrywał wyczerpany
# limit na nowo, płacąc za to wywołaniem. Ta sama pętla porzucała pulę połączeń:
# 338 cykli w jednym procesie to 338 nieczynnych pul httpx i SDK.
#
# Poprawka nie może przy tym cofnąć rzeczy, której broni komentarz w `main`:
# klucz pojawia się w bazie wtedy, gdy użytkownik wpisze go w interfejsie,
# a nie wtedy, gdy restartujemy poda.

class _Koniec(BaseException):
    """Wyjście z nieskończonej pętli workera.

    Dziedziczy z `BaseException`, bo `main` łapie `Exception` i liczy je jako
    awarię cyklu — sentinel z rodziny `Exception` zamieniłby się w kolejny
    nieudany cykl zamiast zakończyć test.
    """


class _DostawcaAtrapa(Dostawca):
    """Dostawca, który niczego nie pyta — liczy tylko własne zamknięcia.

    Testy niżej nie dotykają analizy: sprawdzają CYKL ŻYCIA obiektu, a nie to,
    co on odpowiada.
    """

    nazwa = "atrapa"
    model_triazu = "tani"
    model_analizy = "drogi"

    def __init__(self, klucze):
        self.klucze = dict(klucze)
        self.zamkniety = 0
        self.blad_zamkniecia = False

    async def zapytaj(self, **kw):
        raise AssertionError("ten test nie ma prawa pytać dostawcy")

    async def zamknij(self):
        self.zamkniety += 1
        if self.blad_zamkniecia:
            raise RuntimeError("gniazdo już zerwane")


class _PolaczenieCyklu:
    """Połączenie żyjące JEDEN cykl — dokładnie tak, jak w `main`.

    Nowy obiekt na każdy cykl jest tu istotą testu, a nie dekoracją: gdyby
    dostawca zatrzymał połączenie, przeżyłby razem z nim cykl i pracował potem
    na zerwanym.
    """

    def __init__(self):
        self.zamkniete = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.zamkniete = True
        return False

    def execute(self, sql, params=None):
        raise AssertionError("klucze czyta podmieniony `_klucz`")


class _Stanowisko:
    """Worker z podmienionym wszystkim, co wychodzi poza `main`.

    Zostaje sama pętla: odczyt kluczy, budowa dostawcy, zamykanie.
    """

    STATS = {"kanaly": 0, "pobrane": 0, "nowe": 0, "przeszly_filtr": 0,
             "odsiane": 0, "przeanalizowane": 0, "zapisane": 0, "zaleglosci": 0,
             "koszt": 0.0, "bledy": 0, "opisy_bledow": []}

    def __init__(self, klucze):
        self.klucze = dict(klucze)
        self.zbudowane = []          # dostawcy w kolejności budowy
        self.polaczenia = []         # jedno na cykl
        self.dostawcy_cykli = []     # obiekt użyty w każdym cyklu
        self.cykli = None            # po tylu cyklach kończymy pętlę
        self.blad_cyklu = None       # fabryka wyjątku rzucanego przez `run_cycle`
        self.po_cyklu = None         # hak: co zrobić po n-tym cyklu

    # ---- podmianki
    def _polacz(self, url):
        p = _PolaczenieCyklu()
        self.polaczenia.append(p)
        return p

    def _klucz(self, conn, nazwa_sekretu, nazwa_env):
        assert conn is self.polaczenia[-1], "klucze czytane ze starego połączenia"
        return self.klucze.get(nazwa_sekretu)

    def _zbuduj(self, klucze):
        if not any(klucze.values()):
            return None
        d = _DostawcaAtrapa(klucze)
        self.zbudowane.append(d)
        return d

    async def _run_cycle(self, conn, ai, http, verbose, analizuj=True, silniki=None):
        self.dostawcy_cykli.append(ai)
        numer = len(self.dostawcy_cykli)
        if self.po_cyklu is not None:
            self.po_cyklu(numer)
        if self.blad_cyklu is not None:
            raise self.blad_cyklu()
        if self.cykli is not None and numer > self.cykli:
            raise _Koniec()
        return dict(self.STATS)

    def zainstaluj(self, monkeypatch, *argv):
        import sys as _sys
        import types

        monkeypatch.setenv("DATABASE_URL", "postgresql:///atrapa")
        monkeypatch.setattr(_sys, "argv", ["news_worker", "--quiet",
                                           "--interval", "0", *argv])
        monkeypatch.setattr(news_worker, "psycopg",
                            types.SimpleNamespace(connect=self._polacz))
        monkeypatch.setattr(news_worker, "_klucz", self._klucz)
        monkeypatch.setattr(news_worker, "zbuduj_dostawce", self._zbuduj)
        monkeypatch.setattr(news_worker, "run_cycle", self._run_cycle)
        monkeypatch.setattr(news_worker, "puls",
                            types.SimpleNamespace(bij=lambda *a, **k: None,
                                                  zglos_awarie=lambda *a, **k: None))
        return self

    def uruchom(self):
        """Zwraca kod wyjścia `main`; `_Koniec` znaczy „pętla przerwana testem"."""
        try:
            return asyncio.run(news_worker.main())
        except _Koniec:
            return None


def test_ten_sam_zestaw_kluczy_znaczy_TEN_SAM_obiekt_dostawcy():
    """Kanarek na zwarcie, które nie przeżywało cyklu.

    Bezpiecznik nie ma gdzie mieszkać poza instancją dostawcy, więc „ten sam
    obiekt" i „zwarcie na 60 minut naprawdę trwa 60 minut" to jedno zdanie.
    Przed poprawką `main` budował dostawcę przy każdym obrocie pętli i zwarcie
    trwało tyle, co cykl — pięć minut.
    """
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    try:
        s = _Stanowisko({"gemini_api_key": "g-1"}).zainstaluj(mp)
        s.cykli = 2
        s.uruchom()
    finally:
        mp.undo()

    assert len(s.polaczenia) >= 3, "test nie odtworzył kolejnych cykli"
    assert len(s.zbudowane) == 1, (
        f"dostawca zbudowany {len(s.zbudowane)} razy przy niezmienionych "
        f"kluczach — stan bezpiecznika i pula połączeń giną co cykl")
    assert s.dostawcy_cykli[0] is s.dostawcy_cykli[1], (
        "drugi cykl dostał inny obiekt niż pierwszy")
    assert s.zbudowane[0].zamkniety == 1, (
        "obiekt użyty do końca ma być zamknięty raz — na wyjściu z procesu")


def test_klucze_SA_CZYTANE_w_kazdym_cyklu_mimo_trwalego_dostawcy():
    """Druga strona tej samej poprawki, i to ona łatwo ginie.

    Klucz pojawia się w bazie wtedy, gdy użytkownik wpisze go w interfejsie.
    Trzymanie dostawcy między cyklami wolno pogodzić z tym tylko wtedy, gdy
    ODCZYT zostaje co cykl, a oszczędzamy wyłącznie na BUDOWIE. Sprawdzanie raz
    na starcie oznaczałoby proces pobierający artykuły jeszcze dobę po tym, jak
    klucz już jest.
    """
    from _pytest.monkeypatch import MonkeyPatch

    czytane = []
    mp = MonkeyPatch()
    try:
        s = _Stanowisko({"gemini_api_key": "g-1"}).zainstaluj(mp)
        prawdziwy = s._klucz
        mp.setattr(news_worker, "_klucz",
                   lambda conn, sekret, env: (czytane.append((len(s.polaczenia), sekret))
                                              or prawdziwy(conn, sekret, env)))
        s.cykli = 3
        s.uruchom()
    finally:
        mp.undo()

    cykle_z_odczytem = {nr for nr, _ in czytane}
    assert cykle_z_odczytem == {1, 2, 3, 4}, (
        f"klucze czytane tylko w cyklach {sorted(cykle_z_odczytem)} — nowy klucz "
        f"z interfejsu nie zadziała bez restartu poda")


def test_zmiana_klucza_PRZEBUDOWUJE_dostawce_i_ZAMYKA_poprzedniego():
    """Nowy klucz to jedyna rzecz, która naprawia awarię trwałą.

    Musi więc dać nowy obiekt — ze świeżym bezpiecznikiem, bo inaczej
    właściciel doładowuje konto i przez sześć godzin nic się nie dzieje. I musi
    zamknąć poprzedni: wymiana klucza bez zamknięcia byłaby po prostu kolejnym
    wyciekiem puli, tyle że rzadszym.
    """
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    try:
        s = _Stanowisko({"gemini_api_key": "g-1"}).zainstaluj(mp)
        s.cykli = 2
        # Użytkownik dopisuje klucz Anthropica po pierwszym cyklu.
        s.po_cyklu = lambda nr: (s.klucze.update(anthropic_api_key="a-1")
                                 if nr == 1 else None)
        s.uruchom()
    finally:
        mp.undo()

    assert len(s.zbudowane) == 2, (
        f"po zmianie klucza zbudowano {len(s.zbudowane)} dostawców — nowy klucz "
        f"nie zadziałał albo zadziałał bez przebudowy")
    stary, nowy = s.zbudowane
    assert stary.zamkniety == 1, (
        "poprzedni dostawca nie został zamknięty — jego pula połączeń zostaje "
        "otwarta do końca procesu")
    assert s.dostawcy_cykli[1] is nowy and s.dostawcy_cykli[1] is not stary
    assert nowy.klucze["anthropic"] == "a-1"


def test_zamkniecie_dostawcy_przy_WYJSCIU_z_procesu_takze_przy_once():
    """`--once` to jedyny obrót pętli, więc jedyna okazja na sprzątanie.

    Bez tego uruchomienie wsadowe kończyło się z otwartą pulą połączeń —
    ciszej niż pętla, ale tak samo bez domknięcia.
    """
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    try:
        s = _Stanowisko({"gemini_api_key": "g-1"}).zainstaluj(mp, "--once")
        kod = s.uruchom()
    finally:
        mp.undo()

    assert kod == 0
    assert len(s.zbudowane) == 1
    assert s.zbudowane[0].zamkniety == 1, "`--once` wyszedł bez zamknięcia dostawcy"


def test_zamkniecie_takze_po_wyczerpaniu_POD_RZAD_MAX():
    """Wyjście przez licznik awarii to WYJŚCIE — sprząta tak samo.

    Ta ścieżka kończy proces po to, żeby restart poda był widoczny; zostawienie
    na niej otwartej puli byłoby sprzątaniem wyłącznie na drodze szczęśliwej.
    """
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    try:
        s = _Stanowisko({"gemini_api_key": "g-1"}).zainstaluj(mp)
        s.blad_cyklu = lambda: RuntimeError("baza padła")
        kod = s.uruchom()
    finally:
        mp.undo()

    assert kod == 1, "pętla miała skończyć się po POD_RZAD_MAX nieudanych cyklach"
    assert len(s.zbudowane) == 1, (
        "awaria cyklu nie jest zmianą klucza — dostawca nie ma być przebudowany")
    assert s.zbudowane[0].zamkniety == 1


def test_blad_w_zamknij_NIE_PRZESLANIA_kodu_wyjscia():
    """Porażka sprzątania jest mniej ważna niż powód, dla którego sprzątamy.

    Wyjątek z `zamknij()` puszczony dalej zamieniłby czyste zakończenie
    w awarię, a przy wyjściu z powodu błędu przesłoniłby prawdziwą przyczynę —
    i kazałby szukać awarii w zamykaniu gniazda.
    """
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    try:
        s = _Stanowisko({"gemini_api_key": "g-1"}).zainstaluj(mp, "--once")
        s.po_cyklu = lambda nr: setattr(s.zbudowane[0], "blad_zamkniecia", True)
        kod = s.uruchom()
    finally:
        mp.undo()

    assert kod == 0, "błąd zamykania puli zamienił poprawne zakończenie w awarię"
    assert s.zbudowane[0].zamkniety == 1, "próba zamknięcia miała się odbyć"


# ============ dostawca żyje dłużej niż połączenie — i nie wolno ich sklejać ============

def _sciezka_do(obiekt, cel, glebokosc=4):
    """Ścieżka atrybutów od `obiekt` do `cel`, albo None. Wszerz, z limitem."""
    widziane, front = {id(obiekt)}, [(obiekt, "ai")]
    for _ in range(glebokosc):
        nastepny = []
        for o, sciezka in front:
            for nazwa, w in (vars(o).items() if hasattr(o, "__dict__") else ()):
                if w is cel:
                    return f"{sciezka}.{nazwa}"
                if id(w) not in widziane and hasattr(w, "__dict__"):
                    widziane.add(id(w))
                    nastepny.append((w, f"{sciezka}.{nazwa}"))
        front = nastepny
    return None


class _PolaczenieBezSekretow:
    """Baza bez wpisanych kluczy — `_klucz` spadnie wtedy na środowisko."""

    def execute(self, sql, params=None):
        return self

    def fetchone(self):
        return None


def test_dostawca_NIE_TRZYMA_polaczenia_z_ktoregos_cyklu():
    """Dostawca przeżywa cykl, połączenie z bazą — celowo nie.

    `psycopg.connect` jest w pętli świadomie: po restarcie bazy każdy kolejny
    cykl padał na tym samym zerwanym połączeniu, a proces szedł dalej i wyglądał
    na żywy. Dostawca, który przeżywa cykle, zatrzymałby takie połączenie na
    zawsze — ta sama awaria, tylko schowana w obiekcie, o którym nikt nie myśli.
    """
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    try:
        mp.setenv("GEMINI_API_KEY", "g-1")
        mp.delenv("ANTHROPIC_API_KEY", raising=False)
        conn = _PolaczenieBezSekretow()

        # Same klucze wychodzą z odczytu: nic z połączenia nie ma prawa
        # przeciec do `zbuduj_dostawce`.
        klucze = news_worker.odczytaj_klucze(conn)
        assert klucze == {"anthropic": None, "gemini": "g-1"}
        assert all(w is None or isinstance(w, str) for w in klucze.values())

        ai = news_worker.wybierz_dostawce(conn)
        try:
            sciezka = _sciezka_do(ai, conn)
            assert sciezka is None, (
                f"dostawca trzyma połączenie z tego cyklu pod `{sciezka}` — "
                f"przeżyje je i będzie pracował na zerwanym")
        finally:
            asyncio.run(ai.zamknij())
    finally:
        mp.undo()


# ============ nieudana przebudowa nie ma prawa zostawić ZAMKNIĘTEGO dostawcy ============

def test_nieudana_przebudowa_NIE_ZOSTAWIA_zamknietego_dostawcy(monkeypatch):
    """Kolejność „zamknij stary, potem zbuduj nowy" ma jeden stan, z którego
    nie ma wyjścia.

    Budowa potrafi paść: `DostawcaAnthropic` importuje pakiet `anthropic`
    leniwie, więc dopisanie klucza Anthropica w obrazie złożonym pod samo Gemini
    kończy się tutaj wyjątkiem. Przy starej kolejności stary dostawca był wtedy
    już ZAMKNIĘTY, a wyjątek przerywał przypisanie u wołającego: `ai` zostawało
    przy zamkniętym obiekcie, `klucze_ai` przy STARYM zestawie kluczy.

    I to jest cała pułapka: gdy właściciel cofnie nieudany klucz, zestaw znów
    zgadza się z zapamiętanym, warunek „bez zmian" oddaje tego zamkniętego
    dostawcę i każde wywołanie do końca życia procesu pada na zamkniętym
    kliencie. Cicho — w logu dalej stoi „Tryb: pobieranie i ANALIZA".
    """
    sekrety = {"gemini_api_key": "g-1"}
    monkeypatch.setattr(news_worker, "_klucz",
                        lambda conn, sekret, env: sekrety.get(sekret))

    def zbuduj(klucze):
        if klucze.get("anthropic"):
            # Obraz złożony pod samo Gemini nie ma z czego zbudować analizy.
            raise ImportError("No module named 'anthropic'")
        return _DostawcaAtrapa(klucze)

    monkeypatch.setattr(news_worker, "zbuduj_dostawce", zbuduj)

    stary = _DostawcaAtrapa({"anthropic": None, "gemini": "g-1"})
    klucze_ai = {"anthropic": None, "gemini": "g-1"}

    # Właściciel dopisuje klucz Anthropica — budowa pada.
    sekrety["anthropic_api_key"] = "a-1"
    with pytest.raises(ImportError):
        asyncio.run(news_worker.dostawca_na_cykle(None, stary, klucze_ai,
                                                  wymagany=True))

    assert stary.zamkniety == 0, (
        "nieudana przebudowa zamknęła dostawcę, którego wołający nadal trzyma — "
        "od tej chwili pracuje on na zamkniętym kliencie")

    # Właściciel cofa nieudany klucz. Zestaw znów zgadza się z zapamiętanym,
    # więc wraca TEN SAM obiekt - i musi być zdatny do użytku.
    sekrety.pop("anthropic_api_key")
    ai, klucze_po = asyncio.run(
        news_worker.dostawca_na_cykle(None, stary, klucze_ai, wymagany=True))

    assert ai is stary and klucze_po == klucze_ai
    assert stary.zamkniety == 0, (
        "dostawca wrócił do użytku zamknięty — worker milczy, a w logu dalej "
        "stoi, że analizuje")


def test_UDANA_przebudowa_dalej_zamyka_poprzedniego(monkeypatch):
    """Druga strona tej samej zmiany kolejności — i to ona łatwo ginie.

    Budowa przed zamknięciem nie może się zamienić w „budowa ZAMIAST
    zamknięcia": wymiana klucza bez domknięcia starej puli to po prostu ten sam
    wyciek, tylko rzadszy. Dwie pule naraz też nie istnieją — zamknięcie idzie
    zaraz po udanej budowie, w tej samej funkcji.
    """
    sekrety = {"gemini_api_key": "g-1"}
    monkeypatch.setattr(news_worker, "_klucz",
                        lambda conn, sekret, env: sekrety.get(sekret))
    monkeypatch.setattr(news_worker, "zbuduj_dostawce",
                        lambda klucze: _DostawcaAtrapa(klucze))

    stary = _DostawcaAtrapa({"anthropic": None, "gemini": "g-1"})
    klucze_ai = {"anthropic": None, "gemini": "g-1"}

    sekrety["gemini_api_key"] = "g-2"
    nowy, klucze_po = asyncio.run(
        news_worker.dostawca_na_cykle(None, stary, klucze_ai, wymagany=True))

    assert nowy is not stary, "zmiana klucza ma dać NOWY obiekt, ze świeżym bezpiecznikiem"
    assert nowy.klucze["gemini"] == "g-2"
    assert klucze_po == {"anthropic": None, "gemini": "g-2"}
    assert stary.zamkniety == 1, (
        "poprzedni dostawca nie został zamknięty — wymiana klucza byłaby "
        "kolejnym wyciekiem puli")
