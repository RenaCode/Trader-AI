"""Flagi z chartu przyjmuje parser kodu, ktory jest w tym samym commicie.

2026-10-04 05:50-06:03 UTC silnik lezal w BackOff: commit 6c9f28d dodal do
`workers.engine.command` flage `--strategia momentum`, a `loop.py` w obrazie
jej nie znal - argparse konczy sie wtedy kodem 2, zanim cokolwiek zaloguje.
Galaz `deploy` (.github/skrypty/galaz_deploy.py) pilnuje, zeby chart nie
wyprzedzal OBRAZU. Ten test pilnuje drugiej polowy: zeby chart nie
wyprzedzal KODU - literowka w nazwie flagi albo wartosc spoza `choices`
wywraca pod tak samo, a `helm template` jej nie widzi.

JAK. Chart renderowany helmem (wartosci domyslne, czyli produkcja), z kazdego
kontenera kazde wywolanie `python <plik>.py ...` - takze wewnatrz `sh -c`
i za `przebieg_zadania.py --`. Skrypt uruchamiany w osobnym procesie
z podmienionym `ArgumentParser.parse_args`: parsuje naprawde (ten sam parser,
te same `choices` i typy), a zaraz po sukcesie przerywa, zanim skrypt dotknie
bazy albo gieldy. Kod 2 = parser odrzucil flagi z chartu.

Zmienne powloki (`$RC_CRYPTO`) zastepowane zerem - liczba jest tym, czym
w tych miejscach sa w chwili uruchomienia.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

KORZEN = Path(__file__).resolve().parents[1]
CHART = KORZEN / "charts/trader"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm niedostępny")

OPERATORY = {";", "&&", "||", "|", "&", "(", ")", "((", "))", ";;"}
PRZEDROSTKI = {"exec", "if", "then", "else", "elif", "do", "while", "until", "!", "time"}
PYTHON = {"python", "python3"}

# Podmiana `parse_args`: prawdziwe parsowanie, potem wyjatek spoza `Exception`
# (skrypty lapia `Exception` w petlach i nie moga go polknac).
URUCHOM = r"""
import argparse, runpy, sys
class Sparsowano(BaseException):
    pass
_parse = argparse.ArgumentParser.parse_args
def parse_args(self, args=None, namespace=None):
    _parse(self, args, namespace)
    raise Sparsowano
argparse.ArgumentParser.parse_args = parse_args
skrypt = sys.argv[1]
sys.argv = sys.argv[1:]
sys.path[:0] = [sys.argv[0].rsplit("/", 1)[0], "."]
try:
    runpy.run_path(skrypt, run_name="__main__")
except Sparsowano:
    sys.exit(0)
