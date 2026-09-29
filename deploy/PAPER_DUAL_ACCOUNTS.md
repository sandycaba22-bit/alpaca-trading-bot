# Paper dual: élite + Top 50 sin choques

Objetivo: **probar en paper** con los dos bots en paralelo y, cuando el resultado sea estable, pasar **solo la élite** a live con otra key — sin mezclar cuentas.

## Regla de oro

| Bot | Archivo env | Cuenta Alpaca | PM2 |
|-----|-------------|---------------|-----|
| Élite (6 símbolos vol_2x) | `.env.stocks` | **Paper A** (PK… propia) | `trading-bot-stocks` |
| Top 50 | `.env.stocks_top50` | **Paper B** (PK… distinta) | `trading-bot-stocks-top50` |

**Nunca** uses la misma `APCA_API_KEY_ID` en los dos `.env` si ambos van a operar a la vez.

Con dos papers separados:

- Equity, buying power y posiciones **no se pisan**.
- Cada bot respeta su `MAX_OPEN_POSITIONS` solo sobre **su** cuenta.
- Puedes tener `MARKET_STREAM_ENABLED=true` en élite y REST en Top 50 **sin** pelear por un solo websocket de la misma cuenta.
- TSLA/META/NVDA pueden operarse en **ambos** bots a la vez (cuentas distintas) — no hay doble exposición en un solo balance.

## Crear la segunda paper en Alpaca

1. Entra en [Alpaca](https://app.alpaca.markets/) → **Paper Trading**.
2. Si tu plan lo permite, abre una **segunda cuenta paper** (o un segundo usuario/equipo con paper propia). Anota el **Account ID** de cada una.
3. Genera **API keys distintas** para Paper A (élite) y Paper B (Top 50). Guarda secretos solo en el VPS, no en git.

Asignación sugerida:

- **Paper A (élite):** ~$100k virtual — mismo sizing que ya documentamos (`MAX_NOTIONAL_PER_ORDER=12000`, riesgo 1%).
- **Paper B (Top 50):** ~$100k virtual — riesgo 0,5%, `API_DATA_PER_MINUTE=180`.

Alpaca permite reset de paper; si quieres empezar limpio mañana, resetea **antes** de la apertura US y anota el equity inicial en un cuaderno/Telegram.

## Configurar el VPS

```bash
cd ~/alpaca-trading-bot
nano .env.stocks          # Paper A: APCA_* + paper-api.alpaca.markets
nano .env.stocks_top50    # Paper B: otras APCA_* + paper-api.alpaca.markets
```

Comprueba que las keys **no** son iguales:

```bash
python scripts/verify_dual_stock_separation.py
```

Debe terminar con `OK: cuentas Alpaca distintas` (exit 0). Si dice `CHOQUE`, corrige keys antes de `pm2 restart`.

Actualiza bots:

```bash
bash deploy/update-vps.sh
```

## Qué sigue separado (sin tocar)

- `data-stocks/` vs `data-stocks-top50/` (pausa, estado, confirm live)
- Paneles `:3000` y `:3001`
- Telegram `[ACCIONES]` vs `[TOP50]`
- Crypto Night (`.env.crypto_night`) — **tercera** key paper o la misma que Top 50 **solo si** no opera acciones en la misma cuenta a la vez; lo más limpio: cuenta paper C o pausar crypto en horario acciones.

## Camino a live (cuando sea “seguro”)

Criterios razonables (tú decides el umbral):

- Varias semanas paper con ejecución real (fills), no solo señales.
- Drawdown y freno diario (-2,5%) casi nunca activado, o activado y comportamiento correcto.
- Entiendes Telegram + SL/trail en posiciones abiertas.

Pasos:

1. Crea cuenta **live** real (~$300 o lo que uses).
2. **Solo** cambia `.env.stocks`: `APCA_*` live + `https://api.alpaca.markets` + sizing live (`MAX_NOTIONAL_PER_ORDER=45`, etc. en `deploy/stocks.env.example`).
3. Deja Top 50 en **Paper B** hasta que también quieras live (no recomendado al inicio con $300).
4. `data-stocks/live_confirm.txt` según doc live.
5. `pm2 restart trading-bot-stocks --update-env` — **no** mezcles keys live en `.env.stocks_top50`.

## Si solo tienes **una** paper (plan B temporal)

No es lo ideal. Opciones:

1. **Un bot activo:** Top 50 **o** élite; el otro `paused: true` o PM2 stopped.
2. **Misma paper, sin solapamiento:** élite solo `SLV,SPY` + símbolos fuera del Top 50 (pierdes OOS en TSLA/META en élite).
3. Arreglar pronto la **segunda paper** (recomendado).

Verificación:

```bash
bash deploy/pre-open-stocks.sh
python scripts/verify_dual_stock_separation.py
```
