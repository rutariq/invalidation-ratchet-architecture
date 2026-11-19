"""
Microstructure-Aware Invalidation Loop & Volumetric Ratchet Stop Engine
Proof of Execution (PoE) Reference Implementation
Author: Tasaum Networks Inc.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional
import time


class PositionSide(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class ExitReason(Enum):
    NONE = "NONE"
    HARD_STOP = "HARD_STOP"
    HYPOTHESIS_INVALIDATION = "HYPOTHESIS_INVALIDATION"
    INFORMATION_DECAY = "INFORMATION_DECAY"
    STRUCTURAL_BREACH = "STRUCTURAL_BREACH"
    RATCHET_TRIGGERED = "RATCHET_TRIGGERED"


@dataclass
class VolumeNode:
    price_level: float
    volume: float
    is_hvn: bool = False  # High Volume Node


@dataclass
class TradeHypothesis:
    entry_price: float
    side: PositionSide
    entry_timestamp: float
    expected_delta_polarity: int  # +1 for aggressive buying expected, -1 for selling
    min_acceptable_delta: float
    initial_hard_stop: float
    current_stop: float
    highest_hvn_floor: float = 0.0


class InvalidationAndRatchetEngine:
    """
    Evaluates order-flow decay, hypothesis invalidation, and adjusts
    a one-way mechanical trailing ratchet behind High-Volume Nodes (HVN).
    """

    def __init__(self, decay_timeout_seconds: float = 45.0, hvn_volume_percentile: float = 0.75):
        self.decay_timeout = decay_timeout_seconds
        self.hvn_volume_percentile = hvn_volume_percentile

    def identify_hvn_nodes(self, volume_profile: List[VolumeNode]) -> List[VolumeNode]:
        """Classify price levels into High Volume Nodes (liquidity shields)."""
        if not volume_profile:
            return []
        vols = [n.volume for n in volume_profile]
        threshold = sorted(vols)[int(len(vols) * self.hvn_volume_percentile)]
        for node in volume_profile:
            node.is_hvn = node.volume >= threshold
        return [node for node in volume_profile if node.is_hvn]

    def update_ratchet_stop(
        self, hypothesis: TradeHypothesis, current_price: float, volume_profile: List[VolumeNode]
    ) -> float:
        """
        One-way mechanical ratchet: moves trailing stop strictly upward (for LONGs)
        directly behind the highest validated High Volume Node (HVN) shelf.
        """
        hvns = self.identify_hvn_nodes(volume_profile)
        if not hvns:
            return hypothesis.current_stop

        if hypothesis.side == PositionSide.LONG:
            # Look for HVNs below current market price but above initial entry/previous stop
            valid_shelves = [
                n.price_level for n in hvns
                if hypothesis.current_stop < n.price_level < current_price
            ]
            if valid_shelves:
                highest_shelf = max(valid_shelves)
                # Shield stop 1 tick behind the massive limit block
                ratchet_target = highest_shelf - 0.25  # tick offset
                if ratchet_target > hypothesis.current_stop:
                    hypothesis.current_stop = ratchet_target
                    hypothesis.highest_hvn_floor = highest_shelf

        elif hypothesis.side == PositionSide.SHORT:
            valid_shelves = [
                n.price_level for n in hvns
                if current_price < n.price_level < hypothesis.current_stop
            ]
            if valid_shelves:
                lowest_shelf = min(valid_shelves)
                ratchet_target = lowest_shelf + 0.25
                if ratchet_target < hypothesis.current_stop:
                    hypothesis.current_stop = ratchet_target
                    hypothesis.highest_hvn_floor = lowest_shelf

        return hypothesis.current_stop

    def evaluate_invalidation(
        self,
        hypothesis: TradeHypothesis,
        current_price: float,
        rolling_cumulative_delta: float,
        structural_support_level: Optional[float] = None,
    ) -> ExitReason:
        """
        Evaluates the real-time state space of the trade against microstructure decay
        and structural boundaries rather than waiting for an arbitrary price hit.
        """
        now = time.time()
        elapsed = now - hypothesis.entry_timestamp

        # 1. Hard Ratchet Stop Breach
        if hypothesis.side == PositionSide.LONG and current_price <= hypothesis.current_stop:
            return ExitReason.RATCHET_TRIGGERED
        if hypothesis.side == PositionSide.SHORT and current_price >= hypothesis.current_stop:
            return ExitReason.RATCHET_TRIGGERED

        # 2. Information Decay Exit (Delta Exhaustion)
        # If expected buying delta flipped negative or died out past the time threshold
        if hypothesis.side == PositionSide.LONG:
            if rolling_cumulative_delta < hypothesis.min_acceptable_delta and elapsed > self.decay_timeout:
                # Hypothesis is dead: EV is zero. Exit immediately before price collapses.
                return ExitReason.INFORMATION_DECAY

        # 3. Structural Vector Breach
        if structural_support_level is not None:
            if hypothesis.side == PositionSide.LONG and current_price < structural_support_level:
                return ExitReason.STRUCTURAL_BREACH
            if hypothesis.side == PositionSide.SHORT and current_price > structural_support_level:
                return ExitReason.STRUCTURAL_BREACH

        return ExitReason.NONE
