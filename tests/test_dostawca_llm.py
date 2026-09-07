"""Testy warstwy dostawcy modelu jezykowego.

Nie wolaja zadnego API - sprawdzaja to, co przy wywolaniu prawdziwego API
byloby najtrudniejsze do zauwazenia: ze koszt jest policzony z WSZYSTKICH
platnych tokenow, ze schemat wychodzi w postaci, ktora dostawca przyjmie,
i ze odpowiedz uciecie na limicie nie udaje wyniku.
"""
import asyncio
import json

import httpx
import pytest

from services.ingest.llm import (
    BladOdpowiedzi, DostawcaGemini, Odmowa, _bez_additional_properties,
    limit_dobowy_usd,
)
from services.ingest.news_worker import ANALYSIS_SCHEMA, TRIAGE_SCHEMA


def gemini(odpowiedz: dict, status: int = 200) -> DostawcaGemini:
    """Dostawca z podstawionym transportem - bez sieci."""
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(status, json=odpowiedz)))
    return DostawcaGemini("klucz-testowy", http=http)


def uruchom(d: DostawcaGemini):
    """asyncio.run zamiast markera pytest-asyncio: kilka testow nie jest powodem,
    zeby dokladac zaleznosc do obrazu."""
    return asyncio.run(d.zapytaj(model="m", system="s", user="u",
                                 schema=TRIAGE_SCHEMA, max_tokens=100))


def odp(tekst: str, finish: str = "STOP", usage: dict | None = None) -> dict:
    return {
        "candidates": [{"finishReason": finish,
                        "content": {"parts": [{"text": tekst}]}}],
        "usageMetadata": usage or {"promptTokenCount": 100, "candidatesTokenCount": 50},
    }


# --------------------------------------------------------------- schemat

def test_schemat_traci_additionalProperties_ale_nic_wiecej():
    """`responseSchema` Gemini to podzbior OpenAPI - `additionalProperties`
    konczy sie bledem 400. Reszta schematu MUSI przejsc bez zmian, bo to ona
    wymusza pola, ktorych oczekuje zapis do bazy."""
    czysty = _bez_additional_properties(ANALYSIS_SCHEMA)
    assert "additionalProperties" not in czysty
    assert czysty["required"] == ANALYSIS_SCHEMA["required"]
    assert set(czysty["properties"]) == set(ANALYSIS_SCHEMA["properties"])
    assert czysty["properties"]["direction"]["enum"] == ["bullish", "bearish", "neutral"]


def test_czyszczenie_schematu_siega_w_glab():
    zagniezdzony = {"type": "object", "additionalProperties": False,
                    "properties": {"a": {"type": "object", "additionalProperties": False,
                                         "properties": {"b": {"type": "string"}}}},
                    "anyOf": [{"additionalProperties": True}]}
    czysty = _bez_additional_properties(zagniezdzony)
    assert json.dumps(czysty).count("additionalProperties") == 0
    assert czysty["properties"]["a"]["properties"]["b"]["type"] == "string"


def test_czyszczenie_nie_rusza_oryginalu():
    """Gdyby ciecie szlo w miejscu, sciezka Anthropica dostalaby schemat
    okrojony pod Gemini - i to bez zadnego bledu, po prostu slabiej wymuszony."""
    przed = json.dumps(TRIAGE_SCHEMA, sort_keys=True)
    _bez_additional_properties(TRIAGE_SCHEMA)
    assert json.dumps(TRIAGE_SCHEMA, sort_keys=True) == przed


# --------------------------------------------------------------- koszt

def test_tokeny_rozumowania_wchodza_do_kosztu():
    """KANAREK NA NAJDROZSZY BLAD TEJ WARSTWY.

    Modele 3.x placa za tokeny rozumowania stawka wyjscia, a raportuja je
    w OSOBNYM polu. Zmierzone na gemini-3.5-flash: 732 tokeny myslenia przy
    121 tokenach odpowiedzi. Liczenie samego `candidatesTokenCount` zanizaloby
    koszt siedmiokrotnie - a limit dobowy jest jedynym bezpiecznikiem przed
    petla, ktora analizuje w kolko to samo.
    """
    u = DostawcaGemini._uzycie({"promptTokenCount": 458,
                                "candidatesTokenCount": 121,
                                "thoughtsTokenCount": 732})
    assert u["output"] == 121 + 732, "tokeny rozumowania wypadly z rachunku"
    assert u["input"] == 458


def test_brak_pola_rozumowania_nie_wywraca_rachunku():
    u = DostawcaGemini._uzycie({"promptTokenCount": 56, "candidatesTokenCount": 60})
    assert u == {"input": 56, "output": 60, "cache_read": 0}


def test_nieznany_model_wyceniamy_najdrozej_ze_znanych():
    """Pomylka w te strone zatrzymuje potok na limicie. Pomylka w druga
    pozwala mu wydac wielokrotnosc limitu, zanim ktokolwiek to zauwazy."""
    d = DostawcaGemini("k")
    u = {"input": 1_000_000, "output": 1_000_000, "cache_read": 0}
    nieznany = d.koszt("gemini-99-turbo", u)
    assert nieznany >= max(d.koszt(m, u) for m in d.CENNIK)


