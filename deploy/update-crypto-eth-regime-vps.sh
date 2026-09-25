#!/usr/bin/env bash
# Activa régimen ETH paper en .env.crypto (no toca .env.stocks).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ROOT}/.env.crypto"
EXAMPLE="${ROOT}/deploy/crypto.env.example"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Falta $ENV_FILE — copia desde deploy/crypto.env.example"
  exit 1
fi

set_kv() {
  local key="$1" val="$2"
  if grep -q "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    echo "${key}=${val}" >> "$ENV_FILE"
  fi
}

set_kv "CRYPTO_REGIME_ENTRY_ENABLED" "true"
set_kv "CRYPTO_REGIME_ENTRY_SYMBOLS" "ETH/USD"
set_kv "SYNC_ENTRY_ENABLED" "true"
set_kv "CRYPTO_ASYMMETRIC_SL_ATR_MULT" "1.2"
set_kv "CRYPTO_ASYMMETRIC_TRAIL_ATR_MULT" "2.75"
set_kv "CRYPTO_ASYMMETRIC_TRAIL_ACTIVATE_ATR_MULT" "1.25"
set_kv "CRYPTO_ASYMMETRIC_TICK_SECONDS" "3600"

echo "OK: régimen ETH paper aplicado en $ENV_FILE"
grep -E '^(CRYPTO_REGIME|SYNC_ENTRY|CRYPTO_ASYMMETRIC_SL|CRYPTO_ASYMMETRIC_TRAIL|APCA_API_BASE)' "$ENV_FILE" || true
