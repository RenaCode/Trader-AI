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
