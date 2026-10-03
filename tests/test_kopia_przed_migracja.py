"""Kopia bazy przed migracją, tylko gdy są nowe migracje (audyt Fable U-13).

Hook Argo CD `PreSync`, domyślnie wyłączony (`kopiaPrzedMigracja.enabled`).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

KORZEN = Path(__file__).resolve().parents[1]
bez_helma = pytest.mark.skipif(shutil.which("helm") is None, reason="helm niedostępny")


def _render(*ustaw):
    argv = ["helm", "template", str(KORZEN / "charts/trader")]
    for u in ustaw:
        argv += ["--set", u]
    w = subprocess.run(argv, capture_output=True, text=True)
    assert w.returncode == 0, w.stderr
    return [d for d in yaml.safe_load_all(w.stdout) if d]


def test_oczekujace_z_katalogu_minus_zastosowane():
    from db.migrate import oczekujace_z
    pliki = [Path("0001_a.sql"), Path("0002_b.sql"), Path("0003_c.sql")]
    assert oczekujace_z(pliki, {"0001_a", "0002_b"}) == ["0003_c"]
    assert oczekujace_z(pliki, {"0001_a", "0002_b", "0003_c"}) == []


@bez_helma
def test_domyslnie_wylaczone():
    assert not [d for d in _render() if d["metadata"]["name"].endswith("kopia-przed-migracja")]


@bez_helma
def test_hook_presync_z_lista_oczekujacych_przed_zrzutem():
    job = next(d for d in _render("kopiaPrzedMigracja.enabled=true")
               if d["metadata"]["name"] == "trader-kopia-przed-migracja")
    assert job["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "PreSync"
    pod = job["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    init = pod["initContainers"][0]
    assert init["command"][-2:] == ["--oczekujace", "/wspolne/oczekujace"]
    # Lista oczekujących z roli czujki (sam odczyt), zrzut rolą właściciela.
    assert init["env"][1]["value"].startswith("postgresql://trader_czujka@")
    assert job["metadata"].get("labels") and \
        pod.get("securityContext", {}).get("seccompProfile")
    assert job["spec"]["template"]["metadata"]["labels"][
        "trader.renacode.com/component"] == "job", "netpol bazy wpuszcza joby po etykiecie"


@bez_helma
@pytest.mark.parametrize("oczekujace,zrzut", [("", False), ("0078_x\n", True)])
def test_zrzut_tylko_gdy_sa_nowe_migracje(tmp_path, oczekujace, zrzut):
    job = next(d for d in _render("kopiaPrzedMigracja.enabled=true")
               if d["metadata"]["name"] == "trader-kopia-przed-migracja")
    skrypt = job["spec"]["template"]["spec"]["containers"][0]["command"][2]
    skrypt = skrypt.replace("/wspolne", str(tmp_path)).replace("/backup", str(tmp_path))
    (tmp_path / "oczekujace").write_text(oczekujace)
    slad = tmp_path / "slad"
    for nazwa in ("pg_dump", "pg_restore"):
        f = tmp_path / nazwa
        f.write_text(f'#!/bin/sh\necho {nazwa} >> {slad}\n'
                     + ('for a in "$@"; do case $a in --file=*) : > "${a#--file=}";; esac; done\n'
                        if nazwa == "pg_dump" else ""))
        f.chmod(0o755)
    w = subprocess.run(["sh", "-c", skrypt], capture_output=True, text=True,
                       env={"PATH": f"{tmp_path}:/usr/bin:/bin"})
    assert w.returncode == 0, w.stderr
    wywolane = slad.read_text().split() if slad.exists() else []
    assert (wywolane == ["pg_dump", "pg_restore"]) is zrzut, (wywolane, w.stdout)


DB = os.environ.get("TEST_DB_URL")


@pytest.mark.skipif(not DB, reason="ustaw TEST_DB_URL na jednorazowa baze")
def test_oczekujace_na_postgresie(tmp_path, monkeypatch):
    from db import migrate

    monkeypatch.setenv("DATABASE_URL", DB)
    plik = tmp_path / "o"
    assert migrate.zapisz_oczekujace(str(plik)) == 0
    assert plik.read_text() == "", "zmigrowana baza nie ma czego oczekiwać"