def test_odczyt_z_cache_jest_tanszy_od_swiezego_wejscia():
    d = DostawcaGemini("k")
    swieze = d.koszt("gemini-3.5-flash", {"input": 100_000, "output": 0, "cache_read": 0})
    z_cache = d.koszt("gemini-3.5-flash", {"input": 100_000, "output": 0, "cache_read": 100_000})
    assert 0 < z_cache < swieze


def test_limit_dobowy_czyta_stara_nazwe_zmiennej(monkeypatch):
    """Na klastrze stoi juz ANTHROPIC_DAILY_USD. Gdyby nowa nazwa go nie
    honorowala, limit po cichu wrocilby do wartosci domyslnej."""
    monkeypatch.delenv("LLM_DAILY_USD", raising=False)
    monkeypatch.setenv("ANTHROPIC_DAILY_USD", "1.25")
    assert limit_dobowy_usd() == 1.25
    monkeypatch.setenv("LLM_DAILY_USD", "3.5")
    assert limit_dobowy_usd() == 3.5, "nowa nazwa ma miec pierwszenstwo"


# --------------------------------------------------------------- odpowiedzi

def test_poprawna_odpowiedz_wraca_jako_slownik():
    d = gemini(odp('{"worth_analyzing": true, "reason": "decyzja regulatora"}'))
    dane, usage = uruchom(d)
    assert dane["worth_analyzing"] is True
    assert usage["input"] == 100


def test_odpowiedz_ucieta_na_limicie_NIE_jest_wynikiem():
    """Bez tego warunku `json.loads` rzucalby blad skladni, a w logu widnialoby
    'cykl przerwany: JSONDecodeError' - komunikat, ktory nie mowi, co sie stalo,
    i kieruje szukanie w zla strone."""
    d = gemini(odp('{"worth_analyz', finish="MAX_TOKENS"))
    with pytest.raises(RuntimeError, match="ucięta na limicie"):
        uruchom(d)


def test_odmowa_jest_odpowiedzia_a_nie_awaria():
    d = gemini(odp("", finish="SAFETY"))
    with pytest.raises(Odmowa):
        uruchom(d)


def test_odmowa_niesie_zuzycie_tokenow():
    """Odrzucone zapytanie tez kosztuje. Gdyby zuzycie przepadalo, licznik
    wydatkow rozjechalby sie z rachunkiem u dostawcy dokladnie wtedy, gdy
    cos idzie nie tak - czyli gdy zapytan jest najwiecej."""
    d = gemini({"promptFeedback": {"blockReason": "SAFETY"},
                "usageMetadata": {"promptTokenCount": 77, "candidatesTokenCount": 0}})
    with pytest.raises(Odmowa) as e:
        uruchom(d)
    assert e.value.usage["input"] == 77


def test_pusta_odpowiedz_nie_udaje_wyniku():
    d = gemini(odp("   "))
    with pytest.raises(RuntimeError, match="pusta odpowiedź"):
        uruchom(d)


# ------------------------------------------------- koszt wywolan nieudanych

def test_blad_odpowiedzi_niesie_zuzycie_do_zapisu():
    """Wywolanie ucięte na limicie kosztuje tyle samo, co udane - i wraca
    przy kazdym cyklu nadrabiania zaleglosci, bo artykul dalej nie ma analizy.
    Bez zuzycia w wyjatku limit dobowy nie ma czego policzyc."""
    d = gemini(odp('{"worth', finish="MAX_TOKENS",
                   usage={"promptTokenCount": 460, "candidatesTokenCount": 4000}))
    with pytest.raises(BladOdpowiedzi) as e:
        uruchom(d)
    assert e.value.usage["input"] == 460
    assert e.value.usage["output"] == 4000
    assert d.koszt("gemini-3.5-flash", e.value.usage) > 0


def test_blad_bez_zuzycia_ma_zerowy_licznik_zamiast_None():
    """Zerowy slownik zamiast None: strona wolajaca liczy koszt bezwarunkowo,
    a `None['input']` byloby awaria w obsludze awarii."""
    b = BladOdpowiedzi("sieć padła")
    assert b.usage == {"input": 0, "output": 0, "cache_read": 0}


def test_odmowa_jest_bledem_odpowiedzi():
    """Wspolna nadklasa, zeby strona wolajaca miala jeden warunek na
    'wywolanie kosztowalo, a nic nie dalo'."""
    assert issubclass(Odmowa, BladOdpowiedzi)


# ------------------------------------------------------ limit tempa i ponawianie
#
# Zmierzone na produkcji 2026-09-07, pierwszy cykl po wpieciu klucza Gemini:
# jedna analiza przeszla, siedem kolejnych zapytan dostalo 429. W logu widnialo
# "przeanalizowanych 0, bledow 7" i proces szedl dalej - potok wygladal na
# dzialajacy i milczal.

