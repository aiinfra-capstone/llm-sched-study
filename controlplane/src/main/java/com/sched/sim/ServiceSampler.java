package com.sched.sim;

import com.sched.core.models.CostModelSnapshot;
import com.sched.core.models.CostModelSnapshot.CostEntry;
import java.util.Map;
import java.util.Random;

/**
 * Service times for the simulator, drawn from the C-3 cost model.
 *
 * <p>The noise model is i.i.d. (0.3): one lognormal multiplier per request,
 * {@code exp(sigma * Z - sigma^2 / 2)} with Z standard normal, so its mean is 1, and sigma
 * from the snapshot's {@code stochastic.sigma}. Draws are independent across requests.
 * There is no autocorrelation: {@code stochastic.autocorr_time_s} is deliberately not read.
 * K6 found no drift the instrument could resolve on any class in the pool, so an i.i.d.
 * model is what was measured. With {@code --deterministic} the multiplier is 1.
 */
public class ServiceSampler {
    private final Map<String, CostModelSnapshot> snaps;
    private final Random rng;
    private final Map<String, Random> nodeRng;
    private boolean deterministic = false;

    public ServiceSampler(Map<String, CostModelSnapshot> snaps, Random rng) {
        this(snaps, rng, null);
    }

    public ServiceSampler(Map<String, CostModelSnapshot> snaps, Random rng,
            Map<String, Random> nodeRng) {
        this.snaps = snaps;
        this.rng = rng;
        this.nodeRng = nodeRng;
    }

    public void setDeterministic(boolean deterministic) {
        this.deterministic = deterministic;
    }

    /**
     * The C-3 cost model's own mean for this cell, interpolated across concurrency, and
     * nothing else on top. Whatever the client observes beyond the engine's span is
     * transport, and transport is added once at the client boundary in
     * ServiceCompletionEvent, not folded into the service time that drives queueing here.
     */
    public double getMeanMs(String nId, int pLen, int oLen, int conc) {
        CostModelSnapshot snap = snaps.get(nId);
        if (snap == null) return -1;
        return snap.meanServiceMs(pLen, oLen, conc);
    }

    /**
     * The share of this cell's service time the engine attributed to prompt evaluation,
     * or -1 when the snapshot does not carry a phase split.
     *
     * Nearest concurrency rather than interpolated: a ratio between two phases moves far
     * less across the grid than the absolute time does, and interpolating it would suggest
     * a precision the two-point measurement underneath does not have.
     */
    public double getPrefillShare(String nId, int pLen, int oLen, int conc) {
        CostEntry e = nearestEntry(nId, pLen, oLen, conc);
        if (e == null || !e.hasPhaseSplit() || e.serviceMsMean() <= 0) return -1;
        return e.prefillMsMean() / e.serviceMsMean();
    }

    private CostEntry nearestEntry(String nId, int pLen, int oLen, int conc) {
        CostModelSnapshot snap = snaps.get(nId);
        if (snap == null) return null;
        CostEntry best = null;
        int bestGap = Integer.MAX_VALUE;
        for (CostEntry e : snap.entries()) {
            if (pLen >= e.promptBucket().get(0) && pLen <= e.promptBucket().get(1) &&
                    oLen >= e.outputBucket().get(0) && oLen <= e.outputBucket().get(1)) {
                int gap = Math.abs(e.concurrency() - conc);
                if (gap < bestGap) { bestGap = gap; best = e; }
            }
        }
        return best;
    }

    public long sampleServiceNs(String nId, int pLen, int oLen, int conc) {
        double meanMs = getMeanMs(nId, pLen, oLen, conc);
        if (meanMs < 0) return -1;
        CostModelSnapshot snap = snaps.get(nId);
        double finMs = meanMs;
        if (!deterministic) {
            double sig = snap.stochastic().sigma();
            Random drawRng = nodeRng != null && nodeRng.containsKey(nId)
                    ? nodeRng.get(nId) : rng;
            double noise = Math.exp(drawRng.nextGaussian() * sig - (sig * sig) / 2.0);
            finMs = meanMs * noise;
        }
        return (long) (finMs * 1_000_000L);
    }
}