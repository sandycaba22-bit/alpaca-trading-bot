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

## Cinco candados (siempre activos en live)

1. **Sesión** — solo entre cierre US (16:00 ET) y apertura (09:30 ET); off viernes noche y domingo; corte 09:00–09:30 ET.
2. **Volatilidad** — ATR% 1H en banda de percentiles (strict p30–70; relaxed p20–80).
3. **Bias** — **4H+1D** o `4h_only`; relaxed usa swings más cortos y estructura “floja” (OR + cierre a favor). ETH puede heredar bias de BTC.
4. **Setup** — sweep/reclaim `SWEEP_VARIANT` (V1); relaxed: vol ≥1.2× y más barras escaneadas.
5. **Calidad** — strict: score ≥4/5 y R net ≥1.8; relaxed: ≥3/5 y R net ≥1.5.

Perfil en `.env.crypto_night`: `CRYPTO_NIGHT_FILTER_PROFILE=strict|relaxed` (mismos 5 candados; relaxed = más trades paper, más riesgo).

Extra: **riesgo noche** (máx. 3 trades, −1% noche, −3% semana, profit lock +2%), archivo kill switch, `MAX_POSITIONS=1`.

Estado riesgo: `data-crypto-night/crypto_night_state.json`. Tras el fix de calidad (ATR), más setups pueden pasar el candado 5; el sweep offline sigue mostrando pocas operaciones — normal con filtros estrictos.

**Cuenta:** usa paper **dedicada** o distinta de acciones si operas stocks de día (misma key = posiciones mezcladas).

## Salidas (live, commit reciente)

Tras **fill** de la limit: **stop GTC** en broker al `stop_price` del setup.

Gestión software (cada poll):

- **50% @ +1R**, runner stop a **+0.3R**
- Trail runner a **+1.5R** si llega **+2R**
- **Time stop 3h** si no llegó a +0.5R; cierre a las 3h si sí hubo movimiento
- **Cierre total** al abrir sesión US (**09:30 ET**) para no mezclar con acciones

Estado: `data-crypto-night/crypto_night_trades.json`

Cambiar variante: editar `SWEEP_VARIANT` y `pm2 restart crypto-night --update-env`.

## Mapeo offline (PC o VPS sin PM2 stocks)

Ver `strategies/crypto_night/sweeps/README.md`.
