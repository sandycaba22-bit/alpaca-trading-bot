from bot.universe import (
    ELITE_STOCK_SYMBOLS,
    ELITE_TOP50_OVERLAP,
    TOP50_US_STOCK_SYMBOLS_EXCLUDING_ELITE,
    apply_top50_elite_exclusion,
)


def test_elite_top50_overlap_is_four_mega_caps():
    assert ELITE_TOP50_OVERLAP == frozenset({"GOOGL", "META", "NVDA", "TSLA"})


def test_top50_excluding_elite_count():
    assert len(TOP50_US_STOCK_SYMBOLS_EXCLUDING_ELITE) == 50 - len(ELITE_TOP50_OVERLAP)
    for sym in ELITE_STOCK_SYMBOLS:
        assert sym not in TOP50_US_STOCK_SYMBOLS_EXCLUDING_ELITE or sym not in ELITE_TOP50_OVERLAP


def test_apply_exclusion():
    kept, removed = apply_top50_elite_exclusion(
        ["AAPL", "NVDA", "SLV"], exclude=True
    )
    assert removed == ["NVDA"]
    assert kept == ["AAPL", "SLV"]
