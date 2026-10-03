"""Procedura operatora korekty podzialow (scripts/korekta-podzialow-equity.sh).

Skrypt ma jedno zadanie, ktorego nie wolno zrobic zle: NIE zapisac niczego,
gdy pomiar z kroku 1 mowi, ze korekta nie dziala. Rowne liczby dla `split`
i `raw` znacza, ze parametr nie dochodzi do dostawcy - `--przepisz` zapisalby
wtedy te same nieskorygowane swiece, a `--znacznik` zdjalby bramke z zatrutych
danych.

URUCHAMIANY naprawde, z atrapa `kubectl` na PATH, ktora oddaje zadany pomiar
i zapisuje kazde wywolanie. Nic nie dotyka klastra.
"""
from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

SKRYPT = Path(__file__).resolve().parents[1] / "scripts/korekta-podzialow-equity.sh"


def _pomiar(razem: int, czynniki: dict[str, str], niepelne: bool = False) -> str:
    linie = ["POMIAR (adjustment=split) - nic nie zapisuje", ""]
    for s, cz in czynniki.items():
        linie.append(f"  {s:10s} baza   9000  dostawca   9000  brak    0  nowych    0"
                     f"  zmienionych   4000  {cz}")
    if niepelne:
        linie.append("NIEPELNE POKRYCIE: NVDA - dla tych instrumentow --przepisz ODMOWI zapisu")
    linie.insert(-1 if niepelne else len(linie),
                 f"RAZEM zmieni wartosc: {razem} wierszy market.candle w 3 szeregach")
    return "\n".join(linie) + "\n"


OK = {"NVDA": "x0.1000", "AVGO": "x0.1000", "NFLX": "x0.1000", "AAPL": "bez zmian"}


def _uruchom(tmp_path, split: str, raw: str, *arg):
    (tmp_path / "split.txt").write_text(split)
    (tmp_path / "raw.txt").write_text(raw)
    dziennik = tmp_path / "wywolania.log"
    atrapa = tmp_path / "kubectl"
    atrapa.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> {dziennik}\n'
        'case "$*" in\n'
        f'  *"--sucho --korekta raw"*) cat {tmp_path}/raw.txt ;;\n'
        f'  *--sucho*) cat {tmp_path}/split.txt ;;\n'
        '  *) echo "ATRAPA $*" ;;\n'
        "esac\n")
    atrapa.chmod(atrapa.stat().st_mode | stat.S_IEXEC)
    w = subprocess.run(["bash", str(SKRYPT), *arg], capture_output=True, text=True,
                       env={"PATH": f"{tmp_path}:/usr/bin:/bin", "TMPDIR": str(tmp_path)})
    wyw = dziennik.read_text().splitlines() if dziennik.exists() else []
    return w, wyw


def _zapisy(wyw):
    return [w for w in wyw if "--przepisz" in w or "--znacznik" in w or "features.py" in w]


def test_skrypt_jest_poprawnym_bashem():
    assert subprocess.run(["bash", "-n", str(SKRYPT)]).returncode == 0
    assert "set -euo pipefail" in SKRYPT.read_text()


def test_bez_wykonaj_tylko_mierzy(tmp_path):
    w, wyw = _uruchom(tmp_path, _pomiar(12000, OK), _pomiar(0, {**OK, "NVDA": "bez zmian"}))
    assert w.returncode == 0, w.stderr
    assert _zapisy(wyw) == [], "zapis bez --wykonaj"


def test_wykonaj_idzie_krokami_2_3_4_w_tej_kolejnosci(tmp_path):
    w, wyw = _uruchom(tmp_path, _pomiar(12000, OK), _pomiar(0, OK), "--wykonaj")
    assert w.returncode == 0, w.stderr
    z = _zapisy(wyw)
    assert len(z) == 3
    assert "--przepisz" in z[0] and "features.py" in z[1] and "--okno-dni 1400" in z[1]
    assert "--znacznik" in z[2]


def test_rowne_split_i_raw_przerywa_przed_zapisem(tmp_path):
    w, wyw = _uruchom(tmp_path, _pomiar(12000, OK), _pomiar(12000, OK), "--wykonaj")
    assert w.returncode != 0
    assert "TE SAMA" in w.stderr
    assert _zapisy(wyw) == []


def test_niepelne_pokrycie_przerywa(tmp_path):
    w, wyw = _uruchom(tmp_path, _pomiar(12000, OK, niepelne=True), _pomiar(0, OK), "--wykonaj")
    assert w.returncode != 0 and _zapisy(wyw) == []


@pytest.mark.parametrize("cz", ["x1.0000", "bez zmian", "x0.5000"])
def test_czynnik_inny_niz_dziesiata_przerywa(tmp_path, cz):
    w, wyw = _uruchom(tmp_path, _pomiar(12000, {**OK, "AVGO": cz}), _pomiar(0, OK), "--wykonaj")
    assert w.returncode != 0, f"czynnik {cz} przepuszczony"
    assert "AVGO" in w.stderr and _zapisy(wyw) == []


def test_zaden_szablon_chartu_nie_wola_skryptu():
    """Procedura jest dla czlowieka - ArgoCD nie moze jej uruchomic sam."""
    szablony = (SKRYPT.parents[1] / "charts/trader/templates").glob("*.yaml")
    assert not any("korekta-podzialow-equity" in p.read_text() for p in szablony)
