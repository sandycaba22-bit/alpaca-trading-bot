#!/usr/bin/env bash
# Actualiza .env.crypto_night (aggressive + TP 2.5R). Borra claves viejas y deja una sola línea por key.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV="${ROOT}/.env.crypto_night"

if [[ ! -f "$ENV" ]]; then
  echo "No existe $ENV — copia deploy/crypto_night.env.example primero."
  exit 1
fi

KEYS=(
  BOT_PROFILE
  SWEEP_VARIANT
  CRYPTO_NIGHT_FILTER_PROFILE
  CRYPTO_NIGHT_BIAS_MODE
  CRYPTO_NIGHT_SESSION_MODE
  CRYPTO_NIGHT_RISK_LIMITS
  CRYPTO_NIGHT_MAX_TRADES_PER_NIGHT
  CRYPTO_NIGHT_TP_REWARD_RISK
  CRYPTO_NIGHT_SCALE_AT_1R
  MAX_POSITIONS
)

for k in "${KEYS[@]}"; do
  sed -i "/^${k}=/d" "$ENV"
done

cat >> "$ENV" <<'EOF'

# --- crypto night aggressive (update-crypto-night-aggressive-env.sh) ---
BOT_PROFILE=crypto_night
SWEEP_VARIANT=V1
CRYPTO_NIGHT_FILTER_PROFILE=aggressive
CRYPTO_NIGHT_BIAS_MODE=4h_only
CRYPTO_NIGHT_SESSION_MODE=always
CRYPTO_NIGHT_RISK_LIMITS=false
CRYPTO_NIGHT_MAX_TRADES_PER_NIGHT=0
CRYPTO_NIGHT_TP_REWARD_RISK=2.5
CRYPTO_NIGHT_SCALE_AT_1R=false
MAX_POSITIONS=2
EOF

echo "=== Claves crypto night en $ENV ==="
grep -E '^(BOT_PROFILE|SWEEP_VARIANT|CRYPTO_NIGHT_|MAX_POSITIONS)=' "$ENV" || true
echo "OK → pm2 restart crypto-night --update-env"
