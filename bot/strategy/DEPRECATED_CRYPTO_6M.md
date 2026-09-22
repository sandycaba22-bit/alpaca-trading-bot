# DEPRECATED — pipeline cripto 6m / 15m (NO reutilizar)

A partir de la migración **asimétrica 1H + confirmación 4H**, queda **descartado para siempre**:

- Capa `6m` con `signal_6m` y confirmación `15Min` / `30Min` en el perfil cripto.
- Take profit fijo de cripto (`CRYPTO_MIN_TP_PCT`, p. ej. +3%) como objetivo de salida.
- Perfil “agresivo” basado en multi-régimen + `eval 6m` (`CRYPTO_REGIME_AGGRESSIVE_ENABLED`).

**No reactivar** `CRYPTO_LEGACY_MTF_ENABLED` salvo rollback explícito documentado en git.

**Se mantiene sin cambios:** acciones (3-6-9), SMA/ADX/ATR de acciones, ejecución inteligente (spread, reintentos, rate limits, dust).

**Producción / paper cripto:** solo con `CRYPTO_ASYMMETRIC_LIVE_ENABLED=true` tras backtest IS+OOS positivos.
