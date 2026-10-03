"""NetworkPolicy chartu: kto moze otworzyc polaczenie do podow tradera (2026-10-03).

Do tej daty `default` nie mial zadnej polityki i trader-db:5432 byl osiagalny
z kazdego poda w klastrze. Mapa przeplywow: `networkPolicy` w values.yaml.

Najwazniejszy test to KRZYZOWY: kazdy pod, ktory ma w srodowisku adres bazy,
musi byc wpuszczony przez polityke bazy. Nowy CronJob albo worker bez etykiety
nie zawiedzie w `helm template` - zawiedzie dopiero na produkcji, timeoutem
polaczenia, wiec pilnujemy tego tutaj.

Czesc testow czyta szablony jako tekst (jak test_chart_zadania_cykliczne.py),
czesc wymaga `helm` i pomija sie bez niego.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

KORZEN = Path(__file__).resolve().parents[1]
CHART = KORZEN / "charts/trader"
SZABLONY = CHART / "templates"
VALUES = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
CRONY = sorted(p.name for p in SZABLONY.glob("*.yaml")
               if "kind: CronJob" in p.read_text(encoding="utf-8"))

z_helmem = pytest.mark.skipif(shutil.which("helm") is None, reason="helm niedostepny")


def _render(*ustawienia: str) -> list[dict]:
    args = [a for s in ustawienia for a in ("--set", s)]
    wynik = subprocess.run(["helm", "template", "trader", str(CHART), *args],
                           capture_output=True, text=True, check=True)
    return [d for d in yaml.safe_load_all(wynik.stdout) if d]


def _polityki(docs: list[dict]) -> dict[str, dict]:
    return {d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "NetworkPolicy"}


def _pasuje(etykiety: dict, selektor: dict) -> bool:
    for k, v in (selektor.get("matchLabels") or {}).items():
        if etykiety.get(k) != v:
            return False
    for w in selektor.get("matchExpressions") or []:
        assert w["operator"] == "In", "test obsluguje tylko In"
        if etykiety.get(w["key"]) not in w["values"]:
            return False
    return True


def _pody(docs: list[dict]):
    """(nazwa, etykiety poda, kontenery) dla Deploymentow i CronJobow."""
    for d in docs:
        if d["kind"] == "Deployment":
            szablon = d["spec"]["template"]
        elif d["kind"] == "CronJob":
            szablon = d["spec"]["jobTemplate"]["spec"]["template"]
        else:
            continue
        spec = szablon["spec"]
        yield (d["metadata"]["name"], (szablon.get("metadata") or {}).get("labels") or {},
               spec.get("initContainers", []) + spec["containers"])


def _laczy_sie_z_baza(kontenery: list[dict]) -> bool:
    for k in kontenery:
        if "trader-db" in " ".join(k.get("command") or []):
            return True
        for e in k.get("env") or []:
            if e["name"] == "DATABASE_URL" and "@trader-db:5432/" in e.get("value", ""):
                return True
    return False


# ------------------------------------------------------------ bez helma

def test_polityki_domyslnie_wlaczone():
    assert VALUES["networkPolicy"]["enabled"] is True


@pytest.mark.parametrize("plik", CRONY)
def test_pod_kazdego_cronjoba_ma_etykiete_zadania(plik):
    """Bez niej Job nie dostanie sie do bazy - a pod Joba nie ma innej stalej
    etykiety, po ktorej polityka moglaby go wskazac."""
    t = (SZABLONY / plik).read_text(encoding="utf-8")
    assert ('      template:\n        metadata:\n          labels: '
            '{{- include "trader.etykietyZadania" . | nindent 12 }}\n') in t, plik


# ------------------------------------------------------------ z helmem

@z_helmem
def test_KAZDY_pod_z_adresem_bazy_jest_wpuszczany_do_bazy():
    docs = _render()
    baza = _polityki(docs)["trader-db"]
    zrodla = baza["ingress"][0]["from"]
    sprawdzone = []
    for nazwa, etykiety, kontenery in _pody(docs):
        if not _laczy_sie_z_baza(kontenery):
            continue
        sprawdzone.append(nazwa)
        assert any(_pasuje(etykiety, z["podSelector"]) for z in zrodla), (
            f"{nazwa} laczy sie z trader-db, a polityka bazy go nie wpuszcza "
            f"(etykiety poda: {etykiety})")
    # api, kilkanascie workerow i dziewiec CronJobow - pusta petla nie jest dowodem.
    assert "trader-api" in sprawdzone and "trader-backup" in sprawdzone
    assert len(sprawdzone) >= 20, sprawdzone


@z_helmem
def test_baza_NIE_wpuszcza_frontu_ani_innych_aplikacji():
    docs = _render()
    zrodla = _polityki(docs)["trader-db"]["ingress"][0]["from"]
    for obcy in ({"app": "trader-web"},
                 {"app.kubernetes.io/name": "kidwatch", "app.kubernetes.io/instance": "kidwatch"},
                 {"app.kubernetes.io/name": "dietetyk", "app.kubernetes.io/component": "backend"}):
        assert not any(_pasuje(obcy, z["podSelector"]) for z in zrodla), obcy
    # Bez namespaceSelector: peer z samym podSelector to TA SAMA przestrzen nazw.
    assert all(set(z) == {"podSelector"} for z in zrodla)
    assert _polityki(docs)["trader-db"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 5432}]


@z_helmem
def test_api_tylko_z_frontu_a_front_tylko_z_traefika():
    p = _polityki(_render())
    assert p["trader-api"]["ingress"] == [{
        "from": [{"podSelector": {"matchLabels": {"app": "trader-web"}}}],
        "ports": [{"protocol": "TCP", "port": 8000}]}]
    assert p["trader-web"]["ingress"] == [{
        "from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                  "podSelector": {"matchLabels": {"app.kubernetes.io/name": "traefik"}}}],
        "ports": [{"protocol": "TCP", "port": 80}]}]


@z_helmem
def test_workery_i_joby_bez_ruchu_przychodzacego():
    docs = _render()
    p = _polityki(docs)
    for nazwa in ("trader-workery", "trader-zadania"):
        assert p[nazwa]["policyTypes"] == ["Ingress"] and not p[nazwa].get("ingress"), nazwa
    workery = {n for n, _, _ in _pody(docs)
               if any(d["kind"] == "Deployment" and d["metadata"]["name"] == n
                      and d["metadata"]["labels"].get("trader.renacode.com/component") == "worker"
                      for d in docs)}
    assert workery and set(p["trader-workery"]["podSelector"]["matchExpressions"][0]["values"]) == workery


@z_helmem
def test_polityki_ingress_nie_maja_egress():
    for nazwa, spec in _polityki(_render()).items():
        if "egress" not in nazwa:
            assert spec["policyTypes"] == ["Ingress"] and "egress" not in spec, nazwa


# ------------------------------------------------- egress (2026-10-03)
SIEC_DOMOWA = ["192.168.3.0/24", "10.13.13.0/24"]


@z_helmem
def test_egress_obejmuje_KAZDY_pod_chartu():
    """Audyt 03.10: pod tradera dochodzil przez wg0 do UDM, TV i Fibaro.
    Pod pominiety przez selektor egress zostaje z ta sama luka."""
    docs = _render()
    p = _polityki(docs)
    selektory = [p["trader-egress"]["podSelector"], p["trader-egress-zadania"]["podSelector"]]
    pody = list(_pody(docs))
    assert len(pody) >= 20
    for nazwa, etykiety, _ in pody:
        assert any(_pasuje(etykiety, s) for s in selektory), f"{nazwa} bez polityki egress"


@z_helmem
def test_egress_blokuje_dom_i_zostawia_reszte():
    p = _polityki(_render())
    for nazwa in ("trader-egress", "trader-egress-zadania"):
        spec = p[nazwa]
        assert spec["policyTypes"] == ["Egress"] and "ingress" not in spec
        dns, klaster, swiat = spec["egress"]
        assert dns["to"][0]["podSelector"] == {"matchLabels": {"k8s-app": "kube-dns"}}
        assert {(x["protocol"], x["port"]) for x in dns["ports"]} == {("UDP", 53), ("TCP", 53)}
        assert klaster == {"to": [{"namespaceSelector": {}}]}
        assert swiat == {"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": SIEC_DOMOWA}}]}


@z_helmem
def test_flaga_egress_wylacza_tylko_egress():
    p = _polityki(_render("networkPolicy.egress.enabled=false"))
    assert p and not [n for n in p if "egress" in n]


@z_helmem
def test_flaga_wylacza_wszystkie_polityki():
    assert _polityki(_render("networkPolicy.enabled=false")) == {}
