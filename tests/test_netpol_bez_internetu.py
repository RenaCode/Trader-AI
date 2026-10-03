"""Egress bez internetu dla podów, które nie rozmawiają z nikim na zewnątrz
(audyt Fable U-16, 2026-10-03).

Do tej daty każdy pod chartu - także baza i nginx - mógł łączyć się z dowolnym
adresem w internecie (`regulyEgress`: 0.0.0.0/0 poza siecią domową). Przejęty
proces bazy albo potoku cech mógł wynieść dane bez przeszkód.
"""
from __future__ import annotations

import ast
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

KORZEN = Path(__file__).resolve().parents[1]
VALUES = yaml.safe_load((KORZEN / "charts/trader/values.yaml").read_text(encoding="utf-8"))
BEZ = VALUES["networkPolicy"]["egress"]["bezInternetu"]
bez_helma = pytest.mark.skipif(shutil.which("helm") is None, reason="helm niedostępny")


def _polityki() -> dict:
    wynik = subprocess.run(["helm", "template", str(KORZEN / "charts" / "trader")],
                           capture_output=True, text=True)
    assert wynik.returncode == 0, wynik.stderr
    return {d["metadata"]["name"]: d for d in yaml.safe_load_all(wynik.stdout)
            if d and d.get("kind") == "NetworkPolicy"}


def _wybrane(pol: dict) -> set[str]:
    return set(pol["spec"]["podSelector"]["matchExpressions"][0]["values"])


def test_lista_bez_internetu():
    assert set(BEZ) == {"db", "web", "features", "features-equity"}


@bez_helma
def test_pody_bez_internetu_maja_tylko_dns_i_klaster():
    p = _polityki()
    bez = p["trader-egress-bez-internetu"]
    assert _wybrane(bez) == {f"trader-{n}" for n in BEZ}
    assert "ipBlock" not in str(bez["spec"]["egress"])
    # Polityki się SUMUJĄ: pod z obu dostałby internet z pierwszej.
    assert not _wybrane(p["trader-egress"]) & _wybrane(bez)
    assert {"trader-engine", "trader-market", "trader-news", "trader-api",
            "trader-dozorca"} <= _wybrane(p["trader-egress"])


def test_potok_cech_nie_importuje_niczego_sieciowego():
    """Warunek wpisania `features` na listę: moduł nie ma czym wyjść do
    internetu. Nowy import adaptera, klienta HTTP albo LLM zapali ten test -
    i wtedy trzeba zdjąć `features` z listy, a nie test."""
    drzewo = ast.parse((KORZEN / "services/ingest/features.py").read_text(encoding="utf-8"))
    moduly = set()
    for w in ast.walk(drzewo):
        if isinstance(w, ast.Import):
            moduly |= {a.name for a in w.names}
        elif isinstance(w, ast.ImportFrom) and w.module:
            moduly.add(w.module)
    zakazane = {m for m in moduly if m.split(".")[0] in {"httpx", "requests", "urllib",
                                                          "aiohttp", "anthropic", "socket"}
                or m.startswith(("services.exchanges", "services.ingest.llm",
                                 "services.api.notifications"))}
    assert not zakazane, f"features importuje {zakazane} — zdejmij go z bezInternetu"
