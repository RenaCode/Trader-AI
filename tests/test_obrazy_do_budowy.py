"""Job `changes`: buduje obraz, gdy jego kod rozni sie od kodu obecnego tagu.

2026-10-14: PR #28 zmienil web, jego przebieg padl na tescie dymnym, wiec
obrazu web nie zbudowano. Nastepny push (#29) zmienial tylko CI; filtr sciezek
OSTATNIEGO pushu pominal web, `values.yaml` zostal przy `web: sha-7ecd3ba`,
a `galaz_deploy.py` czekal na obraz, ktorego nikt nie budowal. Regula
i uzasadnienie: .github/skrypty/obrazy_do_budowy.py.

Prawdziwy git w katalogu tymczasowym (repozytorium z test_galaz_deploy.py):
regula stoi na `git diff` miedzy commitami.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys

import pytest
import yaml

from test_galaz_deploy import KORZEN, WORKFLOW, Repo, _git, gd

sys.path.insert(0, str(KORZEN / ".github/skrypty"))
_spec = importlib.util.spec_from_file_location(
    "obrazy_do_budowy", KORZEN / ".github/skrypty/obrazy_do_budowy.py")
odb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(odb)


def zawsze(_obraz, _tag):
    return True


@pytest.fixture
def repo(tmp_path, monkeypatch):
    return Repo(tmp_path, monkeypatch)


def budowane(glowa, w_rejestrze=zawsze, **kw) -> set[str]:
    return {u for u, powod in odb.do_budowy(glowa, w_rejestrze, **kw).items() if powod}


# ------------------------------------------------------------------ regula

def test_nic_sie_nie_zmienilo_nic_nie_buduje(repo):
    assert budowane(repo.pelny("HEAD")) == set()


def test_zmiana_w_ostatnim_commicie_buduje_tylko_ten_obraz(repo):
    w = repo.zapisz({"web/app.js": "v2\n"}, "front")
    assert budowane(w) == {"web"}
    a = repo.zapisz({"services/engine/loop.py": "v2\n"}, "silnik")
    assert budowane(a) == {"api", "web"}, "web dalej bez obrazu"


def test_14_10_zmiana_z_NIEUDANEGO_przebiegu_buduje_sie_w_nastepnym(repo):
    """#28 zmienia web i nie dostaje obrazu; #29 zmienia tylko workflow."""
    start = repo.pelny("HEAD")
    assert gd.przesun(start, odstep_s=0, limit_s=0, w_rejestrze=zawsze) == 0
    repo.zapisz({"web/app.js": "v2 przelacznik\n"}, "UI (#28)")  # przebieg padl
    ci = repo.zapisz({".github/workflows/docker-publish.yml": "x\n"}, "CI (#29)")

    # Stara regula (filtr ostatniego pushu) widziala tylko workflow:
    zmienione = _git("diff", "--name-only", f"{ci}~1", ci, cwd=repo.klon).split()
    assert not any(p.startswith("web/") for p in zmienione)
    # Nowa: web rozni sie od zrodla tagu; api przez zmiane samego CI.
    powody = odb.do_budowy(ci, zawsze)
    assert powody["web"] and "rozni sie" in powody["web"]
    assert powody["api"]

    # Build + podbicie z tego przebiegu odblokowuje `deploy` od razu.
    bump = repo.podbij(api=ci[:7], web=ci[:7])
    assert gd.powod_odmowy(bump, zawsze) is None
    assert gd.przesun(ci, odstep_s=0, limit_s=0, w_rejestrze=zawsze) == 0
    assert repo.deploy() == bump
    assert budowane(bump) == set(), "po podbiciu nic juz nie czeka"


def test_14_10_nastepny_push_samej_dokumentacji_tez_buduje_zalegly_web(repo):
    repo.zapisz({"web/app.js": "v2\n"}, "UI")
    d = repo.zapisz({"docs/a.md": "a\n"}, "docs")
    assert budowane(d) == {"web"}


def test_zmiana_samego_chartu_nie_buduje(repo):
    c = repo.chart(', "--interval", "300"')
    assert budowane(c) == set()


