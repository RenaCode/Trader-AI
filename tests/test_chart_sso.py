"""SSO z 2FA (Authelia z renacode-infra) przed panelem - `sso` w values.yaml.

Middleware forwardAuth ma objąć KAŻDY router HTTPS hosta: router `/api/auth`
wygrywa z `/` (dłuższa reguła), więc bez middleware'u na nim formularz
logowania aplikacji zostałby wystawiony obok Authelii. Domyślnie wyłączone -
włącza `valuesObject` w argocd-apps.yaml po rejestracji 2FA właściciela.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

KORZEN = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm niedostępny")
SSO = "default-authelia-sso@kubernetescrd"
MW = "traefik.ingress.kubernetes.io/router.middlewares"


def _ingressy(*ustaw: str) -> dict[str, dict]:
    argv = ["helm", "template", str(KORZEN / "charts" / "trader")]
    for u in ustaw:
        argv += ["--set", u]
    wynik = subprocess.run(argv, capture_output=True, text=True)
    assert wynik.returncode == 0, wynik.stderr
    return {d["metadata"]["name"]: d for d in yaml.safe_load_all(wynik.stdout)
            if d and d["kind"] == "Ingress"}


def _middleware(ing: dict) -> list[str]:
    return [m for m in ing["metadata"].get("annotations", {}).get(MW, "").split(",") if m]


def _wejscie(ing: dict) -> str:
    return ing["metadata"]["annotations"]["traefik.ingress.kubernetes.io/router.entrypoints"]


def test_domyslnie_BEZ_sso():
    """Scalenie chartu niczego nie zamyka - podpięcie to osobny krok."""
    for nazwa, ing in _ingressy().items():
        assert SSO not in _middleware(ing), nazwa


def test_sso_na_KAZDYM_routerze_HTTPS():
    ingressy = _ingressy("sso.enabled=true")
    https = {n: i for n, i in ingressy.items() if _wejscie(i) == "websecure"}
    assert set(https) == {"trader", "trader-auth"}
    for nazwa, ing in https.items():
        assert SSO in _middleware(ing), nazwa


def test_limit_logowania_PRZED_sso():
    """Zalew z jednego adresu ma dostać 429, zanim dojdzie do Authelii."""
    mw = _middleware(_ingressy("sso.enabled=true")["trader-auth"])
    assert mw.index("default-trader-limit-logowania@kubernetescrd") < mw.index(SSO)


def test_router_HTTP_tylko_przekierowuje():
    """Bez sso, ale też bez dostępu do aplikacji: jedyny middleware to
    przekierowanie na HTTPS, które odpowiada bez pytania backendu."""
    http = [i for i in _ingressy("sso.enabled=true").values() if _wejscie(i) == "web"]
    assert [_middleware(i) for i in http] == [["default-trader-redirect-https@kubernetescrd"]]


def test_sso_razem_z_awaryjnym_basicauth():
    ingressy = _ingressy("sso.enabled=true", "ingress.basicAuth.enabled=true")
    assert _middleware(ingressy["trader"]) == [SSO, "default-trader-basicauth@kubernetescrd"]
    assert _middleware(ingressy["trader-auth"])[-1] == "default-trader-basicauth@kubernetescrd"
