"""Universo de símbolos: bot élite (live) vs Top 50 (bot secundario / research)."""

from __future__ import annotations

# Bot principal (live) — vol_2x OOS-validados; sin MSFT, AAPL, QQQ
ELITE_STOCK_SYMBOLS: tuple[str, ...] = (
    "SLV",
    "TSLA",
    "GOOGL",
    "META",
    "NVDA",
    "SPY",
)

# Bot secundario + sweep research — 50 large caps US (Alpaca)
TOP50_US_STOCK_SYMBOLS: tuple[str, ...] = (
    "AAPL",
    "MSFT",
    "GOOGL",
    "AMZN",
    "NVDA",
    "META",
    "TSLA",
    "BRK.B",
    "UNH",
    "JNJ",
    "XOM",
    "JPM",
    "V",
    "PG",
    "MA",
    "HD",
    "CVX",
    "MRK",
    "LLY",
    "ABBV",
    "PEP",
    "KO",
    "COST",
    "AVGO",
    "WMT",
    "MCD",
    "CSCO",
    "TMO",
    "ACN",
    "ABT",
    "DHR",
    "WFC",
    "DIS",
    "VZ",
    "CMCSA",
    "AMD",
    "INTU",
    "TXN",
    "PM",
    "NKE",
    "ORCL",
    "CRM",
    "INTC",
    "UPS",
    "QCOM",
    "AMAT",
    "HON",
    "IBM",
    "LOW",
    "SBUX",
)

UNIVERSE_CHOICES = frozenset({"elite", "top50"})


def symbols_for_universe(name: str) -> tuple[str, ...]:
    key = (name or "top50").strip().lower()
    if key == "elite":
        return ELITE_STOCK_SYMBOLS
    if key == "top50":
        return TOP50_US_STOCK_SYMBOLS
    raise ValueError(f"universo desconocido: {name!r} (elite|top50)")
