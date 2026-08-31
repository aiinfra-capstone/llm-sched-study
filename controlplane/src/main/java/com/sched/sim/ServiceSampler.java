package com.sched.sim;

import com.sched.core.models.CostModelSnapshot;
import com.sched.core.models.CostModelSnapshot.CostEntry;
import java.util.Map;
import java.util.Random;

public class ServiceSampler {
    private final Map<String, CostModelSnapshot> snaps;
    private final Random rng;

    public ServiceSampler(Map<String, CostModelSnapshot> snaps, Random rng) {
        this.snaps = snaps;
        this.rng = rng;
    }

    public long sampleServiceNs(String nId, int pLen, int oLen, int conc) {
        CostModelSnapshot snap = snaps.get(nId);
        if (snap == null) {
            return -1;
        }

        CostEntry bestMatch = null;
        int minDistance = Integer.MAX_VALUE;

        for (CostEntry e : snap.entries()) {
            // 1. Must match the token length buckets exactly
            if (pLen >= e.promptBucket().get(0) && pLen <= e.promptBucket().get(1) &&
                    oLen >= e.outputBucket().get(0) && oLen <= e.outputBucket().get(1)) {

                // 2. Concurrency Snapping: find the calibrated point closest to reality
                int distance = Math.abs(e.concurrency() - conc);
                if (distance < minDistance) {
                    minDistance = distance;
                    bestMatch = e;
                }
            }
        }

        if (bestMatch == null) {
            return -1; // Still return -1 if prompt/output lengths are totally off-grid
        }

        double meanMs = bestMatch.serviceMsMean();
        double sig = snap.stochastic().sigma();

        // MPR-1 Lognormal variance injection
        double noise = Math.exp(rng.nextGaussian() * sig - (sig * sig) / 2.0);
        double finMs = meanMs * noise;

        return (long) (finMs * 1_000_000L);
    }
}