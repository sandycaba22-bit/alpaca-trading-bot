"""Motor paper/live mínimo — no usa TradingEngine stocks."""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from types import SimpleNamespace

import pandas as pd

from bot.alpaca.client import AlpacaClient
from bot.alpaca.market_data import MarketDataService
from bot.crypto_night_settings import CryptoNightSettings
from bot.notify.telegram import TelegramNotifier
from strategies.crypto_night.asymmetric_tp import round_crypto_price
from strategies.crypto_night.compression import resolve_entry_volatility
from strategies.crypto_night.protective_pct import (
    apply_compression_protective,
    apply_crypto_night_protective,
)
from strategies.crypto_night.bias import resolve_night_bias
from strategies.crypto_night.quality import score_setup
from strategies.crypto_night.exits_live import ExitDecision, current_r, evaluate_exit
from strategies.crypto_night.position_book import NightTradeRecord, load_trades, save_trades
from strategies.crypto_night.risk import (
    CIRCUIT_BREAKER_CONSECUTIVE_LOSSES,
    CIRCUIT_BREAKER_HALT_REASON,
    NightRiskState,
    RiskLimits,
    can_open_trade,
    register_trade_result,
    reset_circuit_breaker_for_new_cycle,
)
from strategies.crypto_night.session import (
    entries_allowed,
    in_night_trading_window,
    should_flatten_crypto_positions,
)
from strategies.crypto_night.types import GateResult, NightBias, TradeSide
from strategies.crypto_night.variants import SweepVariant, find_setup_for_variant

logger = logging.getLogger("crypto_night")

# Ventana de referencia V1 (min/max de sesión antes del sweep+reclaim en 15m).
SESSION_LOOKBACK_HOURS = 4
_ET = ZoneInfo("America/New_York")


