# Bot de trading — Alpaca Paper

Bot modular en Python integrado con la API de Alpaca Markets (solo paper trading).

Incluye cruce SMA, P&L verde/rojo por operación, stop/take-profit dinámicos, filtro de flujo de precios en vivo, walk-forward 3+3 años y avisos de Telegram.

## Universo y ejecucion

El universo de nuevas entradas es fijo: AAPL, MSFT, NVDA, AMZN, GOOGL,
META, JNJ, JPM, PG, XOM, V, HD y UNH. `SYMBOLS` y `CRYPTO_SYMBOLS`
no cambian esta lista. No se abren posiciones cripto ni entradas fuera de
la sesion regular US; no se ha habilitado la sesion overnight de Alpaca.
Las posiciones existentes siguen bajo gestion del motor.

Defaults: `BREAKOUT_VOLUME_MULT=1.45` y `ADX_THRESHOLD=24.0`.
Los valores anteriores 1.85 y 26.0 se migran al cargar la configuracion,
tambien en overrides por simbolo; otros valores personalizados se conservan.

El cierre se bloquea por simbolo antes del envio. Una orden aceptada o
`pending_cancel` sigue en vuelo; los errores de consulta no confirman cierre.
Una cancelacion/rechazo terminal permite otro intento si queda posicion.
SL/TP usan distancias ancladas a la entrada y el sizing usa esa misma
distancia de SL. Los porcentajes configurados son pisos frente a ATR diminuto;
la cantidad cripto se trunca, las entradas de acciones usan lotes enteros
y los cierres preservan hasta nueve decimales en acciones.

Pruebas offline, sin credenciales ni ordenes al broker:

```bash
python -m unittest discover -v
```

## Arranque rápido

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

```bash
python main.py --validate
python main.py --once
python main.py --backtest --years 6
python main.py
```

`--backtest` descarga 6 años de velas OHLC, entrena 3 años y valida/optimiza los 3 siguientes. Los parámetros quedan en `data/strategy_params.json` y los usa el loop en vivo.

`DRY_RUN=true` registra órdenes sin enviarlas. Pon `DRY_RUN=false` para ejecutar en paper.

Telegram: configura `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` en `.env`. El bot avisa cada compra/venta con precio y P&L. El botón Activar/Detener del panel pausa el loop y cancela órdenes pendientes.

## Panel web

```bash
cd web
npm install
npm start
```

Abre `http://127.0.0.1:3000`. Configura `SESSION_SECRET`, `ADMIN_USER` y `ADMIN_PASS` en `.env`. El dashboard está fuera de `public/` y exige sesión.

El panel lista compras/ventas guardadas en `data/trades.db`, el balance paper y el P&L acumulado, y permite activar o detener el bot. Detener cancela órdenes abiertas en Alpaca. Las claves de Alpaca solo viven en `.env` (`APCA_API_*`). El bot rechaza cualquier URL que no sea `paper-api.alpaca.markets`.

## Estructura

```
bot-trading/
├── main.py
├── bot/
│   ├── alpaca/          # cliente, tape en vivo, órdenes
│   ├── strategy/        # SMA + filtro de flujo de precios
│   ├── risk/            # tamaño, SL/TP dinámicos (ATR)
│   ├── reporting/       # P&L VERDE/ROJO
│   ├── backtest/        # simulación 5-6 años y métricas
│   └── security/        # validación, rate limit, auditoría
└── logs/                # bot.log + security_audit.log
```
