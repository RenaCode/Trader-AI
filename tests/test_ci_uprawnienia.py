"""CI: prawo zapisu tylko w jobach, które go potrzebują (audyt Fable U-10).

Do 2026-10-03 `docker-publish.yml` dawał `contents: write, packages: write`
CAŁEMU workflow, więc dziedziczyły je `changes`, `verify-migrations`, `smoke`
i testy - joby, które wykonują kod z repozytorium (w tym zależności z PyPI
i npm). Skompromitowana zależność w testach mogła pchnąć commit na `main`
albo podmienić obraz w GHCR.
"""
from __future__ import annotations

from pathlib import Path

import yaml

KORZEN = Path(__file__).resolve().parents[1]
WORKFLOWS = KORZEN / ".github" / "workflows"


def _wf(nazwa: str) -> dict:
    return yaml.safe_load((WORKFLOWS / nazwa).read_text(encoding="utf-8"))


def test_globalnie_tylko_odczyt():
    for nazwa in ("docker-publish.yml", "tests.yml"):
        assert _wf(nazwa).get("permissions") == {"contents": "read"}, (
            f"{nazwa}: globalne permissions inne niż contents: read")


def test_zapis_wylacznie_w_build_i_update_git():
    joby = _wf("docker-publish.yml")["jobs"]
    for nazwa, job in joby.items():
        prawa = job.get("permissions") or {}
        zapis = {k for k, v in prawa.items() if v == "write"}
        if nazwa == "build":
            assert zapis == {"packages"}, f"build: {zapis}"
        elif nazwa == "update-git":
            assert zapis == {"contents"}, f"update-git: {zapis}"
        else:
            assert not zapis, f"{nazwa} dostaje zapis: {zapis}"
