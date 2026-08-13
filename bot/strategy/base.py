"""Plantilla de estrategia: señales BUY / SELL / HOLD."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

import pandas as pd


class Signal(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass(frozen=True)
class StrategyContext:
    symbol: str
    bars: pd.DataFrame
    has_long_position: bool
    has_short_position: bool
    last_price: float | None = None
    spread_pct: float | None = None
    momentum_pct: float | None = None
    atr: float | None = None


class Strategy(ABC):
    """Contrato que debe implementar cualquier estrategia del bot."""

    name: str = "base"

    @abstractmethod
    def generate_signal(self, ctx: StrategyContext) -> Signal:
        raise NotImplementedError
