# Bot de trading — Alpaca Paper

Bot modular en Python integrado con la API de Alpaca Markets (solo paper trading).

Incluye cruce SMA, P&L verde/rojo por operación, stop/take-profit dinámicos, filtro de flujo de precios en vivo, walk-forward 3+3 años y avisos de Telegram.

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
