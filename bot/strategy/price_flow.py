"""Filtro de flujo de precios en tiempo real (no solo patrones históricos)."""

from __future__ import annotations

import logging

from bot.strategy.base import Signal, StrategyContext
from bot.strategy.regime_selector import StrategyId

logger = logging.getLogger(__name__)

_PULLBACK_FLOW = frozenset(
    {
        StrategyId.PULLBACK.value,
        StrategyId.TREND_PULLBACK.value,
    }
)
# sync_entry ya valida impulso en velas cerradas; no repetir "ruptura ágil" en tape.
_SYNC_ENTRY_FLOW = frozenset({"sync_entry"})


class PriceFlowFilter:
    """
    Confirma o rechaza una señal SMA según el tape actual:

    - último trade vs último cierre
    - momentum de corto plazo
    - amplitud del spread bid/ask
    """

    def __init__(
        self,
        max_spread_pct: float = 0.003,
        adverse_momentum_pct: float = 0.008,
        max_adverse_vs_close_pct: float = 0.006,
    ) -> None:
        self.max_spread_pct = max_spread_pct
        self.adverse_momentum_pct = adverse_momentum_pct
        self.max_adverse_vs_close_pct = max_adverse_vs_close_pct

    def _zone_buffer_pct(self, ctx: StrategyContext) -> float:
        atr_pct = 0.0
        if ctx.atr and ctx.last_price:
            atr_pct = max(0.0, float(ctx.atr) / float(ctx.last_price))
        spread_pct = max(0.0, float(ctx.spread_pct or 0.0))
        return max(
            self.max_adverse_vs_close_pct * 0.40,
            self.adverse_momentum_pct * 0.25,
            spread_pct * 1.50,
            atr_pct * 0.20,
            0.0012,
        )

    def _recent_levels(self, ctx: StrategyContext) -> tuple[float, float]:
        window = min(len(ctx.bars), 6)
        highs = ctx.bars["high"].tail(window)
        lows = ctx.bars["low"].tail(window)
        if len(highs) > 1:
            resistance = float(highs.iloc[:-1].max())
            support = float(lows.iloc[:-1].min())
        else:
            resistance = float(highs.iloc[-1])
            support = float(lows.iloc[-1])
        return resistance, support

    def _has_recent_impulse(self, signal: Signal, ctx: StrategyContext, zone_pct: float) -> bool:
        if len(ctx.bars) < 3:
            return False
        recent = ctx.bars.tail(3)
        last = recent.iloc[-1]
        prev = recent.iloc[-2]
        last_open = float(last["open"])
        last_close = float(last["close"])
        prev_close = float(prev["close"])
        body_pct = abs(last_close - last_open) / last_close if last_close else 0.0
        min_body_pct = max(zone_pct * 0.75, 0.0010)
        if signal is Signal.BUY:
            return (
                last_close > last_open
                and last_close >= prev_close
                and body_pct >= min_body_pct
            )
        return (
            last_close < last_open
            and last_close <= prev_close
            and body_pct >= min_body_pct
        )

    def _trigger_ready(self, signal: Signal, ctx: StrategyContext, zone_pct: float) -> tuple[bool, str]:
        resistance, support = self._recent_levels(ctx)
        impulse = self._has_recent_impulse(signal, ctx, zone_pct)
        if signal is Signal.BUY:
            in_zone = ctx.last_price >= resistance * (1.0 - zone_pct)
            if in_zone and impulse:
                return True, "ruptura/impulso dentro de zona"
            if in_zone:
                return True, "precio en zona de ruptura"
            if impulse:
                return True, "impulso alcista reciente"
            return False, f"sin ruptura ágil (zona<{resistance * (1.0 - zone_pct):.4f})"
        in_zone = ctx.last_price <= support * (1.0 + zone_pct)
        if in_zone and impulse:
            return True, "quiebre/impulso dentro de zona"
        if in_zone:
            return True, "precio en zona de quiebre"
        if impulse:
            return True, "impulso bajista reciente"
        return False, f"sin quiebre ágil (zona>{support * (1.0 + zone_pct):.4f})"

    def _is_pullback_entry(self, ctx: StrategyContext) -> bool:
        key = str(ctx.entry_strategy or "").strip().lower()
        return key in _PULLBACK_FLOW

    def _is_sync_entry(self, ctx: StrategyContext) -> bool:
        key = str(ctx.entry_strategy or "").strip().lower()
        return key in _SYNC_ENTRY_FLOW

    def confirm(self, signal: Signal, ctx: StrategyContext) -> tuple[bool, str]:
        if signal is Signal.HOLD:
            return False, "hold"

        if ctx.last_price is None or ctx.last_price <= 0:
            return False, "sin precio en tiempo real"

        last_close = float(ctx.bars["close"].iloc[-1])
        vs_close = (ctx.last_price - last_close) / last_close if last_close else 0.0
        zone_pct = self._zone_buffer_pct(ctx)
        pullback_entry = self._is_pullback_entry(ctx)
        sync_entry = self._is_sync_entry(ctx)
        tape_relaxed = pullback_entry or sync_entry
        trigger_ready, trigger_reason = self._trigger_ready(signal, ctx, zone_pct)
        spread_limit = self.max_spread_pct * (2.0 if trigger_ready or tape_relaxed else 1.0)

        if ctx.spread_pct is not None and ctx.spread_pct > spread_limit:
            return False, (
                f"spread {ctx.spread_pct:.4%} > máximo dinámico {spread_limit:.4%}"
            )

        if signal is Signal.BUY:
            if tape_relaxed:
                if vs_close <= -(self.max_adverse_vs_close_pct + zone_pct):
                    adverse_msg = (
                        "sync_entry no compra contra el tape"
                        if sync_entry
                        else "pullback no compra contra el tape"
                    )
                    return False, (
                        f"flujo bajista vs cierre ({vs_close:.2%}); {adverse_msg}"
                    )
                if (
                    ctx.momentum_pct is not None
                    and ctx.momentum_pct <= -(self.adverse_momentum_pct + zone_pct)
                ):
                    return False, f"momentum corto plazo {ctx.momentum_pct:.2%} adverso"
                flow_label = (
                    "sync_entry sin exigir ruptura en tape"
                    if sync_entry
                    else "pullback sin exigir ruptura"
                )
                logger.info(
                    "%s | flujo OK | motivo=%s | last=%.4f close=%.4f "
                    "vs_close=%+.2f%% spread=%s",
                    ctx.symbol,
                    flow_label,
                    ctx.last_price,
                    last_close,
                    vs_close * 100,
                    f"{ctx.spread_pct:.4%}" if ctx.spread_pct is not None else "n/a",
                )
                ok_reason = (
                    "sync_entry — flujo sin exigir ruptura en tape"
                    if sync_entry
                    else "pullback — flujo sin exigir ruptura"
                )
                return True, ok_reason
            if not trigger_ready:
                return False, trigger_reason
            if vs_close <= -(self.max_adverse_vs_close_pct + zone_pct):
                return False, (
                    f"flujo bajista vs cierre ({vs_close:.2%}); "
                    "no se compra contra el tape"
                )
            if (
                ctx.momentum_pct is not None
                and ctx.momentum_pct <= -(self.adverse_momentum_pct + zone_pct)
            ):
                return False, f"momentum corto plazo {ctx.momentum_pct:.2%} adverso"
        elif signal is Signal.SELL:
            if not trigger_ready:
                return False, trigger_reason
            if (
                vs_close >= (self.max_adverse_vs_close_pct + zone_pct)
                and not ctx.has_long_position
            ):
                return False, "flujo alcista; no se abre corto contra el tape"

        logger.info(
            "%s | flujo OK | motivo=%s | last=%.4f close=%.4f vs_close=%+.2f%% momentum=%s spread=%s buffer=%s",
            ctx.symbol,
            trigger_reason,
            ctx.last_price,
            last_close,
            vs_close * 100,
            f"{ctx.momentum_pct:.2%}" if ctx.momentum_pct is not None else "n/a",
            f"{ctx.spread_pct:.4%}" if ctx.spread_pct is not None else "n/a",
            f"{zone_pct:.4%}",
        )
        return True, trigger_reason
