package com.sched.core;

import com.sched.core.models.CostModelSnapshot;

/**
 * The one number a policy knows about a node: how many output tokens it delivers per
 * second of service, read off its C-3 snapshot.
 *
 * <p>The live scheduler and the simulator both seed their nodes from here, so the two
 * vehicles cannot drift apart on what capability means.
 *
 * <p>It used to be the reference cell's {@code tokens_per_s}, which C-3 defines as decode
 * tok/s. Decode is memory-bandwidth-bound and prefill is compute-bound, and a pair of
 * machines can be close on one and far apart on the other. On the GTX 1650 Ti and the
 * RTX 3050 laptop decode differs by 1.18x and prefill by about 10x, so a decode-only
 * capability told {@code static_weighted}, {@code wjsq} and {@code threshold} the pool was
 * nearly homogeneous while the 3050 served the anchor trace 1.76x faster. H1 asks whether
 * calibration adds anything over queue depth, and a calibration signal blind to prefill
 * answers that question for the wrong reason.
 *
 * <p>Service rate is decode tok/s times the share of service time spent decoding. Decode
 * tok/s times decode time recovers the output length, so this is output tokens over the
 * whole service time, prefill and the engine's unattributed residual included, and it needs
 * no field C-3 does not already carry. The cell is the lowest prompt and output bucket at
 * concurrency 1: the uncontended cell, since the calibration grid fires concurrent requests
 * together and their prefills serialise in a way Poisson arrivals do not produce.
 */
public final class Capability {
    private Capability() {
    }

    /** The lowest prompt and output bucket at concurrency 1, or null if the grid has none. */
    public static CostModelSnapshot.CostEntry referenceCell(CostModelSnapshot snap) {
        int minPrompt = Integer.MAX_VALUE;
        int minOutput = Integer.MAX_VALUE;
        for (CostModelSnapshot.CostEntry e : snap.entries()) {
            if (e.promptBucket().get(0) < minPrompt) minPrompt = e.promptBucket().get(0);
            if (e.outputBucket().get(0) < minOutput) minOutput = e.outputBucket().get(0);
        }
        for (CostModelSnapshot.CostEntry e : snap.entries()) {
            if (e.promptBucket().get(0) == minPrompt && e.outputBucket().get(0) == minOutput
                    && e.concurrency() == 1) {
                return e;
            }
        }
        return null;
    }

    /**
     * Output tokens per second of service at the reference cell.
     *
     * <p>A snapshot fitted before the prefill/decode split existed cannot say how much of
     * its service time was decode, so it falls back to decode tok/s and
     * {@link #usesServiceRate} reports false. Callers print that, because a pool mixing the
     * two definitions compares unlike numbers.
     *
     * @throws IllegalArgumentException if the snapshot has no reference cell
     */
    public static double referenceTokS(CostModelSnapshot snap) {
        CostModelSnapshot.CostEntry cell = referenceCell(snap);
        if (cell == null) {
            throw new IllegalArgumentException("Cost model snapshot " + snap.snapshotId()
                    + " has no cell for lowest bucket at concurrency 1.");
        }
        if (!hasServiceRate(cell)) return cell.tokensPerS();
        return cell.tokensPerS() * cell.decodeMsMean() / cell.serviceMsMean();
    }

    /** Whether {@link #referenceTokS} is a service rate here, rather than the decode fallback. */
    public static boolean usesServiceRate(CostModelSnapshot snap) {
        CostModelSnapshot.CostEntry cell = referenceCell(snap);
        return cell != null && hasServiceRate(cell);
    }

    private static boolean hasServiceRate(CostModelSnapshot.CostEntry cell) {
        return cell.hasPhaseSplit() && cell.decodeMsMean() > 0 && cell.serviceMsMean() > 0;
    }
}
