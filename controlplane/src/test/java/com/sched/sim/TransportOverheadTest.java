package com.sched.sim;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.Random;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/**
 * The measured cost of everything that is not the engine.
 *
 * The C-3 cost model is fitted on the worker's own service span, and a client also pays for
 * the gRPC hop in, the decision, the dispatch and the direct return. On the 1B anchors that
 * is 5.86 +/- 2.66 ms and it does not move with load, which is why it is added in
 * milliseconds rather than applied as a percentage.
 */
class TransportOverheadTest {

    @Test
    @DisplayName("a run with no measurement gets nothing, not a default")
    void noneIsZero() {
        // Absent has to mean absent. A default here would be a number nobody measured turning
        // up inside a reported latency.
        assertEquals(0L, TransportOverhead.NONE.sampleNs());
        assertEquals(0.0, TransportOverhead.NONE.meanMs(), 1e-9);
    }

    @Test
    @DisplayName("a zero mean stays zero however wide the spread claims to be")
    void zeroMeanShortCircuits() {
        TransportOverhead o = new TransportOverhead(0.0, 5.0, new Random(1), false);
        for (int i = 0; i < 50; i++) assertEquals(0L, o.sampleNs());
    }

    @Test
    @DisplayName("deterministic mode returns the mean exactly")
    void deterministicReturnsMean() {
        TransportOverhead o = new TransportOverhead(5.86, 2.66, new Random(1), true);
        for (int i = 0; i < 20; i++) assertEquals(5_860_000L, o.sampleNs());
    }

    @Test
    @DisplayName("stochastic mode disperses around the mean")
    void stochasticVaries() {
        TransportOverhead o = new TransportOverhead(5.86, 2.66, new Random(7), false);

        long first = o.sampleNs();
        long second = o.sampleNs();
        assertNotEquals(first, second);

        double total = 0;
        int n = 20000;
        for (int i = 0; i < n; i++) total += o.sampleNs() / 1e6;
        assertEquals(5.86, total / n, 0.15, "the draw should centre on the measured mean");
    }

    @Test
    @DisplayName("a draw never comes back negative")
    void drawIsTruncatedAtZero() {
        // With mean 1 and sd 5 a normal draw goes negative often, and a negative network hop
        // would subtract time from a request that already happened.
        TransportOverhead o = new TransportOverhead(1.0, 5.0, new Random(3), false);
        for (int i = 0; i < 5000; i++) {
            assertTrue(o.sampleNs() >= 0L, "a negative transport time is not a thing");
        }
    }

    @Test
    @DisplayName("a negative mean or spread is clamped rather than trusted")
    void negativeInputsAreClamped() {
        assertEquals(0.0, new TransportOverhead(-5.0, 2.0, new Random(1), true).meanMs(), 1e-9);
        assertEquals(0.0, new TransportOverhead(5.0, -2.0, new Random(1), true).sdMs(), 1e-9);
    }

    @Test
    @DisplayName("zero spread makes every draw the mean")
    void zeroSpreadIsConstant() {
        TransportOverhead o = new TransportOverhead(3.0, 0.0, new Random(1), false);
        for (int i = 0; i < 20; i++) assertEquals(3_000_000L, o.sampleNs());
    }

    @Test
    @DisplayName("one seed reproduces another")
    void seededDrawsAreReproducible() {
        TransportOverhead a = new TransportOverhead(5.86, 2.66, new Random(99), false);
        TransportOverhead b = new TransportOverhead(5.86, 2.66, new Random(99), false);
        for (int i = 0; i < 20; i++) assertEquals(a.sampleNs(), b.sampleNs());
    }
}
