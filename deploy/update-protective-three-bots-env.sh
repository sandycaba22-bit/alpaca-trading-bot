#!/usr/bin/env bash
# Perfil SL/TP/BE unificado — .env.stocks, .env.stocks_top50, .env.crypto_night
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

set_kv() {
  local file="$1" key="$2" val="$3"
  sed -i "/^[[:space:]]*${key}=/d" "$file"
  echo "${key}=${val}" >> "$file"
}

patch_stocks() {
  local f="$1"
  sed -i 's/\r$//' "$f"
  set_kv "$f" STOCK_MAX_STOP_PCT "0.0021"
  set_kv "$f" STOCK_MIN_STOP_PCT "0"
  set_kv "$f" STOCK_ASYMMETRIC_TP_MIN_PCT "0.0065"
  set_kv "$f" STOCK_ASYMMETRIC_TP_MAX_PCT "0.008"
  set_kv "$f" STOCK_TP_TARGET_PCT "0.0075"
  set_kv "$f" BREAKEVEN_ACTIVATE_PCT "0.00375"
  set_kv "$f" BREAKEVEN_ACTIVATE_ATR_MULT "10"
  set_kv "$f" BREAKEVEN_BUFFER "0"
}

patch_crypto() {
  local f="$1"
  sed -i 's/\r$//' "$f"
  set_kv "$f" CRYPTO_NIGHT_MAX_STOP_PCT "0.0021"
  set_kv "$f" CRYPTO_NIGHT_MIN_TP_PCT "0.0065"
  set_kv "$f" CRYPTO_NIGHT_MAX_TP_PCT "0.008"
  set_kv "$f" CRYPTO_NIGHT_TP_TARGET_PCT "0.0075"
  set_kv "$f" CRYPTO_NIGHT_BREAKEVEN_ACTIVATE_PCT "0.00375"
  set_kv "$f" CRYPTO_NIGHT_BREAKEVEN_BUFFER_PCT "0"
}

for f in .env.stocks .env.stocks_top50; do
  [[ -f "$ROOT/$f" ]] && patch_stocks "$ROOT/$f" && echo "OK $f"
done
[[ -f "$ROOT/.env.crypto_night" ]] && patch_crypto "$ROOT/.env.crypto_night" && echo "OK .env.crypto_night"

echo "→ pm2 restart trading-bot-stocks trading-bot-stocks-top50 crypto-night --update-env"
