"""CronJoby: jawna strefa, kontekst bez uprawnien, obraz bazy po digescie (2026-10-03).

Trzy uwagi z audytu infrastruktury (G4, G5, G6), kazda o tym samym ksztalcie -
chart zalezal od czegos, czego nie wpisal:

  * strefa harmonogramu: bez `timeZone` liczy sie w strefie HOSTA k3s,
  * kontekst bezpieczenstwa: c2561e4 objal nim tylko Deploymenty, a Joby
    chodzace na tym samym obrazie (trener, proba, post-mortem z kluczem
    `SECRETS_KEY`) dostawaly domyslne uprawnienia i token API klastra,
  * obraz bazy: ruchomy tag `pg17` przy `IfNotPresent`.

Test czyta szablony jako tekst - bez `helm` - tak jak test_chart_kontekst_bezpieczenstwa.py.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

KORZEN = Path(__file__).resolve().parents[1]
SZABLONY = KORZEN / "charts/trader/templates"
VALUES = yaml.safe_load((KORZEN / "charts/trader/values.yaml").read_text(encoding="utf-8"))
CRONY = sorted(p.name for p in SZABLONY.glob("*.yaml")
               if "kind: CronJob" in p.read_text(encoding="utf-8"))
Z_OBRAZEM_API = [p for p in CRONY
                 if ".Values.api.image.pullPolicy" in (SZABLONY / p).read_text(encoding="utf-8")]


def _tekst(plik: str) -> str:
    return (SZABLONY / plik).read_text(encoding="utf-8")


def test_jest_czego_pilnowac():
    assert len(CRONY) >= 9 and "backup.yaml" in CRONY and "trainer-cron.yaml" in CRONY
    assert len(Z_OBRAZEM_API) == len(CRONY) - 1, "backup jako jedyny ma inny obraz"


@pytest.mark.parametrize("plik", CRONY)
def test_kazdy_cronjob_ma_jawna_strefe(plik):
    t = _tekst(plik)
    assert re.search(r"^  timeZone: .*\.Values\.strefaCzasowa", t, re.M), (
        f"{plik}: harmonogram w strefie hosta - zmiana VPS przesunie okno nocne")
    # Jedno pole: post-mortem mial wlasne `Etc/UTC`, a drugi klucz w YAML-u
    # albo nadpisuje pierwszy, albo wywraca parser - zalezy od narzedzia.
    assert len(re.findall(r"^  timeZone:", t, re.M)) == 1, f"{plik}: dwie strefy"


def test_strefa_jest_jedna_i_jest_warszawa():
    """Jedna strefa dla wszystkich: zmiana czasu przesuwa zadania RAZEM,
    wiec odstepy kopia -> trener -> proba -> post-mortem zostaja stale."""
    assert VALUES["strefaCzasowa"] == "Europe/Warsaw"


@pytest.mark.parametrize("plik", Z_OBRAZEM_API)
def test_cronjob_z_obrazem_api_bez_uprawnien(plik):
    t = _tekst(plik)
    assert t.count('include "trader.podBezUprawnien"') == 1, plik
    assert t.count('include "trader.kontenerBezUprawnien"') == t.count(
        ".Values.api.image.pullPolicy"), f"{plik}: kontener bez kontekstu"
    assert "automountServiceAccountToken: false" in t, (
        f"{plik}: pod dostaje token API klastra, z ktorym nie rozmawia")


def test_kopia_bez_tokenu_i_z_ograniczonym_rootem():
    t = _tekst("backup.yaml")
    assert "automountServiceAccountToken: false" in t
    assert 'add: ["CHOWN", "FOWNER"]' in t, "kontener startowy z pelnym rootem"
    zrzut = t[t.index("- name: dump"):]
    assert "runAsNonRoot: true" in zrzut and "runAsUser: 1000" in zrzut


def test_obraz_bazy_przypiety_po_digescie():
    obraz = VALUES["db"]["image"]
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", obraz.get("digest") or ""), (
        "obraz bazy bez digestu - ruchomy tag przy IfNotPresent")
    for plik in ("db.yaml", "backup.yaml"):
        t = _tekst(plik)
        assert 'include "trader.obrazBazy"' in t
        assert ".Values.db.image.repository }}:{{ .Values.db.image.tag }}\"" not in t, (
            f"{plik}: obraz bazy z samego tagu, obok helpera")
