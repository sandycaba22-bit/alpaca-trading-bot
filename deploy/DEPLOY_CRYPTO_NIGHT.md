# Crypto Night Fortress (caja separada)

No comparte PM2, `DATA_DIR`, capital ni motor con `trading-bot-stocks` / Top 50.

## VPS

```bash
cd ~/alpaca-trading-bot
cp deploy/crypto_night.env.example .env.crypto_night
# Editar APCA_* paper + Telegram + SWEEP_VARIANT=V1
pm2 start ecosystem.crypto_night.config.js
pm2 save
```

Kill switch solo cripto: `echo '{"halt":true}' > data-crypto-night/crypto_night_kill_switch.json`

Cambiar variante: editar `SWEEP_VARIANT` y `pm2 restart crypto-night --update-env`.

## Mapeo offline (PC o VPS sin PM2 stocks)

Ver `strategies/crypto_night/sweeps/README.md`.
