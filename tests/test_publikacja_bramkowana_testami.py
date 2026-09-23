"""Publikacja obrazu musi być bramkowana testami jednostkowymi.

Znalezisko nr 8 audytu 2026-09-23. `pytest` żył w osobnym workflow `testy`
(`.github/workflows/tests.yml`), który nie był ani zależnością, ani warunkiem
żadnego joba w `docker-publish.yml`. Push na `main` z czerwonym pakietem
testów budował obraz i wypychał go do rejestru normalnie.

DLACZEGO „przecież jest ochrona gałęzi" NIE JEST ODPOWIEDZIĄ. Ochrona gałęzi
konfiguruje się poza repozytorium: nie widać jej w przeglądzie zmian, nie
przenosi się przy klonowaniu ani forku i nie da się jej sprawdzić testem.
Zabezpieczenie, którego istnienia nie da się potwierdzić z kodu, jest
założeniem, a nie zabezpieczeniem — ta sama zasada, dla której progi sond
w `api.yaml` są wpisane jawnie, mimo że równe domyślnym.

CZEGO TEN PLIK ŚWIADOMIE NIE ZGŁASZA: `update-git` z `if: false`. To jest
udokumentowana i świadoma decyzja (`docker-publish.yml:31-46`): brak
`imagePullSecrets` w namespace, obrazy budowane na VPS-ie i importowane wprost
do containerd, `pullPolicy: IfNotPresent`, tag w `values.yaml` podbijany
ręcznie. Skutek — push na `main` nie rusza produkcji — jest opisany razem
z kolejnością odblokowania.
"""
from __future__ import annotations

from pathlib import Path

import yaml

KORZEN = Path(__file__).resolve().parents[1]
PRZEPLYWY = KORZEN / ".github/workflows"


def _workflow(nazwa: str) -> dict:
    return yaml.safe_load((PRZEPLYWY / nazwa).read_text(encoding="utf-8"))


def _wyzwalacze(w: dict) -> dict:
    """`on:` w YAML-u jest wartością logiczną `True`, nie napisem „on"."""
    return w.get("on") if "on" in w else w[True]


def test_testy_jednostkowe_sa_WYWOLYWALNE_z_publikacji():
    """Bez `workflow_call` bramki nie da się zbudować inaczej niż przez
    skopiowanie kroku `pytest` — a kopia rozjeżdża się z oryginałem przy
    pierwszej zmianie wersji Pythona albo listy zależności i widać to dopiero
    wtedy, gdy jedna z dwóch przestaje cokolwiek sprawdzać."""
    assert "workflow_call" in _wyzwalacze(_workflow("tests.yml")), (
        "`tests.yml` nie jest wywoływalny z innego przebiegu")


def test_build_obrazu_ZALEZY_od_testow_jednostkowych():
    """Sedno wady: między czerwonym `pytest` a obrazem w rejestrze nie stało
    NIC."""
    build = _workflow("docker-publish.yml")["jobs"]["build"]
    assert "testy" in build["needs"], (
        "job `build` nie zależy od testów jednostkowych — push z czerwonym "
        "pakietem testów buduje i wypycha obraz")


def test_bramka_testow_NIE_przepuszcza_joba_POMINIETEGO():
    """Kanarek, i to ten, który ma tu największą wartość.

    `build` ma `if: always()` (bo `verify-migrations` i `smoke` wolno pominąć
    filtrem ścieżek), więc sama obecność w `needs` NICZEGO nie bramkuje:
    `always()` uruchamia joba także wtedy, gdy zależność padła. Warunek musi
    wymagać `result == 'success'` — i w odróżnieniu od tamtych dwóch NIE
    wolno mu dopuszczać `'skipped'`, bo job pominięty wygląda w przeglądzie
    dokładnie tak samo jak job, który przeszedł.
    """
    build = _workflow("docker-publish.yml")["jobs"]["build"]
    warunek = " ".join(build["if"].split())
    assert "needs.testy.result == 'success'" in warunek, (
        f"`build` nie wymaga POWODZENIA testów. Warunek: {warunek!r}. "
        f"Przy `if: always()` sama obecność w `needs` nie bramkuje niczego")
    assert "needs.testy.result == 'skipped'" not in warunek, (
        "bramka dopuszcza pominięte testy — a pominięty job nie sprawdza nic")


def test_testy_biegna_NIEZALEZNIE_od_filtru_sciezek():
    """Pakiet testów czyta w tym projekcie także charty i źródła frontu, więc
    „filtr nie widzi zmian w `services/`" nie znaczy „nie ma czego sprawdzać".
    Job pominięty przez filtr nie bramkuje publikacji."""
    testy = _workflow("docker-publish.yml")["jobs"]["testy"]
    assert "if" not in testy, (
        "job `testy` ma warunek — pominięty warunkiem nie zatrzyma publikacji, "
        "a wygląda tak samo jak zaliczony")
    assert testy["uses"] == "./.github/workflows/tests.yml", (
        "bramka woła skopiowany krok zamiast istniejącego workflow — dwie "
        "kopie tej samej rzeczy rozjadą się po cichu")


def test_pakiet_testow_chodzi_z_wylaczonym_bajtkodem():
    """Znany błąd tego projektu: edycja pliku niezmieniająca jego rozmiaru
    potrafi trafić w stary `.pyc` i dać wynik nie z tego kodu. Bramka, która
    testuje poprzednią wersję, jest gorsza niż jej brak."""
    krok = (PRZEPLYWY / "tests.yml").read_text(encoding="utf-8")
    assert "python -B -m pytest" in krok, (
        "`pytest` bez `-B` — stary bajtkod może przepuścić kod, którego nikt "
        "nie sprawdził")
