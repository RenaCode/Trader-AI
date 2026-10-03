"""Pody z obrazem API nie mogą chodzić z domyślnymi uprawnieniami kontenera.

Do 02.10.2026 w całym chartcie nie było ani jednego `securityContext` poza
kopią zapasową. Obraz API ustawia `USER trader`, ale nic tego nie wymuszało,
a każdy kontener miał domyślne capabilities i `allowPrivilegeEscalation`.
Te pody trzymają `SECRETS_KEY` i klucze giełdy (`engine`) albo przetwarzają
treść od obcych (`news`).

Test czyta szablony jako tekst — bez `helm` — tak jak tests/test_chart_nadzor.py.
"""
from __future__ import annotations

import re
from pathlib import Path

KORZEN = Path(__file__).resolve().parents[1]
SZABLONY = KORZEN / "charts/trader/templates"
HELPERY = (SZABLONY / "_helpers.tpl").read_text(encoding="utf-8")


def _definicja(nazwa: str) -> str:
    i = HELPERY.index(f'define "{nazwa}"')
    return HELPERY[i:HELPERY.index("{{- end -}}", i)]


def test_pod_wymaga_uzytkownika_innego_niz_root():
    pod = _definicja("trader.podBezUprawnien")
    assert "runAsNonRoot: true" in pod
    assert "RuntimeDefault" in pod


def test_uid_jest_LICZBOWY_i_zgodny_z_obrazem():
    """`runAsNonRoot` z nienumerycznym `USER` w obrazie to pod, który nie
    wstaje: kubelet nie umie sprawdzić, że `trader` nie jest rootem. Liczba
    w chartcie musi więc istnieć i zgadzać się z Dockerfile'em."""
    uid_chart = re.search(r"runAsUser:\s*(\d+)", _definicja("trader.podBezUprawnien"))
    assert uid_chart, "brak liczbowego runAsUser — pod z runAsNonRoot nie wystartuje"
    dockerfile = (KORZEN / "docker/api.Dockerfile").read_text(encoding="utf-8")
    uid_obrazu = re.search(r"useradd[^\n]*--uid\s+(\d+)", dockerfile)
    assert uid_obrazu and uid_obrazu.group(1) == uid_chart.group(1), (
        "uid w chartcie rozjechał się z uid użytkownika w obrazie API")


def test_kontener_bez_eskalacji_i_bez_capabilities():
    k = _definicja("trader.kontenerBezUprawnien")
    assert "allowPrivilegeEscalation: false" in k
    assert re.search(r'drop:\s*\[\s*"ALL"\s*\]', k)


def test_api_i_workery_uzywaja_obu_helperow_dla_KAZDEGO_kontenera():
    for plik in ("api.yaml", "workers.yaml"):
        tekst = (SZABLONY / plik).read_text(encoding="utf-8")
        assert tekst.count('include "trader.podBezUprawnien"') == 1, plik
        # Kontener startowy i główny — oba z obrazu API, oba z tym samym zakazem.
        obrazy = tekst.count(".Values.api.image.pullPolicy")
        # `kontenerTylkoOdczyt` (U-8) to ten sam zakaz plus root fs RO.
        z_kontekstem = (tekst.count('include "trader.kontenerBezUprawnien"')
                        + tekst.count('include "trader.kontenerTylkoOdczyt"'))
        assert z_kontekstem == obrazy, (
            f"{plik}: nie każdy kontener z obrazem API dostał kontekst bezpieczeństwa")


def test_wariant_tylko_do_odczytu_zawiera_caly_zakaz():
    k = _definicja("trader.kontenerTylkoOdczyt")
    assert "allowPrivilegeEscalation: false" in k
    assert re.search(r'drop:\s*\[\s*"ALL"\s*\]', k)
    assert "readOnlyRootFilesystem: true" in k
