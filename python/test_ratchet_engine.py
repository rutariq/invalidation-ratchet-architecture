"""
Unit tests for Microstructure-Aware Invalidation Loop & Volumetric Ratchet Stop Engine
"""

import unittest
import time
import os
import sys

# Ensure local module directory is in path
sys.path.insert(0, os.path.dirname(__file__))

from ratchet_invalidation_engine import (
    InvalidationAndRatchetEngine,
    TradeHypothesis,
    PositionSide,
    VolumeNode,
    ExitReason,
)


class TestInvalidationAndRatchetEngine(unittest.TestCase):

    def setUp(self):
        self.engine = InvalidationAndRatchetEngine(decay_timeout_seconds=5.0, hvn_volume_percentile=0.70)

    def test_identify_hvn_nodes(self):
        profile = [
            VolumeNode(price_level=5000.0, volume=100),
            VolumeNode(price_level=5001.0, volume=500),
            VolumeNode(price_level=5002.0, volume=2000),
            VolumeNode(price_level=5003.0, volume=3500),
        ]
        hvns = self.engine.identify_hvn_nodes(profile)
        self.assertTrue(len(hvns) >= 1)
        self.assertEqual(hvns[-1].price_level, 5003.0)

    def test_ratchet_advancement_long(self):
        hypothesis = TradeHypothesis(
            entry_price=5000.0,
            side=PositionSide.LONG,
            entry_timestamp=time.time(),
            expected_delta_polarity=1,
            min_acceptable_delta=500.0,
            initial_hard_stop=4990.0,
            current_stop=4990.0,
        )

        profile = [
            VolumeNode(price_level=4995.0, volume=4000, is_hvn=True),
            VolumeNode(price_level=5005.0, volume=6000, is_hvn=True),
        ]

        # Price advances to 5010.0; stop should ratchet behind 5005.0 HVN shelf (- 0.25 ticks = 5004.75)
        new_stop = self.engine.update_ratchet_stop(hypothesis, current_price=5010.0, volume_profile=profile)
        self.assertEqual(new_stop, 5004.75)
        self.assertEqual(hypothesis.current_stop, 5004.75)

        # Ensure ratchet is one-way: price drops back to 5006.0; stop does not degrade
        profile_lower = [
            VolumeNode(price_level=4992.0, volume=8000, is_hvn=True),
        ]
        unchanged_stop = self.engine.update_ratchet_stop(hypothesis, current_price=5006.0, volume_profile=profile_lower)
        self.assertEqual(unchanged_stop, 5004.75)

    def test_ratchet_trigger_exit(self):
        hypothesis = TradeHypothesis(
            entry_price=5000.0,
            side=PositionSide.LONG,
            entry_timestamp=time.time(),
            expected_delta_polarity=1,
            min_acceptable_delta=100.0,
            initial_hard_stop=4990.0,
            current_stop=5004.75,
        )

        # Price hits ratcheted stop
        reason = self.engine.evaluate_invalidation(
            hypothesis, current_price=5004.50, rolling_cumulative_delta=500.0
        )
        self.assertEqual(reason, ExitReason.RATCHET_TRIGGERED)

    def test_information_decay_exit(self):
        # Trade entered 10 seconds ago (decay timeout is 5.0)
        past_time = time.time() - 10.0
        hypothesis = TradeHypothesis(
            entry_price=5000.0,
            side=PositionSide.LONG,
            entry_timestamp=past_time,
            expected_delta_polarity=1,
            min_acceptable_delta=200.0,
            initial_hard_stop=4980.0,
            current_stop=4980.0,
        )

        # Price hasn't hit stop (price = 4998.0), but cumulative delta collapsed to -50.0
        reason = self.engine.evaluate_invalidation(
            hypothesis, current_price=4998.0, rolling_cumulative_delta=-50.0
        )
        self.assertEqual(reason, ExitReason.INFORMATION_DECAY)

    def test_structural_support_breach(self):
        hypothesis = TradeHypothesis(
            entry_price=5000.0,
            side=PositionSide.LONG,
            entry_timestamp=time.time(),
            expected_delta_polarity=1,
            min_acceptable_delta=50.0,
            initial_hard_stop=4970.0,
            current_stop=4970.0,
        )

        # Structural trendline support is at 4995.0, price touches 4994.0
        reason = self.engine.evaluate_invalidation(
            hypothesis,
            current_price=4994.0,
            rolling_cumulative_delta=300.0,
            structural_support_level=4995.0,
        )
        self.assertEqual(reason, ExitReason.STRUCTURAL_BREACH)


if __name__ == "__main__":
    unittest.main()
