"""Galaz `deploy`: Argo CD dostaje wylacznie commity, ktorych obrazy istnieja.

2026-10-04 05:50-06:03 UTC silnik lezal w BackOff: chart z 6c9f28d podal
flage `--strategia momentum` obrazowi `sha-35f533e`, ktory jej nie znal, bo
Argo CD sledzilo HEAD `main`, a tag obrazu dochodzi osobnym commitem po
buildzie. Regula i uzasadnienie: .github/skrypty/galaz_deploy.py.

Testy chodza na prawdziwym gicie (repozytorium `origin` w katalogu
tymczasowym) - regula stoi na `git diff` miedzy commitami, a atrapa gita
odtworzylaby wlasnie to, co ma byc sprawdzone.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest
import yaml

KORZEN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "galaz_deploy", KORZEN / ".github/skrypty/galaz_deploy.py")
gd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gd)

WORKFLOW = KORZEN / ".github/workflows/docker-publish.yml"

VALUES = """\
api:
  image:
    repository: ghcr.io/renacode/trader-ai-api
    tag: "sha-{api}"
web:
  image:
    repository: ghcr.io/renacode/trader-ai-web
    tag: "sha-{web}"
workers:
  engine:
    command: ["python", "services/engine/loop.py"{flaga}]
"""


def _git(*argv, cwd):
    return subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


class Repo:
    """`origin` (bare) i klon roboczy; `chdir` do klonu, bo skrypt woła `git` w cwd."""

    def __init__(self, tmp_path, monkeypatch):
        for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                     "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}.items():
            monkeypatch.setenv(k, v)
        self.origin = tmp_path / "origin.git"
        self.klon = tmp_path / "klon"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)],
                       check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.klon)],
                       check=True, capture_output=True)
        _git("checkout", "-q", "-b", "main", cwd=self.klon)
        self.flaga = ""
        self.zapisz({"services/engine/loop.py": "v1\n", "web/app.js": "v1\n"}, "start")
        sha = self.krotki()
        self.podbij(api=sha, web=sha)
        monkeypatch.chdir(self.klon)

    def krotki(self, rev="HEAD"):
        return _git("rev-parse", "--short=7", rev, cwd=self.klon)

    def pelny(self, rev):
        return _git("rev-parse", rev, cwd=self.klon)

    def zapisz(self, pliki: dict[str, str], temat: str, *, push=True) -> str:
        for sciezka, tresc in pliki.items():
            p = self.klon / sciezka
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(tresc)
        if not (self.klon / gd.VALUES).exists():
            self._values("0000000", "0000000")
        _git("add", "-A", cwd=self.klon)
        _git("commit", "-q", "-m", temat, cwd=self.klon)
        if push:
            _git("push", "-q", "origin", "main", cwd=self.klon)
        return self.pelny("HEAD")

    def _values(self, api, web):
        p = self.klon / gd.VALUES
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(VALUES.format(api=api, web=web, flaga=self.flaga))

    def tagi(self):
        t = (self.klon / gd.VALUES).read_text()
        return gd.tag_z_values(t, "api")[4:], gd.tag_z_values(t, "web")[4:]

    def podbij(self, *, api=None, web=None) -> str:
        """Commit bota - dokladnie taki, jaki robi `update-git`."""
        stare_api, stare_web = self.tagi() if (self.klon / gd.VALUES).exists() else (None, None)
        self._values(api or stare_api, web or stare_web)
        _git("add", gd.VALUES, cwd=self.klon)
        _git("commit", "-q", "-m", f"chore: obrazy sha-{api or web} [skip ci]", cwd=self.klon)
        _git("push", "-q", "origin", "main", cwd=self.klon)
        return self.pelny("HEAD")

    def chart(self, flaga: str, temat="chart: nowa flaga") -> str:
        self.flaga = flaga
        api, web = self.tagi()
        self._values(api, web)
        _git("add", "-A", cwd=self.klon)
        _git("commit", "-q", "-m", temat, cwd=self.klon)
        _git("push", "-q", "origin", "main", cwd=self.klon)
        return self.pelny("HEAD")

    def deploy(self):
        w = subprocess.run(["git", "rev-parse", "refs/heads/deploy"], cwd=self.origin,
                           capture_output=True, text=True)
        return w.stdout.strip() if w.returncode == 0 else None


def zawsze(_obraz, _tag):
    return True


@pytest.fixture
def repo(tmp_path, monkeypatch):
    return Repo(tmp_path, monkeypatch)


def przesun(zweryfikowany, w_rejestrze=zawsze):
    return gd.przesun(zweryfikowany, odstep_s=0, limit_s=0, w_rejestrze=w_rejestrze)


# ------------------------------------------------------------------ regula

def test_kod_z_chartem_przed_podbiciem_tagu_NIE_idzie_na_deploy(repo):
    """Dokladnie 6c9f28d: kod i chart w jednym commicie, tag jeszcze stary."""
    dobry = repo.pelny("HEAD")
    assert przesun(dobry) == 0 and repo.deploy() == dobry
    t = repo.zapisz({"services/engine/loop.py": "v2 z --strategia\n"}, "silnik: momentum")
    repo.chart(', "--strategia", "momentum"', "chart: flaga")
    # `deploy` stoi na ostatnim dobrym commicie, mimo ze ten przebieg czekal.
    assert przesun(repo.pelny("HEAD")) == 0
    assert repo.deploy() == dobry
    bump = repo.podbij(api=t[:7])
    assert przesun(t) == 0 and repo.deploy() != bump, "nad T lezy cudzy commit chartu"
    assert przesun(repo.pelny("HEAD~1")) == 0 and repo.deploy() == bump


def test_podbicie_tagu_idzie_na_deploy(repo):
    t = repo.zapisz({"services/engine/loop.py": "v2\n"}, "kod")
    assert przesun(t) == 0 and repo.deploy() is None
    bump = repo.podbij(api=t[:7])
    assert przesun(t) == 0 and repo.deploy() == bump


def test_commit_samego_chartu_idzie_na_deploy_bez_buildu(repo):
    """Job `changes` nie buduje niczego - a zmiana chartu i tak musi dojsc."""
    c = repo.chart(', "--interval", "300"')
    assert przesun(c) == 0 and repo.deploy() == c


def test_dokumentacja_tez_przesuwa_deploy(repo):
    c = repo.zapisz({"docs/notatka.md": "x\n"}, "docs")
    assert przesun(c) == 0 and repo.deploy() == c


def test_ROWNOLEGLY_push_podbicie_T_nad_kodem_X_NIE_idzie(repo):
    """Podbicie z przebiegu T laduje nad commitem X, ktory zmienil kod: tag
    mowi T, kod jest z X. Ani przebieg T, ani X nie przesuwa `deploy`."""
    start = repo.pelny("HEAD")
    przesun(start)
    t = repo.zapisz({"services/engine/loop.py": "v2\n"}, "kod T")
    x = repo.zapisz({"services/engine/loop.py": "v3\n"}, "kod X")
    repo.podbij(api=t[:7])
    assert przesun(t) == 0 and repo.deploy() == start, "nad T lezy X"
    assert przesun(x) == 0 and repo.deploy() == start, "kod X bez obrazu"
    bump_x = repo.podbij(api=x[:7])
    assert przesun(x) == 0 and repo.deploy() == bump_x


def test_zmiana_tylko_weba_nie_wymaga_nowego_obrazu_api(repo):
    w = repo.zapisz({"web/app.js": "v2\n"}, "front")
    bump = repo.podbij(web=w[:7])
    assert przesun(w) == 0 and repo.deploy() == bump


def test_obraz_ktorego_nie_ma_w_rejestrze_NIE_idzie(repo):
    assert przesun(repo.pelny("HEAD"), w_rejestrze=lambda o, t: o != "trader-ai-api") == 0
    assert repo.deploy() is None


def test_deploy_PRZESUNIETY_RECZNIE_nie_jest_nadpisywany(repo):
    _git("push", "-q", "origin", "HEAD:refs/heads/deploy", cwd=repo.klon)
    _git("checkout", "-q", "-b", "boczna", cwd=repo.klon)
    (repo.klon / "x").write_text("x")
    _git("add", "x", cwd=repo.klon)
    _git("commit", "-q", "-m", "boczna", cwd=repo.klon)
    _git("push", "-q", "-f", "origin", "boczna:refs/heads/deploy", cwd=repo.klon)
    reczny = repo.deploy()
    _git("checkout", "-q", "main", cwd=repo.klon)
    c = repo.zapisz({"docs/a.md": "a\n"}, "docs")
    assert przesun(c) == 1 and repo.deploy() == reczny


def test_deploy_nie_cofa_sie(repo):
    stary = repo.pelny("HEAD")
    nowy = repo.zapisz({"docs/a.md": "a\n"}, "docs")
    assert przesun(nowy) == 0 and repo.deploy() == nowy
    _git("push", "-q", "-f", "origin", f"{stary}:refs/heads/main", cwd=repo.klon)
    assert przesun(stary) == 0 and repo.deploy() == nowy


def test_tag_z_values_czyta_to_samo_co_sed_update_git():
    t = (KORZEN / "charts/trader/values.yaml").read_text(encoding="utf-8")
    v = yaml.safe_load(t)
    assert gd.tag_z_values(t, "api") == v["api"]["image"]["tag"]
    assert gd.tag_z_values(t, "web") == v["web"]["image"]["tag"]


# ------------------------------------------------------ zgodnosc z workflow

def _wf():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.mark.parametrize("usluga,dockerfile", [("api", "docker/api.Dockerfile"),
                                               ("web", "docker/web.Dockerfile")])
def test_kazdy_COPY_z_dockerfile_jest_w_sciezkach_obrazu(usluga, dockerfile):
    zrodla = re.findall(r"^COPY (?!--from)(\S+)", (KORZEN / dockerfile).read_text(),
                        re.MULTILINE)
    assert zrodla
    for z in zrodla:
        z = z.rstrip("/")
        assert any(z == s or z.startswith(s + "/") for s in gd.SCIEZKI_OBRAZU[usluga]), z


def test_update_git_nie_cofa_tagu_tymi_samymi_sciezkami():
    krok = "\n".join(s.get("run", "") for s in _wf()["jobs"]["update-git"]["steps"])
    for sciezki in gd.SCIEZKI_OBRAZU.values():
        assert f'HEAD -- {" ".join(sciezki)}' in krok


def test_job_deploy_bez_filtra_po_testach_i_po_podbiciu():
    job = _wf()["jobs"]["deploy"]
    assert {"testy", "build", "update-git"} <= set(job["needs"])
    warunek = job["if"]
    assert "always()" in warunek, "commit samego chartu musi dojsc do deploy"
    assert "needs.testy.result == 'success'" in warunek
    for j in ("build", "update-git", "smoke", "verify-migrations"):
        assert f"needs.{j}.result == 'success'" in warunek
    krok = "\n".join(s.get("run", "") for s in job["steps"])
    assert "galaz_deploy.py" in krok and "github.sha" in krok
    assert job["steps"][0]["with"]["fetch-depth"] == 0, "regula potrzebuje historii"
    assert "--force" not in krok and " -f " not in krok
