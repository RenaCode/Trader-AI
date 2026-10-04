"""W repozytorium nie ma śledzonych plików `*.pyc` (przegląd 2026-10-04, T-5).

`.gitignore` wyklucza `__pycache__/`, ale plik dodany wcześniej zostaje
śledzony - `scripts/__pycache__/porownaj_warianty.cpython-314.pyc` leżał
w drzewie i mógł podać stary kod przy imporcie z innej wersji Pythona.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

KORZEN = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("git") is None or not (KORZEN / ".git").exists(),
                    reason="poza repozytorium git")
def test_zadnego_sledzonego_pyc():
    wynik = subprocess.run(["git", "ls-files", "*.pyc"], cwd=KORZEN,
                           capture_output=True, text=True, check=True)
    assert wynik.stdout.strip() == "", wynik.stdout
