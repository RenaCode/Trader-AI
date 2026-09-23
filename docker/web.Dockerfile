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

# nginx:alpine, digest odczytany z registry-1.docker.io 2026-09-23.
FROM nginx:alpine@sha256:1ed1b0e1d7652937d6cbdaf4018c7b6fc009a7dd6c3047351e2eddda745de43f
# TLS terminuje Traefik (cert-manager, letsencrypt-prod) - nginx serwuje czysty HTTP.
# Dublowanie certyfikatow w kontenerze bylo zbedne i trudne w utrzymaniu.
COPY --from=builder /app/dist /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
# Naglowki bezpieczenstwa sa `include`'owane z DWOCH miejsc w nginx.conf -
# bez tego pliku nginx nie wstanie w ogole (a nie wstanie po cichu bez CSP).
COPY docker/naglowki-bezpieczenstwa.conf /etc/nginx/naglowki-bezpieczenstwa.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
