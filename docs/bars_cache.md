# Cache de velas (6 años, local / VPS)

Histórico OHLC para **backtests y scripts** en `data/bars_cache/`. Los procesos PM2 **no** leen esta carpeta en vivo; siguen pidiendo velas recientes a Alpaca.

## Recomendado: sin API keys en el PC Windows

Las keys **solo en el VPS** (`.env.crypto_night`, `.env.stocks`, `.env.stocks_top50`). En tu PC no hace falta copiar `APCA_*` ni correr el warm.

1. En el VPS (SSH): `git pull` + `bash deploy/warm-bars-cache.sh` (o un preset suelto).
2. Los `.pkl` quedan en `~/alpaca-trading-bot/data/bars_cache/`.
3. Sweeps y backtests que corras **en el VPS** usan esa carpeta.
4. **Opcional:** si algún día quieres gráficos/backtest en Windows, copia solo `data/bars_cache/` con WinSCP/rsync (sin keys).

Los bots PM2 **no necesitan** esta cache para operar.

## Formato

| Archivo | Ejemplo |
|---------|---------|
| Pickle pandas | `AAPL_5Min.pkl`, `BTC-USD_1Hour.pkl` |
| Índice | `DatetimeIndex` UTC |
| Columnas | `open`, `high`, `low`, `close`, `volume` |
| Manifiesto | `data/bars_cache/manifest.json` (fechas y conteos) |

Override de ruta: `BARS_CACHE_DIR=/ruta/custom`.

## Presets (por bot)

| Preset | Bot / uso | Símbolos | Timeframes |
|--------|-----------|----------|------------|
| `crypto_night` | PM2 crypto-night | BTC/USD, ETH/USD | 15Min, 1Hour, 4Hour, 1Day |
| `stocks_elite` | Élite vol_2x | `ELITE_STOCK_SYMBOLS` | 5Min, 15Min, 1Hour, 4Hour, 1Day |
| `stocks_top50` | Top 50 vol_2x | 50 large caps | 5Min, 15Min, 1Hour |
| `all` | Unión de los tres | — | — |

## VPS (keys ya configuradas)

Tras `git pull`:

```bash
cd ~/alpaca-trading-bot
git pull origin main
bash deploy/warm-bars-cache.sh              # crypto + élite + top50
bash deploy/warm-bars-cache.sh crypto_night # solo BTC/ETH (rápido)
bash deploy/warm-bars-cache.sh stocks_top50 # largo (~50 tickers)
```

Equivalente manual:

```bash
ENV_FILE=.env.crypto_night python scripts/warm_bars_cache.py --preset crypto_night
ENV_FILE=.env.stocks python scripts/warm_bars_cache.py --preset stocks_elite
ENV_FILE=.env.stocks_top50 python scripts/warm_bars_cache.py --preset stocks_top50
```

**No** hacer commit de los `.pkl` (`.gitignore`).

## PC local (Windows, solo si tienes APCA_* en .env)

```powershell
cd C:\Users\sandy\bot-trading
.venv\Scripts\activate
python scripts\warm_bars_cache.py --preset crypto_night
```

Si no quieres keys en el PC, usa el flujo VPS de arriba.

## Opciones útiles

- `--years 6` (default)
- `--force` — ignorar cache y volver a bajar
- `--symbols AAPL,MSFT --timeframes 5Min,15Min` — sin preset

Scripts de research que ya usan `data/bars_cache/` (p. ej. sweeps trailing) reutilizan los mismos nombres de archivo.