def gemini_sekwencja(odpowiedzi: list, odstep: float = 0.0) -> tuple:
    """Dostawca oddajacy kolejne odpowiedzi z listy. Zwraca (dostawca, licznik)."""
    licznik = {"n": 0}

    def handler(req):
        i = min(licznik["n"], len(odpowiedzi) - 1)
        licznik["n"] += 1
        status, tresc, naglowki = odpowiedzi[i]
        return httpx.Response(status, json=tresc, headers=naglowki or {})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    d = DostawcaGemini("klucz-testowy", http=http)
    d.ODSTEP_S = odstep          # bez czekania w tescie
    return d, licznik


def bez_czekania(monkeypatch):
    """Usuwa realne spanie, zeby test odporny na 429 nie trwal minuty."""
    async def natychmiast(_s):
        return None
    monkeypatch.setattr(asyncio, "sleep", natychmiast)


DOBRA = (200, {"candidates": [{"finishReason": "STOP",
                               "content": {"parts": [{"text": '{"worth_analyzing": true, "reason": "x"}'}]}}],
               "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}}, None)
LIMIT = (429, {"error": {"code": 429, "message": "Too Many Requests"}}, None)


def test_zapytanie_po_limicie_jest_PONAWIANE(monkeypatch):
    """Kanarek na blad, ktory wlasnie zdarzyl sie na produkcji.

    Bez ponawiania pierwsze 429 konczylo caly artykul i szlo dalej - a limit
    tempa jest stanem PRZEJSCIOWYM, nie odmowa.
    """
    bez_czekania(monkeypatch)
    d, licznik = gemini_sekwencja([LIMIT, LIMIT, DOBRA])
    dane, _ = asyncio.run(d.zapytaj(model="m", system="s", user="u",
                                    schema=TRIAGE_SCHEMA, max_tokens=100))
    assert dane["worth_analyzing"] is True
    assert licznik["n"] == 3, f"powinny byc trzy proby, bylo {licznik['n']}"


def test_wyczerpane_proby_mowia_ZE_TO_LIMIT_a_nie_zly_klucz(monkeypatch):
    """Sam kod 429 w logu wyglada jak blad konfiguracji i kieruje szukanie
    w zla strone. Komunikat musi nazwac rzecz po imieniu."""
    bez_czekania(monkeypatch)
    d, licznik = gemini_sekwencja([LIMIT])
    with pytest.raises(RuntimeError, match="limit tempa albo dobowy limit klucza"):
        asyncio.run(d.zapytaj(model="m", system="s", user="u",
                              schema=TRIAGE_SCHEMA, max_tokens=100))
    assert licznik["n"] == d.PROBY, "ma probowac dokladnie PROBY razy, nie w nieskonczonosc"


def test_blad_klienta_NIE_jest_ponawiany(monkeypatch):
    """Kanarek do poprzednich. Zly klucz (401) albo zly schemat (400) to nie
    stan przejsciowy - ponawianie ich tylko opoznia komunikat o prawdziwej
    przyczynie i zuzywa limit."""
    bez_czekania(monkeypatch)
    d, licznik = gemini_sekwencja([(401, {"error": {"message": "bad key"}}, None)])
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(d.zapytaj(model="m", system="s", user="u",
                              schema=TRIAGE_SCHEMA, max_tokens=100))
    assert licznik["n"] == 1, f"401 ma isc bez ponawiania, prob bylo {licznik['n']}"


def test_retry_after_od_dostawcy_bije_nasze_zgadywanie():
    """On wie, kiedy okno limitu sie odnowi, my nie."""
    r = httpx.Response(429, headers={"Retry-After": "12"})
    assert DostawcaGemini._ile_czekac(r, proba=1) == 12.0
    # Bez naglowka - rosnace odstepy, z sufitem.
    goly = httpx.Response(429)
    kroki = [DostawcaGemini._ile_czekac(goly, p) for p in (1, 2, 3, 9)]
    assert kroki[0] < kroki[1] < kroki[2], f"odstepy maja rosnac, sa {kroki}"
    assert kroki[3] <= 60.0, "bez sufitu jedno zapytanie blokowaloby cykl na godziny"


def test_przepustnica_rozklada_zapytania_w_czasie(monkeypatch):
    """Ponawianie ratuje pojedyncze zapytanie; przepustnica sprawia, ze limit
    nie jest przekraczany w ogole. Bez niej cykl z 20 zaleglosciami wystrzeliwuje
    20 zapytan w sekunde i pierwsze kilkanascie wraca z 429."""
    spane = []

    async def zapisz(s):
        spane.append(s)

    monkeypatch.setattr(asyncio, "sleep", zapisz)
    d, _ = gemini_sekwencja([DOBRA], odstep=4.0)
    asyncio.run(d.zapytaj(model="m", system="s", user="u",
                          schema=TRIAGE_SCHEMA, max_tokens=100))
    asyncio.run(d.zapytaj(model="m", system="s", user="u",
                          schema=TRIAGE_SCHEMA, max_tokens=100))
    assert spane, "drugie zapytanie ma odczekac, pierwsze nie"
    assert 0 < spane[-1] <= 4.0
