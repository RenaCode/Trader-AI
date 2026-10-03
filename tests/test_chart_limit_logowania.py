"""Limit per IP na ścieżkach logowania w Traefiku (audyt Fable U-4).

Blokada konta w API nie zeruje się po blokadzie - jedno złe żądanie co 15 min
z dowolnego adresu odcina właściciela, a tylko on zwalnia kill-switch.
Potwierdzone na produkcji 03.10: 401×5 -> 429 dla nieistniejącego loginu,
brak jakiegokolwiek middleware limitu w klastrze.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

KORZEN = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm niedostępny")


def _manifesty(*ustaw: str) -> list[dict]:
    argv = ["helm", "template", str(KORZEN / "charts" / "trader")]
    for u in ustaw:
        argv += ["--set", u]
    wynik = subprocess.run(argv, capture_output=True, text=True)
    assert wynik.returncode == 0, wynik.stderr
    return [d for d in yaml.safe_load_all(wynik.stdout) if d]


def test_middleware_limitu_per_adres_zrodlowy():
    mw = next(d for d in _manifesty() if d["kind"] == "Middleware"
              and d["metadata"]["name"] == "trader-limit-logowania")
    rl = mw["spec"]["rateLimit"]
    assert (rl["average"], rl["period"], rl["burst"]) == (10, "1m", 20)
    # XFF podaje klient - limit po nim dałby się obejść zmianą nagłówka.
    assert rl["sourceCriterion"]["ipStrategy"]["depth"] == 0


def test_limit_tylko_na_api_auth_a_nie_na_panelu():
    ingressy = {d["metadata"]["name"]: d for d in _manifesty()
                if d["kind"] == "Ingress"}
    auth = ingressy["trader-auth"]
    sciezki = [p["path"] for r in auth["spec"]["rules"] for p in r["http"]["paths"]]
    assert sciezki == ["/api/auth"]
    assert "limit-logowania" in auth["metadata"]["annotations"][
        "traefik.ingress.kubernetes.io/router.middlewares"]
    assert "cert-manager.io/cluster-issuer" not in auth["metadata"]["annotations"], (
        "dwa Ingressy z adnotacją cert-managera walczą o jeden Certificate")
    glowny = ingressy["trader"]
    assert "limit-logowania" not in str(glowny["metadata"].get("annotations"))


def test_bramka_basicauth_obejmuje_tez_sciezke_logowania():
    """Awaryjny wyłącznik (`basicAuth.enabled`) ma odcinać CAŁY ruch - router
    `/api/auth` wygrywa z `/`, więc bez tego omijałby bramkę."""
    auth = next(d for d in _manifesty("ingress.basicAuth.enabled=true")
                if d["kind"] == "Ingress" and d["metadata"]["name"] == "trader-auth")
    assert "basicauth" in auth["metadata"]["annotations"][
        "traefik.ingress.kubernetes.io/router.middlewares"]
