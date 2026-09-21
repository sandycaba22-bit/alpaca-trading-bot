#!/usr/bin/env bash
# Split stocks + crypto desde el .env híbrido existente (VPS).
# Uso: cd ~/alpaca-trading-bot && bash deploy/setup-split.sh
#
# No pide contraseñas: lee ~/alpaca-trading-bot/.env que ya tienes.
# No sube secretos a git.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}"

PYTHON="${REPO_ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  PYTHON="$(command -v python3)"
fi

echo "=== 1) Parar bots (corta spam Telegram) ==="
pm2 stop all 2>/dev/null || true

echo "=== 2) Generar .env.stocks y .env.crypto desde .env ==="
if [[ ! -f "${REPO_ROOT}/.env" ]]; then
  echo "ERROR: falta ${REPO_ROOT}/.env"
  exit 1
fi
"${PYTHON}" deploy/generate_split_env.py

echo "=== 3) Migrar / separar data/ ==="
"${PYTHON}" deploy/sync_split_data.py

echo "=== 4) Validar conexion (no opera) ==="
ENV_FILE=.env.stocks "${PYTHON}" main.py --validate
ENV_FILE=.env.crypto "${PYTHON}" main.py --validate

echo "=== 5) PM2 — procesos split ==="
pm2 delete all 2>/dev/null || true

if [[ -f ecosystem.stocks.config.js ]]; then
  pm2 start ecosystem.stocks.config.js
  pm2 start ecosystem.crypto.config.js
else
  cp -f ecosystem.stocks.cjs ecosystem.stocks.config.js
  cp -f ecosystem.crypto.cjs ecosystem.crypto.config.js
  pm2 start ecosystem.stocks.config.js
  pm2 start ecosystem.crypto.config.js
fi

pm2 save

echo ""
echo "=== Listo ==="
pm2 status
echo ""
echo "Paneles: stocks :3000 | crypto :3001"
echo "Cuando tengas keys LIVE (AK...), edita solo APCA_* en .env.stocks y:"
echo "  pm2 restart trading-bot-stocks trading-web-stocks --update-env"
