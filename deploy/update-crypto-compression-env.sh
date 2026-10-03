#!/usr/bin/env bash
# Modo compresión asimétrica — dedupe claves en .env.crypto_night
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV="${ROOT}/.env.crypto_night"

if [[ ! -f "$ENV" ]]; then
  echo "No existe $ENV"
  exit 1
fi

sed -i 's/\r$//' "$ENV"

set_kv() {
  local key="$1" val="$2"
  sed -i "/^[[:space:]]*${key}=/d" "$ENV"
  echo "${key}=${val}" >> "$ENV"
}

set_kv CRYPTO_NIGHT_ASYMMETRIC_LOW_VOL_MODE "true"
set_kv CRYPTO_NIGHT_COMPRESSION_MAX_STOP_PCT "0.0018"
set_kv CRYPTO_NIGHT_COMPRESSION_TRAIL_ATR_MULT "2.0"
set_kv CRYPTO_NIGHT_COMPRESSION_MAX_RUNNER_PCT "0.025"

echo "=== Compresión (.env.crypto_night) ==="
grep -E '^CRYPTO_NIGHT_(ASYMMETRIC|COMPRESSION_)' "$ENV" || true
echo "→ pm2 restart crypto-night --update-env"
