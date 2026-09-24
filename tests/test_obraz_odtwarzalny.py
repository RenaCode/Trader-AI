"""Tag `sha-<commit>` ma znaczyc "to jest ten commit" - wiec baza po digescie.

DLACZEGO TEN PLIK ISTNIEJE

CI taguje obrazy `trader-ai-<usluga>:sha-<commit>`, co stwierdza, ze zawartosc
wynika z commita. Przy ruchomym tagu bazy (`FROM python:3.12-slim`) to
nieprawda: dwa buildy tego samego commita, wykonane w odstepie tygodni, daja
rozne obrazy pod ta sama nazwa.

Od 2026-09-24 waga tego zalozenia ROSNIE, a nie maleje. Wczesniej obraz
produkcji powstawal na VPS i tam zostawal; dzis `update-git` wskazuje produkcje
na tag z rejestru, a cofniecie sie do starszego commita znaczy pobranie obrazu
zbudowanego wtedy. Jesli tag nie wynika z commita, cofniecie nie przywraca
stanu przetestowanego - a awaria po cofnieciu wyglada na awarie starego kodu.

CZEGO TEN PLIK NIE SPRAWDZA, A CO ZOSTAJE OTWARTE: `services/api/requirements.txt`
ma zaleznosci z `>=` i bez pliku blokady, wiec ta sama luka istnieje po stronie
pakietow Pythona (np. `anthropic>=0.60` przy zainstalowanym 1.1.0 - inna
GLOWNA wersja biblioteki, ktora zmienia API). Ten plik nalezy do obszaru
`services/api/` i jest poprawiany osobno.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

KORZEN = Path(__file__).resolve().parents[1]
DOCKERFILE = {p.name: (KORZEN / "docker" / p.name).read_text(encoding="utf-8")
              for p in (KORZEN / "docker").glob("*.Dockerfile")}


def test_stend_widzi_oba_pliki_budowania():
    assert set(DOCKERFILE) == {"api.Dockerfile", "web.Dockerfile"}, sorted(DOCKERFILE)


@pytest.mark.parametrize("nazwa", sorted(DOCKERFILE))
def test_kazdy_FROM_jest_przypiety_po_digescie(nazwa):
    for linia in DOCKERFILE[nazwa].splitlines():
        if not linia.strip().upper().startswith("FROM "):
            continue
        obraz = linia.split()[1]
        if obraz.startswith("scratch") or " AS " in linia.upper() and obraz in DOCKERFILE:
            continue
        assert re.search(r"@sha256:[0-9a-f]{64}$", obraz.split(" AS ")[0]), (
            f"{nazwa}: `{obraz}` to ruchomy tag - obraz nazwany sha-<commit> "
            f"nie wynikalby z commita")


@pytest.mark.parametrize("nazwa", sorted(DOCKERFILE))
def test_etapy_wielostopniowe_odwoluja_sie_po_nazwie_a_nie_po_digescie(nazwa):
    # `COPY --from=builder` ma zostac nazwa etapu - digest w tym miejscu
    # sciagalby obraz z rejestru zamiast wziac wynik poprzedniego etapu.
    for m in re.finditer(r"--from=(\S+)", DOCKERFILE[nazwa]):
        assert "@sha256:" not in m.group(1)


# --------------------------------------------------------------------------
# .dockerignore
#
# `web.Dockerfile` robi `COPY web/ ./` PO `npm ci`. Bez wykluczenia obecny na
# maszynie budujacej `web/node_modules` NADPISYWAL to, co wlasnie zainstalowal
# `npm ci` - a `git reset --hard origin/main` w build-on-vps.sh nie usuwa
# plikow niesledzonych, wiec katalog zostawal miedzy przebiegami.

IGNOROWANE = (KORZEN / ".dockerignore").read_text(encoding="utf-8")
WZORCE = [w.strip() for w in IGNOROWANE.splitlines()
          if w.strip() and not w.strip().startswith("#")]


def test_dockerignore_wyklucza_node_modules_z_kontekstu():
    assert any(w.rstrip("/").endswith("node_modules") for w in WZORCE), WZORCE


@pytest.mark.parametrize("potrzebne", ["package-lock.json", "scripts", "services", "db"])
def test_dockerignore_NIE_wyklucza_tego_co_build_musi_dostac(potrzebne):
    # `npm ci` bez package-lock.json nie dziala w ogole, a api.Dockerfile
    # kopiuje services/, db/ i scripts/. Pomylka tutaj konczy sie bledem
    # dopiero na serwerze budujacym.
    for wzorzec in WZORCE:
        czysty = wzorzec.lstrip("!").rstrip("/")
        assert czysty != potrzebne, f"`{wzorzec}` wyciecie {potrzebne} z kontekstu"
        assert not czysty.endswith(f"/{potrzebne}"), \
            f"`{wzorzec}` wycina {potrzebne} z kontekstu"


def test_copy_w_web_dockerfile_nadal_idzie_po_npm_ci():
    # Gdyby kolejnosc sie odwrocila, .dockerignore przestalby byc potrzebny -
    # a ten plik nadal twierdzilby, ze jest. Sprawdzamy zalozenie, nie tylko
    # skutek.
    tresc = DOCKERFILE["web.Dockerfile"]
    assert tresc.index("npm ci") < tresc.index("COPY web/ ./")


# ------------------------------------------------- plik blokady drzewa zaleznosci
def _piny(tekst: str) -> dict[str, str]:
    """nazwa -> wersja z linii `pakiet[extras]==X`. Komentarze i puste pomijamy."""
    out = {}
    for linia in tekst.splitlines():
        linia = linia.split("#")[0].strip()
        if "==" not in linia:
            continue
        nazwa, wersja = linia.split("==", 1)
        nazwa = nazwa.split("[")[0].strip().lower().replace("_", "-")
        out[nazwa] = wersja.strip()
    return out


def test_constraints_zgadza_sie_z_requirements():
    """Dwa pliki z ta sama wersja to rozjazd przy pierwszej zmianie.

    `requirements.txt` przypina warstwe bezposrednia, `constraints.txt` cale
    drzewo. Gdyby podniesc wersje w jednym i zapomniec o drugim, pip przerwie
    build konfliktem - ale dopiero w CI, po pelnym przebiegu testow. Ten test
    mowi to samo w sekunde i w miejscu, w ktorym zmiana powstaje.
    """
    req = _piny((KORZEN / "services/api/requirements.txt").read_text(encoding="utf-8"))
    con = _piny((KORZEN / "services/api/constraints.txt").read_text(encoding="utf-8"))
    assert req, "requirements.txt nie ma ani jednego przypiecia =="
    for nazwa, wersja in req.items():
        assert nazwa in con, (
            f"{nazwa} jest w requirements.txt, a nie ma go w constraints.txt — "
            f"plik blokady przestal obejmowac warstwe bezposrednia")
        assert con[nazwa] == wersja, (
            f"{nazwa}: requirements.txt mowi {wersja}, constraints.txt {con[nazwa]}")


def test_constraints_nie_ma_extras():
    """Skladnia plikow ograniczen ich nie dopuszcza - pip odrzuca caly plik."""
    tresc = (KORZEN / "services/api/constraints.txt").read_text(encoding="utf-8")
    for nr, linia in enumerate(tresc.splitlines(), 1):
        linia = linia.split("#")[0].strip()
        assert "[" not in linia, (
            f"constraints.txt:{nr} zawiera extras — pip odrzuci cały plik: {linia}")


def test_obraz_instaluje_Z_plikiem_blokady():
    """Plik blokady niewpiety w budowanie jest dokumentacja, nie blokada."""
    dockerfile = (KORZEN / "docker/api.Dockerfile").read_text(encoding="utf-8")
    assert "constraints.txt" in dockerfile, "obraz nie kopiuje pliku blokady"
    assert re.search(r"pip install[^\n]*-c\s+constraints\.txt", dockerfile), (
        "pip instaluje bez `-c constraints.txt` — zaleznosci posrednie nadal "
        "plywaja, mimo ze plik istnieje")
