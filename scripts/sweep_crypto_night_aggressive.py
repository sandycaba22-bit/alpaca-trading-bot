#!/usr/bin/env python3
"""Compara perfiles crypto night + TP asimétrico (strict / relaxed / aggressive).

  python -u scripts/sweep_crypto_night_aggressive.py

Salida: logs/crypto_night_aggressive_sweep.csv
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from bot.config import PROJECT_ROOT
from strategies.crypto_night.backtest import run_variant_backtest
from strategies.crypto_night.filter_profile import AGGRESSIVE, RELAXED, STRICT, filters_for_profile
from strategies.crypto_night.risk import RiskLimits
from strategies.crypto_night.variants import SweepVariant

OUT = PROJECT_ROOT / "logs" / "crypto_night_aggressive_sweep.csv"
OOS = pd.Timestamp("2025-01-01", tz="UTC")


def _synthetic_bars(n: int = 2000, seed: int = 42) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Barras mínimas para smoke-test del pipeline (no sustituye Alpaca)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-06-01", periods=n, freq="15min", tz="UTC")
    px = 50000 + np.cumsum(rng.normal(0, 80, n))
    df15 = pd.DataFrame(
        {
            "open": px,
            "high": px + rng.uniform(20, 120, n),
            "low": px - rng.uniform(20, 120, n),
            "close": px + rng.normal(0, 30, n),
            "volume": rng.uniform(100, 5000, n),
        },
        index=idx,
    )
    df1h = df15.resample("1h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    df4h = df15.resample("4h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    df1d = df15.resample("1D").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    return df15, df1h, df4h, df1d


def _run_profile(name: str, flt, bars) -> dict:
    b15, b1h, b4, b1d = bars
    lim = RiskLimits(limits_enabled=False, max_trades_night=0)
    st = run_variant_backtest(
        SweepVariant.V1,
        symbol="BTC/USD",
        bars_15m=b15,
        bars_1h=b1h,
        bars_4h=b4,
        bars_1d=b1d,
        oos_start=OOS,
        filters=flt,
        limits=lim,
        tp_reward_risk=2.5,
        scale_at_1r=False,
    )
    wr = (st.wins / st.trades * 100) if st.trades else 0.0
    avg_r = (st.r_net_sum / st.trades) if st.trades else 0.0
    return {
        "profile": name,
        "signals": st.signals,
        "trades": st.trades,
        "win_rate_pct": round(wr, 2),
        "avg_r_net": round(avg_r, 3),
        "r_net_sum": round(st.r_net_sum, 3),
        "reject_vol": st.rejections.get("volatility", 0),
        "reject_bias": st.rejections.get("bias", 0),
        "reject_entry": st.rejections.get("entry", 0),
        "reject_quality": st.rejections.get("quality", 0),
        "reject_risk": st.rejections.get("risk", 0),
        "tp_model": "broker_limit+software_2.5R",
    }


def _try_alpaca() -> tuple | None:
    try:
        from bot.alpaca.client import AlpacaClient
        from bot.alpaca.market_data import MarketDataService
        from bot.crypto_night_settings import load_crypto_night_settings

        settings = load_crypto_night_settings()
        client = AlpacaClient(
            type("C", (), {
                "api_key_id": settings.api_key_id,
                "api_secret_key": settings.api_secret_key,
                "api_base_url": settings.api_base_url,
                "paper": settings.paper,
                "api_data_per_minute": 120,
                "order_per_minute": 30,
                "order_per_day": 200,
            })()
        )
        m = MarketDataService(client)
        end = datetime.now(timezone.utc)
        start = end - pd.Timedelta(days=400)
        b15 = m.get_bars_range("BTC/USD", "15Min", start=start, end=end)
        b1h = m.get_bars_range("BTC/USD", "1Hour", start=start, end=end)
        b4 = m.get_bars_range("BTC/USD", "4Hour", start=start, end=end)
        b1d = m.get_bars_range("BTC/USD", "1Day", start=start, end=end)
        if b15.empty:
            return None
        return b15, b1h, b4, b1d
    except Exception as exc:
        print(f"Alpaca no disponible ({exc}) — uso barras sintéticas", flush=True)
        return None


def main() -> int:
    print("=== Crypto Night sweep strict / relaxed / aggressive ===", flush=True)
    bars = _try_alpaca()
    if bars is None:
        bars = _synthetic_bars()
        print("Datos: sintéticos (correr en VPS con .env.crypto_night para BTC real)", flush=True)
    else:
        print("Datos: BTC/USD Alpaca", flush=True)

    rows = [
        _run_profile("strict", STRICT, bars),
        _run_profile("relaxed", RELAXED, bars),
        _run_profile("aggressive", AGGRESSIVE, bars),
    ]
    for r in rows:
        print(
            f"{r['profile']:10} signals={r['signals']} trades={r['trades']} "
            f"avg_R={r['avg_r_net']} rej(entry/qual)={r['reject_entry']}/{r['reject_quality']}",
            flush=True,
        )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"CSV: {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
