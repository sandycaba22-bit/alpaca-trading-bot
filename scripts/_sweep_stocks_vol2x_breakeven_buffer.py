"""Sweep breakeven buffer ATR — vol_2x acciones en producción (research).

IS 2020-01-01 -> 2025-01-01 | OOS 2025-01-01 -> hoy (sweep 335031).

  .venv\\Scripts\\python.exe -u scripts\\_sweep_stocks_vol2x_breakeven_buffer.py
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from bot.alpaca.client import AlpacaClient
from bot.alpaca.market_data import MarketDataService
from bot.config import PROJECT_ROOT, load_settings

from _stocks_asymmetric_backtest_lib import (
    CONFIRM_TF,
    ENTRY_TF,
    EntryVariant,
    OOS_START,
    load_bars,
    pf_str,
    policy_asymmetric,
    precompute_entries,
    simulate_trades,
    summarize_trades,
)

from _research_universe import LIQUID_STOCK_SYMBOLS  # noqa: E402

STOCK_IS_START = pd.Timestamp("2020-01-01", tz="UTC")

STOCK_SYMBOLS = LIQUID_STOCK_SYMBOLS
VOL2X = EntryVariant(name="vol_2x", volume_mult=2.0, volume_period=20, sma_slow=50)
STOCK_FEE = 0.0
STOCK_SLIP = 0.03
OUT_CSV = PROJECT_ROOT / "logs" / "stocks_vol2x_be_buffer_sweep.csv"
OUT_TXT = PROJECT_ROOT / "logs" / "stocks_vol2x_be_buffer_sweep_summary.txt"


def _buffer_variants(settings) -> list[tuple[str, float | None]]:
    baseline = float(settings.breakeven_buffer_atr_mult)
    return [
        (f"baseline_{baseline:.2f}xATR", None),
        ("buffer_0.50xATR", 0.50),
        ("buffer_0.75xATR", 0.75),
        ("buffer_1.00xATR", 1.00),
    ]


def main() -> int:
    end = pd.Timestamp(datetime.now(timezone.utc))
    settings = load_settings()
    client = AlpacaClient(settings)
    market = MarketDataService(client)
    sma_map = {s: 50 for s in STOCK_SYMBOLS}
    buffers = _buffer_variants(settings)

    print("=" * 96, flush=True)
    print("SWEEP vol_2x breakeven buffer | salidas asim 1.2/2.75", flush=True)
    print(f"IS {STOCK_IS_START.date()} -> {OOS_START.date()} | OOS -> {end.date()}", flush=True)
    print("=" * 96, flush=True)

    rows: list[dict] = []
    start_dt = STOCK_IS_START.to_pydatetime()
    end_dt = end.to_pydatetime()

    for symbol in STOCK_SYMBOLS:
        print(f"\n>> {symbol}", flush=True)
        bars = load_bars(market, symbol, ENTRY_TF, start_dt, end_dt)
        htf = load_bars(market, symbol, CONFIRM_TF, start_dt, end_dt)
        if bars.empty or len(bars) < 500:
            print(f"  SKIP datos insuficientes", flush=True)
            continue
        entries = precompute_entries(
            settings,
            symbol,
            bars,
            htf,
            sma_map,
            variant=VOL2X,
            exit_policy=policy_asymmetric(settings),
            quiet=True,
        )
        is_end = OOS_START - pd.Timedelta(minutes=5)
        cash0 = float(settings.backtest_cash)
        for buf_name, buf_mult in buffers:
            pol = policy_asymmetric(settings, breakeven_buffer_atr_mult=buf_mult)
            is_trades = simulate_trades(
                settings,
                bars,
                entries,
                pol,
                symbol=symbol,
                fee_pct=STOCK_FEE,
                slippage_pct=STOCK_SLIP,
                period_start=STOCK_IS_START,
                period_end=is_end,
            )
            oos_trades = simulate_trades(
                settings,
                bars,
                entries,
                pol,
                symbol=symbol,
                fee_pct=STOCK_FEE,
                slippage_pct=STOCK_SLIP,
                period_start=OOS_START,
                period_end=end,
            )
            m_is = summarize_trades(is_trades, cash0)
            m_oos = summarize_trades(oos_trades, cash0)
            for scope, m in (("IS", m_is), ("OOS", m_oos)):
                tr = int(m.get("trades", 0))
                win = float(m.get("win_rate_pct", 0))
                pf = float(m.get("profit_factor", 0))
                net = float(m.get("total_return_net_pct", 0))
                print(
                    f"  {buf_name:<22} {scope:<4} | tr={tr:>4} | win%={win:.1f} | PF={pf_str(pf)} | net%={net:+.2f}",
                    flush=True,
                )
                rows.append(
                    {
                        "symbol": symbol,
                        "buffer": buf_name,
                        "buffer_atr_mult": buf_mult if buf_mult is not None else settings.breakeven_buffer_atr_mult,
                        "scope": scope,
                        "trades": tr,
                        "win_pct": round(win, 2),
                        "pf": round(pf, 4),
                        "net_pct": round(net, 4),
                    }
                )

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "symbol",
                "buffer",
                "buffer_atr_mult",
                "scope",
                "trades",
                "win_pct",
                "pf",
                "net_pct",
            ],
        )
        w.writeheader()
        w.writerows(rows)

    lines = [
        "=== Sweep vol_2x breakeven buffer (7 símbolos prod) ===",
        f"Generated UTC: {datetime.now(timezone.utc).isoformat()}",
        "",
    ]
    for buf_name, _ in buffers:
        lines.append(f"--- {buf_name} ---")
        oos_ok = 0
        for sym in STOCK_SYMBOLS:
            match = [r for r in rows if r["symbol"] == sym and r["buffer"] == buf_name and r["scope"] == "OOS"]
            if not match:
                continue
            r = match[0]
            ok = r["trades"] > 0 and r["pf"] > 1.0 and r["net_pct"] > 0
            if ok:
                oos_ok += 1
            lines.append(
                f"  {sym:<6} OOS tr={r['trades']:>4} win%={r['win_pct']:>5.1f} PF={r['pf']:>5.2f} net%={r['net_pct']:>+7.2f}"
                + (" *" if ok else "")
            )
        lines.append(f"  OOS PF>1 & net>0: {oos_ok}/{len(STOCK_SYMBOLS)}\n")

    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWrote {OUT_CSV}", flush=True)
    print(f"Wrote {OUT_TXT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
