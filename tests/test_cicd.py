"""Pełne CD: obraz z CI trafia na produkcję bez wchodzenia na serwer.

DWIE RZECZY MUSZĄ SIĘ RUSZAĆ RAZEM, I TO JEST CAŁA TREŚĆ TEGO PLIKU

`update-git` podbija tag obrazu w `charts/trader/values.yaml`, a ArgoCD ten
commit synchronizuje — czyli wskazuje produkcję na obraz istniejący WYŁĄCZNIE
w prywatnym rejestrze. Klaster musi mieć czym się do niego uwierzytelnić.

Rozejście się tych dwóch ustawień nie objawia się awarią CI ani błędem helma.
Objawia się `ImagePullBackOff` przy pierwszym pushu kodu — czyli u kogoś, kto
zmienił jedną linijkę w Pythonie i nie ma powodu podejrzewać rejestru.

Sam workflow ostrzegał przed tym zdaniem „kolejność ma znaczenie: zdjęcie
warunku jako pierwsze zatrzymuje produkcję". Ostrzeżenie w komentarzu nie jest
zabezpieczeniem — ten plik nim jest.

TRZECIA RZECZ: `[skip ci]`. `update-git` commituje do `main`, a push do `main`
uruchamia ten workflow. Bez tego znacznika każde wdrożenie wywoływałoby
następne — bez zmiany treści, więc i bez końca.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

KORZEN = Path(__file__).resolve().parents[1]
WORKFLOW = KORZEN / ".github/workflows/docker-publish.yml"
VALUES = KORZEN / "charts/trader/values.yaml"


def _job_update_git() -> dict:
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert "update-git" in wf["jobs"], "zniknął krok podbijający tag obrazu"
    return wf["jobs"]["update-git"]


def test_podbijanie_tagu_wymaga_poswiadczen_do_rejestru():
    """Jeśli CD jest włączone, klaster MUSI umieć pobrać obraz."""
    wlaczone = _job_update_git().get("if") not in (False, "false")
    sekrety = yaml.safe_load(VALUES.read_text(encoding="utf-8"))["imagePullSecrets"]
    if wlaczone:
        assert sekrety, (
            "`update-git` wskazuje produkcję na obraz z prywatnego rejestru, "
            "a `imagePullSecrets` jest puste — pierwszy push kodu skończy się "
            "ImagePullBackOff")
        assert any(s.get("name") == "ghcr-pull" for s in sekrety)


def test_tag_obrazu_jest_niezmienny_wiec_IfNotPresent_wystarcza():
    """`Always` przy tagu `sha-` odpytuje rejestr bez możliwości dowiedzenia się
    czegokolwiek nowego — i robi to także wtedy, gdy rejestr nie odpowiada."""
    v = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
    for usluga in ("api", "web"):
        assert v[usluga]["image"]["pullPolicy"] == "IfNotPresent"
        assert re.fullmatch(r"sha-[0-9a-f]{7,40}", v[usluga]["image"]["tag"]), (
            f"{usluga}: tag nie jest postaci `sha-<commit>` — przy ruchomym tagu "
            f"`IfNotPresent` przestaje być poprawne")


def test_commit_bota_nie_zapetla_ci():
    krok = "\n".join(str(s) for s in _job_update_git()["steps"])
    assert "[skip ci]" in krok, (
        "commit podbijający tag nie ma `[skip ci]` — każde wdrożenie wywoła "
        "następne")


def test_tag_podbija_sie_dopiero_po_udanym_buildzie():
    """Kolejność jest tu gwarancją, nie stylem: chart nie ma prawa wskazać
    obrazu, którego build nie potwierdził."""
    assert "build" in _job_update_git()["needs"]


def test_reczne_budowanie_na_serwerze_zniknelo_razem_z_odwolaniami():
    """Skrypt usunięty 2026-09-24. Odwołanie do nieistniejącego pliku
    w instrukcji wdrożenia jest gorsze niż jego brak: wysyła człowieka na
    serwer po ścieżkę, której już nie ma."""
    assert not (KORZEN / "scripts/build-on-vps.sh").exists()
    assert not (KORZEN / "tests/test_sprzatanie_obrazow.py").exists(), (
        "test pilnujący sprzątania obrazów przez usunięty skrypt")

    # ZAKAZ DOTYCZY WYWOŁANIA, NIE WZMIANKI. Nazwa skryptu ma prawo wystąpić
    # w zdaniu opisującym, co go zastąpiło — komentarz historyczny jest tu
    # wartością, bo tłumaczy, dlaczego chart wygląda, jak wygląda. Szkodliwa
    # jest linia, którą da się wkleić do terminala.
    WYWOLANIE = re.compile(r"(bash|sh|ssh|\./|source)\s[^\n]*build-on-vps")
    for plik in ("README.md", ".claude/agents/koordynator.md",
                 "charts/trader/values.yaml",
                 ".github/workflows/docker-publish.yml"):
        tresc = (KORZEN / plik).read_text(encoding="utf-8")
        for nr, linia in enumerate(tresc.splitlines(), 1):
            assert not WYWOLANIE.search(linia), (
                f"{plik}:{nr} każe uruchomić skrypt, którego już nie ma — "
                f"wysyła człowieka na serwer po ścieżkę usuniętą 2026-09-24: "
                f"{linia.strip()}")
