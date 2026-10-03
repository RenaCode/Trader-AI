"""trainer-cron: kod 3 (`KOD_TOR_NIEGOTOWY`) dochodzi do DOZORU, a Job zostaje zielony.

Do 2026-10-03 kod 3 trafial wylacznie do `NIEGOTOWE` (echo w logu Joba),
a dozor dostawal `--kod-trenera 0`. Tor equity stal na bramce korekty podzialow
(0/20 znacznikow) i jedynym sladem byl log, ktorego nikt nie czyta - dozor
ma czujke stanu trwalego (`dozor.sprawdz_tor_niegotowy`), ale nie mial jej
czym wywolac.

Skrypt jest URUCHAMIANY z atrapa `python`, ktora zapisuje argumenty dozoru -
ten sam sposob co w tests/test_tor_niegotowy_nie_zabija_drugiego.py.
"""
from __future__ import annotations

import stat
import subprocess

from services.engine.train import KOD_NIC_NIE_POLICZONO, KOD_TOR_NIEGOTOWY
from tests.test_tor_niegotowy_nie_zabija_drugiego import _skrypt_trenera


def _przebieg(tmp_path, kod_crypto: int, kod_equity: int):
    dziennik = tmp_path / "dozor.log"
    atrapa = tmp_path / "python"
    atrapa.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *train.py*--engine\\ equity*) exit ' f"{kod_equity} ;;\n"
        '  *train.py*) exit ' f"{kod_crypto} ;;\n"
        f'  *dozor.py*) echo "$*" >> {dziennik}; exit 0 ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n")
    atrapa.chmod(atrapa.stat().st_mode | stat.S_IEXEC)
    skrypt = tmp_path / "zadanie.sh"
    skrypt.write_text(_skrypt_trenera())
    wynik = subprocess.run(["sh", str(skrypt)], capture_output=True, text=True,
                           env={"PATH": f"{tmp_path}:/usr/bin:/bin",
                                "HOME": str(tmp_path)})
    wywolania = dziennik.read_text().splitlines() if dziennik.exists() else []
    return wynik, wywolania


def _kod(wywolania, equity: bool) -> str:
    linia = next(w for w in wywolania if "--kod-trenera" in w
                 and (("--engine equity" in w) == equity))
    return linia.split("--kod-trenera")[1].split()[0]


def test_kod_3_equity_dochodzi_do_dozoru_equity(tmp_path):
    wynik, wyw = _przebieg(tmp_path, 0, KOD_TOR_NIEGOTOWY)
    assert _kod(wyw, equity=True) == str(KOD_TOR_NIEGOTOWY), (
        f"dozor equity dostal {_kod(wyw, equity=True)} - stan niegotowy znowu "
        f"zostaje wylacznie w echu Joba")
    assert _kod(wyw, equity=False) == "0", "kod 3 equity przeciekl do dozoru crypto"
    assert wynik.returncode == 0, "kod 3 zapalil Joba - to jest stan, nie awaria"


def test_kod_3_crypto_dochodzi_do_dozoru_crypto(tmp_path):
    wynik, wyw = _przebieg(tmp_path, KOD_TOR_NIEGOTOWY, 0)
    assert _kod(wyw, equity=False) == str(KOD_TOR_NIEGOTOWY)
    assert wynik.returncode == 0


def test_awaria_wygrywa_z_kodem_3_i_zapala_Joba(tmp_path):
    wynik, wyw = _przebieg(tmp_path, KOD_NIC_NIE_POLICZONO, 0)
    assert _kod(wyw, equity=False) == str(KOD_NIC_NIE_POLICZONO)
    assert wynik.returncode == KOD_NIC_NIE_POLICZONO


def test_kod_3_tylko_dla_horyzontu_ocenianego_przez_dozor():
    """Dozor czyta dziennik per (engine, bar): 3 z toru 1h kazalby mu meldowac
    niegotowosc toru 4h, ktory sie policzyl. Warunek stoi w szablonie."""
    from pathlib import Path
    tekst = (Path(__file__).resolve().parents[1]
             / "charts/trader/templates/trainer-cron.yaml").read_text(encoding="utf-8")
    assert "{{- if eq $h $.Values.trainerCron.bar }}" in tekst
    assert "{{- if eq $h (first $.Values.trainerCron.equity.horyzonty) }}" in tekst
