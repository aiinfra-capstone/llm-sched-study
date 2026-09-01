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

    public long sampleServiceNs(String nId, int pLen, int oLen, int conc) {
        CostModelSnapshot snap = snaps.get(nId);
        if (snap == null) {
            return -1;
        }

        CostEntry bestMatch = null;
        int minDistance = Integer.MAX_VALUE;

        for (CostEntry e : snap.entries()) {
            if (pLen >= e.promptBucket().get(0) && pLen <= e.promptBucket().get(1) &&
                    oLen >= e.outputBucket().get(0) && oLen <= e.outputBucket().get(1)) {

                int distance = Math.abs(e.concurrency() - conc);
                if (distance < minDistance) {
                    minDistance = distance;
                    bestMatch = e;
                }
            }
        }

        if (bestMatch == null) {
            return -1;
        }

        double meanMs = bestMatch.serviceMsMean();
        double finMs = meanMs;
        if (!deterministic) {
            double sig = snap.stochastic().sigma();
            double noise = Math.exp(rng.nextGaussian() * sig - (sig * sig) / 2.0);
            finMs = meanMs * noise;
        }

        return (long) (finMs * 1_000_000L);
    }
}