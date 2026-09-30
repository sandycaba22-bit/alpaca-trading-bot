# Crypto Night — estrategia paper (más trades, riesgo acotado)

Perfil por defecto: **`CRYPTO_NIGHT_FILTER_PROFILE=relaxed`** + **`CRYPTO_NIGHT_BIAS_MODE=4h_only`**.

## Modo 24/7 (recomendado si quieres sábado/domingo sin pausa)

```env
CRYPTO_NIGHT_SESSION_MODE=always
CRYPTO_NIGHT_RISK_LIMITS=false
CRYPTO_NIGHT_MAX_TRADES_PER_NIGHT=0
```

- Opera **día y noche** (sin apagar viernes noche / domingo).
- **No** cierra todo a las 09:30 ET.
- Sin tope automático de trades/noche ni kill −1%/−3% (sigue el archivo kill switch manual).

## Modo noche clásico (`CRYPTO_NIGHT_SESSION_MODE=night`)

- Stop broker al fill, salidas 50% @1R, trail, time 3h
- Cierre total **09:30 ET**
- Riesgo **0,75%**/trade, **máx. 3** trades/noche
- Kill **−1%** noche / **−3%** semana
- `MAX_POSITIONS=1`, kill switch en `data-crypto-night/`

## Relaxed — mismos 5 candados, más flojos

| # | Candado | Relaxed |
|---|---------|---------|
| 1 | Sesión | Noche US; **sin entradas 90 min** tras cierre (16:00 ET) |
| 2 | Vol | ATR% 1H **p20–80** |
| 3 | Bias | 4H estructura floja; **ETH solo hereda BTC** |
| 4 | Setup | **V1**; si vacío → **V2**; vol ≥ **1.2×** |
| 5 | Calidad | **≥3/5**, R net **≥1.5** (no bajar más en live) |

## VPS

```bash
cd ~/alpaca-trading-bot && git pull origin main
# .env.crypto_night:
#   CRYPTO_NIGHT_FILTER_PROFILE=relaxed
#   CRYPTO_NIGHT_BIAS_MODE=4h_only
#   SWEEP_VARIANT=V1
pm2 restart crypto-night --update-env
pm2 logs crypto-night --lines 20
```

Log de arranque debe incluir: `perfil=relaxed`, `+90min post-cierre`, `fallback V2`, `ETH hereda BTC`.

## Volver a modo fortaleza

```env
CRYPTO_NIGHT_FILTER_PROFILE=strict
CRYPTO_NIGHT_BIAS_MODE=4h_1d
```
