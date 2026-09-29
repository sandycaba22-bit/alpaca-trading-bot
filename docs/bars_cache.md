# Cache de velas (6 años, local / VPS)

Histórico OHLC para **backtests y scripts** en `data/bars_cache/`. Los procesos PM2 **no** leen esta carpeta en vivo; siguen pidiendo velas recientes a Alpaca.

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

## PC local (Windows)

```powershell
cd C:\Users\sandy\bot-trading
.venv\Scripts\activate
python scripts\warm_bars_cache.py --preset crypto_night
python scripts\warm_bars_cache.py --preset stocks_elite
$env:ENV_FILE=".env.stocks_top50"; python scripts\warm_bars_cache.py --preset stocks_top50
```

No arranca el bot live; solo descarga datos con las API keys del `.env` indicado.

## VPS

Tras `git pull` (el script sí está en el repo):

```bash
cd ~/alpaca-trading-bot && source .venv/bin/activate
python scripts/warm_bars_cache.py --preset crypto_night
ENV_FILE=.env.stocks python scripts/warm_bars_cache.py --preset stocks_elite
ENV_FILE=.env.stocks_top50 python scripts/warm_bars_cache.py --preset stocks_top50
```

Alternativa: copiar la carpeta `data/bars_cache/` desde el PC (WinSCP/rsync). **No** hacer commit de los `.pkl` (están en `.gitignore`).

## Opciones útiles

- `--years 6` (default)
- `--force` — ignorar cache y volver a bajar
- `--symbols AAPL,MSFT --timeframes 5Min,15Min` — sin preset

Scripts de research que ya usan `data/bars_cache/` (p. ej. sweeps trailing) reutilizan los mismos nombres de archivo.