def test_zmiana_skryptu_CI_przebudowuje_api_a_deploy_jej_nie_wymaga(repo):
    c = repo.zapisz({".github/skrypty/galaz_deploy.py": "x\n"}, "CI")
    assert budowane(c) == {"api"}
    assert gd.powod_odmowy(c, zawsze) is None, "obraz nie zawiera skryptow CI"


def test_brak_commita_zrodlowego_tagu_buduje(repo):
    """Tag z przepisanej historii albo plytki klon: nie da sie porownac - buduj."""
    repo._values("deadbee", "deadbee")
    _git("add", gd.VALUES, cwd=repo.klon)
    _git("commit", "-q", "-m", "tag spoza historii", cwd=repo.klon)
    powody = odb.do_budowy(repo.pelny("HEAD"), zawsze)
    assert all(p and "nie istnieje" in p for p in powody.values())


def test_tag_nie_wskazujacy_commita_buduje(repo):
    t = (repo.klon / gd.VALUES).read_text()
    przed, tag, po = t.rpartition(f'"sha-{repo.tagi()[1]}"')  # ostatni = web
    (repo.klon / gd.VALUES).write_text(przed + '"latest"' + po)
    _git("commit", "-q", "-am", "ruchomy tag", cwd=repo.klon)
    assert budowane(repo.pelny("HEAD")) == {"web"}


def test_obraz_nieobecny_w_rejestrze_buduje(repo):
    assert budowane(repo.pelny("HEAD"), lambda o, t: o != "trader-ai-web") == {"web"}


def test_blad_rejestru_buduje(repo):
    def pada(_o, _t):
        raise OSError("ghcr.io nie odpowiada")
    assert budowane(repo.pelny("HEAD"), pada) == {"api", "web"}


def test_workflow_dispatch_buduje_wszystkie(repo):
    assert budowane(repo.pelny("HEAD"), wszystkie=True) == {"api", "web"}


def test_main_pisze_GITHUB_OUTPUT(tmp_path, monkeypatch):
    wyjscie = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(wyjscie))
    monkeypatch.setattr(odb, "do_budowy",
                        lambda g, wszystkie=False: {"api": None, "web": "kod rozni sie"})
    assert odb.main(["--glowa", "abc1234"]) == 0
    assert wyjscie.read_text().split() == ["api=false", "web=true"]


def test_skrypt_dziala_z_linii_polecen(repo):
    """Tak, jak go wola workflow (osobny proces, import galaz_deploy z katalogu)."""
    w = subprocess.run([sys.executable, "-B", str(KORZEN / ".github/skrypty/obrazy_do_budowy.py"),
                        "--glowa", repo.pelny("HEAD"), "--wszystkie"],
                       capture_output=True, text=True, check=True)
    assert "api=true" in w.stdout and "web=true" in w.stdout


# ------------------------------------------------------ zgodnosc z workflow

def _wf():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_changes_pyta_regule_deploy_z_pelna_historia():
    job = _wf()["jobs"]["changes"]
    assert job["steps"][0]["with"]["fetch-depth"] == 0, "commit z tagu bywa stary"
    krok = "\n".join(s.get("run", "") for s in job["steps"])
    assert "obrazy_do_budowy.py" in krok and "github.sha" in krok
    assert "--wszystkie" in krok and "workflow_dispatch" in krok
    assert not any("paths-filter" in s.get("uses", "") for s in job["steps"]), (
        "filtr ostatniego pushu gubi zmiane z nieudanego przebiegu (14.10)")
    assert job["outputs"] == {"api": "${{ steps.filter.outputs.api }}",
                              "web": "${{ steps.filter.outputs.web }}"}


def test_jedna_lista_sciezek_dla_buildu_i_deploy():
    """Obraz buduje sie dokladnie wtedy, gdy `deploy` by go odrzucil - skrypt
    nie ma wlasnej listy sciezek obrazu, ktora moglaby sie rozjechac."""
    assert not hasattr(odb, "SCIEZKI_OBRAZU")
    assert set(odb.WYZWALACZE_CI) == set(gd.SCIEZKI_OBRAZU)
    assert ".github/workflows/docker-publish.yml" in odb.WYZWALACZE_CI["api"], (
        "zmiana workflow musi przejsc caly tor CI, inaczej jej nikt nie sprawdzi")
