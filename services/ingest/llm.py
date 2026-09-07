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

import asyncio
import json
import os
import time
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

    # TEMPO ZAPYTAN I PONAWIANIE
    #
    # Klient Anthropica ponawia sam - ten jest napisany na golym httpx i nie
    # ponawial nic. Zmierzone na produkcji 2026-09-07, pierwszy cykl po wpieciu
    # klucza: jedna analiza przeszla, siedem kolejnych zapytan dostalo 429.
    # W logu widnialo "przeanalizowanych 0, bledow 7" i proces szedl dalej -
    # potok wygladal na dzialajacy i milczal, a wiadomosci nie byly analizowane.
    #
    # Dwa mechanizmy, bo rozwiazuja dwie rozne rzeczy. PRZEPUSTNICA rozklada
    # zapytania w czasie, zeby limit nie byl przekraczany; PONAWIANIE ratuje
    # te, ktore mimo tego trafia na limit (inny proces, inny model, zmiana
    # limitu po stronie dostawcy).
    ODSTEP_S = float(os.environ.get("GEMINI_MIN_INTERVAL_S", "4.0"))
    PROBY = 5
    # 429 to limit, nie awaria. 5xx to awaria po stronie dostawcy - zmierzone
    # 2026-09-07 na gemini-3.6-flash: 503 "high demand". Obie sa przejsciowe.
    PONAWIALNE = (429, 500, 502, 503, 504)

    def __init__(self, klucz: str, http: httpx.AsyncClient | None = None):
        self._klucz = klucz
        self._http = http or httpx.AsyncClient(timeout=90.0)
        self._wlasny_http = http is None
        self._ostatnie = 0.0
        self.model_analizy = os.environ.get("ANALYSIS_MODEL_GEMINI", "gemini-3.5-flash")
        self.model_triazu = os.environ.get("TRIAGE_MODEL_GEMINI", "gemini-3.5-flash-lite")

    async def _przepustnica(self) -> None:
        czekaj = self.ODSTEP_S - (time.monotonic() - self._ostatnie)
        if czekaj > 0:
            await asyncio.sleep(czekaj)
        self._ostatnie = time.monotonic()

    @staticmethod
    def _ile_czekac(r: httpx.Response, proba: int) -> float:
        """Ile odczekac przed kolejna proba.

        `Retry-After` od dostawcy ma pierwszenstwo przed naszym zgadywaniem -
        on wie, kiedy okno limitu sie odnowi, my nie.
        """
        naglowek = r.headers.get("Retry-After")
        if naglowek:
            try:
                return min(90.0, max(1.0, float(naglowek)))
            except ValueError:
                pass
        return min(60.0, 5.0 * (2 ** (proba - 1)))

    async def _poslij(self, model: str, body: dict) -> httpx.Response:
        for proba in range(1, self.PROBY + 1):
            await self._przepustnica()
            r = await self._http.post(f"{self.BAZA}/{model}:generateContent",
                                      json=body,
                                      headers={"x-goog-api-key": self._klucz})
            if r.status_code not in self.PONAWIALNE:
                r.raise_for_status()
                return r
            if proba == self.PROBY:
                break
            await asyncio.sleep(self._ile_czekac(r, proba))
        # Wyczerpane proby. Komunikat mowi WPROST, ze to limit, a nie zly klucz -
        # bez tego "429" w logu wyglada jak blad konfiguracji i kieruje szukanie
        # w zla strone.
        raise RuntimeError(
            f"dostawca odrzucił {self.PROBY} kolejnych zapytań o model {model} "
            f"(HTTP {r.status_code}) — to limit tempa albo dobowy limit klucza, "
            f"nie błędny klucz. Odstęp między zapytaniami: {self.ODSTEP_S:.1f} s "
            f"(GEMINI_MIN_INTERVAL_S)")

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
        r = await self._poslij(model, body)
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