print("skrypt skonczyl sie bez parse_args", file=sys.stderr)
sys.exit(3)
"""


def _kontenery():
    w = subprocess.run(["helm", "template", "trader", str(CHART)],
                       capture_output=True, text=True)
    assert w.returncode == 0, w.stderr
    for d in yaml.safe_load_all(w.stdout):
        if not d:
            continue
        spec = d.get("spec") or {}
        pod = (spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec")
               or spec.get("template", {}).get("spec"))
        if not pod:
            continue
        for k in pod.get("initContainers", []) + pod.get("containers", []):
            argv = (k.get("command") or []) + (k.get("args") or [])
            if argv:
                yield f"{d['kind']}/{d['metadata']['name']}/{k['name']}", argv


def _z_powloki(skrypt: str):
    """Proste polecenia ze skryptu `sh -c`, linia po linii."""
    for linia in skrypt.replace("\\\n", " ").splitlines():
        lex = shlex.shlex(linia, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = "#"
        try:
            tokeny = list(lex)
        except ValueError:
            continue                      # heredoc albo tekst, nie polecenie
        polecenie: list[str] = []
        for t in tokeny + [";"]:
            if t in OPERATORY:
                if polecenie:
                    yield polecenie
                polecenie = []
            else:
                polecenie.append(t)


def _wywolania(argv: list[str]):
    """(skrypt, argumenty) dla kazdego `python <plik>.py` w poleceniu."""
    if len(argv) >= 3 and argv[0] in ("sh", "bash") and argv[1] == "-c":
        for p in _z_powloki(argv[2]):
            yield from _wywolania(p)
        return
    while argv and (argv[0] in PRZEDROSTKI or "=" in argv[0].split("/")[0]):
        argv = argv[1:]
    if not argv or argv[0] not in PYTHON:
        return
    reszta = argv[1:]
    while reszta and reszta[0].startswith("-") and not reszta[0].endswith(".py"):
        reszta = reszta[1:]                   # -B, -u
    if not reszta or not reszta[0].endswith(".py"):
        return
    skrypt, argumenty = reszta[0], reszta[1:]
    yield skrypt, ["0" if "$" in a else a for a in argumenty]
    if "--" in argumenty:                     # przebieg_zadania.py --nazwa X -- <polecenie>
        yield from _wywolania(argumenty[argumenty.index("--") + 1:])


def _wszystkie():
    wynik = {}
    for gdzie, argv in _kontenery():
        for skrypt, argumenty in _wywolania(argv):
            wynik.setdefault((skrypt, tuple(argumenty)), gdzie)
    return wynik


WYWOLANIA = _wszystkie() if shutil.which("helm") else {}


def test_kanarek_znajduje_silnik_z_flaga_strategii():
    """Gdyby ekstrakcja przestala widziec `sh -c` albo `przebieg_zadania`,
    reszta przechodzilaby pusto."""
    skrypty = {s for s, _ in WYWOLANIA}
    assert "services/engine/loop.py" in skrypty
    assert "services/engine/przebieg_zadania.py" in skrypty
    assert "services/engine/train.py" in skrypty, "wywolanie za `przebieg_zadania --`"
    # Do 2026-10-07 kanarek szukal `--strategia` (flaga z incydentu 04.10).
    # Decyzja wlasciciela z 2026-10-07 zdjela ja z chartu na rzecz
    # `--model-status candidate` - kanarek pilnuje teraz tej flagi, bo to ona
    # jest najmlodsza flaga petli w charcie, czyli ta, ktora obraz moze nie znac.
    assert any(s == "services/engine/loop.py" and "--model-status" in a
               for s, a in WYWOLANIA), "flaga z decyzji 07.10"
    assert len(WYWOLANIA) >= 20, len(WYWOLANIA)


@pytest.mark.parametrize("skrypt,argumenty", sorted(WYWOLANIA),
                         ids=[f"{s.rsplit('/', 1)[-1]} {' '.join(a)}"[:90]
                              for s, a in sorted(WYWOLANIA)])
def test_parser_kodu_przyjmuje_flagi_z_chartu(skrypt, argumenty):
    assert (KORZEN / skrypt).is_file(), f"{WYWOLANIA[(skrypt, argumenty)]}: brak {skrypt}"
    env = {k: v for k, v in os.environ.items()
           if k not in ("DATABASE_URL", "TEST_DB_URL", "BRAMKA_KLUCZ")}
    w = subprocess.run([sys.executable, "-B", "-c", URUCHOM, skrypt, *argumenty],
                       cwd=KORZEN, env=env, capture_output=True, text=True, timeout=120)
    assert w.returncode == 0, (
        f"{WYWOLANIA[(skrypt, argumenty)]}: `{skrypt} {' '.join(argumenty)}` - "
        f"kod {w.returncode}\n{w.stderr[-1500:]}")


def test_odrzucona_flaga_jest_bledem_testu():
    """Kanarek dla samego mechanizmu: flaga, ktorej `loop.py` nie zna."""
    w = subprocess.run([sys.executable, "-B", "-c", URUCHOM, "services/engine/loop.py",
                        "--strategia", "nie-ma-takiej"],
                       cwd=KORZEN, capture_output=True, text=True, timeout=120)
    assert w.returncode == 2 and "--strategia" in w.stderr, w.stderr
