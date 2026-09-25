#!/usr/bin/env bash
# Actualiza código + reinicia solo PM2 acciones (NO toca cripto).
# Uso en VPS: cd ~/alpaca-trading-bot && bash deploy/update-stocks-vol2x-vps.sh
set -euo pipefail
cd "$(dirname "$0")/.."
echo "=== git pull ==="
git pull origin main
git log -1 --oneline

ENV=".env.stocks"
if [[ ! -f "$ENV" ]]; then
  echo "ERROR: falta $ENV — copia deploy/stocks.env.example y pon keys Alpaca live."
  exit 1
fi

set_kv() {
  local key="$1" val="$2"
  if grep -q "^${key}=" "$ENV" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV"
  else
    echo "${key}=${val}" >> "$ENV"
  fi
}

echo "=== Ajustar .env.stocks (vol_2x fase 1; no sobrescribe APCA_* ) ==="
set_kv BOT_PROFILE stocks
set_kv SYNC_ENTRY_ENABLED false
set_kv STOCK_ENTRY_TIMEFRAME 5Min
set_kv STOCK_REGIME_TIMEFRAME 15Min
set_kv CONFIRM_HIGHER_TF 15Min
set_kv STOCK_ENTRY_VOL_MULT 2.0
set_kv STOCK_ENTRY_VOLUME_PERIOD 20
set_kv SMA_SLOW 50
set_kv STOCK_ASYMMETRIC_EXITS_ENABLED true
set_kv STOCK_ASYMMETRIC_SL_ATR_MULT 1.2
set_kv STOCK_ASYMMETRIC_TRAIL_ATR_MULT 2.75
set_kv USE_FIXED_RISK_SIZING true
set_kv RISK_PERCENT_PER_TRADE 0.01
set_kv MAX_OPEN_POSITIONS 3
set_kv POSITION_SIZE_PCT 0.14
set_kv MAX_NOTIONAL_PER_ORDER 45
set_kv SYMBOLS "AAPL,MSFT,SLV,TSLA"

echo "=== pm2 restart solo stocks ==="
pm2 restart trading-bot-stocks trading-web-stocks --update-env
pm2 status
echo "=== Log (busca Acciones entrada vol_2x) ==="
pm2 logs trading-bot-stocks --lines 40 --nostream
