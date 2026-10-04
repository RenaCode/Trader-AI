# Baza przypieta po digescie, nie po ruchomym tagu - uzasadnienie w api.Dockerfile.
# node:20-alpine, odczytane z registry-1.docker.io 2026-09-23.
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS builder
WORKDIR /app
COPY web/package*.json ./
RUN npm ci
# `COPY web/ ./` idzie PO `npm ci`, wiec bez `.dockerignore` obecny na maszynie
# budujacej `web/node_modules` nadpisywal to, co wlasnie zainstalowal `npm ci`
# (`git reset --hard` w build-on-vps.sh nie usuwa plikow niesledzonych).
# Wykluczenie stoi w .dockerignore w katalogu glownym repozytorium.
COPY web/ ./
RUN npm run build

# nginx-unprivileged:alpine (przeglad 2026-10-04, T-13): proces jako UID 101,
# port 8080, pid i pliki tymczasowe w /tmp - wiec pod dziala z
# `readOnlyRootFilesystem` i `emptyDir` na /tmp. Front jest jedynym podem
# wystawionym do internetu; do tej pory nginx chodzil jako root.
# Digest indeksu odczytany z registry-1.docker.io 2026-10-04.
FROM nginxinc/nginx-unprivileged:alpine@sha256:26b0bf6fbf07297983cb341998d79c831508787de26627dd2a112321b9c3a4af
# TLS terminuje Traefik (cert-manager, letsencrypt-prod) - nginx serwuje czysty HTTP.
# Dublowanie certyfikatow w kontenerze bylo zbedne i trudne w utrzymaniu.
COPY --from=builder /app/dist /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
# Naglowki bezpieczenstwa sa `include`'owane z DWOCH miejsc w nginx.conf -
# bez tego pliku nginx nie wstanie w ogole (a nie wstanie po cichu bez CSP).
COPY docker/naglowki-bezpieczenstwa.conf /etc/nginx/naglowki-bezpieczenstwa.conf
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
