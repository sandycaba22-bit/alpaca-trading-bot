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
    atr_value: float | None = None
    used_atr: bool = False
    trailing_offset: float | None = None


class StopTakeProfitPolicy:
    """
    SL / TP / trailing por múltiplo de ATR, con fallback a % fijos.

    Si hay ATR válido: SL = atr_sl_mult * ATR, TP = atr_tp_mult * ATR
    (acotados por max_stop_pct / max_tp_pct). Si el ATR falla, se usan
    STOP_LOSS_PCT y TAKE_PROFIT_PCT.
    """

    def __init__(
        self,
        stop_loss_pct: float = 0.02,
        take_profit_pct: float = 0.05,
        atr_stop_mult: float = 1.5,
        max_stop_pct: float = 0.08,
        max_tp_pct: float = 0.20,
        atr_sl_mult: float | None = None,
        atr_tp_mult: float = 4.5,
        atr_trailing_mult: float = 2.0,
        breakeven_activate_pct: float = 0.0015,
        breakeven_activate_atr_mult: float = 1.5,
        breakeven_buffer: float = 0.25,
        breakeven_buffer_atr_mult: float = 0.25,
        use_breakeven_lock: bool = True,
        min_tp_pct: float = 0.01,
    ) -> None:
        if stop_loss_pct <= 0 or take_profit_pct <= 0:
            raise ValueError("STOP_LOSS_PCT y TAKE_PROFIT_PCT deben ser > 0")
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.atr_stop_mult = atr_stop_mult
        self.atr_sl_mult = atr_sl_mult if atr_sl_mult is not None else atr_stop_mult
        self.atr_tp_mult = atr_tp_mult
        self.atr_trailing_mult = atr_trailing_mult
        self.max_stop_pct = max_stop_pct
        self.max_tp_pct = max_tp_pct
        self.min_tp_pct = max(0.0, float(min_tp_pct))
        self.breakeven_activate_pct = breakeven_activate_pct
        self.breakeven_activate_atr_mult = breakeven_activate_atr_mult
        self.breakeven_buffer = breakeven_buffer
        self.breakeven_buffer_atr_mult = breakeven_buffer_atr_mult
        self.use_breakeven_lock = use_breakeven_lock

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
        used_atr = False
        trailing_offset = None

        if atr_value and atr_value > 0 and ref > 0:
            sl_pct = (atr_value * self.atr_sl_mult) / ref
            tp_from_atr = (atr_value * self.atr_tp_mult) / ref
            if sl_pct > 0:
                stop_pct = min(self.max_stop_pct, sl_pct)
                used_atr = True
            if tp_from_atr > 0:
                # Piso de TP: en acciones con ATR bajo, ATR×N puede quedar
                # por debajo del coste de spread/comisión (~0.45%).
                tp_pct = min(self.max_tp_pct, max(self.min_tp_pct, tp_from_atr))
                used_atr = True
            trailing_offset = atr_value * self.atr_trailing_mult

        long = qty >= 0
        if long:
            stop_price = entry_price * (1 - stop_pct)
            tp_price = entry_price * (1 + tp_pct)
        else:
            stop_price = entry_price * (1 + stop_pct)
            tp_price = entry_price * (1 - tp_pct)

        return ProtectiveLevels(
            stop_price,
            tp_price,
            stop_pct,
            tp_pct,
            atr_value=atr_value if used_atr else None,
            used_atr=used_atr,
            trailing_offset=trailing_offset,
        )

    def trailing_candidate(
        self,
        entry_price: float,
        qty: float,
        peak_price: float,
        current_sl: float,
        atr_value: float | None,
        *,
        activate_pct: float = 0.025,
        offset_pct: float = 0.0125,
    ) -> tuple[float | None, str]:
        """Nuevo SL de trailing (piso A breakeven + piso B chase ATR), o None si no sube."""
        if qty <= 0 or entry_price <= 0 or peak_price <= 0:
            return None, "invalid"
        long = qty >= 0
        floor_a: float | None = None
        floor_b: float | None = None
        source_b = "pct"

        if (
            self.use_breakeven_lock
            and atr_value
            and atr_value > 0
            and self.breakeven_activate_atr_mult > 0
        ):
            activate_dist = float(atr_value) * float(self.breakeven_activate_atr_mult)
            buffer = max(0.0, float(atr_value) * float(self.breakeven_buffer_atr_mult))
            if long and peak_price >= entry_price + activate_dist:
                floor_a = entry_price + buffer
            elif (not long) and peak_price <= entry_price - activate_dist:
                floor_a = entry_price - buffer

        if atr_value and atr_value > 0:
            offset = atr_value * self.atr_trailing_mult
            if offset <= 0:
                if floor_a is None:
                    return None, "atr_zero"
            elif long and peak_price >= entry_price + offset:
                floor_b = peak_price - offset
                source_b = "atr"
            elif (not long) and peak_price <= entry_price - offset:
                floor_b = peak_price + offset
                source_b = "atr"
        else:
            pnl_pct = peak_price / entry_price - 1.0
            if long and pnl_pct >= activate_pct:
                floor_b = peak_price * (1.0 - offset_pct)
                source_b = "pct"
            elif (not long) and pnl_pct <= -activate_pct:
                floor_b = peak_price * (1.0 + offset_pct)
                source_b = "pct"

        floors = [value for value in (floor_a, floor_b) if value is not None]
        if not floors:
            return None, "wait"

        if long:
            candidate = max(floors)
            if current_sl > 0:
                candidate = max(current_sl, candidate)
            if candidate <= current_sl:
                return None, source_b if floor_b is not None else "breakeven"
        else:
            candidate = min(floors)
            if current_sl > 0:
                candidate = min(current_sl, candidate)
            if current_sl > 0 and candidate >= current_sl:
                return None, source_b if floor_b is not None else "breakeven"

        if floor_b is not None and (
            floor_a is None
            or (long and floor_b >= floor_a)
            or ((not long) and floor_b <= floor_a)
        ):
            return candidate, source_b
        return candidate, "breakeven"

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
