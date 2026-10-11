"""Chart: pamiec trenera tygodniowego (audyt 2026-10-11).

Tor crypto 1h zmierzony 2026-10-11 na produkcji: 974 MiB RSS przy limicie
1024 MiB. H-10 dodaje do kazdego toru crypto przebieg kalibracyjny, wiec limit
1 GiB nie ma zapasu, a request 384 MiB nie mowi planiscie prawdy.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

KORZEN = Path(__file__).resolve().parents[1]
CHART = KORZEN / "charts" / "trader"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm niedostępny")

ZMIERZONE_MIB = 977   # max working set toru 1h, Job trader-trainer-29861440
JEDNOSTKI = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024}


def _mib(wartosc: str) -> float:
    for przyrostek, mnoznik in JEDNOSTKI.items():
        if wartosc.endswith(przyrostek):
            return float(wartosc[:-len(przyrostek)]) * mnoznik
    raise AssertionError(f"nieobslugiwana jednostka pamieci: {wartosc}")


@pytest.fixture(scope="module")
def trener():
    w = subprocess.run(["helm", "template", "trader", str(CHART)],
                       capture_output=True, text=True, check=True)
    for d in yaml.safe_load_all(w.stdout):
        if d and d["kind"] == "CronJob" and d["metadata"]["name"] == "trader-trainer":
            kontenery = d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
            return next(c for c in kontenery if c["name"] == "trainer")
    raise AssertionError("brak CronJoba trader-trainer")


def test_limit_pamieci_ma_zapas_nad_zmierzonym_torem_1h(trener):
    # 25% zapasu: przebieg kalibracyjny H-10 i tygodniowy przyrost danych 1h.
    assert _mib(trener["resources"]["limits"]["memory"]) >= ZMIERZONE_MIB * 1.25


def test_request_pamieci_nie_ponizej_zmierzonego_zuzycia(trener):
    assert _mib(trener["resources"]["requests"]["memory"]) >= 1000
