# Crypto Night — mapeo offline (sin PM2 local)

No usa `run_bot.py` ni el motor stocks. Respeta `data/LOCAL_DISABLED`.

## Requisitos

- `.env.crypto_night` o `ENV_FILE=.env` con keys **paper** Alpaca (solo datos).
- Python venv del repo.

## Correr sweep V1–V5

```powershell
cd C:\Users\sandy\bot-trading
$env:ENV_FILE=".env"
$env:BOT_PROFILE="crypto_night"
# Copia mínima si no tienes .env.crypto_night:
# SYMBOLS=BTC/USD,ETH/USD  APCA_*  BOT_PROFILE=crypto_night
.\.venv\Scripts\python.exe -u strategies/crypto_night/sweeps/run_mapping_sweep.py --years 3
```

Salida:

- `logs/crypto_night_mapping_sweep_summary.txt`
- Tabla por variante + veredicto **CHAMPION** o ninguna pasa umbral.

## VPS (paper en vivo)

```bash
cp deploy/crypto_night.env.example .env.crypto_night
# editar keys + Telegram
pm2 start ecosystem.crypto_night.config.js
# cambiar variante:
# SWEEP_VARIANT=V2 en .env.crypto_night
pm2 restart crypto-night --update-env
```

Kill switch solo cripto: `data-crypto-night/crypto_night_kill_switch.json` → `{"halt": true}`
