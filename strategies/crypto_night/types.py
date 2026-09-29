from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RejectReason(str, Enum):
    SESSION = "session"
    VOLATILITY = "volatility"
    BIAS = "bias"
    ENTRY = "entry"
    QUALITY = "quality"
    RISK = "risk"
    MODE = "mode"


class TradeSide(str, Enum):
    LONG = "long"
    SHORT = "short"


@dataclass
class NightBias:
    side: TradeSide
    symbol: str
    reason: str


@dataclass
class SweepSetup:
    side: TradeSide
    symbol: str
    sweep_extreme: float
    reclaim_bar_idx: int
    limit_price: float
    stop_price: float
    atr_1h: float
    volume_ratio: float
    variant: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class GateResult:
    ok: bool
    reject: RejectReason | None = None
    detail: str = ""


@dataclass
class QualityScore:
    total: int
    points: dict[str, bool]
    min_required: int
    ok: bool
    theoretical_r: float
    skip_low_r: bool = False


@dataclass
class SimulatedTrade:
    variant: str
    symbol: str
    side: TradeSide
    session_start: str
    entry_ts: str
    exit_ts: str
    r_gross: float
    r_net: float
    fees_r: float
    exit_reason: str
    mode: str
