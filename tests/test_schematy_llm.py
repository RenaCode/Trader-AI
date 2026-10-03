"""Kazdy schemat wysylany do modelu ma `additionalProperties: false` w KAZDYM obiekcie.

Od 26.09.2026 API Anthropic odrzuca strukturalne wyjscie, w ktorym ktorykolwiek
obiekt - takze zagniezdzony w `items` tablicy - nie ustawia tego pola jawnie:

    400 output_config.format.schema: For 'object' type,
        'additionalProperties' must be explicitly set to false

CronJob `trader-postmortem` padal na tym co noc, bo `SCHEMA` w `postmortem.py`
nie mial pola nigdzie. Test idzie po schemacie REKURENCYJNIE: poprawka dopisana
tylko na korzeniu przeszlaby przez sprawdzenie plytkie i dalej dostawala 400
za `findings.items`.

Gemini tego pola nie przyjmuje - `llm._bez_additional_properties` tnie je
w locie - wiec drugi test pilnuje, ze ciecie tez jest rekurencyjne.
"""
from __future__ import annotations

import pytest

from services.engine.postmortem import SCHEMA as POSTMORTEM
from services.ingest.llm import _bez_additional_properties
from services.ingest.news_worker import ANALYSIS_SCHEMA, TRIAGE_SCHEMA

SCHEMATY = {"postmortem": POSTMORTEM, "analiza": ANALYSIS_SCHEMA,
            "triaz": TRIAGE_SCHEMA}


def _obiekty(schemat, sciezka="$"):
    if isinstance(schemat, dict):
        if schemat.get("type") == "object":
            yield sciezka, schemat
        for k, v in schemat.items():
            yield from _obiekty(v, f"{sciezka}.{k}")
    elif isinstance(schemat, list):
        for i, v in enumerate(schemat):
            yield from _obiekty(v, f"{sciezka}[{i}]")


@pytest.mark.parametrize("nazwa", sorted(SCHEMATY))
def test_kazdy_obiekt_zamkniety(nazwa):
    obiekty = list(_obiekty(SCHEMATY[nazwa]))
    assert obiekty, "schemat bez obiektu - test nic by nie sprawdzil"
    otwarte = [s for s, o in obiekty if o.get("additionalProperties") is not False]
    assert not otwarte, f"{nazwa}: brak additionalProperties=false w {otwarte}"


def test_postmortem_ma_obiekt_zagniezdzony():
    """Kanarek: gdyby test szedl tylko po korzeniu, zagniezdzony obiekt by zniknal."""
    assert "$.properties.findings.items" in dict(_obiekty(POSTMORTEM))


@pytest.mark.parametrize("nazwa", sorted(SCHEMATY))
def test_gemini_dostaje_schemat_bez_pola(nazwa):
    assert "additionalProperties" not in repr(_bez_additional_properties(SCHEMATY[nazwa]))
