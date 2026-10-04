"""Front i API bez roota (przegląd 2026-10-04, P-T8, T-13).

Front jest jedynym podem wystawionym do internetu, a nginx chodził jako root
z zapisywalnym systemem plików; w podzie API proces mógł nadpisać własny kod.
Port 8080 musi się zgadzać w czterech miejscach naraz - pomyłka w jednym to
niedostępny front.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

KORZEN = Path(__file__).resolve().parents[1]
CHART = KORZEN / "charts" / "trader"
z_helmem = pytest.mark.skipif(shutil.which("helm") is None, reason="brak helm")


def _render() -> list[dict]:
    out = subprocess.run(["helm", "template", "trader", str(CHART)],
                         capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _obiekt(docs, kind, name):
    (d,) = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    return d


def test_dockerfile_i_nginx_na_8080_bez_roota():
    df = (KORZEN / "docker/web.Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^FROM nginxinc/nginx-unprivileged:alpine@sha256:[0-9a-f]{64}$", df, re.M)
    assert "EXPOSE 8080" in df and "EXPOSE 80\n" not in df
    assert re.search(r"^\s*listen 8080;", (KORZEN / "docker/nginx.conf").read_text(), re.M)
    api = (KORZEN / "docker/api.Dockerfile").read_text(encoding="utf-8")
    assert "chown" not in re.sub(r"#.*", "", api), "kod API ma należeć do roota"


@z_helmem
def test_web_port_8080_spojny_w_podzie_sondzie_service_i_netpol():
    docs = _render()
    dep = _obiekt(docs, "Deployment", "trader-web")["spec"]["template"]["spec"]
    (c,) = dep["containers"]
    assert c["ports"] == [{"containerPort": 8080}]
    assert c["readinessProbe"]["httpGet"]["port"] == 8080
    svc = _obiekt(docs, "Service", "trader-web")["spec"]["ports"]
    assert svc == [{"port": 80, "targetPort": 8080}]
    netpol = _obiekt(docs, "NetworkPolicy", "trader-web")["spec"]["ingress"][0]["ports"]
    assert netpol == [{"protocol": "TCP", "port": 8080}]


@z_helmem
@pytest.mark.parametrize("nazwa", ["trader-web", "trader-api"])
def test_runAsNonRoot_i_readOnlyRootFilesystem_z_tmp(nazwa):
    spec = _obiekt(_render(), "Deployment", nazwa)["spec"]["template"]["spec"]
    assert spec["securityContext"]["runAsNonRoot"] is True
    c = [k for k in spec["containers"] if k["name"] in ("web", "api")][0]
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert {"name": "tmp", "mountPath": "/tmp"} in c["volumeMounts"]
    assert any(v["name"] == "tmp" and "emptyDir" in v for v in spec["volumes"])
