"""Dostawca modelu jezykowego dla analizy wiadomosci.

DLACZEGO TA WARSTWA ISTNIEJE

Analiza wiadomosci byla napisana wprost na kliencie Anthropica: `messages.create`
z `output_config`, `thinking` i `cache_control` siedzialy w `news_worker`. Dopoki
dostawca byl jeden, nie bylo w tym nic zlego. Gdy pojawil sie drugi klucz - Gemini,
na czas oczekiwania na Anthropica - okazalo sie, ze zamiana dostawcy wymaga zmian
w piecu miejscach potoku, ktory dziala na produkcji.

Interfejs jest WASKI CELOWO. Dostawca umie dwie rzeczy: zadac pytanie z wymuszonym
schematem odpowiedzi i policzyc, ile to kosztowalo. Prompty, schematy, progi
istotnosci i cala reszta zadania zostaja w `news_worker` - inaczej dodanie
trzeciego dostawcy oznaczaloby przepisanie promptow po raz trzeci.

CZEGO TA WARSTWA NIE ZALATWIA

Analiza z dwoch roznych modeli NIE JEST tym samym pomiarem. Cecha `news_sent`
zbudowana w polowie z ocen Gemini, a w polowie z ocen Claude'a, ma w srodku
uskok, ktorego model nie widzi - a kalibracja wagi zaklada, ze cecha znaczy przez
caly czas to samo. Dlatego kolumna `news.analysis.model` zapisuje model przy
KAZDYM wierszu i zmiana dostawcy jest w logu wykrzyczana, a nie cicha.
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx


class BladOdpowiedzi(RuntimeError):
    """Wywolanie doszlo do skutku, ale wynik jest bezuzyteczny.

    NIESIE ZUZYCIE TOKENOW, bo nieudane wywolanie tez kosztuje. Do 2026-09-07
    tabela `news.api_spend` miala kolumne `ok`, do ktorej nikt nigdy nie zapisal
    `false`: nieudane wywolanie nie bylo zapisywane w ogole. Limit dobowy - jedyny
    bezpiecznik przed petla palaca budzet - byl wiec slepy dokladnie na te
    wywolania, ktore potrafia sie powtarzac w kolko.
    """

    def __init__(self, powod: str, usage: dict | None = None):
        super().__init__(powod)
        self.usage = usage or {"input": 0, "output": 0, "cache_read": 0}


class Odmowa(BladOdpowiedzi):
    """Model odmowil odpowiedzi. Nie jest to awaria - jest to odpowiedz."""

    def __init__(self, usage: dict, powod: str = "model odmówił odpowiedzi"):
        super().__init__(powod, usage)


def limit_dobowy_usd() -> float:
    """Twardy limit wydatkow na dobe.

    `ANTHROPIC_DAILY_USD` zostaje jako nazwa zapasowa, bo tak nazywa sie zmienna
    juz ustawiona na klastrze; nowa nazwa jest neutralna wobec dostawcy.
    """
    for nazwa in ("LLM_DAILY_USD", "ANTHROPIC_DAILY_USD"):
        v = os.environ.get(nazwa)
        if v:
            return float(v)
    return 5.0


def _bez_additional_properties(schema: Any) -> Any:
    """Schemat w wersji, ktora przyjmuje Gemini.

    `additionalProperties` jest w JSON Schema, ale nie w podzbiorze OpenAPI,
    ktorego uzywa `responseSchema`. Wyslanie go konczy sie bledem 400, wiec
    zamiast utrzymywac drugi schemat - z ryzykiem, ze rozjedzie sie z pierwszym -
    tniemy pole w locie.
    """
    if isinstance(schema, dict):
        return {k: _bez_additional_properties(v) for k, v in schema.items()
                if k != "additionalProperties"}
    if isinstance(schema, list):
        return [_bez_additional_properties(v) for v in schema]
    return schema


class Dostawca:
    """Wspolny interfejs. Podklasa dostarcza transport i cennik."""

    nazwa: str = ""
    model_analizy: str = ""
    model_triazu: str = ""
    # USD za milion tokenow: model -> (wejscie, wyjscie)
    CENNIK: dict[str, tuple[float, float]] = {}
    CENNIK_DOMYSLNY: tuple[float, float] = (5.0, 25.0)

    async def zapytaj(self, *, model: str, system: str, user: str,
                      schema: dict, max_tokens: int) -> tuple[dict, dict]:
        raise NotImplementedError

    def koszt(self, model: str, usage: dict) -> float:
        inp, out = self.CENNIK.get(model, self.CENNIK_DOMYSLNY)
        # Odczyt z cache kosztuje ~10% ceny wejscia u obu dostawcow.
        billed_in = (usage["input"] - usage["cache_read"]) + usage["cache_read"] * 0.1
        return round(billed_in / 1e6 * inp + usage["output"] / 1e6 * out, 6)

    async def zamknij(self) -> None:
        return None


class DostawcaAnthropic(Dostawca):
    nazwa = "anthropic"

    CENNIK = {
        "claude-opus-5": (5.0, 25.0),
        "claude-sonnet-5": (3.0, 15.0),
        "claude-haiku-4-5": (1.0, 5.0),
    }

    def __init__(self, klucz: str):
        from anthropic import AsyncAnthropic

        self._c = AsyncAnthropic(api_key=klucz)
        self.model_analizy = os.environ.get("ANALYSIS_MODEL", "claude-opus-5")
        self.model_triazu = os.environ.get("TRIAGE_MODEL", "claude-haiku-4-5")

    async def zapytaj(self, *, model: str, system: str, user: str,
                      schema: dict, max_tokens: int) -> tuple[dict, dict]:
        kw: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            # Prefiks systemowy jest miedzy wywolaniami staly, wiec trafia
            # w cache promptow - okolo dziesieciokrotnie tanszy przy odczycie.
            "system": [{"type": "text", "text": system,
                        "cache_control": {"type": "ephemeral"}}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
            "messages": [{"role": "user", "content": user}],
        }
        if model == self.model_analizy:
            kw["thinking"] = {"type": "adaptive"}
            kw["output_config"]["effort"] = "medium"
        r = await self._c.messages.create(**kw)
        usage = {
            "input": r.usage.input_tokens,
            "output": r.usage.output_tokens,
            "cache_read": getattr(r.usage, "cache_read_input_tokens", 0) or 0,
        }
        if r.stop_reason == "refusal":
            raise Odmowa(usage)
        return json.loads(next(b.text for b in r.content if b.type == "text")), usage


class DostawcaGemini(Dostawca):
    """Google Gemini przez REST.

    Bez pakietu SDK: potok ma juz `httpx`, a jedyne, czego tu potrzeba, to jedno
    wywolanie POST. Dodatkowa zaleznosc w obrazie kosztuje wiecej niz te
    trzydziesci linii.
    """

    nazwa = "gemini"
    BAZA = "https://generativelanguage.googleapis.com/v1beta/models"

    # Cennik odczytany z ai.google.dev/gemini-api/docs/pricing 2026-09-07,
    # taryfa platna, USD za milion tokenow.
    CENNIK = {
        "gemini-3.5-flash": (1.50, 9.00),
        "gemini-3.5-flash-lite": (0.30, 2.50),
        "gemini-3.6-flash": (0.75, 3.75),
        "gemini-3.1-flash-lite": (0.25, 1.50),
        "gemini-3.1-pro-preview": (2.00, 12.00),
    }
    # Nieznany model wyceniamy najdrozszym ze znanych, nie najtanszym: pomylka
    # w te strone zatrzymuje potok na limicie dobowym, pomylka w druga strone
    # pozwala mu wydac wielokrotnosc limitu, zanim ktokolwiek to zauwazy.
    CENNIK_DOMYSLNY = (2.00, 12.00)

    def __init__(self, klucz: str, http: httpx.AsyncClient | None = None):
        self._klucz = klucz
        self._http = http or httpx.AsyncClient(timeout=90.0)
        self._wlasny_http = http is None
        self.model_analizy = os.environ.get("ANALYSIS_MODEL_GEMINI", "gemini-3.5-flash")
        self.model_triazu = os.environ.get("TRIAGE_MODEL_GEMINI", "gemini-3.5-flash-lite")

    async def zapytaj(self, *, model: str, system: str, user: str,
                      schema: dict, max_tokens: int) -> tuple[dict, dict]:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _bez_additional_properties(schema),
                "maxOutputTokens": max_tokens,
            },
        }
        r = await self._http.post(f"{self.BAZA}/{model}:generateContent",
                                  json=body,
                                  headers={"x-goog-api-key": self._klucz})
        r.raise_for_status()
        d = r.json()
        usage = self._uzycie(d.get("usageMetadata", {}))

        kandydaci = d.get("candidates") or []
        if not kandydaci:
            # Odfiltrowany prompt nie zwraca kandydata w ogole.
            raise Odmowa(usage, "prompt odrzucony przez filtry dostawcy")
        c = kandydaci[0]
        powod = c.get("finishReason")
        if powod in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "RECITATION"):
            raise Odmowa(usage, f"odpowiedź zablokowana: {powod}")
        if powod == "MAX_TOKENS":
            # ODCIETY JSON TO NIE JEST WYNIK. Bez tego warunku `json.loads`
            # rzucalby blad skladni, a w logu widnialoby "cykl przerwany:
            # JSONDecodeError" - komunikat, ktory nie mowi, co sie stalo.
            raise BladOdpowiedzi(
                f"odpowiedź ucięta na limicie {max_tokens} tokenów "
                f"(model {model} zużył {usage['output']} na wyjście razem z rozumowaniem)",
                usage)

        tekst = "".join(p.get("text", "") for p in c.get("content", {}).get("parts", []))
        if not tekst.strip():
            raise BladOdpowiedzi(
                f"pusta odpowiedź modelu {model} (finishReason={powod})", usage)
        return json.loads(tekst), usage

    @staticmethod
    def _uzycie(u: dict) -> dict:
        # TOKENY ROZUMOWANIA SA PLATNE JAK WYJSCIE i u modeli 3.x potrafia byc
        # ich wielokrotnoscia: zmierzone na gemini-3.5-flash 732 tokeny myslenia
        # przy 121 tokenach odpowiedzi. Liczenie samego `candidatesTokenCount`
        # zanizaloby koszt szesciokrotnie, a limit dobowy jest jedynym
        # bezpiecznikiem przed petla, ktora analizuje w kolko to samo.
        return {
            "input": u.get("promptTokenCount", 0),
            "output": u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0),
            "cache_read": u.get("cachedContentTokenCount", 0),
        }

    async def zamknij(self) -> None:
        if self._wlasny_http:
            await self._http.aclose()
