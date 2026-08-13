"""Límites de riesgo antes de enviar una orden, más salidas SL/TP."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from bot.alpaca.client import AccountSnapshot
from bot.config import Settings
from bot.risk.stops import ExitReason, ProtectiveLevels, StopTakeProfitPolicy
from bot.strategy.base import Signal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    qty: float
    reason: str


class RiskManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.stops = StopTakeProfitPolicy(
            stop_loss_pct=settings.stop_loss_pct,
            take_profit_pct=settings.take_profit_pct,
            atr_stop_mult=settings.atr_stop_mult,
        )

    def evaluate(
        self,
        signal: Signal,
        symbol: str,
        last_price: float,
        account: AccountSnapshot,
        open_positions: int,
        has_position: bool,
    ) -> RiskDecision:
        if signal is Signal.HOLD:
            return RiskDecision(False, 0.0, "hold")

        if signal is Signal.SELL:
            if not has_position:
                return RiskDecision(False, 0.0, "sell sin posición abierta")
            return RiskDecision(True, 0.0, "cerrar posición")

        if signal is Signal.BUY and has_position:
            return RiskDecision(False, 0.0, f"ya hay posición larga en {symbol}")

        if not self.settings.allow_short and signal is Signal.SELL and not has_position:
            return RiskDecision(False, 0.0, "shorts deshabilitados")

        if open_positions >= self.settings.max_open_positions:
            return RiskDecision(
                False,
                0.0,
                f"máximo de posiciones abiertas ({self.settings.max_open_positions})",
            )

        if last_price <= 0:
            return RiskDecision(False, 0.0, "precio inválido")

        notional = min(
            account.equity * self.settings.position_size_pct,
            self.settings.max_notional_per_order,
            account.buying_power,
        )
        if notional <= 0:
            return RiskDecision(False, 0.0, "buying power insuficiente")

        qty = math.floor(notional / last_price)
        if qty < 1:
            return RiskDecision(
                False,
                0.0,
                f"notional {notional:.2f} no alcanza 1 acción a {last_price:.2f}",
            )

        logger.info(
            "Riesgo OK | %s BUY qty=%s notional~%.2f (%.1f%% equity)",
            symbol,
            qty,
            qty * last_price,
            self.settings.position_size_pct * 100,
        )
        return RiskDecision(True, float(qty), "ok")

    def protective_levels(
        self,
        entry_price: float,
        qty: float,
        last_price: float | None = None,
        atr_value: float | None = None,
    ) -> ProtectiveLevels:
        return self.stops.levels(entry_price, qty, last_price, atr_value)

    def evaluate_exit(
        self,
        entry_price: float,
        qty: float,
        last_price: float,
        atr_value: float | None = None,
    ) -> ExitReason:
        reason = self.stops.evaluate_price(entry_price, qty, last_price, atr_value)
        if reason is not ExitReason.NONE:
            levels = self.protective_levels(entry_price, qty, last_price, atr_value)
            logger.info(
                "Salida %s | last=%.4f SL=%.4f (%.2f%%) TP=%.4f (%.2f%%)",
                reason.value,
                last_price,
                levels.stop_price,
                levels.stop_pct * 100,
                levels.take_profit_price,
                levels.take_profit_pct * 100,
            )
        return reason
