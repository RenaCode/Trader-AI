"""Bramka dobowa ma liczyć to, co liczy dostawca: ŻĄDANIA HTTP, nie wywołania.

CO SIĘ NIE ZGADZAŁO

`news.api_spend` dostaje jeden wiersz na WYWOŁANIE LOGICZNE — jeden triaż, jedna
analiza. Dostawcy liczą co innego:

  * Gemini: `_poslij` ponawia do `PROBY = 5` razy, czyli jedno logiczne
    wywołanie triażu to do PIĘCIU POST-ów do Google,
  * Anthropic: `AsyncAnthropic(..., max_retries=1)` ponawia WEWNĄTRZ
    `messages.create` i z zewnątrz tego nie widać w ogóle.

Do tego wywołanie zakończone `Przeciazony` / `Wyczerpany` / timeoutem nie niesie
`usage`, więc `news_worker.zapisz_nieudane` nie wstawia wiersza W OGÓLE — takie
żądania nie były policzone nawet jako jedno.

Skutek nazywa komentarz przy `limit_wywolan_dobowy`: „Bramka, ktora mierzy nie te
wielkosc co dostawca, nie zatrzyma niczego przed nim — zatrzyma dopiero po
fakcie." Doba z presją limitu tempa to 500 wywołań logicznych i do 2500 żądań do
Google; bramka czytała 500/1500 i meldowała „wykorzystanie 33%", gdy darmowy próg
500/dobę był już przekroczony pięciokrotnie. 11.09.2026 potok stanął przy $0,114
z $5 — zatrzymał go próg dostawcy, którego żadna z dwóch bramek nie widziała.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import httpx
import pytest

from services.ingest import llm, news_worker
from services.ingest.llm import DostawcaGemini, Przeciazony

KORZEN = Path(__file__).resolve().parents[1]
ZRODLO = (KORZEN / "services/ingest/llm.py").read_text(encoding="utf-8")


@pytest.fixture
def bez_czekania(monkeypatch):
    """Ponawianie bez odstępów — badamy LICZNIK, nie zegar.

    Prawdziwe odstępy (5, 10, 20, 40 s) plus przepustnica to ponad minuta na
    jeden test. Zerujemy je na poziomie metod dostawcy, a nie podmieniając
    `asyncio.sleep` globalnie: podmiana globalna dotknęłaby też `wait_for`
    i innych testów w tym samym procesie.
    """
    monkeypatch.setattr(DostawcaGemini, "_ile_czekac",
                        staticmethod(lambda r, proba: 0.0))

    async def _natychmiast(self):
        return None

    monkeypatch.setattr(DostawcaGemini, "_przepustnica", _natychmiast)


def _gemini_odbijajacy(status: int, tresc: dict) -> DostawcaGemini:
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(status, json=tresc)))
    return DostawcaGemini("klucz-testowy", http=http)


def _pytaj(d):
    return asyncio.run(d.zapytaj(model="gemini-3.5-flash-lite", system="s",
                                 user="u", schema={"type": "object"},
                                 max_tokens=100))


LIMIT_TEMPA = {"error": {"code": 429, "message": "Resource exhausted",
                         "status": "RESOURCE_EXHAUSTED"}}


# ================================================= licznik po stronie dostawcy

def test_PIEC_PONOWIEN_TO_PIEC_ZADAN_a_nie_jedno(bez_czekania):
    """Rdzeń znaleziska.

    Pięć POST-ów do Google zjada pięć jednostek jego progu dobowego, a dziennik
    `api_spend` nie dostaje z nich ani jednego wiersza — wyjątek `Przeciazony`
    nie niesie `usage`.
    """
    d = _gemini_odbijajacy(429, LIMIT_TEMPA)

    with pytest.raises(Przeciazony):
        _pytaj(d)

    assert d._zadan_http == DostawcaGemini.PROBY, (
        f"dostawca policzył {d._zadan_http} żądań przy {DostawcaGemini.PROBY} "
        f"próbach — ponowienia są dla bramki niewidoczne")
    assert d.zadania_poza_dziennikiem() == DostawcaGemini.PROBY, (
        "żadne z tych żądań nie trafiło do `api_spend`, więc wszystkie są "
        "nadwyżką, której bramka musi się dowiedzieć")


def test_UDANE_WYWOLANIE_nie_zostawia_nadwyzki(bez_czekania):
    """Granica: jedno żądanie, jeden wiersz — nadwyżka zero.

    Bez tego warunku bramka liczyłaby każde udane wywołanie dwa razy
    i zamykałaby potok po połowie limitu.
    """
    d = _gemini_odbijajacy(200, {
        "candidates": [{"finishReason": "STOP",
                        "content": {"parts": [{"text": '{"worth_analyzing": true}'}]}}],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 50}})

    _pytaj(d)
    assert d._zadan_http == 1

    news_worker.Budzet(0.0, 0, d).dolicz(0.001)      # wiersz w dzienniku
    assert d.zadania_poza_dziennikiem() == 0


def test_ODMOWA_TRWALA_TEZ_JEST_ZADANIEM(bez_czekania):
    """Wywołanie odbite na 400 z martwym kluczem nie niesie `usage`, nie dostaje
    wiersza w dzienniku — a poszło do dostawcy i zjadło jednostkę jego progu."""
    d = _gemini_odbijajacy(400, {"error": {"code": 400,
                                           "message": "API key not valid"}})

    with pytest.raises(llm.Wyczerpany):
        _pytaj(d)

    assert d.zadania_poza_dziennikiem() == 1


# ========================================================== bramka po stronie workera

class _DostawcaZLicznikiem(llm.Dostawca):
    """Sam licznik, bez transportu — bramka nie potrzebuje niczego więcej."""

    nazwa = "atrapa"


def test_BRAMKA_ZAMYKA_SIE_NA_PONOWIENIACH_a_nie_dopiero_po_fakcie():
    """Dokładnie scenariusz z audytu: dziennik daleko od progu, dostawca za nim.

    Przy starym pomiarze `wyczerpany()` patrzyłby na 600 wierszy z 1500
    i przepuścił — podczas gdy dostawca policzył sobie już 1500 żądań.
    """
    d = _DostawcaZLicznikiem()
    wierszy = 600
    d.zanotuj_zadanie(news_worker.DZIENNY_LIMIT_WYWOLAN)
    d.zanotuj_rozliczenie(wierszy)

    budzet = news_worker.Budzet(0.0, wierszy, d)

    assert budzet.zadania() >= news_worker.DZIENNY_LIMIT_WYWOLAN
    powod = budzet.wyczerpany()
    assert powod, (
        "bramka przepuściła potok, który wysłał do dostawcy tyle żądań, ile "
        "wynosi cały limit dobowy — bo w dzienniku widziała tylko 600 wierszy")
    assert str(wierszy) in powod, (
        "komunikat ma pokazywać OBIE liczby: rozjazd między żądaniami "
        "a wierszami jest tu informacją, a nie szczegółem")


def test_BRAMKA_NIE_ZAMYKA_SIE_PRZEDWCZESNIE():
    """Kanarek na drugą stronę pomyłki.

    Bramka, która liczy każde udane wywołanie dwa razy, zatrzymuje zdrowy potok
    w połowie doby — a zdrowa doba to 503-516 wywołań przy limicie 1500.
    """
    d = _DostawcaZLicznikiem()
    for _ in range(500):
        d.zanotuj_zadanie()
    budzet = news_worker.Budzet(0.0, 0, d)
    for _ in range(500):
        budzet.dolicz(0.0002)

    assert budzet.zadania() == 500, (
        f"500 żądań i 500 wierszy dało {budzet.zadania()} — nadwyżka liczy "
        f"to samo żądanie drugi raz")
    assert budzet.wyczerpany() == ""


def test_BUDZET_BEZ_DOSTAWCY_dalej_dziala():
    """Domyśl istnieje po to, żeby testy bramki kwotowej nie budowały dostawcy.
    Produkcja podaje go zawsze (`run_cycle`)."""
    b = news_worker.Budzet(0.0, 0)
    b.dolicz(0.5)
    assert b.zadania() == 1


def test_RUN_CYCLE_PODAJE_DOSTAWCE_DO_BUDZETU():
    """Kanarek wiążący bramkę z produkcją.

    `Budzet` z dostawcą liczy poprawnie, ale jest bezużyteczny, dopóki
    `run_cycle` buduje go bez niego — a wtedy bramka po cichu wraca do starego,
    zaniżonego pomiaru.
    """
    zrodlo = (KORZEN / "services/ingest/news_worker.py").read_text(encoding="utf-8")
    drzewo = ast.parse(zrodlo)
    for w in ast.walk(drzewo):
        if (isinstance(w, ast.Call)
                and getattr(w.func, "id", None) == "Budzet"
                and len(w.args) >= 3):
            return
    pytest.fail(
        "`run_cycle` buduje `Budzet` bez dostawcy — bramka widzi wtedy same "
        "wiersze dziennika i nie wie nic o ponowieniach")


# ======================================== licznik żądań po stronie Anthropica

def test_MIDDLEWARE_LICZY_KAZDA_PROBE_SDK():
    """SDK ponawia wewnątrz `messages.create` i z zewnątrz tego nie widać.

    Middleware klienta chodzi raz na KAŻDĄ próbę wewnątrz jego własnej pętli
    ponowień (`_middleware.CallNext`: „The middleware chain runs inside the
    SDK's retry loop, once per attempt"), więc jest to jedyne miejsce, z którego
    da się policzyć to, co naprawdę liczy dostawca.
    """
    d = _DostawcaZLicznikiem()
    licznik = llm._LicznikZadanHTTP(d)

    async def dalej(request):
        return "odpowiedź"

    asyncio.run(licznik(None, dalej))
    asyncio.run(licznik(None, dalej))

    assert d._zadan_http == 2


def test_MIDDLEWARE_LICZY_TAKZE_ZADANIE_ZERWANE():
    """Timeout i zerwane połączenie nie mają odpowiedzi do zwrócenia, więc
    `call_next` rzuca. Żądanie i tak poszło i i tak zjadło próg dostawcy —
    liczenie PO wywołaniu gubiłoby dokładnie te przypadki."""
    d = _DostawcaZLicznikiem()
    licznik = llm._LicznikZadanHTTP(d)

    async def zrywa(request):
        raise TimeoutError("gniazdo milczy")

    with pytest.raises(TimeoutError):
        asyncio.run(licznik(None, zrywa))

    assert d._zadan_http == 1


def test_KLIENT_ANTHROPICA_MA_WPIETY_LICZNIK():
    """Kanarek jak `test_klient_anthropica_ma_OGRANICZONY_czas_oczekiwania`:
    czyta źródło, bo pakiet `anthropic` jest tylko w obrazie."""
    drzewo = ast.parse(ZRODLO)
    wywolania = [w for w in ast.walk(drzewo)
                 if isinstance(w, ast.Call)
                 and getattr(w.func, "id", None) == "AsyncAnthropic"]
    assert wywolania, "nie znaleziono konstrukcji klienta Anthropica"
    for w in wywolania:
        nazwane = {k.arg for k in w.keywords}
        assert "middleware" in nazwane, (
            "klient bez middleware nie liczy własnych ponowień — bramka dobowa "
            "widzi wtedy jedno żądanie tam, gdzie SDK wysłało dwa")


def test_MIESZANKA_SUMUJE_ZADANIA_OBU_SKLADOWYCH():
    """Mieszanka sama nie wysyła nic — liczą jej składowe, a rozliczenia
    przychodzą do NIEJ (bo `Budzet` zna tylko obiekt, którego pyta potok)."""

    class _Skladowa(llm.Dostawca):
        def __init__(self, nazwa, triaz, analiza):
            self.nazwa = nazwa
            self.model_triazu = triaz
            self.model_analizy = analiza

    triaz = _Skladowa("g", "g-triaz", "g-analiza")
    analiza = _Skladowa("a", "a-triaz", "a-analiza")
    m = llm.DostawcaMieszany(triaz, analiza)

    triaz.zanotuj_zadanie(5)
    analiza.zanotuj_zadanie(2)
    m.zanotuj_rozliczenie(3)

    assert m.zadania_poza_dziennikiem() == 4
