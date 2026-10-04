"""Kopia bazy przed migracją, tylko gdy są nowe migracje (audyt Fable U-13).

Hook Argo CD `PreSync` (`kopiaPrzedMigracja.enabled`), włączony decyzją
właściciela 2026-10-04: retencja po liczbie, sprawdzenie miejsca przed zrzutem,
nieudany zrzut zatrzymuje synchronizację.
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
def test_wlaczone_i_da_sie_wylaczyc():
    def jest(*ustaw):
        return any(d["metadata"]["name"].endswith("kopia-przed-migracja")
                   for d in _render(*ustaw))
    assert jest(), "decyzja 2026-10-04: kopia przed KAZDA migracja"
    assert not jest("kopiaPrzedMigracja.enabled=false")


@bez_helma
def test_hook_presync_z_lista_oczekujacych_przed_zrzutem():
    job = next(d for d in _render()
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


def _uruchom(tmp_path, oczekujace="0092_x\n", *, wolne_mib=50000, dump_pada=False,
             zostaw=5, min_wolne_mib=2048):
    """Skrypt kontenera `kopia` z atrapami narzędzi. Zwraca (wynik, ślad wywołań).

    Atrapy także dla `stat` i `df`: skrypt chodzi na obrazie bazy (GNU), a test
    bywa uruchamiany na macOS, gdzie `stat -c` nie istnieje.
    """
    job = next(d for d in _render()
               if d["metadata"]["name"] == "trader-kopia-przed-migracja")
    kontener = job["spec"]["template"]["spec"]["containers"][0]
    skrypt = kontener["command"][2]
    kopie = tmp_path / "backup"
    kopie.mkdir(exist_ok=True)
    wspolne = tmp_path / "wspolne"
    wspolne.mkdir(exist_ok=True)
    narzedzia = tmp_path / "bin"
    narzedzia.mkdir(exist_ok=True)
    skrypt = skrypt.replace("/wspolne", str(wspolne)).replace("/backup", str(kopie))
    (wspolne / "oczekujace").write_text(oczekujace)
    slad = tmp_path / "slad"
    atrapy = {
        "pg_dump": (f'echo pg_dump >> {slad}\n'
                    + ('exit 1\n' if dump_pada else '')
                    + 'for a in "$@"; do case $a in --file=*) echo zrzut > "${a#--file=}";; esac; done\n'),
        "pg_restore": f"echo pg_restore >> {slad}\n",
        "psql": f'echo psql >> {slad}\ncase "$*" in *extversion*) echo 2.17.2;; *) echo 17.5;; esac\n',
        "stat": 'wc -c < "$3" | tr -d " "\n',
        "df": f'echo "Filesystem 1024-blocks Used Available Capacity Mounted"\n'
              f'echo "/dev/x 100 0 {wolne_mib * 1024} 0% /"\n',
    }
    for nazwa, cialo in atrapy.items():
        f = narzedzia / nazwa
        f.write_text("#!/bin/sh\n" + cialo)
        f.chmod(0o755)
    env = {e["name"]: e["value"] for e in kontener["env"] if "value" in e}
    assert env == {"ZOSTAW": "5", "MIN_WOLNE_MIB": "2048"}
    env.update(ZOSTAW=str(zostaw), MIN_WOLNE_MIB=str(min_wolne_mib),
               PATH=f"{narzedzia}:/usr/bin:/bin")
    w = subprocess.run(["sh", "-c", skrypt], capture_output=True, text=True, env=env)
    return w, (slad.read_text().split() if slad.exists() else []), kopie


@bez_helma
@pytest.mark.parametrize("oczekujace,zrzut", [("", False), ("0078_x\n", True)])
def test_zrzut_tylko_gdy_sa_nowe_migracje(tmp_path, oczekujace, zrzut):
    w, wywolane, kopie = _uruchom(tmp_path, oczekujace)
    assert w.returncode == 0, w.stderr + w.stdout
    assert (["pg_dump", "pg_restore"] == [x for x in wywolane if x.startswith("pg_")]) is zrzut, \
        (wywolane, w.stdout)
    if zrzut:
        komplet = sorted(p.suffix for p in kopie.iterdir())
        assert komplet == [".dump", ".txt", ".wersje"], komplet
        wersje = next(kopie.glob("*.wersje")).read_text()
        assert "timescaledb=2.17.2" in wersje and "0078_x" in wersje


@bez_helma
def test_za_malo_miejsca_ZATRZYMUJE_synchronizacje_przed_zrzutem(tmp_path):
    """Baza leży na tym samym dysku - zrzut, który go zapełni, zatrzyma bazę."""
    w, wywolane, kopie = _uruchom(tmp_path, wolne_mib=1000, min_wolne_mib=2048)
    assert w.returncode != 0
    assert "pg_dump" not in wywolane and not list(kopie.iterdir())


@bez_helma
def test_prog_miejsca_rosnie_z_rozmiarem_ostatniego_zrzutu(tmp_path):
    kopie = tmp_path / "backup"
    kopie.mkdir()
    (kopie / "trader-20261004T011703Z.dump").write_bytes(b"x" * (2 * 1048576))
    w, wywolane, _ = _uruchom(tmp_path, wolne_mib=5, min_wolne_mib=1)
    assert w.returncode != 0 and "pg_dump" not in wywolane, w.stdout


@bez_helma
def test_nieudany_zrzut_konczy_sie_bledem_i_nie_zostawia_pliku(tmp_path):
    w, _, kopie = _uruchom(tmp_path, dump_pada=True)
    assert w.returncode != 0
    assert not list(kopie.glob("trader-przedmigracja-*")), list(kopie.iterdir())


@bez_helma
def test_retencja_zostawia_N_ostatnich_kompletow_i_nie_rusza_nocnych(tmp_path):
    kopie = tmp_path / "backup"
    kopie.mkdir()
    for i in range(4):
        for ext in (".dump", ".wersje", ".odtwarzanie.txt"):
            p = kopie / f"trader-przedmigracja-2026100{i}T000000Z{ext}"
            p.write_text("stary")
            os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
    nocny = kopie / "trader-20260901T011703Z.dump"
    nocny.write_text("nocny")
    os.utime(nocny, (1_600_000_000, 1_600_000_000))
    w, _, _ = _uruchom(tmp_path, zostaw=2, min_wolne_mib=1)
    assert w.returncode == 0, w.stderr + w.stdout
    zrzuty = sorted(p.name for p in kopie.glob("trader-przedmigracja-*.dump"))
    assert len(zrzuty) == 2 and "trader-przedmigracja-20261003T000000Z.dump" in zrzuty, zrzuty
    assert not list(kopie.glob("trader-przedmigracja-2026100[0-2]T*")), "pliki obok zostały"
    assert nocny.exists(), "nocna kopia ma własną retencję"


DB = os.environ.get("TEST_DB_URL")


@pytest.mark.skipif(not DB, reason="ustaw TEST_DB_URL na jednorazowa baze")
def test_oczekujace_na_postgresie(tmp_path, monkeypatch):
    from db import migrate

    monkeypatch.setenv("DATABASE_URL", DB)
    plik = tmp_path / "o"
    assert migrate.zapisz_oczekujace(str(plik)) == 0
    assert plik.read_text() == "", "zmigrowana baza nie ma czego oczekiwać"
