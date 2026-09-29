"""Nombres y presets de cache de velas."""

from bot.market.bars_cache import cache_path, expand_presets, symbol_cache_key


def test_symbol_cache_key_crypto() -> None:
    assert symbol_cache_key("BTC/USD") == "BTC-USD"


def test_cache_path_naming() -> None:
    assert cache_path("AAPL", "5Min").name == "AAPL_5Min.pkl"
    assert cache_path("BTC/USD", "1Hour").name == "BTC-USD_1Hour.pkl"


def test_expand_presets_all_includes_crypto_and_elite() -> None:
    syms, tfs = expand_presets(["all"])
    assert "BTC/USD" in syms
    assert "SLV" in syms
    assert "AAPL" in syms
    assert "5Min" in tfs
    assert "1Day" in tfs
