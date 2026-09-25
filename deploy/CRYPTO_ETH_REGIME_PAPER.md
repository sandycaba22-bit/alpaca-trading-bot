# ETH régimen paper (`trend_4h_sma50_atr_exp_1.2`)

Paper **solo** en perfil `crypto`. BTC sigue con `sync_entry`; ETH usa capa 1H régimen en paralelo.

## Variables (`.env.crypto`)

```env
APCA_API_BASE_URL=https://paper-api.alpaca.markets
BOT_PROFILE=crypto
SYNC_ENTRY_ENABLED=true

CRYPTO_SYMBOLS=BTC/USD,ETH/USD

CRYPTO_REGIME_ENTRY_ENABLED=true
CRYPTO_REGIME_ENTRY_SYMBOLS=ETH/USD

# Salidas asimétricas para ETH régimen (no hace falta CRYPTO_ASYMMETRIC_LIVE_ENABLED global)
CRYPTO_ASYMMETRIC_SL_ATR_MULT=1.2
CRYPTO_ASYMMETRIC_TRAIL_ATR_MULT=2.75
CRYPTO_ASYMMETRIC_TRAIL_ACTIVATE_ATR_MULT=1.25
CRYPTO_ASYMMETRIC_TICK_SECONDS=3600
```

`BOT_PROFILE=crypto` + URL paper mantiene el guardrail que **bloquea live**.

## VPS (solo proceso cripto)

```bash
cd ~/alpaca-trading-bot
git pull
bash deploy/update-crypto-eth-regime-vps.sh
pm2 restart trading-bot-crypto trading-web-crypto
```

## Log esperado al arrancar

- `Cripto régimen paper | entrada trend_4h_sma50_atr_exp_1.2 | símbolos=ETH/USD | ...`
- Compras: `reason=trend_4h_sma50_atr_exp_1.2` en journal / Telegram
- Cierres: `trend_4h_sma50_atr_exp_1.2|stop_loss` (o trailing vía SL dinámico)

## Seguimiento 3–4 semanas

Filtrar journal SQLite en `data-crypto/` por `reason LIKE 'trend_4h_sma50_atr_exp_1.2%'` y comparar PF/net vs walk-forward.
