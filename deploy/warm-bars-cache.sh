#!/usr/bin/env bash
# Rellena data/bars_cache/ en el VPS (keys ya están en .env.*). No arranca bots live.
# Uso:
#   cd ~/alpaca-trading-bot && bash deploy/warm-bars-cache.sh
#   bash deploy/warm-bars-cache.sh crypto_night
#   bash deploy/warm-bars-cache.sh stocks_top50
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}"

PYTHON="${REPO_ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  PYTHON="$(command -v python3)"
fi

PRESET="${1:-all}"
YEARS="${BARS_CACHE_YEARS:-6}"

run_warm() {
  local env_file="$1"
  local preset="$2"
  if [[ ! -f "${env_file}" ]]; then
    echo "SKIP ${preset}: no existe ${env_file}"
    return 0
  fi
  echo "=== warm ${preset} | ENV_FILE=${env_file} | years=${YEARS} ==="
  ENV_FILE="${env_file}" "${PYTHON}" scripts/warm_bars_cache.py --preset "${preset}" --years "${YEARS}"
}

case "${PRESET}" in
  crypto_night)
    run_warm ".env.crypto_night" "crypto_night"
    ;;
  stocks_elite)
    run_warm ".env.stocks" "stocks_elite"
    ;;
  stocks_top50)
    run_warm ".env.stocks_top50" "stocks_top50"
    ;;
  all)
    run_warm ".env.crypto_night" "crypto_night"
    run_warm ".env.stocks" "stocks_elite"
    run_warm ".env.stocks_top50" "stocks_top50"
    ;;
  *)
    echo "Preset desconocido: ${PRESET} (crypto_night|stocks_elite|stocks_top50|all)"
    exit 1
    ;;
esac

echo "=== listo | manifest: ${REPO_ROOT}/data/bars_cache/manifest.json ==="
