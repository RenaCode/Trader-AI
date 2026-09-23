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

# ---------------------------------------------------------------- narzedzia
#
# Skrypt potrzebuje CZTERECH rzeczy i kazda moze zniknac przy sprzataniu
# infrastruktury - a bez tego sprawdzenia znika w polowie budowania, z
# komunikatem powloki w rodzaju "docker: command not found" i bez podpowiedzi,
# co z tym zrobic.
#
# DOCKER JEST TU NAJKRUCHSZY. Maszyna ma dwa srodowiska uruchomieniowe: docker
# do budowania i containerd k3s do uruchamiania. Docker nie jest potrzebny do
# niczego innego, wiec przy porzadkach jest naturalnym kandydatem do usuniecia -
# i wtedy przestaje dzialac cale wdrazanie, mimo ze klaster ma sie dobrze.
#
# Jesli tak sie stanie, zamiennikiem jest `nerdctl`, ktory buduje wprost do
# containerd i nie wymaga kroku save/import:
#
#     nerdctl --namespace k8s.io build -f docker/api.Dockerfile -t "$TAG" .
#
# Skrypt uzyje go SAM, gdy dockera nie bedzie, a nerdctl bedzie.
for narzedzie in k3s kubectl git; do
  command -v "$narzedzie" > /dev/null || {
    echo "BRAK: $narzedzie. Bez niego ten skrypt nie ma jak zadzialac." >&2
    exit 1
  }
done

if command -v docker > /dev/null; then
  BUDOWNICZY=docker
elif command -v nerdctl > /dev/null; then
  BUDOWNICZY=nerdctl
else
  echo "BRAK dockera i nerdctl - nie ma czym zbudowac obrazu." >&2
  echo "Zainstaluj jedno z nich albo wroc do pobierania z rejestru:" >&2
  echo "  sekret ghcr-pull + imagePullSecrets + pullPolicy: Always" >&2
  echo "  (opisane w charts/trader/values.yaml)" >&2
  exit 1
fi
echo "==> buduje przez: $BUDOWNICZY"

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
  if [ "$BUDOWNICZY" = "nerdctl" ]; then
    # nerdctl buduje WPROST do containerd, w podanej przestrzeni nazw -
    # krok save/import odpada w calosci.
    nerdctl --namespace k8s.io build -q -f "docker/$svc.Dockerfile" -t "$TAG" . > /dev/null
  else
    docker build -q -f "docker/$svc.Dockerfile" -t "$TAG" . > /dev/null
    # Import do przestrzeni nazw k8s.io - tam containerd szuka obrazow dla podow.
    docker save "$TAG" | k3s ctr -n k8s.io images import - > /dev/null
  fi

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
Z_KLASTRA=$(mktemp)
trap 'rm -f "$CHRONIONE" "$Z_KLASTRA"' EXIT

# 1. Wszystko, na co wskazuje cokolwiek uruchomionego. Z KLASTRA, nie
#    z values.yaml - liczy sie to, co faktycznie chodzi, a nie to, co powinno.
#
#    TRZY SCIEZKI, BO KAZDY RODZAJ ZASOBU TRZYMA KONTENERY GDZIE INDZIEJ.
#    Pod ma je pod `.spec.containers`, Deployment i Job pod
#    `.spec.template.spec.containers`, a CronJob o jeden poziom glebiej:
#    `.spec.jobTemplate.spec.template.spec.containers`. Do 2026-09-23 tej
#    trzeciej sciezki tu nie bylo, a `kubectl -o jsonpath` pomija brakujacy
#    klucz BEZ BLEDU - wiec `cronjobs` bylo na liscie zasobow, a zaden jego
#    obraz nie trafial do $CHRONIONE. Nie bolalo tylko dlatego, ze wszystkie
#    szesc CronJobow (instrumenty/proba/kurs/trainer/zywotnosc/backup) chodzi
#    na `{{ .Values.api.image.tag }}`, czyli tym samym tagu co Deployment
#    trader-api. Ochrona byla wiec PRZYPADKOWA: przypiecie jednego CronJoba do
#    wlasnego tagu (np. trainer na czas eksperymentu) wystarczalo, zeby ten
#    obraz zostal skasowany - i wyszloby to dopiero przy nastepnym
#    uruchomieniu z harmonogramu, jako ImagePullBackOff godziny pozniej.
SCIEZKI_OBRAZOW='{range .items[*]}'\
'{range .spec.containers[*]}{.image}{"\n"}{end}'\
'{range .spec.template.spec.containers[*]}{.image}{"\n"}{end}'\
'{range .spec.jobTemplate.spec.template.spec.containers[*]}{.image}{"\n"}{end}'\
'{end}'

