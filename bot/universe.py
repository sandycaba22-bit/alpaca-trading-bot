"""Universo de símbolos: bot élite (live) vs Top 50 (bot secundario / research)."""

from __future__ import annotations

# Bot principal (live) — vol_2x; cero solape con TOP50 (mega-caps van solo en Top 50).
# OOS sweep 335031: SLV/SPY/QQQ positivos; SMH/GLD/IWM = beta alta, liquidez, tendencia limpia.
ELITE_STOCK_SYMBOLS: tuple[str, ...] = (
    "SLV",
    "SPY",
    "QQQ",
    "SMH",
    "GLD",
    "IWM",
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

# Élite vive en .env.stocks; Top 50 no debe repetirlos si comparten cuenta Alpaca.
ELITE_TOP50_OVERLAP: frozenset[str] = frozenset(ELITE_STOCK_SYMBOLS) & frozenset(
    TOP50_US_STOCK_SYMBOLS
)

TOP50_US_STOCK_SYMBOLS_EXCLUDING_ELITE: tuple[str, ...] = tuple(
    s for s in TOP50_US_STOCK_SYMBOLS if s not in ELITE_STOCK_SYMBOLS
)


def apply_top50_elite_exclusion(
    symbols: list[str],
    *,
    exclude: bool,
) -> tuple[list[str], list[str]]:
    """Quita tickers élite del universo Top 50 (evita doble compra misma paper)."""
    if not exclude or not symbols:
        return list(symbols), []
    elite = frozenset(ELITE_STOCK_SYMBOLS)
    removed = [s for s in symbols if s.upper() in elite]
    kept = [s for s in symbols if s.upper() not in elite]
    return kept, removed


def symbols_for_universe(name: str) -> tuple[str, ...]:
    key = (name or "top50").strip().lower()
    if key == "elite":
        return ELITE_STOCK_SYMBOLS
    if key == "top50":
        return TOP50_US_STOCK_SYMBOLS
    raise ValueError(f"universo desconocido: {name!r} (elite|top50)")
