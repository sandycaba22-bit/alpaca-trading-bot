# Bot de trading — Alpaca Paper

Bot modular en Python integrado con la API de Alpaca Markets (solo paper trading).

Incluye cruce SMA, P&L verde/rojo por operación, stop/take-profit dinámicos, filtro de flujo de precios en vivo y backtest histórico de 5–6 años.

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

`DRY_RUN=true` registra órdenes sin enviarlas. Pon `DRY_RUN=false` para ejecutar en paper.

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
