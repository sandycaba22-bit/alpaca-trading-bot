from bot.universe import (
    ELITE_STOCK_SYMBOLS,
    ELITE_TOP50_OVERLAP,
    TOP50_US_STOCK_SYMBOLS_EXCLUDING_ELITE,
    apply_top50_elite_exclusion,
)


def test_elite_disjoint_from_top50():
    assert ELITE_TOP50_OVERLAP == frozenset()
    for sym in ELITE_STOCK_SYMBOLS:
        assert sym not in TOP50_US_STOCK_SYMBOLS


def test_top50_excluding_elite_is_full_universe_when_disjoint():
    assert len(ELITE_STOCK_SYMBOLS) == 6
    assert len(TOP50_US_STOCK_SYMBOLS_EXCLUDING_ELITE) == len(TOP50_US_STOCK_SYMBOLS)


def test_apply_exclusion():
    kept, removed = apply_top50_elite_exclusion(
        ["AAPL", "NVDA", "SLV"], exclude=True
    )
    assert removed == ["NVDA"]
    assert kept == ["AAPL", "SLV"]
