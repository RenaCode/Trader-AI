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


# --- JEDEN PELNY PRZEBIEG NA COMMIT (audyt 2026-10-09, W-3) -------------------
#
# Do 09.10 push na `main` puszczal `tests.yml` dwa razy (wlasny `push` +
# `workflow_call` z publikacji), a kazdy przebieg mial drugi pelny `pytest`
# w kroku pilnujacym pominietych: 4 pelne przebiegi na commit, ok. 1046 minut
# od 01.10 przy limicie 2000/mies. Ponizsze testy pilnuja, ze oszczednosc NIE
# zostala kupiona utrata bramki.

def _kroki_pytest(nazwa: str) -> list[str]:
    jobs = _workflow(nazwa)["jobs"]
    return [str(k.get("run", "")) for job in jobs.values()
            for k in job.get("steps", []) if "-m pytest" in str(k.get("run", ""))]


def test_tests_yml_NIE_odpala_sie_osobno_na_push():
    """Push na `main` dostaje testy z `workflow_call` publikacji. Wlasny
    `push` byl drugim, identycznym przebiegiem tego samego commita."""
    assert "push" not in _wyzwalacze(_workflow("tests.yml")), (
        "`tests.yml` ma `push` — push na `main` puszcza pytest dwa razy "
        "(drugi raz przez `workflow_call` z docker-publish.yml)")


def test_testy_na_main_NADAL_biegna_przez_publikacje():
    """Druga strona tamtego testu: bez `push` w `tests.yml` jedyna droga
    testow na `main` to job `testy` w publikacji. Musi sie odpalac na kazdy
    push na `main`, bez warunku (warunek pilnuje test wyzej)."""
    wyzw = _wyzwalacze(_workflow("docker-publish.yml"))
    assert "main" in wyzw["push"]["branches"]
    assert "paths" not in wyzw["push"], (
        "filtr sciezek na poziomie wyzwalacza pominie testy dla czesci commitow")
    assert "pull_request" in _wyzwalacze(_workflow("tests.yml"))


def test_jeden_pelny_pytest_na_przebieg():
    """Krok „zaden test nie moze byc pominiety" puszczal caly zestaw drugi
    raz. Pominiecia sprawdza sie z wyniku pierwszego przebiegu."""
    kroki = _kroki_pytest("tests.yml")
    assert len(kroki) == 1, f"`tests.yml` puszcza pytest {len(kroki)}x: {kroki}"
    assert not _kroki_pytest("docker-publish.yml"), (
        "docker-publish.yml ma wlasna kopie pytest obok `workflow_call`")


def test_kontrola_pominietych_czyta_TEN_przebieg_z_pipefail():
    """`pytest | tee` bez `pipefail` daje kod wyjscia `tee`, czyli 0: czerwony
    zestaw konczylby sie zielonym krokiem (audyt N-3). Po zlaczeniu krokow to
    bylaby JEDYNA bramka, wiec dziura przestalaby byc teoretyczna."""
    (krok,) = _kroki_pytest("tests.yml")
    assert "set -o pipefail" in krok
    assert "skipped" in krok and "exit 1" in krok, (
        "jedyny przebieg pytest nie pada przy pominietych testach — brak bazy "
        "wygladalby jak zielone CI")


def test_stend_ui_jest_uruchamiany_w_CI():
    """Audyt 2026-10-09, S-1: `stend_ui.spec.mjs` byl czerwony 16 dni, bo nie
    wolal go zaden workflow. Test, ktorego nikt nie uruchamia, nie pilnuje
    niczego - a wyglada w repozytorium tak samo jak ten, ktory pilnuje."""
    kroki = [str(k.get("run", "")) for k in
             _workflow("docker-publish.yml")["jobs"]["smoke"]["steps"]]
    assert any("node tests/stend_ui.spec.mjs" in k for k in kroki)