# BRAK ODPOWIEDZI OD KLASTRA TO NIE JEST PUSTA LISTA.
#
# Do 2026-09-23 stalo tutaj `2>/dev/null ... || true`: blad kubectl byl
# wyciszony, a jego niepowodzenie zignorowane. Caly mechanizm "czego nie wolno
# skasowac" stal wiec na jednym poleceniu, ktoremu wolno bylo milczkiem nie
# zadzialac. Gdy serwer API k3s sie restartuje, trwa aktualizacja albo wygasl
# certyfikat klienta, $CHRONIONE zostawalo z samymi tagami z krokow 2 i 3,
# a cala reszta - z obrazem produkcji wlacznie - szla do `k3s ctr images rm`.
# `k3s ctr` rozmawia z containerd WPROST i dziala, gdy serwer API nie
# odpowiada, wiec stan "containerd zyje, API nie" jest w pelni osiagalny.
#
# Skutek zmierzony na stendzie: produkcja na sha-1298b23, main 8 commitow dalej
# (czyli poza $ZOSTAW ostatnich z historii), kubectl milczy - obraz produkcji
# skasowany, skrypt konczy sie slowem "gotowe" i kodem 0. Biegnace pody
# przezyja, ale pierwszy restart, przeniesienie poda albo skalowanie konczy sie
# ImagePullBackOff: repozytorium jest prywatne, pullPolicy: IfNotPresent,
# nie ma skad pobrac. Produkcja nie wstaje do czasu recznego przebudowania.
#
# Dlatego niepewnosc PRZERYWA sprzatanie zamiast je przeprowadzac. Pusta
# odpowiedz tez, bo "klaster nie uzywa niczego" jest nieodroznialne od "nie
# wiem, czego klaster uzywa" - a dzialajacy klaster ma zawsze co najmniej pody
# kube-system, wiec zero obrazow oznacza w praktyce to drugie.
POWOD_NIEPEWNOSCI=""
if ! kubectl get pods,deployments,cronjobs,jobs -A -o jsonpath="$SCIEZKI_OBRAZOW" \
     > "$Z_KLASTRA"; then
  POWOD_NIEPEWNOSCI="kubectl nie odczytal zasobow klastra"
elif ! grep -q '[^[:space:]]' "$Z_KLASTRA"; then
  POWOD_NIEPEWNOSCI="kubectl odpowiedzial, ale nie wskazal ANI JEDNEGO obrazu"
fi

if [ -n "$POWOD_NIEPEWNOSCI" ]; then
  {
    echo "BLAD: $POWOD_NIEPEWNOSCI."
    echo "      Nie wiem, ktorych obrazow uzywa klaster, wiec NIE KASUJE ZADNEGO."
    echo "      Sprzatanie pominiete w calosci - dysku nie ubylo, ale i nie"
    echo "      przybylo ImagePullBackOff."
    echo
    echo "      BUDOWANIE SIE UDALO. Obrazy sa w containerd i mozna wdrazac:"
    echo "        api.image.tag: \"sha-$SHA\""
    echo "        web.image.tag: \"sha-$SHA\""
    echo
    echo "      Sprzatanie powtorz osobnym przebiegiem, gdy"
    echo "      'kubectl get pods -A' znowu odpowiada."
  } >&2
  exit 1
fi

grep -v '^[[:space:]]*$' "$Z_KLASTRA" >> "$CHRONIONE"

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

  # `grep -vxF -f` z PUSTYM plikiem wzorcow nie dopasowuje niczego, czyli
  # przepuszcza WSZYSTKO - pusta lista chronionych znaczylaby "skasuj komplet".
  # Tu jest niepusta z definicji (krok 2 dopisuje dwa swiezo zbudowane tagi),
  # ale to jedno polecenie dzieli obrazy na zostawione i nieodwracalnie
  # skasowane, wiec nie opiera sie na "z definicji".
  if [ ! -s "$CHRONIONE" ]; then
    echo "BLAD: lista chronionych obrazow jest pusta - nie kasuje niczego." >&2
    exit 1
  fi
  DO_USUNIECIA=$(printf '%s\n' "$POSIADANE" | grep -vxF -f "$CHRONIONE" || true)
  for obraz in $DO_USUNIECIA; do
    k3s ctr -n k8s.io images rm "$obraz" > /dev/null 2>&1 || true
    "$BUDOWNICZY" rmi -f "$obraz" > /dev/null 2>&1 || true
    echo "    usuniety: $obraz"
  done
  echo "    $svc: zachowane $(printf '%s\n' "$POSIADANE" | grep -cxF -f "$CHRONIONE" || true), usuniete $(printf '%s\n' "$DO_USUNIECIA" | grep -c . || true)"
done

# Cache builda rosnie osobno i nie jest niczyim obrazem. nerdctl buduje
# przez BuildKit i ma wlasna komende, wiec probujemy obu i ignorujemy
# nieudana - to sprzatanie, nie warunek powodzenia wdrozenia.
docker builder prune -f --filter until=168h > /dev/null 2>&1 || true
nerdctl --namespace k8s.io system prune -f > /dev/null 2>&1 || true

echo "    wolne na /: $(df -h / | awk 'NR==2 {print $4}')"

echo
echo "==> gotowe. Ustaw w charts/trader/values.yaml:"
echo "    api.image.tag: \"sha-$SHA\""
echo "    web.image.tag: \"sha-$SHA\""
echo "    (oba z pullPolicy: IfNotPresent)"
