"""Stop Loss y Take Profit dinámicos basados en porcentaje y ATR."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class ExitReason(str, Enum):
    NONE = "none"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"


@dataclass(frozen=True)
class ProtectiveLevels:
    stop_price: float
    take_profit_price: float
    stop_pct: float
    take_profit_pct: float


class StopTakeProfitPolicy:
    """
    Niveles porcentuales con ensanchamiento dinámico vía ATR.

    Si la volatilidad (ATR/precio) exige un stop más amplio que el mínimo
    configurado, se usa ese valor (acotado por max_stop_pct / max_tp_pct).
    """

    def __init__(
        self,
        stop_loss_pct: float = 0.02,
        take_profit_pct: float = 0.05,
        atr_stop_mult: float = 1.5,
        max_stop_pct: float = 0.08,
        max_tp_pct: float = 0.20,
    ) -> None:
        if stop_loss_pct <= 0 or take_profit_pct <= 0:
            raise ValueError("STOP_LOSS_PCT y TAKE_PROFIT_PCT deben ser > 0")
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.atr_stop_mult = atr_stop_mult
        self.max_stop_pct = max_stop_pct
        self.max_tp_pct = max_tp_pct

    def levels(
        self,
        entry_price: float,
        qty: float,
        last_price: float | None = None,
        atr_value: float | None = None,
    ) -> ProtectiveLevels:
        ref = last_price if last_price and last_price > 0 else entry_price
        stop_pct = self.stop_loss_pct
        tp_pct = self.take_profit_pct

        if atr_value and ref > 0:
            atr_pct = (atr_value / ref) * self.atr_stop_mult
            stop_pct = min(self.max_stop_pct, max(self.stop_loss_pct, atr_pct))
            tp_pct = min(self.max_tp_pct, max(self.take_profit_pct, atr_pct * 2))

        long = qty >= 0
        if long:
            stop_price = entry_price * (1 - stop_pct)
            tp_price = entry_price * (1 + tp_pct)
        else:
            stop_price = entry_price * (1 + stop_pct)
            tp_price = entry_price * (1 - tp_pct)

        return ProtectiveLevels(stop_price, tp_price, stop_pct, tp_pct)

    def evaluate_price(
        self,
        entry_price: float,
        qty: float,
        last_price: float,
        atr_value: float | None = None,
    ) -> ExitReason:
        """Evalúa el último precio de mercado (flujo en tiempo real)."""
        levels = self.levels(entry_price, qty, last_price, atr_value)
        long = qty >= 0
        if long:
            if last_price <= levels.stop_price:
                return ExitReason.STOP_LOSS
            if last_price >= levels.take_profit_price:
                return ExitReason.TAKE_PROFIT
        else:
            if last_price >= levels.stop_price:
                return ExitReason.STOP_LOSS
            if last_price <= levels.take_profit_price:
                return ExitReason.TAKE_PROFIT
        return ExitReason.NONE

    def evaluate_bar(
        self,
        entry_price: float,
        qty: float,
        high: float,
        low: float,
        close: float,
        atr_value: float | None = None,
    ) -> tuple[ExitReason, float]:
        """
        Evaluación intra-barra para backtest.
        Si SL y TP se tocan en la misma barra, se asume SL (conservador).
        """
        levels = self.levels(entry_price, qty, close, atr_value)
        long = qty >= 0
        if long:
            if low <= levels.stop_price:
                return ExitReason.STOP_LOSS, levels.stop_price
            if high >= levels.take_profit_price:
                return ExitReason.TAKE_PROFIT, levels.take_profit_price
        else:
            if high >= levels.stop_price:
                return ExitReason.STOP_LOSS, levels.stop_price
            if low <= levels.take_profit_price:
                return ExitReason.TAKE_PROFIT, levels.take_profit_price
        return ExitReason.NONE, close
