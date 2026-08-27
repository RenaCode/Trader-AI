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
# Uzycie (z maszyny lokalnej):
#   ssh root@185.201.112.104 'bash -s' < scripts/build-on-vps.sh
set -euo pipefail

REPO_DIR=/opt/trader-ai
REGISTRY=ghcr.io/renacode

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
  echo "    $TAG zaimportowany"
done

echo
echo "==> gotowe. Ustaw w charts/trader/values.yaml:"
echo "    api.image.tag: \"sha-$SHA\""
echo "    web.image.tag: \"sha-$SHA\""
echo "    (oba z pullPolicy: IfNotPresent)"
