package com.sched.sim;

import java.util.Random;

/**
 * The part of client-observed latency that happens outside the engine.
 *
 * The C-3 cost model is fitted on the worker's own `service_ns`, which is the span of the
 * engine call and nothing else. A client's `e2e_duration_ns` also contains the gRPC hop to
 * the scheduler, the decision, the dispatch to the worker, and the direct return to the
 * client under F-11. That difference is what this class carries.
 *
 * It is additive in milliseconds, not a fraction of service time. Measured on the four
 * hardware anchors it is 5.86 +/- 2.66 ms and is flat in load: 5.33 ms at quiet against
 * 5.66 ms at heavy, while service time over the same range moves by a factor of ten. A
 * multiplicative term would have to claim the opposite, that a 13-second request pays 650 ms
 * of transport where a 1-second request pays 50 ms, and the measurement does not support it.
 *
 * The mean and standard deviation are never chosen here. They come from the run manifest's
 * `transport_overhead` block, which the harness measures per environment, and a manifest
 * without that block gets no overhead at all rather than a default that someone guessed.
 */
public final class TransportOverhead {
    private final double meanMs;
    private final double sdMs;
    private final Random rng;
    private final boolean deterministic;

    /** What a run with no measured overhead gets: nothing, and visibly nothing. */
    public static final TransportOverhead NONE = new TransportOverhead(0.0, 0.0, new Random(0), true);

    public TransportOverhead(double meanMs, double sdMs, Random rng, boolean deterministic) {
        this.meanMs = Math.max(0.0, meanMs);
        this.sdMs = Math.max(0.0, sdMs);
        this.rng = rng;
        this.deterministic = deterministic;
    }

    public double meanMs() { return meanMs; }
    public double sdMs() { return sdMs; }

    /**
     * One draw, truncated at zero because a negative network hop is not a thing. Under
     * --deterministic the mean is returned, matching how ServiceSampler drops its own noise.
     */
    public long sampleNs() {
        if (meanMs <= 0.0) return 0L;
        double ms = meanMs;
        if (!deterministic && sdMs > 0.0) {
            ms = meanMs + rng.nextGaussian() * sdMs;
            if (ms < 0.0) ms = 0.0;
        }
        return (long) (ms * 1_000_000.0);
    }
}
