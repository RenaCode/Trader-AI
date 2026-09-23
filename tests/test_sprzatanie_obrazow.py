"""`build-on-vps.sh` nie moze kasowac obrazow, gdy nie wie, czego uzywa klaster.

DLACZEGO TEN PLIK ISTNIEJE

Caly mechanizm "czego nie wolno skasowac" stoi na JEDNYM poleceniu `kubectl`.
Do 2026-09-23 jego blad byl wyciszony (`2>/dev/null`), a niepowodzenie
zignorowane (`|| true`) - wiec gdy kubectl nie dostawal odpowiedzi, lista
chronionych zostawala prawie pusta i reszta szla do `k3s ctr images rm`.

To nie jest stan teoretyczny. `k3s ctr` rozmawia z containerd WPROST i dziala,
gdy serwer API nie odpowiada: restart k3s, aktualizacja, wygasly certyfikat
klienta. Wtedy "containerd zyje, API nie" jest dokladnie tym, co sie dzieje,
a skrypt kasuje obraz produkcji (jesli jej commit wypadl poza N ostatnich
z historii) i konczy sie slowem "gotowe" z kodem 0. Biegnace pody przezyja,
ale pierwszy restart, przeniesienie poda albo skalowanie konczy sie
`ImagePullBackOff`: repozytorium jest prywatne, `pullPolicy: IfNotPresent`,
nie ma skad pobrac.

Drugie, z tej samej rodziny: CronJoby trzymaja kontenery pod
`.spec.jobTemplate.spec.template.spec.containers`, a wyrazenie jsonpath pytalo
tylko o `.spec.template.spec.containers`. `kubectl -o jsonpath` pomija
brakujacy klucz BEZ BLEDU, wiec `cronjobs` bylo na liscie zasobow, a zaden ich
obraz nie byl chroniony. Maskowal to wspolny tag: wszystkie szesc CronJobow
chodzi na `{{ .Values.api.image.tag }}`. Ochrona byla przypadkowa - test nizej
przypina jeden CronJob do wlasnego tagu, czyli do sytuacji, w ktorej przypadek
przestaje dzialac.

JAK TO JEST SPRAWDZANE

Skrypt jest uruchamiany naprawde, z atrapami `kubectl`, `k3s`, `git`
i `docker` na PATH. Atrapa `k3s` trzyma stan obrazow w pliku i notuje kazde
`images rm`, wiec "co zostalo skasowane" jest zmierzone, a nie wyczytane
z tekstu skryptu. Atrapa `kubectl` odtwarza obie wlasciwosci oryginalu:
kod wyjscia 1 przy braku odpowiedzi i MILCZENIE na sciezce, ktorej dany rodzaj
zasobu nie ma.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

KORZEN = Path(__file__).resolve().parents[1]
SKRYPT = KORZEN / "scripts/build-on-vps.sh"

PRODUKCJA = "1298b23"          # commit, na ktorym stoi produkcja
CRONJOB = "ttttttt"            # CronJob przypiety do wlasnego tagu
NOWY = "aaaaaaa"               # to, co skrypt wlasnie zbudowal
# Historia od najnowszego. Produkcja jest na 9. miejscu, czyli poza ZOSTAW=5.
HISTORIA = [NOWY, "bbbbbbb", "ccccccc", "ddddddd", "eeeeeee", "fffffff",
            "ggggggg", "hhhhhhh", PRODUKCJA]

ATRAPY = {
    "kubectl": r"""#!/usr/bin/env bash
set -uo pipefail
case "${TRYB_KUBECTL:-ok}" in
  awaria)
    echo "Unable to connect to the server: dial tcp 127.0.0.1:6443: connection refused" >&2
    exit 1 ;;
  pusto) exit 0 ;;
esac
JSONPATH=""
for a in "$@"; do
  case "$a" in
    jsonpath=*)          JSONPATH="${a#jsonpath=}" ;;
    --output=jsonpath=*) JSONPATH="${a#--output=jsonpath=}" ;;
  esac
done
while IFS=$'\t' read -r sciezka obraz; do
  [ -n "${sciezka:-}" ] || continue
  case "$JSONPATH" in *"{range $sciezka[*]}"*) printf '%s\n' "$obraz" ;; esac
done < "$FIXTURE_KLASTRA"
exit 0
""",
    "k3s": r"""#!/usr/bin/env bash
