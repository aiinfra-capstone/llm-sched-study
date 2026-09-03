package com.sched;

import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.core.models.CostModelSnapshot;
import com.sched.core.models.CostModelSnapshot.Admissibility;
import com.sched.core.models.CostModelSnapshot.CostEntry;
import com.sched.core.models.CostModelSnapshot.Provenance;
import com.sched.core.models.CostModelSnapshot.Stochastic;
import java.util.List;
import java.util.Random;

/**
 * Builders for the two records every test here needs, so that a test reads as the one thing
 * it is asserting rather than as ten lines of constructor.
 *
 * Both records are wide and positional, and a test that spells them out in full hides its
 * own point. Everything these helpers fill in is deliberately uninteresting; anything a test
 * actually depends on is passed as an argument.
 */
public final class Fixtures {
    private Fixtures() {}

    /** An admissible node with the queue state and speed a policy is being asked about. */
    public static NodeView node(String id, int queueDepth, int inflight, double capabilityTokS) {
        return new NodeView(id, queueDepth, inflight, capabilityTokS, 0L, true);
    }

    /** One cost model cell with no phase split, the shape of every snapshot fitted before it existed. */
    public static CostEntry cell(int pLo, int pHi, int oLo, int oHi, int concurrency, double serviceMsMean) {
        return new CostEntry(List.of(pLo, pHi), List.of(oLo, oHi), concurrency,
                serviceMsMean, serviceMsMean, serviceMsMean * 1.2, null, null, 100.0, 8);
    }

    /** The same cell, carrying the prefill and decode split the phase-aware path needs. */
    public static CostEntry splitCell(int pLo, int pHi, int oLo, int oHi, int concurrency,
                                      double serviceMsMean, double prefillMs, double decodeMs) {
        return new CostEntry(List.of(pLo, pHi), List.of(oLo, oHi), concurrency,
                serviceMsMean, serviceMsMean, serviceMsMean * 1.2, prefillMs, decodeMs, 100.0, 8);
    }

    public static CostModelSnapshot snapshot(String nodeClass, double sigma, List<CostEntry> entries) {
        return new CostModelSnapshot(
                1, "cm_" + nodeClass + "_test", nodeClass, 1_788_000_000L, List.of("cal_test"),
                "lookup_table", entries,
                new Stochastic("lognormal_multiplier", sigma, 5.0, 0.0),
                new Admissibility(512, 128, 60_000),
                new Provenance("llamacpp", "b10569+p1", "Q4_K_M", "none", null, false,
                        new Provenance.EngineConfig(99, 6, 4)));
    }

    /**
     * A Random whose nextDouble is whatever the test says it is.
     *
     * The tie-break draw decides which node a request goes to, so a test of tie-breaking that
     * cannot name the draw is only testing that something was returned.
     */
    public static final class FixedRandom extends Random {
        private final double value;

        public FixedRandom(double value) {
            super(0L);
            this.value = value;
        }

        @Override
        public double nextDouble() {
            return value;
        }
    }
}
