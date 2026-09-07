#!/usr/bin/env bash
# Buduje obrazy Trader-AI bezposrednio na VPS i importuje je do containerd k3s.
#
# DLACZEGO to istnieje: repozytorium jest prywatne, wiec pakiety w ghcr.io tez sa
# prywatne, a klaster nie ma tokenu z uprawnieniem `read:packages`. Zamiast czekac
# na PAT-a, obrazy powstaja na miejscu - kubelet znajduje je lokalnie dzieki
# pullPolicy: IfNotPresent i nie probuje niczego pobierac.
#
# To jest obejscie, nie docelowa architektura. Gdy pojawi sie sekret `ghcr-pull`:
#   1. odkomentuj wpis imagePullSecrets w charts/trader/values.yaml
#   2. przywroc pullPolicy: Always
#   3. ten skrypt przestaje byc potrzebny - CI buduje i wypycha do rejestru
#
# ------------------------------------------------------------------------------
# "RANCHER" W SCIEZKACH TO NIE JEST RANCHER
#
# k3s jest produktem Rancher Labs i trzyma swoje rzeczy w /var/lib/rancher/k3s.
# Provisioner wolumenow ma identyfikator `rancher.io/local-path`. Zadne z nich
# NIE MA ZWIAZKU z serwerem zarzadzania Rancher (namespace'y cattle-*, Fleet),
# ktory z tego klastra zostal zdjety 2026-09-07.
#
# Konsekwencja jest twarda i warto ja przeczytac przed jakimkolwiek sprzataniem:
#
#     /var/lib/rancher/k3s/storage/pvc-..._default_trader-db-data
#
# to jest CALA BAZA DANYCH produkcji. Skasowanie /var/lib/rancher "bo usuwamy
# Ranchera" kasuje klaster razem z danymi. Provisioner local-path stoi
# w kube-system i jest wdrazany przez sam k3s z /var/lib/rancher/k3s/server/
# manifests/local-storage.yaml - usuniety, wroci przy nastepnym starcie k3s,
# ale wolumeny do niego nie wroca.
# ------------------------------------------------------------------------------
#
# Uzycie (z maszyny lokalnej):
#   ssh root@185.201.112.104 'bash -s' < scripts/build-on-vps.sh
#
# Zmienne:
#   ZOSTAW=N   ile poprzednich obrazow kazdej uslugi zachowac (domyslnie 5)
set -euo pipefail

REPO_DIR=/opt/trader-ai
REGISTRY=ghcr.io/renacode
ZOSTAW=${ZOSTAW:-5}

export GIT_SSH_COMMAND="ssh -i /root/.ssh/argocd_trader -o StrictHostKeyChecking=accept-new"

if [ -d "$REPO_DIR/.git" ]; then
  git -C "$REPO_DIR" fetch -q origin main
  git -C "$REPO_DIR" reset -q --hard origin/main
else
  git clone -q git@github.com:RenaCode/Trader-AI.git "$REPO_DIR"
fi

cd "$REPO_DIR"
SHA=$(git rev-parse --short HEAD)
echo "==> budowanie z commita $SHA"

for svc in api web; do
  TAG="$REGISTRY/trader-ai-$svc:sha-$SHA"
  echo "==> $svc"
  docker build -q -f "docker/$svc.Dockerfile" -t "$TAG" . > /dev/null
  # Import do przestrzeni nazw k8s.io - tam containerd szuka obrazow dla podow.
  docker save "$TAG" | k3s ctr -n k8s.io images import - > /dev/null

  # SPRAWDZENIE, ZE OBRAZ TAM JEST.
  #
  # `set -o pipefail` lapie import zakonczony bledem, ale nie lapie importu,
  # ktory zakonczyl sie sukcesem i nic nie zrobil. Bez tego warunku awaria
  # objawia sie dopiero przy synchronizacji ArgoCD - jako ImagePullBackOff
  # na obrazie, ktorego kubelet nie moze pobrac, bo repozytorium jest prywatne.
  # Czyli kilkanascie minut pozniej i w zupelnie innym miejscu.
  if ! k3s ctr -n k8s.io images ls -q | grep -qx "$TAG"; then
    echo "BLAD: $TAG nie jest w containerd mimo udanego importu" >&2
    exit 1
  fi
  echo "    $TAG zaimportowany i potwierdzony"
