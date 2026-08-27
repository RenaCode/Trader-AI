FROM node:20-alpine AS builder
WORKDIR /app
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM nginx:alpine
# TLS terminuje Traefik (cert-manager, letsencrypt-prod) - nginx serwuje czysty HTTP.
# Dublowanie certyfikatow w kontenerze bylo zbedne i trudne w utrzymaniu.
COPY --from=builder /app/dist /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
