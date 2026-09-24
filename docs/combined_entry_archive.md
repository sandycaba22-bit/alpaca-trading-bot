# combined_entry — archivo de research (no producción)

## Qué es

**combined_entry** es una propuesta de **entrada** evaluada solo en backtest (scripts bajo `scripts/`), que combina:

1. **vol_2x** — volumen de la vela de señal ≥ 2× la media reciente (20 velas).
2. **ADX** — régimen con tendencia (umbral ≥ 20 en la variante completa).
3. **Pullback a EMA** — entrada en retroceso a la EMA de referencia dentro de tendencia, no en ruptura directa.
4. **Volumen en el pullback** — confirmación adicional (mult 1.2× en `combined_full`).
5. **Exclusión de ruido de apertura** — primeros 30 min post 9:30 NY (acciones); primeros 60 min post 00:00 UTC (cripto, variante completa).

Marco de timeframes usado en el backtest:

- **Acciones:** 5Min con confirmación 15Min; salidas asimétricas existentes (SL 1.2× ATR, trailing 2.75×, sin TP fijo %).
- **Cripto:** 1H con tendencia 4H; salidas del backtest asimétrico existente (sin cambiar la lógica de salida del bot live).

**No incluye** lógica de velas 3/6/9 minutos (eliminada de forma permanente).

### Código (research, intacto)

| Archivo | Uso |
|---------|-----|
| `scripts/_combined_entry_backtest_lib.py` | Perfiles `vol2x_pullback`, `combined_full`; precompute acciones/cripto |
| `scripts/_backtest_combined_entry.py` | Runner IS/OOS, CSV y resumen en `logs/` |

Variantes comparadas en backtest:

- `vol2x_pullback` — pullback + vol 2× en señal (sin ADX / skip apertura / vol extra en pullback).
- `combined_full` — los cinco filtros anteriores.
- `sweep_vol2x_legacy` — referencia acciones: multi-régimen + vol 2× (baseline del sweep de entradas).
- `breakout_asim_legacy` — referencia cripto: entrada asimétrica tipo breakout actual en backtest.

---

## Por qué se archivó (decisión operativa)

Tras la corrida **335026** (~2,7 h, backtest completo con baselines legacy), **no se implementará combined_entry en producción**. La entrada activa deseada sigue siendo **vol_2x multi-régimen** (baseline del sweep) en acciones; en cripto se mantiene la referencia **breakout_asim_legacy** frente al pullback combined.

### Resultados OOS destacados (corrida 335026)

**Acciones vs `sweep_vol2x_legacy`:**

| Símbolo | Variante | Trades OOS | PF | Net % OOS |
|---------|----------|------------|-----|-----------|
| AAPL | `combined_full` | 205 | 0.79 | −0.13 |
| AAPL | `sweep_vol2x_legacy` | 275 | **1.29** | **+0.22** |
| MSFT | `combined_full` | 197 | ~0.99 | ~0.00 |
| MSFT | `sweep_vol2x_legacy` | 266 | ~1.00 | ~0.00 |

**Cripto vs `breakout_asim_legacy` (OOS):**

| Símbolo | `combined_full` net % | `breakout_asim_legacy` net % |
|---------|------------------------|------------------------------|
| BTC/USD | −2.33 | −1.11 |
| ETH/USD | −1.78 | −0.65 |

En acciones, `combined_full` mejoró IS en AAPL respecto al pullback solo, pero **empeoró OOS** frente al vol_2x legacy. En cripto, el pullback combined **no superó** el baseline asimétrico de breakout.

---

## Evidencia y trazabilidad

Números y variantes completas:

- `logs/combined_entry_backtest.csv` — filas por activo, scope (IS/OOS), símbolo y variante.
- `logs/combined_entry_backtest_summary.txt` — resumen legible (incluye legacy tras corrida 335026).

Regenerar backtest (solo research, no afecta live):

```bash
.venv/bin/python -u scripts/_backtest_combined_entry.py
# Sin baselines lentos:
.venv/bin/python -u scripts/_backtest_combined_entry.py --skip-legacy
```

---

## Cuándo reconsiderar (plan B)

Reabrir combined_entry **solo si**, en un backtest de salud periódico o en métricas sostenidas de paper/live:

- **`sweep_vol2x_legacy` (vol_2x multi-régimen) en acciones** muestra **PF &lt; 1 de forma sostenida** en OOS o en ventanas recientes equivalentes, **y**
- Tras revisar causas (régimen, costos, símbolos), tiene sentido probar de nuevo `combined_full` o `vol2x_pullback` **antes de descartar** este enfoque.

Hasta entonces: **no cablear** `combined_entry` en `bot/`, PM2 ni `.env`.

---

## Relación con producción (estado en repo)

- **combined_entry no está conectado al bot live:** no hay `COMBINED_ENTRY_*`, flags en `bot/config.py`, `deploy/` ni `ecosystem.*.config.js`. Solo existen los scripts `scripts/_combined_entry*.py`.
- **Salidas (SL/trailing), perfiles stocks/crypto (`BOT_PROFILE`, `DATA_DIR`) y PM2** no forman parte de este experimento; no deben cambiarse al archivar combined_entry.
- La **entrada en runtime** la gobierna el código ya desplegado (p. ej. `SYNC_ENTRY_ENABLED` / orquestador multi-régimen en `bot/scheduler/multi_tf.py`), **no** combined_entry. Archivar combined_entry **no modifica** ese comportamiento.

*Documento creado: 2026-09-24. Decisión: mantener vol_2x legacy como referencia ganadora; combined_entry permanece en scripts para research futuro.*
