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
        double raw;
        candidates.sort(java.util.Comparator.comparingInt(CostEntry::concurrency));
        for (CostEntry e : candidates) if (e.concurrency() == conc) { raw = e.serviceMsMean(); return raw * 1.05; }
        if (conc <= candidates.get(0).concurrency()) { raw = candidates.get(0).serviceMsMean(); return raw * 1.05; }
        if (conc >= candidates.get(candidates.size() - 1).concurrency()) { raw = candidates.get(candidates.size() - 1).serviceMsMean(); return raw * 1.05; }
        CostEntry lower = null, upper = null;
        for (int i = 0; i < candidates.size() - 1; i++) {
            if (candidates.get(i).concurrency() < conc && conc < candidates.get(i + 1).concurrency()) {
                lower = candidates.get(i); upper = candidates.get(i + 1); break;
            }
        }
        if (lower == null || upper == null) { raw = candidates.get(0).serviceMsMean(); return raw * 1.05; }
        double fraction = (double)(conc - lower.concurrency()) / (double)(upper.concurrency() - lower.concurrency());
        raw = lower.serviceMsMean() + fraction * (upper.serviceMsMean() - lower.serviceMsMean());
        return raw * 1.05;
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