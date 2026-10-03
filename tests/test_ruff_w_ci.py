"""Ruff w CI z regułami F, B, E9 - i kod, który je spełnia (audyt Fable U-15)."""
from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

KORZEN = Path(__file__).resolve().parents[1]


def test_reguly_F_B_E9():
    cfg = tomllib.loads((KORZEN / "ruff.toml").read_text(encoding="utf-8"))
    assert set(cfg["lint"]["select"]) == {"F", "B", "E9"}


def test_ci_uruchamia_ruff_i_instaluje_przypieta_wersje():
    ci = (KORZEN / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    assert "- run: ruff check ." in ci
    assert "ruff==" in (KORZEN / "requirements-dev.txt").read_text(encoding="utf-8")


def test_ruff_przechodzi():
    try:
        import ruff  # noqa: F401
    except ImportError:
        pytest.skip("ruff niezainstalowany (pip install -r requirements-dev.txt)")
    w = subprocess.run([sys.executable, "-m", "ruff", "check", "."], cwd=KORZEN,
                       capture_output=True, text=True)
    assert w.returncode == 0, w.stdout + w.stderr
