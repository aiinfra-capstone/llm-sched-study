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

        CostEntry match = null;
        for (CostEntry e : snap.entries()) {
            if (pLen >= e.promptBucket().get(0) && pLen <= e.promptBucket().get(1) &&
                    oLen >= e.outputBucket().get(0) && oLen <= e.outputBucket().get(1) &&
                    conc == e.concurrency()) {
                match = e;
                break;
            }
        }

        if (match == null) {
            return -1;
        }

        double meanMs = match.serviceMsMean();
        double sig = snap.stochastic().sigma();
        double noise = Math.exp(rng.nextGaussian() * sig - (sig * sig) / 2.0);
        double finMs = meanMs * noise;

        return (long) (finMs * 1_000_000L);
    }
}