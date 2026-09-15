#!/usr/bin/env bash
# Expone trading-web (PM2, :3000) vía Nginx en botsandy.cazadordesuenocigar.com
# Ejecutar EN LA VM (Linux), desde la raíz del repo: bash deploy/setup-public-panel.sh

set -euo pipefail

DOMAIN="botsandy.cazadordesuenocigar.com"
WEB_PORT="3000"
NGINX_SITE="/etc/nginx/sites-available/botsandy"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== 1) Firewall GCP (HTTP/HTTPS) ==="
if command -v gcloud >/dev/null 2>&1; then
  PROJECT="$(curl -sf -H 'Metadata-Flavor: Google' \
    http://metadata.google.internal/computeMetadata/v1/project/project-id 2>/dev/null || true)"
  if [[ -n "${PROJECT}" ]]; then
    gcloud config set project "${PROJECT}" >/dev/null 2>&1 || true
    for RULE in allow-http-botsandy allow-https-botsandy; do
      PORTS="tcp:80"
      [[ "${RULE}" == *https* ]] && PORTS="tcp:443"
      if ! gcloud compute firewall-rules describe "${RULE}" >/dev/null 2>&1; then
        gcloud compute firewall-rules create "${RULE}" \
          --direction=INGRESS \
          --priority=1000 \
          --network=default \
          --action=ALLOW \
          --rules="${PORTS}" \
          --source-ranges=0.0.0.0/0 \
          --target-tags=http-server,https-server \
          --description="Panel botsandy ${PORTS}"
        echo "Creada regla ${RULE}"
      else
        echo "Regla ${RULE} ya existe"
      fi
    done
    INSTANCE="$(hostname -s)"
    ZONE="$(curl -sf -H 'Metadata-Flavor: Google' \
      http://metadata.google.internal/computeMetadata/v1/instance/zone | awk -F/ '{print $NF}')"
    gcloud compute instances add-tags "${INSTANCE}" --zone="${ZONE}" \
      --tags=http-server,https-server 2>/dev/null || echo "Aviso: no se pudieron añadir tags (añade http-server,https-server manualmente)"
  else
    echo "No metadata GCP — crea reglas firewall manualmente para tcp:80 y tcp:443"
  fi
else
  echo "gcloud no instalado — abre tcp:80 y tcp:443 en el firewall de la VM manualmente"
fi

echo "=== 2) Nginx ==="
sudo apt-get update -qq
sudo apt-get install -y nginx
sudo cp "${REPO_ROOT}/deploy/nginx/botsandy.conf" "${NGINX_SITE}"
sudo ln -sf "${NGINX_SITE}" /etc/nginx/sites-enabled/botsandy
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl reload nginx

echo "=== 3) PM2 trading-web (bind 0.0.0.0:${WEB_PORT}) ==="
cd "${REPO_ROOT}"
pm2 restart trading-web --update-env || pm2 start ecosystem.config.cjs --only trading-web
pm2 save

echo "=== 4) Certbot (HTTPS) ==="
if ! command -v certbot >/dev/null 2>&1; then
  sudo apt-get install -y certbot python3-certbot-nginx
fi
sudo certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos \
  -m "${CERTBOT_EMAIL:-admin@${DOMAIN#*.}}" --redirect || \
  echo "Certbot falló — HTTP sigue activo; ejecuta certbot manualmente con tu email"

echo "=== 5) Prueba local ==="
curl -s -o /dev/null -w "HTTP local nginx: %{http_code}\n" -H "Host: ${DOMAIN}" http://127.0.0.1/
curl -s -o /dev/null -w "HTTP app :3000: %{http_code}\n" http://127.0.0.1:${WEB_PORT}/

echo "Listo. Prueba: http://${DOMAIN}"
