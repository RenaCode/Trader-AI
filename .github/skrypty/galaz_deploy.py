#!/usr/bin/env python3
"""Przesuwa galaz `deploy` na commit, ktorego obrazy ISTNIEJA. Argo CD sledzi `deploy`.

    python .github/skrypty/galaz_deploy.py --zweryfikowany <sha commita z testami>

DLACZEGO (2026-10-04, 05:50-06:03 UTC)

Argo CD sledzilo HEAD `main`, a CI podbija tagi obrazow DOPIERO PO buildzie,
osobnym commitem `[skip ci]`. Commit 6c9f28d dodal do chartu flage
`--strategia momentum`; Argo zsynchronizowalo ja od razu, z obrazem
`sha-35f533e`, ktorego `loop.py` tej flagi nie znal (argparse, kod 2). Pod
silnika (strategia Recreate) wisial w BackOff 13 minut, az doszedl commit
z nowym tagiem. Chart wyprzedzil obraz - i na HEAD `main` bedzie go
wyprzedzal przy kazdej zmianie kodu razem z chartem.

REGULA: COMMIT JEST WDRAZALNY, GDY KAZDY OBRAZ Z `values.yaml` ZBUDOWANO
Z TEGO SAMEGO KODU, KTORY TEN COMMIT ZAWIERA.

Tag `sha-S` znaczy "obraz zbudowany z commita S". Commit H jest wdrazalny,
gdy dla api i web: `git diff S H -- <sciezki obrazu>` jest pusty, a obraz
`sha-S` jest w rejestrze. Regula patrzy na TRESC, nie na kolejnosc zdarzen
w CI, wiec obejmuje bez osobnych przypadkow:

  * commit kodu przed podbiciem tagu - kod rozni sie od S, odmowa;
  * commit `chore: obrazy` - kod rowny S, mozna;
  * commit samego chartu albo dokumentacji (filtr sciezek pomija build) -
    kod rowny S z poprzedniego podbicia, mozna;
  * ROWNOLEGLY PUSH: podbicie z przebiegu T laduje na commicie X, ktory
    zmienil kod - kod H rowny X, tag mowi T, odmowa. `deploy` czeka na
    podbicie z przebiegu X.

KTO DECYDUJE. Kazdy przebieg `docker-publish` na `main` konczy sie tym
skryptem. `--zweryfikowany` to commit, ktory TEN przebieg przetestowal;
miedzy nim a HEAD `main` wolno lezec wylacznie commitom bota (podbicia tagow).
Nowszy commit czlowieka = decyzja nalezy do JEGO przebiegu (przeszedl jego
testy albo nie - ten przebieg tego nie wie).

CZEKANIE. Commit samego chartu wypchniety, gdy build poprzedniego commita
jeszcze trwa, nie jest wdrazalny od razu (kod H ma juz zmiany T, tag jeszcze
nie). Przebieg T go nie przesunie (lezy nad nim cudzy commit), wiec ten
przebieg CZEKA, az podbicie z T wyladuje - co `ODSTEP_S`, najwyzej
`LIMIT_CZEKANIA_S`. Po limicie konczy sie bez przesuniecia: `deploy` zostaje
na ostatnim dobrym commicie, a nastepny push (albo `workflow_dispatch`, ktory
buduje oba obrazy) to rozwiaze.

`deploy` PRZESUWA SIE WYLACZNIE DO PRZODU. Push bez `--force`: gdy `deploy`
nie jest przodkiem kandydata, git odmawia, a skrypt to zglasza - ktos
przesunal galaz recznie i trzeba to obejrzec, nie nadpisac.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# SCIEZKI, Z KTORYCH POWSTAJE OBRAZ (COPY w Dockerfile). Z tej samej listy
# korzysta job `changes` w docker-publish.yml (obrazy_do_budowy.py): obraz
# buduje sie, gdy ta regula odmowilaby mu `deploy`. Kompletnosc wzgledem
# Dockerfile pilnuje tests/test_galaz_deploy.py.
SCIEZKI_OBRAZU = {
    "api": ["services", "db", "scripts", "docker/api.Dockerfile"],
    "web": ["web", "docker/web.Dockerfile", "docker/nginx.conf",
            "docker/naglowki-bezpieczenstwa.conf"],
}
OBRAZY = {"api": "trader-ai-api", "web": "trader-ai-web"}
VALUES = "charts/trader/values.yaml"
WLASCICIEL = "renacode"

PODBICIE = re.compile(r"^chore: obrazy sha-[0-9a-f]{7,40} \[skip ci\]$")
ODSTEP_S = 30
LIMIT_CZEKANIA_S = 20 * 60


def git(*argv: str, sprawdz: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *argv], capture_output=True, text=True, check=sprawdz)


def tag_z_values(tresc: str, usluga: str) -> str | None:
    """Pierwszy `tag:` po linii `<usluga>:` - TO SAMO, co podbija sed w `update-git`
    (`/^api:/,/tag:/`). CZYSTA FUNKCJA."""
    w_sekcji = False
    for linia in tresc.splitlines():
        if re.match(rf"^{re.escape(usluga)}:", linia):
            w_sekcji = True
            continue
        if w_sekcji:
            m = re.match(r"^\s+tag:\s*\"?([^\"\s]+)\"?", linia)
            if m:
                return m.group(1)
    return None


def obce_commity(zweryfikowany: str, glowa: str) -> list[str]:
    """Commity miedzy `zweryfikowany` a `glowa`, ktore NIE sa podbiciem tagow bota."""
    obce = []
    for c in git("rev-list", f"{zweryfikowany}..{glowa}").stdout.split():
        temat = git("log", "-1", "--format=%s", c).stdout.strip()
        pliki = git("diff-tree", "--no-commit-id", "--name-only", "-r", c).stdout.split()
        if not (PODBICIE.match(temat) and pliki == [VALUES]):
            obce.append(f"{c[:7]} {temat}")
    return obce


def obraz_w_rejestrze(obraz: str, tag: str) -> bool:
    """Czy manifest `ghcr.io/renacode/<obraz>:<tag>` istnieje (rejestr v2)."""
    zakres = f"repository:{WLASCICIEL}/{obraz}:pull"
    zadanie = urllib.request.Request(f"https://ghcr.io/token?scope={zakres}")
    if os.environ.get("GHCR_TOKEN"):
        para = f"{os.environ.get('GHCR_USER', 'x')}:{os.environ['GHCR_TOKEN']}"
        zadanie.add_header("Authorization",
                           "Basic " + base64.b64encode(para.encode()).decode())
    with urllib.request.urlopen(zadanie, timeout=20) as r:
        token = json.load(r)["token"]
    manifest = urllib.request.Request(
        f"https://ghcr.io/v2/{WLASCICIEL}/{obraz}/manifests/{tag}", method="HEAD")
    manifest.add_header("Authorization", f"Bearer {token}")
    manifest.add_header("Accept", ", ".join((
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json")))
    try:
        with urllib.request.urlopen(manifest, timeout=20) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def niezgodnosc_obrazu(usluga: str, values: str, glowa: str, w_rejestrze=obraz_w_rejestrze,
                       sciezki: list[str] | None = None) -> str | None:
    """None = obraz `usluga` z `values` zbudowano z kodu, ktory zawiera `glowa`;
    inaczej powod, dla ktorego nie.

    JEDNA REGULA DLA DWOCH PYTAN: "czy `deploy` moze isc na `glowa`"
    (`powod_odmowy`) i "czy ten obraz trzeba zbudowac" (obrazy_do_budowy.py).
    Gdyby build pytal o co innego niz `deploy`, obraz moglby zostac niezbudowany
    przy kodzie, na ktory `deploy` czeka - dokladnie 14.10 (patrz tamten plik)."""
    sciezki = SCIEZKI_OBRAZU[usluga] if sciezki is None else sciezki
    tag = tag_z_values(values, usluga)
    m = re.fullmatch(r"sha-([0-9a-f]{7,40})", tag or "")
    if not m:
        return f"{usluga}: tag {tag!r} nie wskazuje commita"
    zrodlo = m.group(1)
    if git("cat-file", "-e", f"{zrodlo}^{{commit}}", sprawdz=False).returncode:
        return f"{usluga}: commit {zrodlo} z tagu nie istnieje w repozytorium"
    if git("diff", "--quiet", zrodlo, glowa, "--", *sciezki,
           sprawdz=False).returncode:
        return (f"{usluga}: kod rozni sie od {zrodlo}, z ktorego zbudowano obraz "
                f"{tag} - build tego kodu jeszcze nie podbil tagu")
    if not w_rejestrze(OBRAZY[usluga], tag):
        return f"{usluga}: obrazu {OBRAZY[usluga]}:{tag} nie ma w rejestrze"
    return None


def powod_odmowy(glowa: str, w_rejestrze=obraz_w_rejestrze) -> str | None:
    """None = `glowa` jest wdrazalna; inaczej powod, dla ktorego nie jest."""
    values = git("show", f"{glowa}:{VALUES}").stdout
    for usluga in SCIEZKI_OBRAZU:
        powod = niezgodnosc_obrazu(usluga, values, glowa, w_rejestrze)
        if powod:
            return powod
    return None


def przesun(zweryfikowany: str, *, odstep_s: float = ODSTEP_S,
            limit_s: float = LIMIT_CZEKANIA_S, w_rejestrze=obraz_w_rejestrze,
            proby: int = 3) -> int:
    koniec = time.monotonic() + limit_s
    while True:
        git("fetch", "--quiet", "origin", "main")
        jest_deploy = git("fetch", "--quiet", "origin", "deploy", sprawdz=False).returncode == 0
        glowa = git("rev-parse", "origin/main").stdout.strip()

        obce = obce_commity(zweryfikowany, glowa)
        if obce:
            print(f"Nad {zweryfikowany[:7]} leza nowsze commity ({'; '.join(obce)}) - "
                  f"o `deploy` zdecyduje ich przebieg.")
            return 0

        powod = powod_odmowy(glowa, w_rejestrze)
        if powod is None:
            break
        if time.monotonic() >= koniec:
            print(f"::warning::{glowa[:7]} niewdrazalny po {limit_s / 60:.0f} min: {powod}. "
                  f"`deploy` zostaje na ostatnim dobrym commicie.")
            return 0
        print(f"{glowa[:7]} jeszcze niewdrazalny ({powod}) - czekam {odstep_s:.0f} s")
        time.sleep(odstep_s)

    if jest_deploy:
        obecny = git("rev-parse", "origin/deploy").stdout.strip()
        if git("merge-base", "--is-ancestor", glowa, obecny, sprawdz=False).returncode == 0:
            print(f"`deploy` ({obecny[:7]}) juz zawiera {glowa[:7]}.")
            return 0
        if git("merge-base", "--is-ancestor", obecny, glowa, sprawdz=False).returncode:
            print(f"::error::`deploy` ({obecny[:7]}) nie jest przodkiem {glowa[:7]} - "
                  f"ktos przesunal galaz recznie; nie nadpisuje.")
            return 1

    w = git("push", "origin", f"{glowa}:refs/heads/deploy", sprawdz=False)
    if w.returncode:
        # Rownolegly przebieg przesunal `deploy` w miedzyczasie: odczyt od nowa
        # rozstrzygnie, czy ten commit juz tam jest.
        if proby <= 1:
            print(f"::error::push na `deploy` odrzucony: {w.stderr.strip()}")
            return 1
        print(f"push odrzucony ({w.stderr.strip()}) - sprawdzam od nowa")
        return przesun(zweryfikowany, odstep_s=odstep_s, limit_s=0,
                       w_rejestrze=w_rejestrze, proby=proby - 1)
    print(f"`deploy` -> {glowa[:7]}: obrazy api i web zbudowane z tego kodu.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--zweryfikowany", required=True,
                    help="commit przetestowany w tym przebiegu (github.sha)")
    ap.add_argument("--limit-czekania", type=float, default=LIMIT_CZEKANIA_S,
                    help="ile sekund czekac na podbicie tagow")
    args = ap.parse_args(argv)
    return przesun(args.zweryfikowany, limit_s=args.limit_czekania)


if __name__ == "__main__":
    sys.exit(main())
