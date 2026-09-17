"""Límites de riesgo antes de enviar una orden, más salidas SL/TP."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from bot.alpaca.client import AccountSnapshot
from bot.config import Settings
from bot.market.assets import is_crypto_symbol
from bot.market.dust import dust_threshold_for
from bot.risk.stops import ExitReason, ProtectiveLevels, StopTakeProfitPolicy
from bot.security.sanitize import floor_fractional_qty
from bot.storage.params import SymbolParams
from bot.strategy.base import Signal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    qty: float
    reason: str


class RiskManager:
    def __init__(
        self,
        settings: Settings,
        overrides: dict[str, SymbolParams] | None = None,
    ) -> None:
        self.settings = settings
        self.overrides = {key.upper(): value for key, value in (overrides or {}).items()}
        self.stops = StopTakeProfitPolicy(
            stop_loss_pct=settings.stop_loss_pct,
            take_profit_pct=settings.take_profit_pct,
            atr_stop_mult=settings.atr_stop_mult,
            atr_sl_mult=settings.atr_sl_mult,
            atr_tp_mult=settings.atr_tp_mult,
            atr_trailing_mult=settings.atr_trailing_mult,
            breakeven_activate_pct=settings.breakeven_activate_pct,
            breakeven_activate_atr_mult=settings.breakeven_activate_atr_mult,
            breakeven_buffer=settings.breakeven_buffer,
            breakeven_buffer_atr_mult=settings.breakeven_buffer_atr_mult,
            min_tp_pct=settings.min_tp_pct,
        )

    def _stops_for(self, symbol: str | None) -> StopTakeProfitPolicy:
        if not symbol:
            return self.stops
        params = self.overrides.get(symbol.upper())
        if params is None:
            return self.stops
        return StopTakeProfitPolicy(
            stop_loss_pct=params.stop_loss_pct,
            take_profit_pct=params.take_profit_pct,
            atr_stop_mult=params.atr_stop_mult,
            atr_sl_mult=params.atr_stop_mult,
            atr_tp_mult=self.settings.atr_tp_mult,
            atr_trailing_mult=self.settings.atr_trailing_mult,
            breakeven_activate_pct=self.settings.breakeven_activate_pct,
            breakeven_activate_atr_mult=self.settings.breakeven_activate_atr_mult,
            breakeven_buffer=self.settings.breakeven_buffer,
            breakeven_buffer_atr_mult=self.settings.breakeven_buffer_atr_mult,
            min_tp_pct=self.settings.min_tp_pct,
        )

    def evaluate(
        self,
        signal: Signal,
        symbol: str,
        last_price: float,
        account: AccountSnapshot | None,
        open_positions: int,
        has_position: bool,
        atr_value: float | None = None,
        atr_sl_mult: float | None = None,
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

        if account is None:
            return RiskDecision(
                False,
                0.0,
                "snapshot de cuenta no disponible para dimensionar la entrada",
            )

        cap_notional = min(
            account.equity * self.settings.position_size_pct,
            self.settings.max_notional_per_order,
            account.buying_power,
        )
        if cap_notional <= 0:
            return RiskDecision(False, 0.0, "buying power insuficiente")

        if self.settings.use_fixed_risk_sizing:
            qty = self._qty_from_risk(
                symbol, last_price, account.equity, atr_value, atr_sl_mult, cap_notional
            )
            if qty <= 0:
                return RiskDecision(False, 0.0, "riesgo fijo no alcanza cantidad mínima")
            sl_dist = self._stop_distance(last_price, atr_value, atr_sl_mult)
            logger.info(
                "Riesgo OK | %s BUY qty=%s notional~%.2f | riesgo fijo %.2f%% equity "
                "| SL dist=%.4f",
                symbol,
                qty,
                qty * last_price,
                self.settings.risk_percent_per_trade * 100,
                sl_dist,
            )
            return RiskDecision(True, float(qty), "ok")

        qty = self._qty_from_notional(symbol, last_price, cap_notional)
        if qty <= 0:
            return RiskDecision(
                False,
                0.0,
                f"notional {cap_notional:.2f} insuficiente a {last_price:.2f}",
            )
        logger.info(
            "Riesgo OK | %s BUY qty=%s notional~%.2f (%.1f%% equity)",
            symbol,
            qty,
            qty * last_price,
            self.settings.position_size_pct * 100,
        )
        return RiskDecision(True, float(qty), "ok")

    def _stop_distance(
        self,
        last_price: float,
        atr_value: float | None,
        atr_sl_mult: float | None,
    ) -> float:
        sl_mult = float(atr_sl_mult if atr_sl_mult is not None else self.settings.atr_sl_mult)
        if atr_value and atr_value > 0:
            return float(atr_value) * sl_mult
        return last_price * self.settings.stop_loss_pct

    def _qty_from_risk(
        self,
        symbol: str,
        last_price: float,
        equity: float,
        atr_value: float | None,
        atr_sl_mult: float | None,
        cap_notional: float,
    ) -> float:
        sl_dist = self._stop_distance(last_price, atr_value, atr_sl_mult)
        if sl_dist <= 0 or last_price <= 0:
            return 0.0
        risk_cash = equity * self.settings.risk_percent_per_trade
        raw_qty = risk_cash / sl_dist
        max_qty = cap_notional / last_price
        qty = min(raw_qty, max_qty)
        return self._normalize_qty(symbol, qty)

    def _qty_from_notional(self, symbol: str, last_price: float, notional: float) -> float:
        return self._normalize_qty(symbol, notional / last_price)

    def _normalize_qty(self, symbol: str, qty: float) -> float:
        if is_crypto_symbol(symbol):
            qty = floor_fractional_qty(float(qty), decimals=6)
            ref_qty = max(abs(float(qty)), float(self.settings.dust_threshold_min_qty))
            min_qty = dust_threshold_for(self.settings, symbol, ref_qty)
            return qty if qty + 1e-12 >= min_qty else 0.0
        qty = math.floor(float(qty))
        return float(qty) if qty >= 1 else 0.0

    def protective_levels(
        self,
        entry_price: float,
        qty: float,
        last_price: float | None = None,
        atr_value: float | None = None,
        symbol: str | None = None,
        atr_sl_mult: float | None = None,
    ) -> ProtectiveLevels:
        policy = self._stops_for(symbol)
        if atr_sl_mult is not None and abs(float(atr_sl_mult) - float(policy.atr_sl_mult)) > 1e-9:
            policy = StopTakeProfitPolicy(
                stop_loss_pct=policy.stop_loss_pct,
                take_profit_pct=policy.take_profit_pct,
                atr_stop_mult=policy.atr_stop_mult,
                atr_sl_mult=float(atr_sl_mult),
                atr_tp_mult=policy.atr_tp_mult,
                atr_trailing_mult=policy.atr_trailing_mult,
                breakeven_activate_pct=policy.breakeven_activate_pct,
                breakeven_activate_atr_mult=policy.breakeven_activate_atr_mult,
                breakeven_buffer=policy.breakeven_buffer,
                breakeven_buffer_atr_mult=policy.breakeven_buffer_atr_mult,
                min_tp_pct=policy.min_tp_pct,
            )
        return policy.levels(entry_price, qty, last_price, atr_value)

    def evaluate_exit(
        self,
        entry_price: float,
        qty: float,
        last_price: float,
        atr_value: float | None = None,
        symbol: str | None = None,
    ) -> ExitReason:
        reason = self._stops_for(symbol).evaluate_price(entry_price, qty, last_price, atr_value)
        if reason is not ExitReason.NONE:
            levels = self.protective_levels(entry_price, qty, last_price, atr_value, symbol=symbol)
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

    def trailing_stop(
        self,
        entry_price: float,
        qty: float,
        peak_price: float,
        current_sl: float,
        atr_value: float | None,
        symbol: str | None = None,
        *,
        activate_pct: float = 0.025,
        offset_pct: float = 0.0125,
    ) -> tuple[float | None, str]:
        return self._stops_for(symbol).trailing_candidate(
            entry_price,
            qty,
            peak_price,
            current_sl,
            atr_value,
            activate_pct=activate_pct,
            offset_pct=offset_pct,
        )
