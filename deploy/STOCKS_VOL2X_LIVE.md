# Despliegue live acciones — vol_2x + SMA 50 (sweep 335031)

Solo perfil **stocks** (`BOT_PROFILE=stocks`, PM2 `trading-bot-stocks`, panel **:3000**).  
Bot secundario Top 50: `.env.stocks_top50` + PM2 `:3001` (paper). Sin turno horario: quitar `STOCK_ENTRY_WINDOW_ET`. Solape élite: `deploy/PAPER_DUAL_ACCOUNTS.md`.

## Parámetros (igual que backtest)

| Parámetro | Valor |
|-----------|--------|
| Entrada | Multi-régimen 5Min + confirmación 15Min |
| SMA lenta | 50 |
| Filtro | `STOCK_ENTRY_VOL_MULT=2.0` (vela señal ≥ 2× media 20) |
| Salidas | SL 1.2× ATR, trail 2.75× ATR, **TP 4.2× ATR** (~3.5R) — ver `deploy/STOCK_TP_ATR.md` |
| Motivo Telegram | `vol_2x` |

Requiere **`SYNC_ENTRY_ENABLED=false`** en `.env.stocks` (sync_entry es otro pipeline).

## Rollout en 2 fases

### Fase 1 — 4 símbolos nuevos parcial + base

En `.env.stocks`:

```env
SYMBOLS=SLV,TSLA,GOOGL,META,NVDA,SPY
```

1. `git pull` en VPS  
2. Copiar/merge desde `deploy/stocks.env.example` (no ejecutar `generate_split_env.py` si ya tienes keys live editadas).  
3. `pm2 restart trading-bot-stocks trading-web-stocks --update-env`  
4. Verificar log arranque: líneas `Acciones entrada vol_2x` y `Acciones asimétrico | LIVE ON`.  
5. Compras: Telegram **Motivo: vol_2x**, SL/trail + TP ATR si `STOCK_ASYMMETRIC_TP_ATR_MULT>0`.

### Universo élite (actual)

```env
SYMBOLS=SLV,TSLA,GOOGL,META,NVDA,SPY
MAX_SYMBOLS=8
```

Restart: `pm2 restart trading-bot-stocks trading-web-stocks --update-env`

## Sizing

**Live ~$300** (`deploy/stocks.env.example`): `RISK_PERCENT_PER_TRADE=0.01`, `MAX_NOTIONAL_PER_ORDER=45`, `MAX_OPEN_POSITIONS=3`.

**Paper / VPS ~$100k** (élite + Top 50 misma cuenta paper): `POSITION_SIZE_PCT=0.04`, `MAX_NOTIONAL_PER_ORDER=12000`. Top 50: `RISK_PERCENT_PER_TRADE=0.005`. Si el `.env` aún tiene `MAX_NOTIONAL_PER_ORDER=40`, el bot en paper **sube automáticamente** el tope al arrancar (ver `bot/risk/stock_sizing.py`).

Sizing: `USE_FIXED_RISK_SIZING=true`. Sin frenos de entrada (`DAILY_LOSS_LIMIT_PERCENT=0`, `CAPITAL_PROTECTION_ENABLED=false`, `STOCK_TRADE_BEST_ONLY=false`) dentro de `STOCK_ENTRY_WINDOW_ET`. TP ATR + trail: `deploy/STOCK_TP_ATR.md`. Top 50: `API_DATA_PER_MINUTE=180`.

Tras `git pull`: `bash deploy/update-vps.sh` (valida env + reinicia bots acciones).

## Evidencia sweep (OOS)

`logs/vol2x_universe_sweep.csv` — corrida 335031. Candidatos OOS: SLV, TSLA, NVDA, GOOGL, META (+ AAPL, MSFT, XOM; XOM no incluido en este rollout).