class CryptoNightEngine:
    def __init__(self, settings: CryptoNightSettings) -> None:
        self.settings = settings
        alpaca_cfg = SimpleNamespace(
            api_key_id=settings.api_key_id,
            api_secret_key=settings.api_secret_key,
            api_base_url=settings.api_base_url,
            paper=settings.paper,
            api_data_per_minute=120,
            order_per_minute=30,
            order_per_day=200,
        )
        self.client = AlpacaClient(alpaca_cfg)
        self.market = MarketDataService(self.client)
        self.notifier = TelegramNotifier(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            prefix=settings.telegram_prefix,
        )
        self.risk = NightRiskState()
        self.limits = RiskLimits(
            limits_enabled=settings.risk_limits_enabled,
            max_trades_night=settings.max_trades_per_night,
        )
        self._shutdown = False
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = settings.data_dir / "crypto_night_state.json"
        self._trades_path = settings.data_dir / "crypto_night_trades.json"
        self._kill_path = settings.data_dir / "crypto_night_kill_switch.json"
        self._circuit_breaker_day_et: str = ""

    def request_shutdown(self) -> None:
        self._shutdown = True

    def _open_crypto_positions(self) -> list[str]:
        try:
            positions = self.client.trading.get_all_positions()
            out: list[str] = []
            for pos in positions:
                sym = str(getattr(pos, "symbol", "") or "")
                if "/" in sym and float(getattr(pos, "qty", 0) or 0) != 0:
                    out.append(sym)
            return out
        except Exception as exc:
            logger.warning("No se pudieron leer posiciones crypto: %s", exc)
            return []

    def _load_risk_state(self) -> None:
        if not self._state_path.is_file():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            self.risk.trades_tonight = int(raw.get("trades_tonight", 0))
            self.risk.night_pnl_pct = float(raw.get("night_pnl_pct", 0.0))
            self.risk.halted = bool(raw.get("halted", False))
            self.risk.halt_reason = str(raw.get("halt_reason", ""))
            self.risk.week_pnl_pct = float(raw.get("week_pnl_pct", 0.0))
            self.risk.week_halted = bool(raw.get("week_halted", False))
            self.risk.consecutive_losses = int(raw.get("consecutive_losses", 0))
            self._circuit_breaker_day_et = str(raw.get("circuit_breaker_day_et", ""))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    def _save_risk_state(self) -> None:
        payload = {
            "trades_tonight": self.risk.trades_tonight,
            "night_pnl_pct": self.risk.night_pnl_pct,
            "halted": self.risk.halted,
            "halt_reason": self.risk.halt_reason,
            "week_pnl_pct": self.risk.week_pnl_pct,
            "week_halted": self.risk.week_halted,
            "consecutive_losses": self.risk.consecutive_losses,
            "circuit_breaker_day_et": self._circuit_breaker_day_et,
            "updated_utc": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("No se pudo guardar estado riesgo: %s", exc)

    def _maybe_reset_daily_circuit_breaker(self, now: datetime) -> None:
        day_et = now.astimezone(_ET).date().isoformat()
        if self._circuit_breaker_day_et and day_et != self._circuit_breaker_day_et:
            if self.risk.halted and self.risk.halt_reason == CIRCUIT_BREAKER_HALT_REASON:
                logger.info(
                    "Circuit breaker | nuevo día ET %s — se levanta halt por %s",
                    day_et,
                    CIRCUIT_BREAKER_HALT_REASON,
                )
            reset_circuit_breaker_for_new_cycle(self.risk)
            self._save_risk_state()
        self._circuit_breaker_day_et = day_et

    def _kill_switch_active(self) -> bool:
        if not self._kill_path.is_file():
            return False
        try:
            raw = json.loads(self._kill_path.read_text(encoding="utf-8"))
            return bool(raw.get("halt", False))
        except (OSError, json.JSONDecodeError):
            return False

    def _load_bars(self, symbol: str, tf: str, lookback: int) -> pd.DataFrame:
        return self.market.get_bars(symbol, tf, lookback, skip_cache=False)

    def _symbol_scan_order(self) -> tuple[str, ...]:
        syms = list(self.settings.symbols)
        syms.sort(key=lambda s: (0 if s.upper().startswith("BTC") else 1, s))
        return tuple(syms)

    def _resolve_symbol_bias(
        self,
        symbol: str,
        bars_4h: pd.DataFrame,
        bars_1d: pd.DataFrame,
        btc_bias: NightBias | None,
    ) -> tuple[GateResult, NightBias | None]:
        flt = self.settings.filters
        if flt.eth_inherit_btc_bias_only and symbol.upper().startswith("ETH"):
            if btc_bias is None:
                return (
                    GateResult(False, detail="ETH espera bias BTC en este ciclo"),
                    None,
                )
            return (
                GateResult(True),
                NightBias(side=btc_bias.side, symbol=symbol, reason="hereda BTC"),
            )
        return resolve_night_bias(
            bars_4h,
            bars_1d,
            symbol,
            btc_bias=btc_bias,
            bias_mode=self.settings.bias_mode,
            swing_lookback_4h=flt.bias_swing_lookback_4h,
            swing_lookback_1d=flt.bias_swing_lookback_1d,
            relaxed_structure=flt.bias_relaxed_structure,
        )

    def _find_entry_setup(
        self,
        variant: SweepVariant,
        *,
        symbol: str,
        bars_15m: pd.DataFrame,
        bars_1d: pd.DataFrame,
        session_start: pd.Timestamp,
        at_ts: pd.Timestamp,
        bias: NightBias,
        vol_ma: pd.Series,
    ):
        flt = self.settings.filters
        setup = find_setup_for_variant(
            variant,
            bars_15m=bars_15m,
            bars_1d=bars_1d,
            session_start=session_start,
            at_ts=at_ts,
            bias=bias,
            vol_ma=vol_ma,
            min_volume_ratio=flt.setup_min_volume_ratio,
            v1_max_bars_scan=flt.setup_v1_max_bars,
        )
        if setup is not None:
            return setup
        if flt.setup_try_v2_fallback and variant == SweepVariant.V1:
            setup = find_setup_for_variant(
                SweepVariant.V2,
                bars_15m=bars_15m,
                bars_1d=bars_1d,
                session_start=session_start,
                at_ts=at_ts,
                bias=bias,
                vol_ma=vol_ma,
                min_volume_ratio=flt.setup_min_volume_ratio,
                v1_max_bars_scan=flt.setup_v1_max_bars,
            )
            if setup is not None:
                logger.info("%s | setup V1 vacio → uso V2", symbol)
            return setup
        return None

    def run_once(self) -> None:
        now = datetime.now(timezone.utc)
        self._maybe_reset_daily_circuit_breaker(now)
        self._manage_lifecycle(now)
        if self._kill_switch_active():
            logger.info("Kill switch crypto_night activo — sin entradas")
            return
        sm = self.settings.session_mode
        if not in_night_trading_window(now, session_mode=sm):
            return
        flt = self.settings.filters
        if not entries_allowed(
            now,
            min_minutes_after_us_close=flt.entry_delay_minutes_after_us_close,
            session_mode=sm,
        ):
            return
        if not self._can_open_new_trade():
            return

        variant = self.settings.sweep_variant
        btc_bias = None
        for symbol in self._symbol_scan_order():
            bars_15m = self._load_bars(symbol, "15Min", 400)
            bars_1h = self._load_bars(symbol, "1Hour", 600)
            bars_4h = self._load_bars(symbol, "4Hour", 400)
            bars_1d = self._load_bars(symbol, "1Day", 120)
            if bars_15m.empty or bars_1h.empty:
                continue
            ts = bars_15m.index[-1]
            session_start = ts - pd.Timedelta(hours=SESSION_LOOKBACK_HOURS)
            bgate, bias = self._resolve_symbol_bias(symbol, bars_4h, bars_1d, btc_bias)
            if not bgate.ok or bias is None:
                logger.info("%s | candado bias | %s", symbol, bgate.detail)
                continue
            if symbol.upper().startswith("BTC"):
                btc_bias = bias
            vgate, atr_now, compression_mode = resolve_entry_volatility(
                bars_1h,
                bars_15m,
                ts,
                bias,
                session_start,
                pct_low=flt.vol_pct_low,
                pct_high=flt.vol_pct_high,
                low_vol_mode=self.settings.asymmetric_low_vol_mode,
            )
            if not vgate.ok:
                logger.info("%s | candado vol | %s", symbol, vgate.detail)
                continue
            if compression_mode:
                logger.info("%s | %s", symbol, vgate.detail)
            vol_ma = bars_15m["volume"].astype(float).rolling(20).mean()
            setup = self._find_entry_setup(
                variant,
                symbol=symbol,
                bars_15m=bars_15m,
                bars_1d=bars_1d,
                session_start=session_start,
                at_ts=ts,
                bias=bias,
                vol_ma=vol_ma,
            )
            if setup is None:
                if flt.setup_try_v2_fallback:
                    logger.info("%s | candado setup | sin patron V1/V2", symbol)
                continue
            setup.symbol = symbol
            setup.meta["compression_mode"] = compression_mode
            setup.meta["atr_pct"] = float(atr_now or 0.0)
            if len(bars_1h) > 14:
                setup.atr_1h = float(
                    bars_1h["close"].astype(float).diff().abs().rolling(14).mean().iloc[-1]
                )
            if not self._can_open_new_trade(symbol):
                logger.info(
                    "Sin trade | tope posiciones/cola=%s (activas=%s)",
                    self.settings.max_positions,
                    len(self._active_trades()),
                )
                return
            tape = self.market.get_live_tape(symbol)
            spread = tape.spread_pct if tape else None
            q = score_setup(
                setup,
                spread_pct=spread,
                atr_pct=atr_now,
                mode_min=flt.quality_min_score,
                min_theoretical_r=flt.quality_min_theoretical_r,
                max_spread_pct=flt.quality_max_spread_pct,
                min_volume_ratio=flt.setup_min_volume_ratio,
            )
            if not q.ok:
                logger.info("%s | candado calidad | score=%s", symbol, q.total)
                continue
            ok, reason = can_open_trade(self.risk, self.limits, mode="normal")
            if not ok:
                if reason == CIRCUIT_BREAKER_HALT_REASON:
                    logger.info(
                        "Sin trade | circuit breaker | %s pérdidas seguidas — "
                        "entradas pausadas hasta nuevo día ET o reinicio PM2",
                        self.risk.consecutive_losses,
                    )
                else:
                    logger.info("Sin trade | %s", reason)
                return
            self._apply_setup_protective(setup, compression=compression_mode)
            self._maybe_place_limit(symbol, setup, bias, compression=compression_mode)
            return

    def _active_trades(self) -> list[NightTradeRecord]:
        return [t for t in load_trades(self._trades_path) if t.status in {"pending", "open"}]

    def _can_open_new_trade(self, symbol: str | None = None) -> bool:
        active = self._active_trades()
        if len(active) >= self.settings.max_positions:
            return False
        if symbol and any(t.symbol == symbol for t in active):
            return False
        return True

    def _manage_lifecycle(self, now: datetime) -> None:
        trades = load_trades(self._trades_path)
        if not trades:
            return
        broker_open = set(self._open_crypto_positions())
        if should_flatten_crypto_positions(now, session_mode=self.settings.session_mode):
            for trade in trades:
                if trade.status == "open" and trade.qty_open > 0:
                    self._close_position(trade, trade.qty_open, "us_session_open")
            save_trades(self._trades_path, trades)
            return
        for trade in list(trades):
            if trade.status == "pending":
                self._sync_pending_entry(trade)
                continue
            if trade.status != "open" or trade.qty_open <= 0:
                continue
            if trade.symbol not in broker_open:
                entry = float(trade.entry_price or 0.0)
                qty = float(trade.qty_open or trade.qty or 0.0)
                tape = self.market.get_live_tape(trade.symbol)
                exit_p = float(tape.last_price) if tape and tape.last_price > 0 else entry
                if entry > 0 and qty > 0:
                    if trade.side == TradeSide.LONG.value:
                        pnl_abs = (exit_p - entry) * qty
                    else:
                        pnl_abs = (entry - exit_p) * qty
                    notional = entry * qty
                    pnl_pct = (pnl_abs / notional * 100.0) if notional > 0 else 0.0
                    self.notifier.notify_closed(
                        trade.symbol,
                        qty,
                        entry,
                        exit_p,
                        pnl_abs,
                        pnl_pct,
                        "broker_stop_or_fill",
                        self.settings.dry_run,
                        event_id=f"cn-broker-close|{trade.symbol}|{trade.entry_order_id}",
                    )
                    register_trade_result(
                        self.risk,
                        self.limits,
                        (pnl_pct / 100.0) if notional > 0 else 0.0,
                    )
                    self._save_risk_state()
                    if self.risk.halted and self.risk.halt_reason == CIRCUIT_BREAKER_HALT_REASON:
                        logger.warning(
                            "Circuit breaker activo | %s pérdidas seguidas — sin nuevas entradas",
                            self.risk.consecutive_losses,
                        )
                trade.status = "closed"
                trade.qty_open = 0.0
                logger.info("%s | posicion cerrada en broker (stop/fill)", trade.symbol)
                continue
            tape = self.market.get_live_tape(trade.symbol)
            if not tape or tape.last_price <= 0:
                continue
            px = float(tape.last_price)
            trade.best_r = max(trade.best_r, current_r(trade, px))
            decision = evaluate_exit(
                trade,
                px,
                now,
                scale_at_1r=self.settings.scale_at_1r,
                breakeven_activate_pct=self.settings.breakeven_activate_pct,
                breakeven_buffer_pct=self.settings.breakeven_buffer_pct,
                trail_offset_pct=self.settings.max_stop_pct,
                compression_trail_atr_mult=self.settings.compression_trail_atr_mult,
                compression_max_runner_pct=self.settings.compression_max_runner_pct,
            )
            if decision.action == "hold":
                continue
            self._apply_exit_decision(trade, decision, px)
        save_trades(self._trades_path, trades)

    def _sync_pending_entry(self, trade: NightTradeRecord) -> bool:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopOrderRequest

        try:
            order = self.client.trading.get_order_by_id(trade.entry_order_id)
        except Exception as exc:
            logger.debug("%s | pending sync: %s", trade.symbol, exc)
            return False
        status = getattr(order, "status", None)
        status_s = str(status).lower() if status is not None else ""
        if "canceled" in status_s or "expired" in status_s or "rejected" in status_s:
            trade.status = "closed"
            logger.info("%s | entrada cancelada/rechazada | %s", trade.symbol, status_s)
            return True
        if "filled" not in status_s and "partially_filled" not in status_s:
            return False
        fill_px = float(getattr(order, "filled_avg_price", 0) or trade.meta.get("limit_price", 0))
        filled_qty = float(getattr(order, "filled_qty", 0) or trade.qty)
        if fill_px <= 0 or filled_qty <= 0:
            return False
        trade.entry_price = fill_px
        trade.qty = filled_qty
        trade.qty_open = filled_qty
        trade.entry_time_utc = datetime.now(timezone.utc).isoformat()
        trade.status = "open"
        compression = bool(trade.meta.get("compression_mode"))
        if compression:
            sp, stop_pct, partial_tp_pct = apply_compression_protective(
                entry_price=fill_px,
                stop_price=float(trade.stop_price),
                side=trade.side,
                max_stop_pct=self.settings.compression_max_stop_pct,
                partial_tp_pct=self.settings.tp_target_pct,
            )
            trade.stop_price = sp
            trade.runner_stop = sp
            trade.take_profit_price = 0.0
            trade.meta["compression_partial_tp_pct"] = partial_tp_pct
            atr_pct = float(trade.meta.get("atr_pct") or 0.0)
            trade.meta["atr_abs"] = fill_px * (atr_pct / 100.0) if atr_pct > 0 else float(
                trade.meta.get("atr_abs") or 0.0
            )
            tp_pct = partial_tp_pct
            logger.info(
                "%s | fill compresión asimétrica | stop @ %.4f (-%.2f%%) | "
                "parcial software +%.2f%% | trail ATR x%.1f | sin TP broker",
                trade.symbol,
                trade.stop_price,
                stop_pct * 100,
                partial_tp_pct * 100,
                self.settings.compression_trail_atr_mult,
            )
        else:
            sp, tp_px, stop_pct, tp_pct = apply_crypto_night_protective(
                entry_price=fill_px,
                stop_price=float(trade.stop_price),
                side=trade.side,
                reward_risk=self.settings.tp_reward_risk,
                max_stop_pct=self.settings.max_stop_pct,
                min_tp_pct=self.settings.min_tp_pct,
                max_tp_pct=self.settings.max_tp_pct,
                target_tp_pct=self.settings.tp_target_pct,
            )
            trade.stop_price = sp
            trade.runner_stop = sp
            trade.take_profit_price = round_crypto_price(tp_px, ref=fill_px)
            if trade.take_profit_price <= 0:
                logger.warning("%s | TP asimétrico inválido — no se abre stop/TP broker", trade.symbol)
                trade.status = "closed"
                return True
        if not trade.meta.get("counted_night"):
            self.risk.trades_tonight += 1
            trade.meta["counted_night"] = True
            self._save_risk_state()
        stop_side = OrderSide.SELL if trade.side == TradeSide.LONG.value else OrderSide.BUY
        try:
            stop_order = self.client.trading.submit_order(
                order_data=StopOrderRequest(
                    symbol=trade.symbol,
                    qty=round(trade.qty_open, 6),
                    side=stop_side,
                    stop_price=round(float(trade.stop_price), 2),
                    time_in_force=TimeInForce.GTC,
                )
            )
            trade.stop_order_id = str(getattr(stop_order, "id", "") or "")
            if not compression:
                self._submit_take_profit_limit(trade)
            logger.info(
                "%s | entrada fill @ %.4f | stop @ %.4f (-%.2f%%) | TP @ %.4f (+%.2f%%) id=%s",
                trade.symbol,
                fill_px,
                trade.stop_price,
                stop_pct * 100,
                trade.take_profit_price,
                tp_pct * 100,
                trade.take_profit_order_id or ("compresión/trail" if compression else "—"),
            )
            side = "buy" if trade.side == TradeSide.LONG.value else "sell"
            self.notifier.notify_opened(
                trade.symbol,
                side,
                filled_qty,
                fill_px,
                "crypto night fill confirmado",
                self.settings.dry_run,
                stop_price=float(trade.stop_price),
                take_profit_price=float(trade.take_profit_price),
                event_id=f"cn-fill|{trade.symbol}|{trade.entry_order_id}",
            )
        except Exception as exc:
            logger.warning("%s | stop broker fallo (gestion software): %s", trade.symbol, exc)
        return True

    def _apply_exit_decision(
        self, trade: NightTradeRecord, decision: ExitDecision, last_price: float
    ) -> bool:
        if decision.action == "update_stop" and decision.new_runner_stop is not None:
            trade.runner_stop = float(decision.new_runner_stop)
            self._replace_stop_order(trade)
            logger.info(
                "%s | trail | runner_stop=%.4f (%s)",
                trade.symbol,
                trade.runner_stop,
                decision.reason,
            )
            return True
        if decision.action == "partial_1r":
            close_qty = round(min(decision.close_qty, trade.qty_open), 6)
            if close_qty <= 0:
                return False
            self._cancel_stop_order(trade)
            self._cancel_take_profit_order(trade)
            self._market_close(trade, close_qty, decision.reason)
            trade.qty_open = round(trade.qty_open - close_qty, 6)
            trade.partial_taken = True
            if decision.new_runner_stop is not None:
                trade.runner_stop = float(decision.new_runner_stop)
            register_trade_result(self.risk, self.limits, 0.5 * 1.0 * self.limits.risk_normal_pct)
            self._save_risk_state()
            if trade.qty_open > 0:
                self._replace_stop_order(trade)
            else:
                trade.status = "closed"
            logger.info(
                "%s | parcial 1R | qty_rest=%s runner=%.4f",
                trade.symbol,
                trade.qty_open,
                trade.runner_stop,
            )
            entry = float(trade.entry_price or 0.0)
            if entry > 0:
                pnl_abs = (
                    (last_price - entry) * close_qty
                    if trade.side == TradeSide.LONG.value
                    else (entry - last_price) * close_qty
                )
                notional = entry * close_qty
                pnl_pct = (pnl_abs / notional * 100.0) if notional > 0 else 0.0
                self.notifier.notify_partial_close(
                    trade.symbol,
                    close_qty,
                    trade.qty_open,
                    entry,
                    last_price,
                    pnl_abs,
                    pnl_pct,
                    decision.reason or "partial_1r",
                    "filled",
                    close_qty,
                    self.settings.dry_run,
                    event_id=f"cn-partial|{trade.symbol}|{int(time.time())}",
                )
            return True
        if decision.action == "close_all":
            qty = round(trade.qty_open, 6)
            if qty <= 0:
                trade.status = "closed"
                return True
            self._cancel_stop_order(trade)
            self._cancel_take_profit_order(trade)
            self._close_position(trade, qty, decision.reason, last_price=last_price)
            return True
        return False

    def _cancel_stop_order(self, trade: NightTradeRecord) -> None:
        if not trade.stop_order_id:
            return
        try:
            self.client.trading.cancel_order_by_id(trade.stop_order_id)
        except Exception:
            pass
        trade.stop_order_id = ""

    def _cancel_take_profit_order(self, trade: NightTradeRecord) -> None:
        if not trade.take_profit_order_id:
            return
        try:
            self.client.trading.cancel_order_by_id(trade.take_profit_order_id)
        except Exception:
            pass
        trade.take_profit_order_id = ""

    def _submit_take_profit_limit(self, trade: NightTradeRecord) -> None:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        if trade.qty_open <= 0 or trade.take_profit_price <= 0:
            return
        side = (
            OrderSide.SELL
            if trade.side == TradeSide.LONG.value
            else OrderSide.BUY
        )
        try:
            tp_order = self.client.trading.submit_order(
                order_data=LimitOrderRequest(
                    symbol=trade.symbol,
                    qty=round(trade.qty_open, 6),
                    side=side,
                    limit_price=trade.take_profit_price,
                    time_in_force=TimeInForce.GTC,
                )
            )
            trade.take_profit_order_id = str(getattr(tp_order, "id", "") or "")
        except Exception as exc:
            logger.warning(
                "%s | limit TP broker fallo (software TP @ %.4f): %s",
                trade.symbol,
                trade.take_profit_price,
                exc,
            )

    def _replace_stop_order(self, trade: NightTradeRecord) -> None:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopOrderRequest

        if trade.qty_open <= 0:
            return
        stop_side = OrderSide.SELL if trade.side == TradeSide.LONG.value else OrderSide.BUY
        try:
            stop_order = self.client.trading.submit_order(
                order_data=StopOrderRequest(
                    symbol=trade.symbol,
                    qty=round(trade.qty_open, 6),
                    side=stop_side,
                    stop_price=round(float(trade.runner_stop), 2),
                    time_in_force=TimeInForce.GTC,
                )
            )
            trade.stop_order_id = str(getattr(stop_order, "id", "") or "")
        except Exception as exc:
            logger.warning("%s | re-stop fallo: %s", trade.symbol, exc)

    def _market_close(self, trade: NightTradeRecord, qty: float, reason: str) -> None:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        close_side = OrderSide.SELL if trade.side == TradeSide.LONG.value else OrderSide.BUY
        try:
            self.client.trading.submit_order(
                order_data=MarketOrderRequest(
                    symbol=trade.symbol,
                    qty=round(qty, 6),
                    side=close_side,
                    time_in_force=TimeInForce.GTC,
                )
            )
            logger.info("%s | cierre mercado qty=%s | %s", trade.symbol, qty, reason)
        except Exception as exc:
            logger.warning("%s | cierre fallo: %s", trade.symbol, exc)

    def _close_position(
        self,
        trade: NightTradeRecord,
        qty: float,
        reason: str,
        *,
        last_price: float | None = None,
    ) -> None:
        self._market_close(trade, qty, reason)
        pnl_frac = 0.0
        if last_price is not None and trade.entry_price:
            entry_px = float(trade.entry_price)
            if trade.side == TradeSide.LONG.value:
                pnl_frac = (last_price - entry_px) / entry_px if entry_px > 0 else 0.0
            else:
                pnl_frac = (entry_px - last_price) / entry_px if entry_px > 0 else 0.0
        register_trade_result(self.risk, self.limits, pnl_frac)
        self._save_risk_state()
        if self.risk.halted and self.risk.halt_reason == CIRCUIT_BREAKER_HALT_REASON:
            logger.warning(
                "Circuit breaker activo | %s pérdidas seguidas — sin nuevas entradas",
                self.risk.consecutive_losses,
            )
        trade.qty_open = 0.0
        trade.status = "closed"
        entry = float(trade.entry_price or 0.0)
        exit_p = float(last_price if last_price is not None else entry)
        if trade.side == TradeSide.LONG.value:
            pnl_abs = (exit_p - entry) * qty
        else:
            pnl_abs = (entry - exit_p) * qty
        notional = entry * qty if entry > 0 else 1.0
        pnl_pct = (pnl_abs / notional) * 100.0
        self.notifier.notify_closed(
            trade.symbol,
            qty,
            entry,
            exit_p,
            pnl_abs,
            pnl_pct,
            reason,
            self.settings.dry_run,
        )

    def _apply_setup_protective(self, setup, *, compression: bool = False) -> None:
        side = setup.side.value if hasattr(setup.side, "value") else str(setup.side)
        if compression:
            sp, _, _ = apply_compression_protective(
                entry_price=float(setup.limit_price),
                stop_price=float(setup.stop_price),
                side=side,
                max_stop_pct=self.settings.compression_max_stop_pct,
                partial_tp_pct=self.settings.tp_target_pct,
            )
        else:
            sp, _, _, _ = apply_crypto_night_protective(
                entry_price=float(setup.limit_price),
                stop_price=float(setup.stop_price),
                side=side,
                reward_risk=self.settings.tp_reward_risk,
                max_stop_pct=self.settings.max_stop_pct,
                min_tp_pct=self.settings.min_tp_pct,
                max_tp_pct=self.settings.max_tp_pct,
                target_tp_pct=self.settings.tp_target_pct,
            )
        setup.stop_price = sp

    def _maybe_place_limit(self, symbol: str, setup, bias, *, compression: bool = False) -> None:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        cid = f"{self.settings.client_order_prefix}{uuid.uuid4().hex[:12]}"
        side = OrderSide.BUY if bias.side.value == "long" else OrderSide.SELL
        risk_dist = abs(float(setup.limit_price) - float(setup.stop_price))
        if risk_dist <= 0:
            logger.info("%s | sin trade | stop inválido", symbol)
            return

        if self.settings.dry_run:
            logger.info(
                "DRY_RUN | %s %s limit=%.4f stop=%.4f variant=%s",
                symbol,
                bias.side.value,
                setup.limit_price,
                setup.stop_price,
                setup.variant,
            )
            return

        try:
            account = self.client.snapshot_account()
        except Exception as exc:
            logger.warning("%s | sin trade | cuenta: %s", symbol, exc)
            return
        risk_cash = float(account.equity) * self.limits.risk_normal_pct
        qty = round(risk_cash / risk_dist, 6)
        if qty <= 0:
            logger.info("%s | sin trade | qty=0 equity=%.2f", symbol, account.equity)
            return

        mode_tag = "compresion_asimetrica" if compression else "normal"
        logger.info(
            "Orden limite | client_order_id=%s | %s %s qty=%s @ %.4f SL ref=%.4f variant=%s | %s",
            cid,
            side.value,
            symbol,
            qty,
            setup.limit_price,
            setup.stop_price,
            setup.variant,
            mode_tag,
        )
        try:
            order = self.client.trading.submit_order(
                order_data=LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=side,
                    time_in_force=TimeInForce.GTC,
                    limit_price=round(float(setup.limit_price), 2),
                    client_order_id=cid[:48],
                )
            )
            oid = str(getattr(order, "id", "") or "")
            trades = load_trades(self._trades_path)
            trades.append(
                NightTradeRecord(
                    symbol=symbol,
                    side=bias.side.value,
                    variant=setup.variant,
                    qty=qty,
                    qty_open=qty,
                    entry_order_id=oid,
                    stop_price=float(setup.stop_price),
                    runner_stop=float(setup.stop_price),
                    status="pending",
                    meta={
                        "limit_price": float(setup.limit_price),
                        "client_id": cid,
                        "compression_mode": compression,
                        "atr_pct": float(setup.meta.get("atr_pct") or 0.0),
                    },
                )
            )
            save_trades(self._trades_path, trades)
            from bot.notify.trade_alert_feed import append_trade_alert

            side_label = "COMPRA" if side.value == "buy" else "VENTA"
            append_trade_alert(
                source=self.settings.telegram_prefix or "[CN]",
                kind="pending",
                symbol=symbol,
                headline=f"Orden limite {side_label} {symbol}",
                body=(
                    f"Limit @ {setup.limit_price:.4f} qty={qty:g} "
                    f"SL ref {setup.stop_price:.4f} variant={setup.variant}"
                ),
                event_id=f"cn-pending|{symbol}|{cid}",
            )
            logger.info("%s | orden enviada | broker_id=%s", symbol, oid)
        except Exception as exc:
            logger.warning("%s | orden rechazada | %s", symbol, exc)

    def run_loop(self) -> None:
        self._load_risk_state()
        logger.info(
            "Crypto Night Fortress | variant=%s | symbols=%s | paper=%s | dry_run=%s",
            self.settings.sweep_variant.value,
            ",".join(self.settings.symbols),
            self.settings.paper,
            self.settings.dry_run,
        )
        flt = self.settings.filters
        bias_label = "4H+1D" if self.settings.bias_mode == "4h_1d" else "4H only"
        if flt.bias_relaxed_structure:
            bias_label = f"{bias_label} (estructura floja)"
        if flt.eth_inherit_btc_bias_only:
            bias_label = f"{bias_label} | ETH hereda BTC"
        delay_note = (
            f" | entradas desde +{flt.entry_delay_minutes_after_us_close}min post-cierre"
            if flt.entry_delay_minutes_after_us_close > 0
            else ""
        )
        v2_note = " + fallback V2" if flt.setup_try_v2_fallback else ""
        session_note = (
            "24/7 (always)"
            if self.settings.session_mode in {"always", "24_7", "247", "continuous"}
            else "US off→open ET"
        )
        risk_note = (
            "sin tope trades/kill auto"
            if not self.limits.limits_enabled
            else f"max {self.limits.max_trades_night}/noche kill -1%/-3%"
        )
        flat_note = (
            "sin flat 09:30"
            if self.settings.session_mode in {"always", "24_7", "247", "continuous"}
            else "flat 09:30 ET"
        )
        logger.info(
            "Candados activos | perfil=%s%s | (1) sesion %s | "
            "(2) ATR%% 1H p%.0f-%.0f | (3) bias %s | (4) setup %s vol>=%.1fx%s | "
            "(5) score>=%s/5 R net>=%.1f | riesgo: %.2f%%/trade | TP asim 1:%.1f | %s | %s",
            self.settings.filter_profile,
            delay_note,
            session_note,
            flt.vol_pct_low,
            flt.vol_pct_high,
            bias_label,
            self.settings.sweep_variant.value,
            flt.setup_min_volume_ratio,
            v2_note,
            flt.quality_min_score,
            flt.quality_min_theoretical_r,
            self.limits.risk_normal_pct * 100,
            self.settings.tp_reward_risk,
            risk_note,
            flat_note,
        )
        logger.info(
            "Calibracion | ventana sesion %sh | vol setup>=%.2fx | R net>=%.1f | "
            "circuit breaker %s perdidas seguidas (reset dia ET)",
            SESSION_LOOKBACK_HOURS,
            flt.setup_min_volume_ratio,
            flt.quality_min_theoretical_r,
            CIRCUIT_BREAKER_CONSECUTIVE_LOSSES,
        )
        if self.settings.asymmetric_low_vol_mode:
            logger.info(
                "Compresion asimetrica | ON | SL max %.2f%% | parcial +%.2f%% | "
                "trail ATR x%.1f | runner max +%.2f%% | candado ATR alto sigue activo",
                self.settings.compression_max_stop_pct * 100,
                self.settings.tp_target_pct * 100,
                self.settings.compression_trail_atr_mult,
                self.settings.compression_max_runner_pct * 100,
            )
        else:
            logger.info(
                "Compresion asimetrica | OFF — solo banda ATR normal (CRYPTO_NIGHT_ASYMMETRIC_LOW_VOL_MODE)"
            )
        logger.info(
            "Salidas live | SL max %.2f%% | TP fijo %.2f–%.2f%% (obj %.2f%%) | "
            "BE en entrada +%.2f%% | scale@1R=%s | time 3h | %s | libro %s",
            self.settings.max_stop_pct * 100,
            self.settings.min_tp_pct * 100,
            self.settings.max_tp_pct * 100,
            self.settings.tp_target_pct * 100,
            self.settings.breakeven_activate_pct * 100,
            self.settings.scale_at_1r,
            flat_note,
            self._trades_path.name,
        )
        while not self._shutdown:
            try:
                self.run_once()
            except Exception as exc:
                logger.warning("Tick error: %s", type(exc).__name__)
            time.sleep(max(15, self.settings.poll_seconds))
