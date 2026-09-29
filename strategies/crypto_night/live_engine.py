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
            logger.debug("Fuera de ventana nocturna")
            return
        if not entries_allowed(now):
            logger.debug("Corte pre-apertura / sesion US")
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
        cid = f"{self.settings.client_order_prefix}{uuid.uuid4().hex[:12]}"
        logger.info(
            "Orden limite paper | client_order_id=%s | %s | variant=%s",
            cid,
            symbol,
            setup.variant,
        )
        self.notifier.notify_signal_filtered(
            symbol,
            bias.side.value,
            f"{setup.variant} limit~{setup.limit_price:.2f} SL~{setup.stop_price:.2f} id={cid}",
        )

    def run_loop(self) -> None:
        logger.info(
            "Crypto Night Fortress | variant=%s | symbols=%s | paper=%s",
            self.settings.sweep_variant.value,
            ",".join(self.settings.symbols),
            self.settings.paper,
        )
        while not self._shutdown:
            try:
                self.run_once()
            except Exception as exc:
                logger.warning("Tick error: %s", type(exc).__name__)
            time.sleep(max(15, self.settings.poll_seconds))
