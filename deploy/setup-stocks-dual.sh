#!/usr/bin/env bash
# Configura dos bots de acciones: élite (live) + Top 50 (paper).
# Uso: cd ~/alpaca-trading-bot && bash deploy/setup-stocks-dual.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}"

PYTHON="${REPO_ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  PYTHON="$(command -v python3)"
fi

echo "=== 1) Dependencias ==="
"${PYTHON}" -m pip install -q -U pip
"${PYTHON}" -m pip install -q -r requirements.txt

echo "=== 2) Plantillas .env (no sobrescribe si ya existen) ==="
if [[ ! -f .env.stocks ]]; then
  cp deploy/stocks.env.example .env.stocks
  echo "Creado .env.stocks — completa APCA_* LIVE (AK...)"
fi
if [[ ! -f .env.stocks_top50 ]]; then
  cp deploy/stocks_top50.env.example .env.stocks_top50
  echo "Creado .env.stocks_top50 — completa APCA_* PAPER (PK...)"
fi

if [[ -f .env ]]; then
  echo "=== 3) Opcional: generar env desde .env híbrido ==="
  "${PYTHON}" deploy/generate_split_env.py || true
fi

echo "=== 4) Datos por perfil ==="
"${PYTHON}" deploy/sync_split_data.py

echo "=== 5) Validar (no opera) ==="
ENV_FILE=.env.stocks "${PYTHON}" main.py --validate
ENV_FILE=.env.stocks_top50 "${PYTHON}" main.py --validate

echo "=== 6) PM2 dual acciones ==="
pm2 delete trading-bot-crypto trading-web-crypto 2>/dev/null || true
pm2 start ecosystem.production.config.js
pm2 save

echo ""
pm2 status
echo ""
echo "Paneles: élite :3000 | Top 50 :3001"
