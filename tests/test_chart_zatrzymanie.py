"""Chart: termin zatrzymania, priorytet rdzenia i zasoby bazy (P-T4).

Pilnuje T-9 (SIGKILL po 30 s w środku cyklu), T-10 (baza z requestem
poniżej zużycia) i T-16 (readiness z 1 s, zrzut bez `.tmp`).
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


@pytest.fixture(scope="module")
def obiekty():
    w = subprocess.run(["helm", "template", "trader", str(CHART)],
                       capture_output=True, text=True, check=True)
    return [d for d in yaml.safe_load_all(w.stdout) if d]


def _deploy(obiekty, nazwa):
    for d in obiekty:
        if d["kind"] == "Deployment" and d["metadata"]["name"] == nazwa:
            return d["spec"]["template"]["spec"]
    raise AssertionError(f"brak Deploymentu {nazwa}")


@pytest.mark.parametrize("nazwa,termin", [
    ("trader-engine", 150), ("trader-engine-equity", 150),
    ("trader-dozorca", 90), ("trader-dozorca-equity", 90),
])
def test_silnik_i_dozorca_maja_termin_i_priorytet(obiekty, nazwa, termin):
    spec = _deploy(obiekty, nazwa)
    assert spec["terminationGracePeriodSeconds"] == termin
    assert spec["priorityClassName"] == "trader-rdzen"


def test_pozostale_workery_bez_priorytetu_rdzenia(obiekty):
    spec = _deploy(obiekty, "trader-market")
    assert spec["terminationGracePeriodSeconds"] == 60
    assert "priorityClassName" not in spec


def test_priorityclass_istnieje_i_nie_jest_domyslna(obiekty):
    pc = [d for d in obiekty if d["kind"] == "PriorityClass"]
    assert [p["metadata"]["name"] for p in pc] == ["trader-rdzen"]
    assert pc[0]["globalDefault"] is False


def test_baza_request_pamieci_priorytet_i_readiness(obiekty):
    spec = _deploy(obiekty, "trader-db")
    assert spec["priorityClassName"] == "trader-rdzen"
    kontener = spec["containers"][0]
    assert kontener["resources"]["requests"]["memory"] == "1280Mi"
    assert kontener["readinessProbe"]["timeoutSeconds"] == 5
    assert kontener["readinessProbe"]["failureThreshold"] == 6


def test_nocny_zrzut_przez_tmp_i_weryfikacje():
    tekst = (CHART / "templates" / "backup.yaml").read_text(encoding="utf-8")
    assert '--file="$PLIK.tmp"' in tekst
    assert 'pg_restore --list "$PLIK.tmp"' in tekst
    assert 'mv "$PLIK.tmp" "$PLIK"' in tekst
