package com.sched.sim;

import com.sched.core.models.CostModelSnapshot;
import com.sched.core.models.CostModelSnapshot.CostEntry;
import java.util.Map;
import java.util.Random;

public class ServiceSampler {
    private final Map<String, CostModelSnapshot> snaps;
    private final Random rng;
    private boolean deterministic = false;

    public ServiceSampler(Map<String, CostModelSnapshot> snaps, Random rng) {
        this.snaps = snaps;
        this.rng = rng;
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
        java.util.List<CostEntry> candidates = new java.util.ArrayList<>();
        for (CostEntry e : snap.entries()) {
            if (pLen >= e.promptBucket().get(0) && pLen <= e.promptBucket().get(1) &&
                    oLen >= e.outputBucket().get(0) && oLen <= e.outputBucket().get(1)) {
                candidates.add(e);
            }
        }
        if (candidates.isEmpty()) return -1;
        candidates.sort(java.util.Comparator.comparingInt(CostEntry::concurrency));
        for (CostEntry e : candidates) if (e.concurrency() == conc) return e.serviceMsMean();
        if (conc <= candidates.get(0).concurrency()) return candidates.get(0).serviceMsMean();
        if (conc >= candidates.get(candidates.size() - 1).concurrency()) return candidates.get(candidates.size() - 1).serviceMsMean();
        CostEntry lower = null, upper = null;
        for (int i = 0; i < candidates.size() - 1; i++) {
            if (candidates.get(i).concurrency() < conc && conc < candidates.get(i + 1).concurrency()) {
                lower = candidates.get(i); upper = candidates.get(i + 1); break;
            }
        }
        if (lower == null || upper == null) return candidates.get(0).serviceMsMean();
        double fraction = (double)(conc - lower.concurrency()) / (double)(upper.concurrency() - lower.concurrency());
        return lower.serviceMsMean() + fraction * (upper.serviceMsMean() - lower.serviceMsMean());
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
            double noise = Math.exp(rng.nextGaussian() * sig - (sig * sig) / 2.0);
            finMs = meanMs * noise;
        }
        return (long) (finMs * 1_000_000L);
    }
}