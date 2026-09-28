#!/usr/bin/env bash
# Checklist antes de la apertura US (élite + Top 50). No reinicia PM2 salvo --restart.
# Uso: cd ~/alpaca-trading-bot && bash deploy/pre-open-stocks.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}"

RESTART=false
if [[ "${1:-}" == "--restart" ]]; then
  RESTART=true
fi

PYTHON="${REPO_ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  PYTHON="$(command -v python3)"
fi

echo "=== Hora VPS / mercado ==="
date -u "+UTC %Y-%m-%d %H:%M:%S"
TZ=America/New_York date "+ET  %Y-%m-%d %H:%M:%S"

echo "=== PM2 ==="
pm2 status || true

echo "=== Validar env ==="
for ENV in .env.stocks .env.stocks_top50; do
  if [[ -f "${ENV}" ]]; then
    ENV_FILE="${ENV}" "${PYTHON}" main.py --validate
  else
    echo "WARN: falta ${ENV}"
  fi
done

echo "=== Pausa (control.json) ==="
for D in data-stocks data-stocks-top50; do
  F="${REPO_ROOT}/${D}/control.json"
  if [[ -f "${F}" ]]; then
    echo -n "${D}: "
    cat "${F}"
    echo
  else
    echo "${D}: (sin control.json — OK)"
  fi
done

echo "=== Posiciones broker (paper Top 50 env) ==="
ENV_FILE=.env.stocks_top50 "${PYTHON}" scripts/close_broker_symbol.py --list || true

echo "=== Claves operativas ==="
grep -E '^(SYMBOLS|MARKET_STREAM_ENABLED|BOT_PROFILE|APCA_API_BASE_URL)=' .env.stocks 2>/dev/null | head -20
grep -E '^(SYMBOLS|MARKET_STREAM_ENABLED|BOT_PROFILE|APCA_API_BASE_URL)=' .env.stocks_top50 2>/dev/null | head -20

if [[ "${RESTART}" == true ]]; then
  echo "=== PM2 restart bots (no web) ==="
  pm2 restart trading-bot-stocks trading-bot-stocks-top50 --update-env
  pm2 save
fi

echo "=== Listo ==="
echo "Apertura regular US: 09:30 ET. Revisa logs: pm2 logs trading-bot-stocks --lines 30 --nostream"
