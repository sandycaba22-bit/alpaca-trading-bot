#!/usr/bin/env bash
# DEPRECATED — sustituido por cripto asimétrico 1H/4H (ver DEPRECATED_CRYPTO_6M.md).
# No usar en VPS; ejecutar scripts/_backtest_crypto_asymmetric.py antes de live.
# Perfil cripto paper: más señales en lateral/squeeze sin tocar el bot de acciones.
# Uso: cd ~/alpaca-trading-bot && bash deploy/apply-crypto-aggressive.sh

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ROOT}/.env.crypto"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "No existe $ENV_FILE — crea split con: bash deploy/setup-split.sh"
  exit 1
fi

upsert() {
  local key="$1"
  local val="$2"
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    echo "${key}=${val}" >> "$ENV_FILE"
  fi
}

upsert CRYPTO_REGIME_AGGRESSIVE_ENABLED true
upsert CRYPTO_ADX_THRESHOLD 12
upsert CRYPTO_ENTRY_SCORE_MIN 40
upsert CRYPTO_RSI_OVERSOLD 38
upsert CRYPTO_RSI_OVERBOUGHT 72
upsert CRYPTO_MEAN_REV_BAND_BUFFER_PCT 0.004
upsert CRYPTO_SQUEEZE_VOLUME_MULT 1.35
upsert CRYPTO_BREAKOUT_VOLUME_MULT 1.25
upsert CRYPTO_TREND_PULLBACK_ALLOW_SIDEWAYS true
upsert CRYPTO_TRADE_BEST_ONLY false
upsert CAPITAL_PROTECTION_ENABLED true
upsert CRYPTO_CAPITAL_PROTECTION_MAX_LOSS_PCT 0.05

echo "OK — .env.crypto actualizado (perfil agresivo calibrado)."
echo "Reinicia: pm2 restart trading-bot-crypto --update-env"
echo "Verifica log: grep 'Cripto régimen agresivo' logs/pm2-bot-crypto-out.log | tail -1"
