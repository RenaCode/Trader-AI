"""Odpowiedź Anthropica ucięta na `max_tokens` NIE JEST wynikiem — i JEST płatna.

DLACZEGO TEN PLIK POWSTAŁ OSOBNO, SKORO `test_dostawca_llm.py` JUŻ ISTNIEJE

Bo tamten plik testuje ucięcie WYŁĄCZNIE dla Gemini
(`test_odpowiedz_ucieta_na_limicie_NIE_jest_wynikiem`, przez `finishReason ==
MAX_TOKENS`), a jego atrapa Anthropica — `anthropic_odmawiajacy` — ZAWSZE rzuca
wyjątek i nigdy nie oddaje odpowiedzi. Żaden test nie przechodził więc ścieżką
SUKCESU tego dostawcy, a to na niej siedział błąd. Atrapa, która nie potrafi
oddać odpowiedzi, nie może wykryć błędu w obsłudze odpowiedzi — niezależnie od
tego, ile testów ją wywołuje.

CO SIĘ DZIAŁO DO 2026-09-23

`DostawcaAnthropic.zapytaj` sprawdzał `stop_reason == "refusal"` i nic więcej.
`stop_reason == "max_tokens"` nie był sprawdzany w ogóle, więc:

    A  sama odpowiedź rozumowania → `next(b.text for b in r.content ...)` bez
       wartości domyślnej podnosiło `StopIteration`, które w funkcji
       asynchronicznej zamienia się w `RuntimeError: coroutine raised
       StopIteration` — komunikat nieniosący żadnej informacji,
    B  ucięty JSON → `json.loads` podnosiło `JSONDecodeError`.

Żaden z tych wyjątków nie niósł `usage`, więc `news_worker.zapisz_nieudane`
odpadało na warunku `if not u or not (u.get("input") or u.get("output"))`
i NIE WSTAWIAŁO wiersza do `news.api_spend`. Spalone tokeny były niewidoczne dla
OBU bramek dobowych — dokładnie dla tego wywołania, które potrafi się powtarzać
(3 próby na artykuł × N artykułów). Nie był to też `Przeciazony`, więc
bezpiecznik nie miał czego zewrzeć.

Analiza chodzi z `thinking={"type": "adaptive"}`, a tokeny rozumowania liczą się
do TEGO SAMEGO `max_tokens` — przy 4000 dłuższy artykuł wyczerpywał limit na
samym myśleniu, zanim padło pierwsze słowo wyniku.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from services.ingest import news_worker
from services.ingest.llm import BladOdpowiedzi, DostawcaAnthropic, Odmowa

KORZEN = Path(__file__).resolve().parents[1]
ZRODLO_LLM = (KORZEN / "services/ingest/llm.py").read_text(encoding="utf-8")
ZRODLO_WORKER = (KORZEN / "services/ingest/news_worker.py").read_text(encoding="utf-8")


# --------------------------------------------------- atrapa ODDAJĄCA odpowiedź

class _Uzycie:
    """`r.usage` w kształcie, jaki naprawdę wypełnia SDK."""

    def __init__(self, wejscie: int = 460, wyjscie: int = 4000, cache: int = 0):
        self.input_tokens = wejscie
        self.output_tokens = wyjscie
        self.cache_read_input_tokens = cache


class _Blok:
    def __init__(self, typ: str, tekst: str = ""):
        self.type = typ
        self.text = tekst


class _Odpowiedz:
    def __init__(self, stop_reason: str, content: list, usage: _Uzycie | None = None):
        self.stop_reason = stop_reason
        self.content = content
        self.usage = usage or _Uzycie()


def anthropic_oddajacy(odpowiedz: _Odpowiedz):
    """DostawcaAnthropic z klientem, który ODDAJE odpowiedź — bez sieci.

    To jest cała różnica wobec `anthropic_odmawiajacy` z `test_dostawca_llm.py`:
    tamten podnosi wyjątek, więc bada klasyfikację BŁĘDÓW HTTP. Tu badamy, co
    kod robi z odpowiedzią, którą dostał — czyli ścieżkę, na której siedział błąd.
    """

    class _Klient:
        def __init__(self):
            self.wywolan = 0
            self.messages = self

        async def create(self, **kw):
            self.wywolan += 1
            self.ostatnie_kw = kw
            return odpowiedz

    d = DostawcaAnthropic("klucz-testowy")
    d._c = _Klient()
    return d, d._c


def _pytaj(d, model="claude-opus-5", max_tokens=4000):
    return asyncio.run(d.zapytaj(model=model, system="s", user="u",
                                 schema={"type": "object"}, max_tokens=max_tokens))


# ------------------------------------------------------------ ucięcie na limicie

def test_UCIETA_ODPOWIEDZ_anthropica_nie_udaje_wyniku():
    """Ucięty JSON kończył się `JSONDecodeError` — komunikatem, który nie mówi,
    co się stało, i kieruje szukanie w stronę schematu zamiast limitu."""
    d, _ = anthropic_oddajacy(_Odpowiedz(
        "max_tokens", [_Blok("text", '{"worth_analyz')]))

    with pytest.raises(BladOdpowiedzi, match="ucięta na limicie"):
        _pytaj(d)


def test_UCIECIE_NIESIE_ZUZYCIE_bo_bez_niego_dziennik_nie_widzi_tych_pieniedzy():
    """To jest sedno znaleziska, a nie ładniejszy komunikat.

    `news_worker.zapisz_nieudane` wstawia wiersz do `news.api_spend` TYLKO
    wtedy, gdy wyjątek niesie niezerowe `usage`. Wyjątek bez zużycia odpada na
    pierwszym warunku i wywołanie znika z obu bramek dobowych — akurat to,
    które wraca przy każdym nadrabianiu zaległości.
    """
    d, _ = anthropic_oddajacy(_Odpowiedz(
        "max_tokens", [_Blok("text", '{"worth')],
        _Uzycie(wejscie=460, wyjscie=4000, cache=0)))

    with pytest.raises(BladOdpowiedzi) as e:
        _pytaj(d)

    assert e.value.usage["input"] == 460
    assert e.value.usage["output"] == 4000
    assert d.koszt("claude-opus-5", e.value.usage) > 0, (
        "zużycie ma dać się wycenić — na tej kwocie stoi bramka dobowa")


def test_ZUZYCIE_Z_UCIECIA_TRAFIA_DO_DZIENNIKA_api_spend():
    """Domknięcie drogi: wyjątek → `zapisz_nieudane` → wiersz w dzienniku.

    Sam fakt, że wyjątek niesie `usage`, jeszcze niczego nie dowodzi — dowodzi
    dopiero wiersz, bo to jego liczy bramka.
    """
    d, _ = anthropic_oddajacy(_Odpowiedz(
        "max_tokens", [_Blok("text", "{")], _Uzycie(460, 4000)))
    try:
        _pytaj(d)
    except BladOdpowiedzi as e:
        blad = e

    class _Baza:
        def __init__(self):
            self.sql = []

        def execute(self, sql, params=None):
            self.sql.append((" ".join(sql.split()), params))

        def commit(self):
            pass

    baza = _Baza()
    budzet = news_worker.Budzet(0.0, 0)
    koszt = news_worker.zapisz_nieudane(baza, baza, d, "claude-opus-5",
                                        "analysis", blad, budzet=budzet)

    wpisy = [p for s, p in baza.sql if s.startswith("INSERT INTO news.api_spend")]
    assert len(wpisy) == 1, (
        "wywołanie ucięte na limicie nie zostawiło śladu w dzienniku — "
        "bramka dobowa nie ma czego policzyć")
    assert koszt > 0
    assert budzet.usd == koszt


def test_SAMO_ROZUMOWANIE_BEZ_BLOKU_TEXT_nie_wywraca_sie_na_StopIteration():
    """Drugi kształt tej samej awarii.

    `next(b.text for b in r.content if b.type == "text")` bez wartości domyślnej
    podnosiło `StopIteration`, a to w funkcji asynchronicznej zamienia się
    w `RuntimeError: coroutine raised StopIteration` — wyjątek nazywający
    mechanikę Pythona zamiast przyczyny, i bez `usage`.
    """
    d, _ = anthropic_oddajacy(_Odpowiedz(
        "end_turn", [_Blok("thinking")], _Uzycie(300, 1200)))

    with pytest.raises(BladOdpowiedzi) as e:
        _pytaj(d)

    assert "StopIteration" not in str(e.value)
    assert e.value.usage["output"] == 1200, (
        "brak bloku `text` też jest płatny — bez zużycia dziennik znów milczy")


def test_ODMOWA_dalej_jest_odmowa_a_nie_bledem_odpowiedzi():
    """Granica poprawki: `refusal` ma zostać `Odmowa`.

    `Odmowa` znaczy „ten artykuł nie idzie dalej, następny próbujemy normalnie"
    i kończy się werdyktem `rejected`; `BladOdpowiedzi` znaczy „spróbuj jeszcze
    raz" i kończy się `failed` z licznikiem prób. Zlanie ich w jedno kupowałoby
    trzy płatne powtórki odmowy, która za każdym razem będzie ta sama.
    """
    d, _ = anthropic_oddajacy(_Odpowiedz("refusal", [_Blok("text", "{}")]))

    with pytest.raises(Odmowa):
        _pytaj(d)


def test_POPRAWNA_ODPOWIEDZ_dalej_wraca_jako_slownik():
    """Kanarek na nadgorliwość: poprawki na ścieżce błędu nie wolno okupić
    zepsuciem ścieżki sukcesu, której do dziś nie testował żaden test."""
    d, _ = anthropic_oddajacy(_Odpowiedz(
        "end_turn", [_Blok("thinking"), _Blok("text", '{"worth_analyzing": true}')],
        _Uzycie(100, 50, 20)))

    dane, usage = _pytaj(d)

    assert dane == {"worth_analyzing": True}
    assert usage == {"input": 100, "output": 50, "cache_read": 20}


# ------------------------------------------------- limit tokenów samej analizy

def _max_tokens_w_funkcji(nazwa: str) -> int:
    """Wartość `max_tokens=` przekazywana przez daną funkcję workera.

    Czytamy ŹRÓDŁO, a nie wywołujemy funkcji, bo `analyze` dostaje dostawcę
    i wywołuje go od razu — sprawdzenie przez wywołanie byłoby testem atrapy,
    a nie liczby wpisanej w kodzie.
    """
    drzewo = ast.parse(ZRODLO_WORKER)
    for w in ast.walk(drzewo):
        if not isinstance(w, ast.AsyncFunctionDef) or w.name != nazwa:
            continue
        for wezel in ast.walk(w):
            if isinstance(wezel, ast.keyword) and wezel.arg == "max_tokens":
                return ast.literal_eval(wezel.value)
    raise AssertionError(f"nie znaleziono `max_tokens` w `{nazwa}`")


def test_LIMIT_ANALIZY_miesci_rozumowanie_razem_z_odpowiedzia():
    """4000 tokenów na analizę z rozszerzonym myśleniem to za mało.

    Tokeny rozumowania liczą się do tego samego limitu co odpowiedź, więc
    dłuższy artykuł wyczerpywał go, zanim padło pierwsze słowo wyniku. Ucięta
    odpowiedź nie jest wynikiem, ale JEST płatna i wraca przy każdym nadrabianiu
    zaległości: ten sam artykuł kupował sobie trzy takie wywołania
    (PROBY_PO_AWARII_MAX) i żadne nie mogło się udać.

    16000 to zdrowy domyśl dla wywołania BEZ strumieniowania — mieści się pod
    limitem czasu klienta (180 s), więc nie zamienia jednej awarii w drugą.
    Podniesienie limitu nie podnosi zużycia: model kończy tam, gdzie ma co
    powiedzieć, a limit jest sufitem, nie zamówieniem.
    """
    assert _max_tokens_w_funkcji("analyze") >= 16000


# --------------------------------------------------------------------- cennik

def test_STAWKA_SONNETA_5_jest_stawka_SONNETA_5_a_nie_poprzedniej_generacji():
    """W `CENNIK` siedziały $3/$15 — to taryfa Sonneta 4.6. Sonnet 5 to $2/$10.

    Pomyłka szła w stronę bezpieczną (zawyżała koszt o 50%, czyli zamykała
    bramkę kwotową przedwcześnie), ale dziennik `news.api_spend` przez nią
    kłamał — a na tym dzienniku stoi zarówno bramka, jak i `/api/api-spend`.
    """
    assert DostawcaAnthropic.CENNIK["claude-sonnet-5"] == (2.0, 10.0)


def test_NIEZNANY_MODEL_anthropica_wycenia_sie_DROZEJ_niz_kazdy_znany():
    """`CENNIK_DOMYSLNY` dziedziczony z `Dostawca` to (5.0, 25.0), czyli
    DOKŁADNIE stawka opusa — a u Anthropica są modele droższe (taryfa klasy
    Fable to $10/$50).

    Skutek zaniżenia opisuje komentarz przy `DostawcaGemini.CENNIK_DOMYSLNY`:
    pomyłka w stronę zawyżenia zatrzymuje potok na limicie, pomyłka w drugą
    stronę pozwala mu wydać wielokrotność limitu, zanim ktokolwiek to zauważy.
    Nowsza rodzina modeli to dokładnie ta wartość, którą ktoś wpisze do chartu.

    Porównujemy KOSZT, a nie samą krotkę: to koszt trafia do dziennika i do
    bramki, więc to on musi być górnym oszacowaniem.
    """
    d = DostawcaAnthropic.__new__(DostawcaAnthropic)     # bez klienta i klucza
    u = {"input": 1_000_000, "output": 1_000_000, "cache_read": 0}
    nieznany = d.koszt("claude-cos-czego-jeszcze-nie-ma", u)
    najdrozszy_znany = max(d.koszt(m, u) for m in DostawcaAnthropic.CENNIK)

    assert nieznany > najdrozszy_znany, (
        f"nieznany model wycenia się na ${nieznany:.2f}, a najdroższy znany na "
        f"${najdrozszy_znany:.2f} — wycena nieznanego ma ZAWYŻAĆ, inaczej "
        f"bramka przestaje wiązać dokładnie wtedy, gdy jest potrzebna")


def test_MODELE_W_KODZIE_sa_z_aktualnej_rodziny():
    """Kanarek na identyfikatory, nie na cennik.

    Identyfikator modelu nie ma sufiksu z datą (`claude-opus-5`, nie
    `claude-opus-5-20260401`) — wersja datowana nie jest w `CENNIK`, więc
    wyceniłaby się po stawce domyślnej i dziennik znów kłamałby.
    """
    for model in DostawcaAnthropic.CENNIK:
        assert not model[-1].isdigit() or "-20" not in model, (
            f"{model!r} wygląda na identyfikator z datą — takie nie są "
            f"używane i nie mają wpisu w cenniku")
    assert "claude-opus-5" in ZRODLO_LLM, (
        "domyślny model analizy zniknął ze źródła — sprawdź, czy nie zastąpił "
        "go identyfikator spoza CENNIK-a")
