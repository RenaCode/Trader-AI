"""Podpis modelu bierzemy z ODPOWIEDZI dostawcy, nie z konfiguracji (audyt 2026-10-10).

Google wycofał `gemini-3.5-flash` i zapytania o niego obsługuje modelem
`gemini-3.6-flash` (ai.google.dev/gemini-api/docs/deprecations). Odpowiedź mówi
o tym tylko w polu `modelVersion`. Potok podpisywał `news.article_analysis.model`
nazwą z konfiguracji, więc każdy wiersz po przekierowaniu nosił model, który go
NIE napisał, a uskok w cesze `news_sent` był niewidoczny dokładnie tam, gdzie
podpis miał go pokazać.

Druga strona tej samej zmiany: odpowiedź nazywa model z przyrostkiem wydania
(`claude-haiku-4-5-20251001`, `...-001`). Wycena po samej nazwie z odpowiedzi
wzięłaby `CENNIK_DOMYSLNY`, czyli dziesięciokrotną stawkę haiku.
"""
import asyncio
from pathlib import Path

import httpx
import pytest
import yaml

from services.ingest.llm import (
    BladOdpowiedzi, Dostawca, DostawcaAnthropic, DostawcaGemini, DostawcaMieszany,
    bez_wydania, kto_wykonal,
)
from services.ingest.news_worker import TRIAGE_SCHEMA

KORZEN = Path(__file__).resolve().parents[1]


def _gemini(odpowiedz: dict) -> DostawcaGemini:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=odpowiedz)))
    d = DostawcaGemini("klucz-testowy", http=http)
    d.ODSTEP_S = 0.0
    return d


def _odp(tekst: str, model_version: str | None, finish: str = "STOP") -> dict:
    d = {"candidates": [{"finishReason": finish,
                         "content": {"parts": [{"text": tekst}]}}],
         "usageMetadata": {"promptTokenCount": 1_000_000, "candidatesTokenCount": 0}}
    if model_version is not None:
        d["modelVersion"] = model_version
    return d


def _pytaj(d, model):
    return asyncio.run(d.zapytaj(model=model, system="s", user="u",
                                 schema=TRIAGE_SCHEMA, max_tokens=100))


def test_PRZEKIEROWANY_model_gemini_podpisuje_wynik_modelem_z_odpowiedzi():
    d = _gemini(_odp('{"worth_analyzing": true, "reason": "x"}', "gemini-3.6-flash"))
    _, usage = _pytaj(d, "gemini-3.5-flash")

    assert d.uzyty_model("gemini-3.5-flash", usage) == "gemini-3.6-flash", (
        "podpis wziety z konfiguracji - uskok news_sent znowu niewidoczny")
    # Wycena po modelu, ktory pracowal: 3.6-flash kosztuje 0,75 $/M wejscia,
    # wycofany 3.5-flash 1,50.
    assert d.koszt(d.uzyty_model("gemini-3.5-flash", usage), usage) == pytest.approx(0.75)


def test_brak_modelVersion_zostawia_model_zadany():
    d = _gemini(_odp('{"worth_analyzing": true, "reason": "x"}', None))
    _, usage = _pytaj(d, "gemini-3.6-flash")
    assert usage["model_uzyty"] == "gemini-3.6-flash"


def test_blad_po_odpowiedzi_niesie_model_z_odpowiedzi():
    """Uciecie tez kosztuje i trafia do `news.api_spend` - z modelem, ktory
    te tokeny spalil, bo wedlug niego liczy sie kwota."""
    d = _gemini(_odp('{"worth', "gemini-3.6-flash", finish="MAX_TOKENS"))
    with pytest.raises(BladOdpowiedzi) as e:
        _pytaj(d, "gemini-3.5-flash")
    assert kto_wykonal(e.value, d, "gemini-3.5-flash").model == "gemini-3.6-flash"
    assert e.value.usage["model_uzyty"] == "gemini-3.6-flash"