class DostawcaMieszany(Dostawca):
    """Triaż u jednego dostawcy, analiza u drugiego.

    DLACZEGO AKURAT TAK, A NIE ODWROTNIE — I DLACZEGO TO NIE JEST SAMA CENA

    Zmierzone na produkcji 2026-09-07, koszt jednego wywołania:

        triaż    gemini-3.5-flash-lite  $0,000213   claude-haiku-4-5  $0,001163
        analiza  gemini-3.5-flash       $0,007809   claude-opus-5     $0,007503

    Triaż to 94% wywołań, więc to on decyduje o rachunku — i tam Gemini jest
    5,5× tańszy. Analiza kosztuje u obu tyle samo, więc o jej przydziale
    decyduje wyłącznie jakość. Mieszanka wychodzi jednocześnie TAŃSZA niż sam
    Anthropic i LEPSZA niż samo Gemini; nie jest to kompromis.

    ALE POWÓD JEST GŁĘBSZY NIŻ CENNIK, i to on wyznacza, czego robić NIE wolno.
    Te dwie role różnią się tym, co psuje ich zmiana:

      * triaż WYBIERA, które wiadomości trafią do analizy. Zmiana modelu zmienia
        więc próbkę — ale nie wstawia uskoku w wartości samej cechy,
      * analiza WYTWARZA wartość cechy `news_sent`. Zmiana modelu wstawia
        w środek szeregu skok, którego kalibracja wagi nie zakłada i którego
        nie widać inaczej niż przez porównanie okresów.

    Dlatego tani dostawca idzie tam, gdzie jest wolumen, a mocny tam, gdzie jest
    POMIAR. Odwrócenie tego oszczędziłoby grosze i zepsuło cechę.

    Model analizy MUSI być inny niż model triażu — inaczej nie da się
    rozstrzygnąć, do którego dostawcy skierować zapytanie, a milczący wybór
    „pierwszy z brzegu" trafiałby raz tu, raz tam.
    """

    nazwa = "mieszany"

    def __init__(self, triaz: Dostawca, analiza: Dostawca):
        if triaz.model_triazu == analiza.model_analizy:
            raise ValueError(
                "model triazu i model analizy sa identyczne - nie da sie "
                "rozstrzygnac, do ktorego dostawcy skierowac zapytanie")
        self._triaz = triaz
        self._analiza = analiza
        self.model_triazu = triaz.model_triazu
        self.model_analizy = analiza.model_analizy
        self.nazwa = f"{triaz.nazwa}+{analiza.nazwa}"

    def _dla(self, model: str) -> Dostawca:
        """Kierowanie PO NAZWIE MODELU, nie po kolejności wywołań.

        Kolejność byłaby założeniem o tym, jak potok woła dostawcę - a to
        założenie przestaje być prawdziwe przy pierwszej zmianie w potoku,
        po cichu i z zachowaniem poprawnego wyglądu wyniku.
        """
        if model == self.model_triazu:
            return self._triaz
        if model == self.model_analizy:
            return self._analiza
        raise ValueError(f"model {model!r} nie nalezy do zadnego z dostawcow "
                         f"mieszanki ({self.model_triazu} / {self.model_analizy})")

    async def zapytaj(self, *, model: str, system: str, user: str,
                      schema: dict, max_tokens: int) -> tuple[dict, dict]:
        return await self._dla(model).zapytaj(
            model=model, system=system, user=user, schema=schema,
            max_tokens=max_tokens)

    def koszt(self, model: str, usage: dict) -> float:
        # CENNIK BIERZE TEN DOSTAWCA, KTORY WYKONAL WYWOLANIE. Wspolna tablica
        # cen dawalaby liczbe wygladajaca poprawnie i bledna - a limit dobowy
        # opiera sie wlasnie na niej.
        return self._dla(model).koszt(model, usage)

    async def zamknij(self) -> None:
        for d in (self._triaz, self._analiza):
            await d.zamknij()
