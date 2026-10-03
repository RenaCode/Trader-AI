"""Twardnienie podów (audyt Fable U-8, 2026-10-03).

1. Token ServiceAccount był montowany w każdym Deploymencie (api, db, web,
   12 workerów), choć żaden proces nie rozmawia z API Kubernetesa. CronJoby
   miały `automountServiceAccountToken: false` od dawna.
2. Workery: root filesystem tylko do odczytu, zapis wyłącznie do `emptyDir`
   na /tmp (jedyny zapis procesu: plik żywotności `/tmp/trader-zywy`,
   `puls.zyje`). Sprawdzone 03.10 uruchomieniem obrazu z `--read-only
   --tmpfs /tmp`: `--help` każdego workera i `puls.zyje` przechodzą.
3. Baza: jawny kontekst bez roota. Obraz timescaledb-ha i tak chodzi jako
   uid 1000 (zmierzone na produkcji: `id` -> uid=1000(postgres), PGDATA
   1000:1000 0700) - zmiana wymusza to, co już jest, bez `fsGroup`
   (rekursywny chown wolumenu z danymi przy każdym starcie).

Web (nginx jako root, port 80) celowo NIE: przejście na nginx-unprivileged
zmienia obraz i port naraz, a chart i tag obrazu wchodzą osobnymi commitami
(update-git) - okno, w którym Service celuje w nieistniejący port.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

KORZEN = Path(__file__).resolve().parents[1]
bez_helma = pytest.mark.skipif(shutil.which("helm") is None, reason="helm niedostępny")


def _deploymenty() -> list[dict]:
    wynik = subprocess.run(["helm", "template", str(KORZEN / "charts" / "trader")],
                           capture_output=True, text=True)
    assert wynik.returncode == 0, wynik.stderr
    return [d for d in yaml.safe_load_all(wynik.stdout)
            if d and d.get("kind") == "Deployment"]


@bez_helma
def test_zaden_deployment_nie_montuje_tokenu():
    deps = _deploymenty()
    assert len(deps) >= 10
    z_tokenem = [d["metadata"]["name"] for d in deps
                 if d["spec"]["template"]["spec"].get("automountServiceAccountToken")
                 is not False]
    assert not z_tokenem, f"token ServiceAccount zamontowany: {z_tokenem}"


@bez_helma
def test_workery_tylko_do_odczytu_z_tmp_w_emptydir():
    workery = [d for d in _deploymenty()
               if d["metadata"].get("labels", {}).get(
                   "trader.renacode.com/component") == "worker"]
    assert workery
    for d in workery:
        pod = d["spec"]["template"]["spec"]
        c = pod["containers"][0]
        assert c["securityContext"].get("readOnlyRootFilesystem") is True, \
            d["metadata"]["name"]
        montaze = {m["mountPath"]: m["name"] for m in c.get("volumeMounts", [])}
        assert "/tmp" in montaze, f"{d['metadata']['name']}: brak /tmp"
        wolumeny = {v["name"]: v for v in pod.get("volumes", [])}
        assert "emptyDir" in wolumeny[montaze["/tmp"]]


@bez_helma
def test_baza_bez_roota_i_bez_fsgroup():
    db = next(d for d in _deploymenty() if d["metadata"]["name"].endswith("-db"))
    pod = db["spec"]["template"]["spec"]
    sc = pod["securityContext"]
    assert sc["runAsNonRoot"] is True
    assert sc["runAsUser"] == 1000 and sc["runAsGroup"] == 1000
    assert "fsGroup" not in sc, "fsGroup = rekursywny chown danych przy starcie"
    c = pod["containers"][0]["securityContext"]
    assert c["allowPrivilegeEscalation"] is False
    assert c["capabilities"]["drop"] == ["ALL"]


def test_puls_pisze_wylacznie_do_tmp(tmp_path, monkeypatch):
    """Jedyny zapis procesu workera: plik żywotności - domyślnie w /tmp."""
    from services.ingest import puls

    assert puls.PLIK_ZYWOTNOSCI.startswith("/tmp/")
    plik = tmp_path / "zywy"
    monkeypatch.setenv("PLIK_ZYWOTNOSCI", str(plik))
    puls.zyje(60)
    assert int(plik.read_text()) > 0