def test_podmiana_modelu_idzie_do_logu_raz(capsys):
    d = _gemini(_odp('{"worth_analyzing": true, "reason": "x"}', "gemini-3.6-flash"))
    _pytaj(d, "gemini-3.5-flash")
    _pytaj(d, "gemini-3.5-flash")
    err = capsys.readouterr().err
    assert err.count("obsluzyl gemini-3.6-flash") == 1


def test_przyrostek_wydania_to_nie_podmiana_i_nie_zmienia_stawki(capsys):
    class _Uzycie:
        input_tokens, output_tokens, cache_read_input_tokens = 1_000_000, 0, 0

    class _Blok:
        type, text = "text", '{"worth_analyzing": true}'

    class _Odp:
        stop_reason, content, usage = "end_turn", [_Blok()], _Uzycie()
        model = "claude-haiku-4-5-20251001"

    class _Klient:
        messages = None

        async def create(self, **kw):
            return _Odp()

    d = DostawcaAnthropic("klucz-testowy")
    d._c = _Klient()
    d._c.messages = d._c
    _, usage = _pytaj(d, "claude-haiku-4-5")

    faktyczny = d.uzyty_model("claude-haiku-4-5", usage)
    assert faktyczny == "claude-haiku-4-5-20251001", "do bazy idzie nazwa pelna"
    assert d.koszt(faktyczny, usage) == pytest.approx(1.0), (
        "datowany identyfikator wyceniony po CENNIK_DOMYSLNY - 10x za drogo")
    assert "obsluzyl" not in capsys.readouterr().err
    assert bez_wydania("gemini-3.6-flash-001") == "gemini-3.6-flash"
    assert bez_wydania("models/gemini-3.6-flash") == "gemini-3.6-flash"


class _Atrapa(Dostawca):
    def __init__(self, nazwa, triaz, analiza):
        self.nazwa, self.model_triazu, self.model_analizy = nazwa, triaz, analiza
        self.CENNIK = {triaz: (1.0, 1.0), analiza: (1.0, 1.0)}

    async def zapytaj(self, *, model, **kw):
        return {"ok": True}, {"input": 0, "output": 0, "cache_read": 0}


def test_MIESZANKA_nie_nadpisuje_modelu_z_odpowiedzi_i_umie_go_wycenic():
    """`_zapytaj_u` wpisywal `model_uzyty = model zadany` bezwarunkowo - podpis
    z odpowiedzi ginal na ostatnim metrze. A wycena kierowana PO NAZWIE
    z odpowiedzi konczyla sie ValueError: `gemini-3.6-flash` nie jest zadnym
    z czterech modeli mieszanki."""
    g = _gemini(_odp('{"worth_analyzing": true, "reason": "x"}', "gemini-3.6-flash"))
    g.model_triazu, g.model_analizy = "gemini-3.5-flash-lite", "gemini-3.5-flash"
    m = DostawcaMieszany(triaz=g, analiza=_Atrapa("a", "haiku", "opus"))

    _, usage = asyncio.run(m.zapytaj(model="gemini-3.5-flash-lite", system="s", user="u",
                                     schema=TRIAGE_SCHEMA, max_tokens=100))
    faktyczny = m.uzyty_model(m.model_triazu, usage)
    assert faktyczny == "gemini-3.6-flash"
    assert m.koszt(faktyczny, usage) == pytest.approx(0.75)


def test_konfiguracja_nie_wskazuje_wycofanego_gemini_3_5_flash(monkeypatch):
    """Wycofany model dziala dalej - przez przekierowanie, czyli na innym
    modelu niz w konfiguracji. Chart, `.env.example` i domysl w kodzie maja
    nazywac model, ktory naprawde odpowiada."""
    wartosci = yaml.safe_load((KORZEN / "charts/trader/values.yaml").read_text("utf-8"))
    assert wartosci["llm"]["analysisModelGemini"] == "gemini-3.6-flash"
    env = (KORZEN / ".env.example").read_text("utf-8")
    assert "ANALYSIS_MODEL_GEMINI=gemini-3.6-flash\n" in env
    monkeypatch.delenv("ANALYSIS_MODEL_GEMINI", raising=False)
    assert DostawcaGemini("k").model_analizy == "gemini-3.6-flash"
