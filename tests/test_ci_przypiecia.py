"""Przypięcia w CI (przegląd 2026-10-04, T-17).

Akcje na SHA (tag można przesunąć na inny kod), a baza testowa CI na tym samym
digeście co produkcja - inaczej CI testuje migracje na innej wersji
TimescaleDB niż ta, na której pójdą.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

KORZEN = Path(__file__).resolve().parents[1]
WORKFLOWY = sorted((KORZEN / ".github/workflows").glob("*.yml"))


def test_kazda_zewnetrzna_akcja_przypieta_do_SHA_z_komentarzem_wersji():
    for plik in WORKFLOWY:
        for linia in plik.read_text(encoding="utf-8").splitlines():
            m = re.search(r"uses:\s*(\S+)", linia)
            if not m or m.group(1).startswith("./"):
                continue
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", m.group(1)), (plik.name, linia)
            assert re.search(r"# v\d+", linia), (plik.name, linia)


def test_baza_CI_na_digescie_z_values():
    wartosci = yaml.safe_load((KORZEN / "charts/trader/values.yaml").read_text())
    obraz = wartosci["db"]["image"]
    oczekiwany = f"{obraz['repository']}:{obraz['tag']}@{obraz['digest']}"
    znalezione = []
    for plik in WORKFLOWY:
        znalezione += re.findall(r"image:\s*(timescale/timescaledb-ha\S*)",
                                 plik.read_text(encoding="utf-8"))
    assert znalezione and set(znalezione) == {oczekiwany}, znalezione


def test_compose_wystawia_baze_i_redis_tylko_na_petli_zwrotnej():
    compose = yaml.safe_load((KORZEN / "docker-compose.yml").read_text())
    for usluga in ("db", "redis"):
        for port in compose["services"][usluga]["ports"]:
            assert str(port).startswith("127.0.0.1:"), (usluga, port)