set -uo pipefail
args=("$@")
podkomenda=""
reszta=()
for i in "${!args[@]}"; do
  if [ "${args[$i]}" = "images" ]; then
    podkomenda="${args[$((i + 1))]:-}"
    reszta=("${args[@]:$((i + 2))}")
    break
  fi
done
case "$podkomenda" in
  import)
    cat >> "$STAN_OBRAZOW"
    sort -u -o "$STAN_OBRAZOW" "$STAN_OBRAZOW" ;;
  ls)
    grep -v '^[[:space:]]*$' "$STAN_OBRAZOW" || true ;;
  rm)
    for obraz in ${reszta[@]+"${reszta[@]}"}; do
      [ "${obraz#-}" = "$obraz" ] || continue
      printf '%s\n' "$obraz" >> "$USUNIETE"
      grep -vxF "$obraz" "$STAN_OBRAZOW" > "$STAN_OBRAZOW.tmp" || true
      mv "$STAN_OBRAZOW.tmp" "$STAN_OBRAZOW"
    done ;;
esac
exit 0
""",
    "git": r"""#!/usr/bin/env bash
set -uo pipefail
args=("$@")
if [ "${args[0]:-}" = "-C" ]; then args=("${args[@]:2}"); fi
case "${args[0]:-}" in
  rev-parse) printf '%s\n' "$FAKE_SHA" ;;
  rev-list)  cat "$FAKE_HISTORIA" ;;
esac
exit 0
""",
    "docker": r"""#!/usr/bin/env bash
