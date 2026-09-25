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
 *
 * <p>S3 sensitivity: {@code capability_mode=decode} returns decode tok/s even when the
 * split exists, reproducing the pre-fix definition. The mode travels in the run manifest's
 * {@code config} so a run set can tell the two apart. Anything else (including absent)
 * means service rate.
 */
public final class Capability {
    private Capability() {
    }

    /** Manifest value that reproduces the pre-fix decode-only definition (S3). */
    public static final String MODE_DECODE = "decode";
    /** Default: output tokens per second of service. */
    public static final String MODE_SERVICE = "service";

    /** The lowest prompt and output bucket at concurrency 1, or null if the grid has none. */
    public static CostModelSnapshot.CostEntry referenceCell(CostModelSnapshot snap) {
        return referenceCell(snap, 1);
    }

    /** The lowest prompt and output bucket at the requested concurrency (or closest), or null. */
    public static CostModelSnapshot.CostEntry referenceCell(CostModelSnapshot snap, int concurrency) {
        if (snap == null || snap.entries() == null) return null;
        int minPrompt = Integer.MAX_VALUE;
        int minOutput = Integer.MAX_VALUE;
        for (CostModelSnapshot.CostEntry e : snap.entries()) {
            if (e.promptBucket().get(0) < minPrompt) minPrompt = e.promptBucket().get(0);
            if (e.outputBucket().get(0) < minOutput) minOutput = e.outputBucket().get(0);
        }
        CostModelSnapshot.CostEntry best = null;
        int bestDiff = Integer.MAX_VALUE;
        for (CostModelSnapshot.CostEntry e : snap.entries()) {
            if (e.promptBucket().get(0) == minPrompt && e.outputBucket().get(0) == minOutput) {
                if (e.concurrency() == concurrency) {
                    return e;
                }
                int diff = Math.abs(e.concurrency() - concurrency);
                if (diff < bestDiff) {
                    bestDiff = diff;
                    best = e;
                }
            }
        }
        return best;
    }

    /**
     * Capability to seed a pool node with, for SimApp and the live scheduler alike.
     *
     * <p>A pool node without a snapshot has no calibrated capability. The live scheduler
     * used to seed it at 0 and carry on, so capability-weighted policies silently starved
     * that node for the whole run. Both vehicles now refuse at startup instead.
     *
     * @throws IllegalStateException if {@code snap} is null
     * @throws IllegalArgumentException from {@link #resolve}, unchanged, if the snapshot has
     *         no reference cell
     */
    public static double forPoolNode(String nodeId, CostModelSnapshot snap, java.util.Map<String, Object> config) {
        if (snap == null) {
            throw new IllegalStateException("node " + nodeId + " is a pool member but has no snapshot");
        }
        return resolve(nodeId, snap, config);
    }

    /**
     * Resolve capability for a node under manifest config (Issue #21 Item 3).
     * Supports capability_override, capability_concurrency, and decode_only / capability_mode.
     */
    public static double resolve(String nodeId, CostModelSnapshot snap, java.util.Map<String, Object> config) {
        if (config != null && config.containsKey("capability_override")) {
            Object ov = config.get("capability_override");
            if (ov instanceof java.util.Map<?, ?> map && map.containsKey(nodeId)) {
                Object v = map.get(nodeId);
                if (v instanceof Number n) return n.doubleValue();
            }
        }
        int concurrency = 1;
        if (config != null && config.containsKey("capability_concurrency")) {
            Object cc = config.get("capability_concurrency");
            if (cc instanceof Number n) concurrency = n.intValue();
        }
        boolean decodeOnly = false;
        if (config != null) {
            if (Boolean.TRUE.equals(config.get("decode_only"))) {
                decodeOnly = true;
            } else if (MODE_DECODE.equals(config.get("capability_mode"))) {
                decodeOnly = true;
            }
        }
        if (snap == null) return 0.0;
        CostModelSnapshot.CostEntry cell = referenceCell(snap, concurrency);
        if (cell == null) {
            throw new IllegalArgumentException("Cost model snapshot " + snap.snapshotId()
                    + " has no cell for lowest bucket at concurrency " + concurrency + ".");
        }
        if (decodeOnly) return cell.tokensPerS();
        if (!hasServiceRate(cell)) return cell.tokensPerS();
        return cell.tokensPerS() * cell.decodeMsMean() / cell.serviceMsMean();
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
        return referenceTokS(snap, MODE_SERVICE);
    }

    /**
     * Capability under an explicit S3 mode.
     *
     * @param mode {@link #MODE_DECODE} for decode-only tok/s even when the split exists;
     *             anything else (including null) for service rate with decode fallback.
     * @throws IllegalArgumentException if the snapshot has no reference cell
     */
    public static double referenceTokS(CostModelSnapshot snap, String mode) {
        CostModelSnapshot.CostEntry cell = referenceCell(snap);
        if (cell == null) {
            throw new IllegalArgumentException("Cost model snapshot " + snap.snapshotId()
                    + " has no cell for lowest bucket at concurrency 1.");
        }
        if (MODE_DECODE.equals(mode)) return cell.tokensPerS();
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