done

# ------------------------------------------------------------------ sprzatanie
#
# Kazdy przebieg dokladal obraz i nie usuwal zadnego. Zmierzone 2026-09-07:
# 56 obrazow trader-ai w containerd, 14 GB w /var/lib/rancher/k3s/agent/containerd.
# Przy 98 GB dysku to jeszcze nie problem, ale rosnie liniowo z liczba wdrozen
# i nikt tego nie oglada, dopoki nie zabraknie miejsca - a wtedy nie da sie
# wdrozyc niczego, w tym poprawki.
#
# CZEGO NIE WOLNO SKASOWAC: obrazu, na ktory wskazuje ktorykolwiek zasob
# w klastrze. Lista pochodzi z klastra, a nie z values.yaml - liczy sie to,
# co faktycznie jest uruchomione, a nie to, co powinno byc.
echo
echo "==> sprzatanie (zostawiam $ZOSTAW ostatnich na usluge)"

CHRONIONE=$(mktemp)
trap 'rm -f "$CHRONIONE"' EXIT

# 1. Wszystko, na co wskazuje cokolwiek uruchomionego. Z KLASTRA, nie
#    z values.yaml - liczy sie to, co faktycznie chodzi, a nie to, co powinno.
kubectl get pods,deployments,cronjobs,jobs -A \
  -o jsonpath='{range .items[*]}{range .spec.template.spec.containers[*]}{.image}{"\n"}{end}{range .spec.containers[*]}{.image}{"\n"}{end}{end}' \
  2>/dev/null >> "$CHRONIONE" || true

# 2. Wlasnie zbudowane - ArgoCD jeszcze o nich nie wie, wiec w punkcie 1 ich nie ma.
for svc in api web; do echo "$REGISTRY/trader-ai-$svc:sha-$SHA" >> "$CHRONIONE"; done

# 3. N ostatnich na usluge, do cofniecia sie w razie czego.
#
#    Kolejnosc "od najnowszego" bierzemy z HISTORII REPOZYTORIUM, bo
#    `ctr images ls` nie ma kolumny z czasem utworzenia, a tagi `sha-*` nie maja
#    porzadku alfabetycznego, ktory cokolwiek by znaczyl. Obraz z commita spoza
#    historii - na przyklad po `commit --amend` - jest z definicji nieaktualny
#    i wypada, i tak ma byc.
HISTORIA=$(git -C "$REPO_DIR" rev-list --abbrev-commit --abbrev=7 -n 300 HEAD)

for svc in api web; do
  PREFIX="$REGISTRY/trader-ai-$svc:sha-"
  POSIADANE=$(k3s ctr -n k8s.io images ls -q 2>/dev/null | grep -F "$PREFIX" | sort -u || true)
  [ -n "$POSIADANE" ] || continue

  n=0
  for c in $HISTORIA; do
    [ "$n" -ge "$ZOSTAW" ] && break
    ref="$PREFIX$c"
    printf '%s\n' "$POSIADANE" | grep -qxF "$ref" || continue
    echo "$ref" >> "$CHRONIONE"
    n=$((n + 1))
  done

  DO_USUNIECIA=$(printf '%s\n' "$POSIADANE" | grep -vxF -f "$CHRONIONE" || true)
  for obraz in $DO_USUNIECIA; do
    k3s ctr -n k8s.io images rm "$obraz" > /dev/null 2>&1 || true
    docker rmi -f "$obraz" > /dev/null 2>&1 || true
    echo "    usuniety: $obraz"
  done
  echo "    $svc: zachowane $(printf '%s\n' "$POSIADANE" | grep -cxF -f "$CHRONIONE" || true), usuniete $(printf '%s\n' "$DO_USUNIECIA" | grep -c . || true)"
done

# Cache builda rosnie osobno i nie jest niczyim obrazem.
docker builder prune -f --filter until=168h > /dev/null 2>&1 || true

echo "    wolne na /: $(df -h / | awk 'NR==2 {print $4}')"

echo
echo "==> gotowe. Ustaw w charts/trader/values.yaml:"
echo "    api.image.tag: \"sha-$SHA\""
echo "    web.image.tag: \"sha-$SHA\""
echo "    (oba z pullPolicy: IfNotPresent)"