set -uo pipefail
case "${1:-}" in
  save) for a in "$@"; do case "$a" in ghcr.io/*) printf '%s\n' "$a" ;; esac; done ;;
  rmi)  for a in "$@"; do case "$a" in ghcr.io/*) printf 'docker:%s\n' "$a" >> "$USUNIETE" ;; esac; done ;;
esac
exit 0
""",
}


def _obraz(usluga: str, commit: str) -> str:
    return f"ghcr.io/renacode/trader-ai-{usluga}:sha-{commit}"


class Przebieg:
    def __init__(self, kod: int, wyjscie: str, obrazy: list[str], usuniete: list[str]):
        self.kod, self.wyjscie = kod, wyjscie
        self.obrazy, self.usuniete = obrazy, usuniete


def uruchom(tmp_path: Path, tryb_kubectl: str) -> Przebieg:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for nazwa, tresc in ATRAPY.items():
        p = bin_dir / nazwa
        p.write_text(tresc, encoding="utf-8")
        p.chmod(0o755)

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    historia = tmp_path / "historia"
    historia.write_text("\n".join(HISTORIA) + "\n", encoding="utf-8")

    # Co containerd ma NAPRAWDE - z obrazem produkcji i obrazem CronJoba.
    stan = tmp_path / "obrazy"
    stan.write_text("\n".join(
        [_obraz(u, c) for c in HISTORIA[1:] for u in ("api", "web")]
        + [_obraz("api", CRONJOB)]) + "\n", encoding="utf-8")

    # Co widzi klaster. Klucz = sciezka, pod ktora dany rodzaj zasobu trzyma
    # kontenery; CronJob ma ja o jeden poziom glebiej niz Deployment.
    fixture = tmp_path / "klaster"
    fixture.write_text("\n".join([
        f".spec.containers\t{_obraz('api', PRODUKCJA)}",
        f".spec.containers\t{_obraz('web', PRODUKCJA)}",
        ".spec.containers\trancher/mirrored-pause:3.6",
        f".spec.template.spec.containers\t{_obraz('api', PRODUKCJA)}",
        f".spec.template.spec.containers\t{_obraz('web', PRODUKCJA)}",
        f".spec.jobTemplate.spec.template.spec.containers\t{_obraz('api', CRONJOB)}",
    ]) + "\n", encoding="utf-8")

    usuniete = tmp_path / "usuniete"
    usuniete.write_text("", encoding="utf-8")

    # Jedyna podmiana w kodzie pod testem: katalog repozytorium na VPS.
    zrodlo = SKRYPT.read_text(encoding="utf-8")
    podmieniony = zrodlo.replace("REPO_DIR=/opt/trader-ai", f"REPO_DIR={repo}")
    assert podmieniony != zrodlo, "nie znalazlem linii REPO_DIR - stend do poprawki"
    kopia = tmp_path / "build-on-vps.sh"
    kopia.write_text(podmieniony, encoding="utf-8")

    srodowisko = dict(os.environ)
    srodowisko.update({
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "TRYB_KUBECTL": tryb_kubectl,
        "FAKE_SHA": NOWY,
        "FAKE_HISTORIA": str(historia),
        "STAN_OBRAZOW": str(stan),
        "FIXTURE_KLASTRA": str(fixture),
        "USUNIETE": str(usuniete),
    })
    w = subprocess.run(["bash", str(kopia)], capture_output=True, text=True,
                       env=srodowisko, timeout=120)
    return Przebieg(
        w.returncode, w.stdout + w.stderr,
        [l for l in stan.read_text(encoding="utf-8").splitlines() if l],
        [l for l in usuniete.read_text(encoding="utf-8").splitlines()
         if l and not l.startswith("docker:")])


@pytest.fixture(scope="module", autouse=True)
def _wymagany_bash():
    if not shutil.which("bash"):
        pytest.skip("bez basha nie ma czym uruchomic skryptu")


# --------------------------------------------------------------- brak klastra

@pytest.mark.parametrize("tryb,opis", [
    ("awaria", "kubectl konczy sie bledem (serwer API nie odpowiada)"),
    ("pusto", "kubectl odpowiada, ale nie wskazuje zadnego obrazu"),
])
def test_bez_odpowiedzi_z_klastra_NIE_KASUJE_NICZEGO(tmp_path, tryb, opis):
    w = uruchom(tmp_path, tryb)
    assert w.usuniete == [], f"{opis}: skasowane mimo braku pewnosci: {w.usuniete}"
    assert _obraz("api", PRODUKCJA) in w.obrazy
    assert _obraz("web", PRODUKCJA) in w.obrazy


@pytest.mark.parametrize("tryb", ["awaria", "pusto"])
def test_bez_odpowiedzi_z_klastra_konczy_sie_BLEDEM_a_nie_slowem_gotowe(tmp_path, tryb):
    # Ostrzezenie na wyjsciu nie wystarcza: przebieg z pominietym sprzataniem
    # ma byc rozpoznawalny po kodzie wyjscia, a nie po czytaniu logu.
    w = uruchom(tmp_path, tryb)
    assert w.kod != 0
    assert "BLAD" in w.wyjscie


@pytest.mark.parametrize("tryb", ["awaria", "pusto"])
def test_komunikat_mowi_ze_budowanie_sie_udalo(tmp_path, tryb):
    # Kod wyjscia 1 nie moze wygladac jak nieudany build - obrazy SA
    # zaimportowane i wdrozenie moze isc dalej.
    w = uruchom(tmp_path, tryb)
    assert f"sha-{NOWY}" in w.wyjscie
    assert _obraz("api", NOWY) in w.obrazy


# ------------------------------------------------------- klaster odpowiada

def test_obraz_produkcji_jest_chroniony_mimo_wypadniecia_z_historii(tmp_path):
    w = uruchom(tmp_path, "ok")
    assert _obraz("api", PRODUKCJA) in w.obrazy
    assert _obraz("web", PRODUKCJA) in w.obrazy


def test_obraz_CRONJOBA_na_wlasnym_tagu_jest_chroniony(tmp_path):
    # Tego wlasnie nie widzialo wyrazenie jsonpath bez galezi jobTemplate.
    w = uruchom(tmp_path, "ok")
    assert _obraz("api", CRONJOB) in w.obrazy, (
        "obraz CronJoba skasowany - wykryloby sie to przy nastepnym "
        "uruchomieniu z harmonogramu, jako ImagePullBackOff godziny pozniej")


def test_sprzatanie_NADAL_USUWA_obrazy_naprawde_niepotrzebne(tmp_path):
    # Bez tego "nie kasuj niczego" bylo by trywialnym sposobem na zaliczenie
    # wszystkich pozostalych testow w tym pliku.
    w = uruchom(tmp_path, "ok")
    assert w.usuniete, "nic nie usuniete - sprzatanie przestalo sprzatac"
    for commit in ("fffffff", "ggggggg", "hhhhhhh"):
        assert _obraz("api", commit) in w.usuniete
    assert w.kod == 0
