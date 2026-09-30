# Crypto Night — perfil aggressive + TP asimétrico

## 1. Filtros (`CRYPTO_NIGHT_FILTER_PROFILE=aggressive`)

| Candado | Strict | Relaxed (legacy) | **Aggressive (nuevo default)** |
|---------|--------|------------------|--------------------------------|
| ATR% 1H | p30–p70 | p20–p80 | **p10–p90** |
| Vol setup V1/V2 | ≥1.5× | ≥1.2× | **≥1.1×** |
| Score calidad | ≥4/5 | ≥3/5 | **≥2/5** |
| R teórico neto | ≥1.8 | ≥1.5 | **≥1.2** |
| Spread máx | 0.05% | 0.10% | **0.15%** |
| Delay post cierre US | 0 | 90 min | **0 min** |
| V2 fallback | no | sí | **sí** |
| Barras scan V1 | 3 | 10 | **14** |

Calidad: el punto **volumen** usa el mismo umbral que el setup (`min_volume_ratio`), no 1.5× fijo.

## 2. Límites de operaciones

| Control | Default nuevo |
|---------|----------------|
| `CRYPTO_NIGHT_RISK_LIMITS` | **false** (sin kill −1%/−3%, sin profit lock auto) |
| `CRYPTO_NIGHT_MAX_TRADES_PER_NIGHT` | **0** = ilimitado |
| `MAX_POSITIONS` | **2** (BTC + ETH en paralelo si hay setup) |
| Cola | Ya no bloquea todo el bot por 1 pending: solo si `activas >= MAX_POSITIONS` o mismo símbolo en cola |

Kill manual: `data-crypto-night/crypto_night_kill_switch.json`.

## 3. Take Profit asimétrico

Variables:

```env
CRYPTO_NIGHT_TP_REWARD_RISK=2.5   # 1:2.5 (usa 3.0 para 1:3)
CRYPTO_NIGHT_SCALE_AT_1R=false    # sin parcial @1R; TP único alto
```

**Cálculo (LONG):**

- `risk = entry_fill − stop`
- `take_profit = entry_fill + risk × CRYPTO_NIGHT_TP_REWARD_RISK`

**SHORT:** `take_profit = entry − risk × RR`.

**Ejecución:**

1. Orden **limit** de entrada (GTC).
2. Tras fill: **stop GTC** en broker + **limit TP GTC** en broker.
3. Software: `evaluate_exit` cierra si precio toca TP (`take_profit`) aunque falle el limit broker.
4. Riesgo por trade: **0.75% equity** / distancia al stop (`risk_normal_pct`).

## 4. Barrido local

```bash
python -u scripts/sweep_crypto_night_aggressive.py
# logs/crypto_night_aggressive_sweep.csv
```

Compara **strict / relaxed / aggressive** (rechazos vs trades). En VPS con `.env.crypto_night` usa barras BTC reales.

## 5. `.env.crypto_night` recomendado

Ver `deploy/crypto_night.env.example`.

```bash
pm2 restart crypto-night --update-env
pm2 logs crypto-night --lines 30 --nostream
```

Log esperado: `perfil=aggressive`, `TP … 2.5R`, `sin tope trades/kill auto`.
