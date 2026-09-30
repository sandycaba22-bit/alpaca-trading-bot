# Take profit ATR — acciones vol_2x (élite + Top 50)

Misma estrategia de entrada; salidas asimétricas con **TP por ATR** para que la ganancia objetivo sea **varias veces el riesgo** (stop 1.2× ATR).

## Parámetros recomendados (post-sweep)

| Variable | Valor | Significado |
|----------|-------|-------------|
| `STOCK_ASYMMETRIC_SL_ATR_MULT` | **1.2** | Riesgo (stop) |
| `STOCK_ASYMMETRIC_TP_ATR_MULT` | **4.2** | TP ≈ **3.5R** (4.2 ÷ 1.2) |
| `STOCK_ASYMMETRIC_TRAIL_ATR_MULT` | **2.75** | Trailing si no llega TP |
| `STOCK_ASYMMETRIC_TP_MAX_PCT` | **0.14** | Tope 14% por trade |
| `STOCK_ASYMMETRIC_TP_MIN_PCT` | **0.008** | Piso 0.8% (costes) |

Poner **`STOCK_ASYMMETRIC_TP_ATR_MULT=0`** → comportamiento anterior (solo trail, sin TP).

## Sweep en VPS (cache ya caliente)

```bash
cd ~/alpaca-trading-bot
source .venv/bin/activate
ENV_FILE=.env.stocks python -u scripts/sweep_stock_tp_atr_vol2x.py
# logs/stock_tp_atr_sweep.csv
```

## Aplicar en live/paper

En **`.env.stocks`** y **`.env.stocks_top50`** (mismas líneas TP), luego:

```bash
pm2 restart trading-bot-stocks trading-bot-stocks-top50 --update-env
```

Log arranque debe mostrar: `TP=4.20x ATR (max 14.0%, ~3.5R vs SL)`.

**Crypto Night** no usa estos flags (motor aparte).
