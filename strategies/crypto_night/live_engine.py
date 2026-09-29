"""Motor paper/live mínimo — no usa TradingEngine stocks."""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd

from bot.alpaca.client import AlpacaClient
from bot.alpaca.market_data import MarketDataService
from bot.crypto_night_settings import CryptoNightSettings
from bot.notify.telegram import TelegramNotifier
from strategies.crypto_night.bias import resolve_night_bias
from strategies.crypto_night.quality import score_setup
from strategies.crypto_night.risk import NightRiskState, RiskLimits, can_open_trade
from strategies.crypto_night.session import entries_allowed, in_night_trading_window
from strategies.crypto_night.variants import find_setup_for_variant
from strategies.crypto_night.volatility import volatility_gate

logger = logging.getLogger("crypto_night")


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
        self.limits = RiskLimits()
        self._shutdown = False
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = settings.data_dir / "crypto_night_state.json"
        self._kill_path = settings.data_dir / "crypto_night_kill_switch.json"

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
            "updated_utc": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("No se pudo guardar estado riesgo: %s", exc)

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

    def run_once(self) -> None:
        if self._kill_switch_active():
            logger.info("Kill switch crypto_night activo — sin entradas")
            return
        now = datetime.now(timezone.utc)
        if not in_night_trading_window(now):
            return
        if not entries_allowed(now):
            return

        variant = self.settings.sweep_variant
        btc_bias = None
        for symbol in self.settings.symbols:
            bars_15m = self._load_bars(symbol, "15Min", 400)
            bars_1h = self._load_bars(symbol, "1Hour", 600)
            bars_4h = self._load_bars(symbol, "4Hour", 400)
            bars_1d = self._load_bars(symbol, "1Day", 120)
            if bars_15m.empty or bars_1h.empty:
                continue
            ts = bars_15m.index[-1]
            vgate, atr_now, _ = volatility_gate(bars_1h, ts)
            if not vgate.ok:
                logger.info("%s | candado vol | %s", symbol, vgate.detail)
                continue
            bgate, bias = resolve_night_bias(
                bars_4h, bars_1d, symbol, btc_bias=btc_bias
            )
            if not bgate.ok or bias is None:
                logger.info("%s | candado bias | %s", symbol, bgate.detail)
                continue
            if symbol.startswith("BTC"):
                btc_bias = bias
            vol_ma = bars_15m["volume"].astype(float).rolling(20).mean()
            session_start = ts - pd.Timedelta(hours=8)
            setup = find_setup_for_variant(
                variant,
                bars_15m=bars_15m,
                bars_1d=bars_1d,
                session_start=session_start,
                at_ts=ts,
                bias=bias,
                vol_ma=vol_ma,
            )
            if setup is None:
                continue
            setup.symbol = symbol
            setup.variant = variant.value
            if len(bars_1h) > 14:
                setup.atr_1h = float(
                    bars_1h["close"].astype(float).diff().abs().rolling(14).mean().iloc[-1]
                )
            open_crypto = self._open_crypto_positions()
            if len(open_crypto) >= self.settings.max_positions:
                logger.info(
                    "Sin trade | max_positions=%s (abiertas=%s)",
                    self.settings.max_positions,
                    ",".join(open_crypto) or "0",
                )
                return
            tape = self.market.get_live_tape(symbol)
            spread = tape.spread_pct if tape else None
            q = score_setup(setup, spread_pct=spread, atr_pct=atr_now, mode_min=4)
            if not q.ok:
                logger.info("%s | candado calidad | score=%s", symbol, q.total)
                continue
            ok, reason = can_open_trade(self.risk, self.limits, mode="normal")
            if not ok:
                logger.info("Sin trade | %s", reason)
                return
            self._maybe_place_limit(symbol, setup, bias)
            return

    def _maybe_place_limit(self, symbol: str, setup, bias) -> None:
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

        logger.info(
            "Orden limite | client_order_id=%s | %s %s qty=%s @ %.4f SL ref=%.4f variant=%s",
            cid,
            side.value,
            symbol,
            qty,
            setup.limit_price,
            setup.stop_price,
            setup.variant,
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
            self.risk.trades_tonight += 1
            self._save_risk_state()
            oid = getattr(order, "id", None)
            msg = (
                f"{setup.variant} limit {setup.limit_price:.2f} qty={qty} "
                f"SL ref {setup.stop_price:.2f} id={cid}"
            )
            if self.notifier.enabled:
                self.notifier.notify_opened(
                    symbol,
                    side.value,
                    qty,
                    setup.limit_price,
                    msg,
                    self.settings.dry_run,
                    stop_price=setup.stop_price,
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
        logger.info(
            "Candados activos | (1) sesion US off→open ET | (2) ATR%% 1H p30-70 | "
            "(3) bias 4H+1D | (4) setup variante %s | (5) score>=%s/5 R net>=1.8 | "
            "riesgo: %.2f%%/trade max %s/noche kill -1%% noche -3%% semana",
            self.settings.sweep_variant.value,
            4,
            self.limits.risk_normal_pct * 100,
            self.limits.max_trades_night,
        )
        while not self._shutdown:
            try:
                self.run_once()
            except Exception as exc:
                logger.warning("Tick error: %s", type(exc).__name__)
            time.sleep(max(15, self.settings.poll_seconds))
