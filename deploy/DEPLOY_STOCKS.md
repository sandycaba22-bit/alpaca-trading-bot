# Despliegue acciones (dual)

| Proceso | Env | PM2 | Puerto | Perfil |
|---------|-----|-----|--------|--------|
| Élite live vol_2x | `.env.stocks` | `trading-bot-stocks` | 3000 | `stocks` |
| Top 50 paper vol_2x | `.env.stocks_top50` | `trading-bot-stocks-top50` | 3001 | `stocks_top50` |

Plantillas: `deploy/stocks.env.example`, `deploy/stocks_top50.env.example`.

## Primera vez en VPS

```bash
cd ~/alpaca-trading-bot
git pull origin main
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
bash deploy/setup-stocks-dual.sh
```

Edita keys en `.env.stocks` (live) y `.env.stocks_top50` (paper) antes de operar.

## Actualización rutinaria

```bash
cd ~/alpaca-trading-bot && bash deploy/update-vps.sh
```

## PM2 manual

```bash
pm2 start ecosystem.production.config.js   # ambos
pm2 start ecosystem.stocks.config.js       # solo élite
pm2 start ecosystem.stocks_top50.config.js # solo Top 50
```

## Research sweep Top 50

```bash
source .venv/bin/activate
python -u scripts/_sweep_vol2x_top50.py
# logs/vol2x_top50_sweep.csv
```
