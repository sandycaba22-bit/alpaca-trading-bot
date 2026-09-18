"""Planificador Tesla 3-6-9 y persistencia de estado para el panel web."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from bot.config import PROJECT_ROOT
from bot.market.assets import all_symbols, is_crypto_symbol, normalize_symbol, positions_by_symbol
from bot.market.mode import TradingMode, is_symbol_tradable, resolve_trading_mode, trading_mode_label
from bot.security.exceptions import RateLimitError
from bot.strategy.base import Signal
from bot.strategy.multi_tf_analysis import (
    MacroSnapshot,
    SpikeSnapshot,
    analyze_macro,
    analyze_signal,
    analyze_structure,
    analyze_trend,
    detect_spike,
    macro_allows,
)

if TYPE_CHECKING:
    from bot.engine import TradingEngine

logger = logging.getLogger(__name__)

STATE_PATH = PROJECT_ROOT / "data" / "scheduler_state.json"

LAYER_NAMES = ("3m", "6m", "9m")
CRYPTO_SYMBOL_QUERY_DELAY_SECONDS = 1.0

_TF_SECONDS = {
    "1Min": 60,
    "3Min": 180,
    "5Min": 300,
    "6Min": 360,
    "9Min": 540,
    "15Min": 900,
    "30Min": 1800,
    "1Hour": 3600,
    "1Day": 86400,
}


def _timeframe_seconds(label: str) -> int:
    return int(_TF_SECONDS.get(str(label), 900))


@dataclass
class LayerScheduler:
    """Dispara cada capa según su intervalo (segundos)."""

    tick_seconds: int
    intervals: dict[str, int]
    last_run: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings) -> LayerScheduler:
        return cls(
            tick_seconds=settings.scheduler_tick_seconds,
            intervals={
                "3m": settings.tf_3m_seconds,
                "6m": settings.tf_6m_seconds,
                "9m": settings.tf_9m_seconds,
            },
        )

    def due(self, layer: str, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        interval = self.intervals[layer]
        last = self.last_run.get(layer, 0.0)
        if last == 0.0 or now - last >= interval:
            self.last_run[layer] = now
            return True
        return False

    def seconds_until(self, layer: str, now: float | None = None) -> int:
        now = now if now is not None else time.monotonic()
        last = self.last_run.get(layer, 0.0)
        if last == 0.0:
            return 0
        elapsed = now - last
        return max(0, int(self.intervals[layer] - elapsed))


@dataclass
class SymbolLayerCache:
    spike: SpikeSnapshot | None = None
    signal: Signal = Signal.HOLD
    signal_detail: str = ""
    structure: str = "sideways"
    structure_return_pct: float = 0.0
    trend: str = "sideways"
    trend_return_pct: float = 0.0
    macro: MacroSnapshot | None = None


class SchedulerStateStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or STATE_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        scheduler: LayerScheduler,
        symbols: dict[str, SymbolLayerCache],
        telegram_enabled: bool,
        *,
        trading_mode: str = "",
        trading_mode_label: str = "",
        active_symbols: list[str] | None = None,
    ) -> None:
        now = time.monotonic()
        payload = {
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "schedule": "tesla-3-6-9",
            "trading_mode": trading_mode,
            "trading_mode_label": trading_mode_label,
            "active_symbols": active_symbols or [],
            "tick_seconds": scheduler.tick_seconds,
            "telegram_enabled": telegram_enabled,
            "layers": {
                name: {
                    "interval_seconds": scheduler.intervals[name],
                    "next_in_seconds": scheduler.seconds_until(name, now),
                }
                for name in LAYER_NAMES
            },
            "symbols": {
                sym: {
                    "spike": sym_cache.spike.detected if sym_cache.spike else False,
                    "spike_move_pct": sym_cache.spike.move_pct if sym_cache.spike else 0.0,
                    "spike_reason": sym_cache.spike.reason if sym_cache.spike else "",
                    "signal": sym_cache.signal.value,
                    "signal_detail": sym_cache.signal_detail,
                    "structure": sym_cache.structure,
                    "structure_return_pct": round(sym_cache.structure_return_pct, 2),
                    "trend": sym_cache.trend,
                    "trend_return_pct": round(sym_cache.trend_return_pct, 2),
                    "macro": sym_cache.macro.regime if sym_cache.macro else "sideways",
                    "macro_return_pct": round(sym_cache.macro.return_pct, 2) if sym_cache.macro else 0.0,
                    "macro_blocks_buy": sym_cache.macro.blocks_buy if sym_cache.macro else False,
                    "macro_blocks_sell": sym_cache.macro.blocks_sell if sym_cache.macro else False,
                }
                for sym, sym_cache in symbols.items()
            },
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @staticmethod
    def read_public() -> dict:
        if not STATE_PATH.exists():
            return {}
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}


class MultiTimeframeEngine:
    """Orquesta las capas Tesla 3-6-9 sobre el motor de trading."""

    def __init__(self, engine: TradingEngine) -> None:
        self.engine = engine
        self.scheduler = LayerScheduler.from_settings(engine.settings)
        self.store = SchedulerStateStore()
        self.cache: dict[str, SymbolLayerCache] = {
            sym: SymbolLayerCache() for sym in all_symbols(engine.settings)
        }
        self.trading_mode: TradingMode | None = None
        self.active_symbols: list[str] = list(engine.settings.stock_symbols)
        self.trading_mode_label: str = trading_mode_label(TradingMode.STOCKS)

    def bootstrap(self) -> None:
        clock = self.engine.client.get_market_clock()
        self._apply_trading_mode(clock)
        self._sync_intervals_for_mode()
        now = time.monotonic()
        for layer in LAYER_NAMES:
            self.scheduler.last_run[layer] = now - self.scheduler.intervals[layer]
        self._persist()

    def run_tick(self) -> None:
        engine = self.engine
        if engine.control.is_paused():
            engine._apply_pause()
            engine.stop()
            return
        engine._pause_applied = False

        clock = engine.client.get_market_clock()
        self._apply_trading_mode(clock)
        self._sync_intervals_for_mode()

        now = time.monotonic()
        run_3m = self.scheduler.due("3m", now)
        run_6m = self.scheduler.due("6m", now)
        run_9m = self.scheduler.due("9m", now)

        if run_9m:
            self._layer_9m()
        if run_3m:
            self._layer_3m()
        if run_6m:
            self._layer_6m_execute()
        elif run_3m:
            stream = engine._stream
            if stream is None or stream.health.in_fallback():
                engine._mark_all_positions()

        self._persist()

    def _layer_3m(self) -> None:
        settings = self.engine.settings
        symbols = list(self.active_symbols)
        for index, symbol in enumerate(symbols):
            try:
                spike_tf = self._spike_timeframe(symbol)
                lookback = 2 if is_crypto_symbol(symbol) else 3
                bars = self.engine.market_data.get_bars(symbol, spike_tf, 30)
                tape = self.engine.live_tape(
                    symbol,
                    fallback_price=float(bars["close"].iloc[-1]) if not bars.empty else None,
                )
                last_price = tape.last_price if tape else (
                    float(bars["close"].iloc[-1]) if not bars.empty else 0.0
                )
                last_price = self.engine.latest_price(symbol, last_price)
                spike = detect_spike(
                    bars,
                    last_price,
                    settings.tf_spike_threshold_pct,
                    lookback_bars=lookback,
                    window_label=f"{lookback} velas {spike_tf}",
                )
                self.cache[symbol].spike = spike
                if spike.detected:
                    logger.warning("%s | spike tf=%s | %s", symbol, spike_tf, spike.reason)
            except Exception as exc:
                logger.debug("%s | capa 3m fallo: %s", symbol, exc)
            self._sleep_between_crypto_symbols(symbols, index)

    def _layer_9m(self) -> None:
        settings = self.engine.settings
        symbols = list(self.active_symbols)
        for index, symbol in enumerate(symbols):
            try:
                regime_tf = self._regime_timeframe(symbol)
                bars = self.engine.market_data.get_bars(symbol, regime_tf, 80)
                structure, struct_ret = analyze_structure(bars)
                trend, trend_ret = analyze_trend(bars)
                macro = analyze_macro(
                    bars,
                    settings.tf_macro_strong_pct,
                    timeframe_label=regime_tf,
                )
                entry = self.cache[symbol]
                entry.structure = structure
                entry.structure_return_pct = struct_ret
                entry.trend = trend
                entry.trend_return_pct = trend_ret
                entry.macro = macro
                logger.info(
                    "%s | tf_regimen=%s | estructura=%s %.2f%% | tendencia=%s %.2f%% | %s",
                    symbol,
                    regime_tf,
                    structure,
                    struct_ret,
                    trend,
                    trend_ret,
                    macro.reason,
                )
            except Exception as exc:
                logger.debug("%s | capa 9m fallo: %s", symbol, exc)
            self._sleep_between_crypto_symbols(symbols, index)

    def _layer_6m_execute(self) -> None:
        engine = self.engine
        clock = engine.client.get_market_clock()
        account = engine.client.snapshot_account_optional()
        if account is None:
            logger.warning(
                "Capa 6m sin snapshot de cuenta — se mantienen mark-to-market y cierres, "
                "pero las nuevas compras quedan bloqueadas hasta recuperar cuenta o cache"
            )
        positions = positions_by_symbol(engine.executor.list_positions())

        mark_symbols = list(dict.fromkeys([*positions, *self.active_symbols]))
        for index, symbol in enumerate(mark_symbols):
            if engine.control.is_paused():
                return
            if symbol not in positions and not is_symbol_tradable(symbol, clock):
                continue
            try:
                engine._mark_to_market(symbol, positions, clock)
            except Exception:
                pass
            self._sleep_between_crypto_symbols(mark_symbols, index)

        positions = positions_by_symbol(engine.executor.list_positions())
        active_symbols = list(self.active_symbols)
        focus_symbol = self._pick_focus_symbol(active_symbols)
        if focus_symbol:
            label = "Cripto" if self.trading_mode is TradingMode.CRYPTO else "Acciones"
            logger.info(
                "%s foco | mejor momentum=%s | candidatos=%s",
                label,
                focus_symbol,
                ",".join(active_symbols),
            )
        for index, symbol in enumerate(active_symbols):
            if engine.control.is_paused():
                return
            if not is_symbol_tradable(symbol, clock):
                continue
            try:
                self._process_6m_symbol(
                    symbol,
                    account,
                    positions,
                    focus_symbol=focus_symbol,
                )
            except RateLimitError as exc:
                logger.warning("%s | capa 6m rate limit — se continúa con el resto | %s", symbol, exc)
            except Exception as exc:
                logger.warning("%s | capa 6m fallo: %s", symbol, exc)
            self._sleep_between_crypto_symbols(active_symbols, index)

    def _focus_best_only_enabled(self) -> bool:
        settings = self.engine.settings
        if self.trading_mode is TradingMode.CRYPTO:
            return bool(getattr(settings, "crypto_trade_best_only", True))
        if self.trading_mode is TradingMode.STOCKS:
            return bool(getattr(settings, "stock_trade_best_only", True))
        return False

    def _pick_focus_symbol(self, symbols: list[str]) -> str | None:
        """Elige el activo con mejor tendencia/retorno HTF (bull > sideways > bear)."""
        if not self._focus_best_only_enabled() or not symbols:
            return None
        if self.trading_mode is TradingMode.CRYPTO:
            candidates = [s for s in symbols if is_crypto_symbol(s)]
        else:
            candidates = [s for s in symbols if not is_crypto_symbol(s)]
        if not candidates:
            return None
        best: str | None = None
        best_score = float("-inf")
        for symbol in candidates:
            entry = self.cache.get(symbol) or SymbolLayerCache()
            score = 0.0
            trend = (entry.trend or "").lower()
            if trend == "bull":
                score += 50.0
            elif trend == "sideways":
                score += 10.0
            elif trend == "bear":
                score -= 40.0
            score += float(entry.trend_return_pct or 0.0) * 10.0
            score += float(entry.structure_return_pct or 0.0) * 5.0
            if score > best_score:
                best_score = score
                best = symbol
        return best

    def _process_6m_symbol(
        self,
        symbol: str,
        account,
        positions: dict,
        focus_symbol: str | None = None,
    ) -> None:
        engine = self.engine
        cache = self.cache[symbol]
        signal_tf = self._signal_timeframe(symbol)
        bars = engine.market_data.get_bars(symbol, signal_tf, engine.settings.lookback_bars)
        if bars.empty:
            return

        tape = engine.live_tape(
            symbol, fallback_price=float(bars["close"].iloc[-1])
        )
        last_price = tape.last_price if tape else float(bars["close"].iloc[-1])
        last_price = engine.latest_price(symbol, last_price)
        position = positions.get(normalize_symbol(symbol)) or positions.get(symbol)
        has_long = position is not None and float(position.qty) > 0

        signal, detail = analyze_signal(
            engine.strategy,
            symbol,
            bars,
            has_long,
            last_price,
            tape.spread_pct if tape else None,
            htf_trend=cache.trend,
        )
        cache.signal = signal
        cache.signal_detail = detail
        logger.info(
            "%s | eval 6m | señal=%s | detalle=%s | precio=%.4f | spread=%s | htf=%s | posicion=%s",
            symbol,
            signal.value,
            detail,
            last_price,
            f"{tape.spread_pct:.4%}" if tape and tape.spread_pct is not None else "n/a",
            cache.trend or "n/a",
            "sí" if position is not None else "no",
        )

        # Macro/regimen superior desactivado: TF_MACRO_STRONG_PCT nunca se activó (siempre sideways).
        # cache.trend / has_long / signal no dependen de macro_allows — no muta nada.
        # Para reactivar, descomentar el bloque siguiente.
        # macro = cache.macro or MacroSnapshot("sideways", 0.0, False, False, "macro pendiente")
        # allowed, macro_reason = macro_allows(signal, macro)
        # if not allowed:
        #     logger.info("%s | señal 6m %s bloqueada por macro: %s", symbol, signal.value, macro_reason)
        #     return

        if cache.trend == "bear" and signal is Signal.BUY:
            logger.info("%s | BUY bloqueado — tendencia %s bajista", symbol, self._regime_timeframe(symbol))
            return
        if cache.trend == "bull" and signal is Signal.SELL and not has_long:
            logger.info("%s | SELL bloqueado — tendencia %s alcista", symbol, self._regime_timeframe(symbol))
            return

        if (
            signal is Signal.BUY
            and focus_symbol
            and normalize_symbol(symbol) != normalize_symbol(focus_symbol)
            and not has_long
        ):
            logger.info(
                "%s | BUY omitido — foco en %s (mejor momentum)",
                symbol,
                focus_symbol,
            )
            return

        if signal is Signal.HOLD:
            logger.info("%s | HOLD 6m | %s", symbol, detail)
            return

        settings = engine.settings
        filters = engine.signal_filters
        last_sl_mult = getattr(engine.strategy, "last_sl_mult", None)
        # SMA y ADX de ruptura ya se aplican en el orquestador / selector de régimen.
        # No se re-filtran aquí para no duplicar el mismo veto.

        if signal is Signal.BUY:
            cool = filters.check_cooldown(symbol, bars)
            if not cool.allowed:
                logger.info("%s | %s", symbol, cool.reason)
                cache.signal_detail = f"{detail} | {cool.reason}"
                engine._notify_signal_filtered(symbol, signal, cool.reason)
                return

        htf_already_bull = cache.trend == "bull"
        if (
            signal is Signal.BUY
            and settings.entry_confirmation_enabled
            and not htf_already_bull
        ):
            higher_tf = None
            higher_tf_label = self._higher_confirmation_timeframe(symbol)
            try:
                lookback = max(settings.confirm_momentum_bars + 5, 30)
                higher_tf = engine.market_data.get_bars(symbol, higher_tf_label, lookback)
            except Exception as exc:
                logger.debug("%s | velas %s no disponibles: %s", symbol, higher_tf_label, exc)
            confirm = filters.check_entry_confirmation(
                symbol,
                bars,
                higher_tf,
                higher_tf_label=higher_tf_label,
            )
            if not confirm.allowed:
                logger.info("%s | %s", symbol, confirm.reason)
                cache.signal_detail = f"{detail} | {confirm.reason}"
                engine._notify_signal_filtered(symbol, signal, confirm.reason)
                return
        elif signal is Signal.BUY and htf_already_bull:
            logger.info(
                "%s | confirmación HTF omitida — tendencia %s ya es bull",
                symbol,
                self._regime_timeframe(symbol),
            )

        engine._execute_signal(
            symbol,
            account,
            positions,
            bars,
            tape,
            last_price,
            position,
            signal,
            atr_sl_mult=last_sl_mult,
        )

    def run_all_layers_once(self) -> None:
        clock = self.engine.client.get_market_clock()
        self._apply_trading_mode(clock)
        self._layer_9m()
        self._layer_3m()
        self._layer_6m_execute()
        self._persist()

    def _apply_trading_mode(self, clock) -> None:
        mode, symbols = resolve_trading_mode(clock, self.engine.settings)
        label = trading_mode_label(mode)
        previous = self.trading_mode
        if previous is None:
            # Arranque: reintentar cierres cruzados que fallaron (p. ej. cripto con TIF inválido).
            self.engine.close_positions_on_mode_switch(mode.value)
        elif mode != previous:
            logger.info(
                "Transicion automatica de mercado | %s -> %s | activos: %s",
                previous.value,
                mode.value,
                ",".join(symbols),
            )
            self.engine.close_positions_on_mode_switch(mode.value)
            if self.engine.notifier.enabled:
                self.engine.notifier.notify_mode_switch(label, symbols)
        self.trading_mode = mode
        self.active_symbols = symbols
        self.trading_mode_label = label
        if previous is None or mode != previous:
            self._sync_intervals_for_mode(reset=True)

    def _persist(self) -> None:
        self.store.save(
            self.scheduler,
            self.cache,
            self.engine.notifier.enabled,
            trading_mode=self.trading_mode.value if self.trading_mode else "",
            trading_mode_label=self.trading_mode_label,
            active_symbols=self.active_symbols,
        )

    def _signal_timeframe(self, symbol: str) -> str:
        if is_crypto_symbol(symbol):
            return self.engine.settings.crypto_bar_timeframe
        return "6Min"

    def _spike_timeframe(self, symbol: str) -> str:
        if is_crypto_symbol(symbol):
            return self.engine.settings.crypto_bar_timeframe
        return "1Min"

    def _regime_timeframe(self, symbol: str) -> str:
        if is_crypto_symbol(symbol):
            return self.engine.settings.crypto_regime_timeframe
        return "9Min"

    def _higher_confirmation_timeframe(self, symbol: str) -> str:
        if is_crypto_symbol(symbol):
            return self.engine.settings.crypto_regime_timeframe
        return self.engine.settings.confirm_higher_tf

    def _sleep_between_crypto_symbols(self, symbols: list[str], index: int) -> None:
        if index >= len(symbols) - 1:
            return
        current = symbols[index]
        remaining = symbols[index + 1 :]
        if not is_crypto_symbol(current):
            return
        if not any(is_crypto_symbol(symbol) for symbol in remaining):
            return
        logger.debug(
            "%s | pausa secuencial %.1fs entre consultas cripto",
            current,
            CRYPTO_SYMBOL_QUERY_DELAY_SECONDS,
        )
        time.sleep(CRYPTO_SYMBOL_QUERY_DELAY_SECONDS)

    def _sync_intervals_for_mode(self, *, reset: bool = False) -> None:
        if self.trading_mode is TradingMode.CRYPTO:
            intervals = {
                "3m": _timeframe_seconds(self.engine.settings.crypto_bar_timeframe),
                "6m": _timeframe_seconds(self.engine.settings.crypto_bar_timeframe),
                "9m": _timeframe_seconds(self.engine.settings.crypto_regime_timeframe),
            }
        else:
            intervals = {
                "3m": self.engine.settings.tf_3m_seconds,
                "6m": self.engine.settings.tf_6m_seconds,
                "9m": self.engine.settings.tf_9m_seconds,
            }
        changed = intervals != self.scheduler.intervals
        self.scheduler.intervals = intervals
        if reset or changed:
            self.scheduler.last_run = {}
