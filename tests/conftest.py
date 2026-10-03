"""Wspolne dla calej suity: plik sondy zywotnosci laduje w katalogu testu.

Workery pisza `puls.zyje()` na starcie i po kazdym obrocie petli. Bez tego
kazdy test petli zostawialby `/tmp/trader-zywy` na maszynie dewelopera i w CI -
a testy uruchamiane rownolegle nadpisywalyby sobie nawzajem ten sam plik.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _plik_zywotnosci_w_katalogu_testu(tmp_path, monkeypatch):
    monkeypatch.setenv("PLIK_ZYWOTNOSCI", str(tmp_path / "trader-zywy"))
