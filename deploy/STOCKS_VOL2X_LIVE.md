# Despliegue live acciones — vol_2x + SMA 50 (sweep 335031)

Solo perfil **stocks** (`BOT_PROFILE=stocks`, PM2 `trading-bot-stocks`, panel **:3000**).  
**No modifica** `.env.crypto` ni el guardrail `BOT_PROFILE=crypto` → paper.

## Parámetros (igual que backtest)

| Parámetro | Valor |
|-----------|--------|
| Entrada | Multi-régimen 5Min + confirmación 15Min |
| SMA lenta | 50 |
| Filtro | `STOCK_ENTRY_VOL_MULT=2.0` (vela señal ≥ 2× media 20) |
| Salidas | `STOCK_ASYMMETRIC_*` SL 1.2× ATR, trail 2.75× ATR, sin TP % |
| Motivo Telegram | `vol_2x` |

Requiere **`SYNC_ENTRY_ENABLED=false`** en `.env.stocks` (sync_entry es otro pipeline).

## Rollout en 2 fases

### Fase 1 — 4 símbolos nuevos parcial + base

En `.env.stocks`:

```env
SYMBOLS=AAPL,MSFT,SLV,TSLA
```

1. `git pull` en VPS  
2. Copiar/merge desde `deploy/stocks.env.example` (no ejecutar `generate_split_env.py` si ya tienes keys live editadas).  
3. `pm2 restart trading-bot-stocks trading-web-stocks --update-env`  
4. Verificar log arranque: líneas `Acciones entrada vol_2x` y `Acciones asimétrico | LIVE ON`.  
5. Compras: Telegram **Motivo: vol_2x**, SL/trail ATR sin TP fijo %.

### Fase 2 — 7 símbolos

Tras 1–2 sesiones OK en fase 1:

```env
SYMBOLS=AAPL,MSFT,SLV,TSLA,NVDA,GOOGL,META
```

Restart solo procesos stocks (mismo comando PM2).

## Sizing (~$300)

Referencia en `deploy/stocks.env.example`:

- `RISK_PERCENT_PER_TRADE=0.01` (~$3 riesgo/trade)  
- `MAX_NOTIONAL_PER_ORDER=45` (~300/7)  
- `MAX_OPEN_POSITIONS=3` (evita sobre-exposición simultánea)  

Alpaca redondea acciones a enteros salvo fraccional habilitado; SLV/TSLA baratos vs NVDA/META — el cap por notional limita el tamaño.

## Evidencia sweep (OOS)

`logs/vol2x_universe_sweep.csv` — corrida 335031. Candidatos OOS: SLV, TSLA, NVDA, GOOGL, META (+ AAPL, MSFT, XOM; XOM no incluido en este rollout).
