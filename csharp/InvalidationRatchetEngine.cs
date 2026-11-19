#region Using declarations
using System;
using System.Collections.Generic;
using System.Linq;
#endregion

namespace Tasaum.Microstructure.Execution
{
    public enum PositionSide
    {
        Flat,
        Long,
        Short
    }

    public enum InvalidationExitReason
    {
        None,
        HardStop,
        InformationDecay,
        StructuralBreach,
        RatchetTriggered
    }

    public class VolumeNode
    {
        public double PriceLevel { get; set; }
        public long Volume { get; set; }
        public bool IsHighVolumeNode { get; set; }

        public VolumeNode(double priceLevel, long volume)
        {
            PriceLevel = priceLevel;
            Volume = volume;
            IsHighVolumeNode = false;
        }
    }

    public class TradeHypothesis
    {
        public double EntryPrice { get; set; }
        public PositionSide Side { get; set; }
        public DateTime EntryTimeUtc { get; set; }
        public int ExpectedDeltaPolarity { get; set; } // +1 for buying, -1 for selling
        public double MinAcceptableCumulativeDelta { get; set; }
        public double InitialHardStop { get; set; }
        public double CurrentStop { get; set; }
        public double HighestHvnFloor { get; set; }

        public TradeHypothesis(
            double entryPrice,
            PositionSide side,
            DateTime entryTimeUtc,
            int expectedDeltaPolarity,
            double minAcceptableCumulativeDelta,
            double initialHardStop)
        {
            EntryPrice = entryPrice;
            Side = side;
            EntryTimeUtc = entryTimeUtc;
            ExpectedDeltaPolarity = expectedDeltaPolarity;
            MinAcceptableCumulativeDelta = minAcceptableCumulativeDelta;
            InitialHardStop = initialHardStop;
            CurrentStop = initialHardStop;
            HighestHvnFloor = 0.0;
        }
    }

    /// <summary>
    /// Microstructure Invalidation and Volumetric Ratchet Engine.
    /// Manages non-arbitrary stop ratcheting behind institutional High-Volume Nodes (HVN)
    /// and detects information decay via tape delta exhaustion.
    /// </summary>
    public class InvalidationRatchetEngine
    {
        public TimeSpan DecayTimeout { get; set; }
        public double HvnPercentileThreshold { get; set; }
        public double TickSize { get; set; }

        public InvalidationRatchetEngine(
            double decayTimeoutSeconds = 45.0,
            double hvnPercentileThreshold = 0.75,
            double tickSize = 0.25)
        {
            DecayTimeout = TimeSpan.FromSeconds(decayTimeoutSeconds);
            HvnPercentileThreshold = hvnPercentileThreshold;
            TickSize = tickSize;
        }

        /// <summary>
        /// Classify volume profile into High-Volume Nodes (resting liquidity shelves).
        /// </summary>
        public List<VolumeNode> IdentifyHvnNodes(List<VolumeNode> profile)
        {
            if (profile == null || profile.Count == 0)
                return new List<VolumeNode>();

            var sortedVols = profile.Select(n => n.Volume).OrderBy(v => v).ToList();
            int cutoffIndex = (int)(sortedVols.Count * HvnPercentileThreshold);
            if (cutoffIndex >= sortedVols.Count)
                cutoffIndex = sortedVols.Count - 1;

            long threshold = sortedVols[cutoffIndex];

            foreach (var node in profile)
            {
                node.IsHighVolumeNode = node.Volume >= threshold;
            }

            return profile.Where(n => n.IsHighVolumeNode).ToList();
        }

        /// <summary>
        /// One-way mechanical ratchet: moves trailing stop strictly upward (for Longs)
        /// shielded 1 tick behind the highest validated High Volume Node (HVN) shelf.
        /// </summary>
        public double UpdateRatchetStop(TradeHypothesis hypothesis, double currentPrice, List<VolumeNode> profile)
        {
            if (hypothesis == null || profile == null)
                return hypothesis?.CurrentStop ?? 0.0;

            var hvns = IdentifyHvnNodes(profile);
            if (hvns.Count == 0)
                return hypothesis.CurrentStop;

            if (hypothesis.Side == PositionSide.Long)
            {
                var validShelves = hvns
                    .Where(n => n.PriceLevel > hypothesis.CurrentStop && n.PriceLevel < currentPrice)
                    .Select(n => n.PriceLevel)
                    .ToList();

                if (validShelves.Count > 0)
                {
                    double highestShelf = validShelves.Max();
                    double ratchetTarget = highestShelf - TickSize;

                    if (ratchetTarget > hypothesis.CurrentStop)
                    {
                        hypothesis.CurrentStop = ratchetTarget;
                        hypothesis.HighestHvnFloor = highestShelf;
                    }
                }
            }
            else if (hypothesis.Side == PositionSide.Short)
            {
                var validShelves = hvns
                    .Where(n => n.PriceLevel < hypothesis.CurrentStop && n.PriceLevel > currentPrice)
                    .Select(n => n.PriceLevel)
                    .ToList();

                if (validShelves.Count > 0)
                {
                    double lowestShelf = validShelves.Min();
                    double ratchetTarget = lowestShelf + TickSize;

                    if (ratchetTarget < hypothesis.CurrentStop)
                    {
                        hypothesis.CurrentStop = ratchetTarget;
                        hypothesis.HighestHvnFloor = lowestShelf;
                    }
                }
            }

            return hypothesis.CurrentStop;
        }

        /// <summary>
        /// Evaluates trade state space against tape delta exhaustion and structural breach.
        /// </summary>
        public InvalidationExitReason EvaluateInvalidation(
            TradeHypothesis hypothesis,
            double currentPrice,
            double rollingCumulativeDelta,
            DateTime currentUtc,
            double? structuralSupportLevel = null)
        {
            if (hypothesis == null)
                return InvalidationExitReason.None;

            // 1. Ratchet Stop Triggered
            if (hypothesis.Side == PositionSide.Long && currentPrice <= hypothesis.CurrentStop)
                return InvalidationExitReason.RatchetTriggered;
            if (hypothesis.Side == PositionSide.Short && currentPrice >= hypothesis.CurrentStop)
                return InvalidationExitReason.RatchetTriggered;

            // 2. Information Decay (Delta Exhaustion)
            TimeSpan elapsed = currentUtc - hypothesis.EntryTimeUtc;
            if (hypothesis.Side == PositionSide.Long)
            {
                if (rollingCumulativeDelta < hypothesis.MinAcceptableCumulativeDelta && elapsed > DecayTimeout)
                {
                    // Trade premise is invalidated by order flow; exit before price drop
                    return InvalidationExitReason.InformationDecay;
                }
            }

            // 3. Structural Vector Breach
            if (structuralSupportLevel.HasValue)
            {
                if (hypothesis.Side == PositionSide.Long && currentPrice < structuralSupportLevel.Value)
                    return InvalidationExitReason.StructuralBreach;
                if (hypothesis.Side == PositionSide.Short && currentPrice > structuralSupportLevel.Value)
                    return InvalidationExitReason.StructuralBreach;
            }

            return InvalidationExitReason.None;
        }
    }
}
