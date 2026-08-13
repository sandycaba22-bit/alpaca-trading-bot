"""Filtro de flujo de precios en tiempo real (no solo patrones históricos)."""

from __future__ import annotations

import logging

from bot.strategy.base import Signal, StrategyContext

logger = logging.getLogger(__name__)


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

    def confirm(self, signal: Signal, ctx: StrategyContext) -> tuple[bool, str]:
        if signal is Signal.HOLD:
            return False, "hold"

        if ctx.last_price is None or ctx.last_price <= 0:
            return False, "sin precio en tiempo real"

        if ctx.spread_pct is not None and ctx.spread_pct > self.max_spread_pct:
            return False, f"spread {ctx.spread_pct:.4%} > máximo {self.max_spread_pct:.4%}"

        last_close = float(ctx.bars["close"].iloc[-1])
        vs_close = (ctx.last_price - last_close) / last_close if last_close else 0.0

        if signal is Signal.BUY:
            if vs_close <= -self.max_adverse_vs_close_pct:
                return False, (
                    f"flujo bajista vs cierre ({vs_close:.2%}); "
                    "no se compra contra el tape"
                )
            if ctx.momentum_pct is not None and ctx.momentum_pct <= -self.adverse_momentum_pct:
                return False, f"momentum corto plazo {ctx.momentum_pct:.2%} adverso"
        elif signal is Signal.SELL:
            if vs_close >= self.max_adverse_vs_close_pct and not ctx.has_long_position:
                return False, "flujo alcista; no se abre corto contra el tape"

        logger.debug(
            "%s | flujo OK | last=%.4f close=%.4f vs_close=%+.2f%% momentum=%s spread=%s",
            ctx.symbol,
            ctx.last_price,
            last_close,
            vs_close * 100,
            f"{ctx.momentum_pct:.2%}" if ctx.momentum_pct is not None else "n/a",
            f"{ctx.spread_pct:.4%}" if ctx.spread_pct is not None else "n/a",
        )
        return True, "flujo confirma"
