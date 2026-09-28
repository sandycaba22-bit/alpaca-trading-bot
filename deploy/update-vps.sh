#!/usr/bin/env bash
# Actualización estándar VPS: pull + deps + validate + restart PM2 acciones.
# Uso: cd ~/alpaca-trading-bot && bash deploy/update-vps.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}"

echo "=== git pull ==="
git fetch origin main
git pull origin main
git log -1 --oneline

PYTHON="${REPO_ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  PYTHON="$(command -v python3)"
fi

echo "=== pip install ==="
"${PYTHON}" -m pip install -q -U pip
"${PYTHON}" -m pip install -q -r requirements.txt

for ENV in .env.stocks .env.stocks_top50; do
  if [[ -f "${ENV}" ]]; then
    echo "=== validate ${ENV} ==="
    ENV_FILE="${ENV}" "${PYTHON}" main.py --validate
  else
    echo "WARN: falta ${ENV} (copia desde deploy/*.env.example)"
  fi
done

echo "=== PM2 restart ==="
pm2 delete trading-bot-crypto trading-web-crypto 2>/dev/null || true

if pm2 describe trading-bot-stocks >/dev/null 2>&1; then
  pm2 restart trading-bot-stocks trading-web-stocks --update-env
else
  pm2 start ecosystem.stocks.config.js
fi

if pm2 describe trading-bot-stocks-top50 >/dev/null 2>&1; then
  pm2 restart trading-bot-stocks-top50 trading-web-stocks-top50 --update-env
else
  pm2 start ecosystem.stocks_top50.config.js
fi

pm2 save
pm2 status
