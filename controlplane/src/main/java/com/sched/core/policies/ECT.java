package com.sched.core.policies;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Random;
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.core.models.CostModelSnapshot;
import com.sched.v1.DispatchRequest;

/**
 * Earliest predicted completion time from the full C-3 cost model (P6).
 *
 * <p>Scalar policies squeeze a node into one number. {@code static_weighted} and
 * {@code wjsq} see capability (output tok/s of service at one reference cell), so a pool
 * whose R moves with the workload looks fixed to them. This policy prices each request
 * from the cell that matches it: the node's C-3 entry for this request's prompt bucket,
 * output bucket and the node's current concurrency, plus its queue.
 *
 * <p>Output length assumption (written down because P6 requires it): the request's
 * {@code output_len} — the forced generation length from the trace — is taken as the
 * output bucket. That is the requested max tokens, not a prediction of early stopping.
 * Live and sim both see the same field, so the assumption holds across vehicles.
 *
 * <p>Prediction: {@code service} is the C-3 mean for (prompt, output, concurrency =
 * inflight+1, clamped to the grid). When the node has a free slot the request starts now
 * and completes in {@code service}. When it is full ({@code inflight >= capacity}) the
 * request queues behind {@code queueDepth} and drains at {@code capacity}-parallel rate:
 * {@code service + (queueDepth+1)*service/capacity}. Capacity defaults to 4 (every
 * measured run uses {@code --parallel 4}) when the launcher did not provide it.
 *
 * <p>Missing cell falls back to {@code (pending+1) * output_len / capability}, in
 * milliseconds, so an incomplete grid degrades to the scalar estimate rather than refusing
 * the node, and a priced node and an unpriced one are still compared in the same unit.
 */
public class ECT implements Policy {
    public static final String MODE_KNOWN = "known";
    public static final String MODE_UNKNOWN = "unknown";

    private final Map<String, CostModelSnapshot> snaps;
    private final Map<String, Integer> capacities;
    private final String mode;
    private final int priorOutputLen;

    public ECT(Map<String, CostModelSnapshot> snaps, Map<String, Integer> capacities) {
        this(snaps, capacities, MODE_KNOWN, 16);
    }

    public ECT(Map<String, CostModelSnapshot> snaps, Map<String, Integer> capacities, String mode, int priorOutputLen) {
        this.snaps = snaps != null ? snaps : Map.of();
        this.capacities = capacities != null ? capacities : Map.of();
        this.mode = MODE_UNKNOWN.equalsIgnoreCase(mode) ? MODE_UNKNOWN : MODE_KNOWN;
        this.priorOutputLen = priorOutputLen > 0 ? priorOutputLen : 16;
    }

    @Override
    public Choice choose(DispatchRequest request, List<NodeView> admissibleNodes, long nowNs, Random rng) {
        if (admissibleNodes.isEmpty()) {
            return new Choice(Optional.empty(), new HashMap<>(), null);
        }
        int promptLen = request.getPromptTokenIdsCount();
        int outputLen = MODE_UNKNOWN.equals(mode) ? priorOutputLen : request.getOutputLen();

        Map<String, Double> scores = new HashMap<>();
        for (NodeView n : admissibleNodes) {
            scores.put(n.nodeId(), predictedMs(n, promptLen, outputLen));
        }
        double draw = rng.nextDouble();
        String best = Policies.breakTie(admissibleNodes, scores, draw);
        return new Choice(Optional.of(best), scores, draw);
    }

    double predictedMs(NodeView n, int promptLen, int outputLen) {
        CostModelSnapshot snap = snaps.get(n.nodeId());
        int cap = Math.max(1, capacities.getOrDefault(n.nodeId(), 4));
        double service = meanMs(snap, promptLen, outputLen, n.inflight() + 1);
        if (service < 0) {
            // A predicted completion in milliseconds, like every other score this policy
            // returns: the request waits out everything on the node and then decodes its own
            // tokens at the node's capability. WJSQ's bare (pending+1)/capability has no
            // output length in it, so it is not a time, and comparing it against a priced
            // node's milliseconds would decide by units rather than by speed.
            double pending = n.queueDepth() + n.inflight();
            double capability = Math.max(n.capabilityTokS(), 0.001);
            return (pending + 1.0) * Math.max(outputLen, 1) / capability * 1000.0;
        }
        if (n.inflight() < cap) {
            return service;
        }
        return service + (n.queueDepth() + 1) * service / cap;
    }

    static double meanMs(CostModelSnapshot snap, int pLen, int oLen, int conc) {
        if (snap == null) return -1;
        List<CostModelSnapshot.CostEntry> candidates = new ArrayList<>();
        for (CostModelSnapshot.CostEntry e : snap.entries()) {
            if (pLen >= e.promptBucket().get(0) && pLen <= e.promptBucket().get(1)
                    && oLen >= e.outputBucket().get(0) && oLen <= e.outputBucket().get(1)) {
                candidates.add(e);
            }
        }
        if (candidates.isEmpty()) return -1;
        candidates.sort(Comparator.comparingInt(CostModelSnapshot.CostEntry::concurrency));
        for (CostModelSnapshot.CostEntry e : candidates) {
            if (e.concurrency() == conc) return e.serviceMsMean();
        }
        if (conc <= candidates.get(0).concurrency()) return candidates.get(0).serviceMsMean();
        if (conc >= candidates.get(candidates.size() - 1).concurrency())
            return candidates.get(candidates.size() - 1).serviceMsMean();
        CostModelSnapshot.CostEntry lower = null, upper = null;
        for (int i = 0; i < candidates.size() - 1; i++) {
            if (candidates.get(i).concurrency() < conc && conc < candidates.get(i + 1).concurrency()) {
                lower = candidates.get(i);
                upper = candidates.get(i + 1);
                break;
            }
        }
        if (lower == null || upper == null) return candidates.get(0).serviceMsMean();
        double f = (double) (conc - lower.concurrency()) / (double) (upper.concurrency() - lower.concurrency());
        return lower.serviceMsMean() + f * (upper.serviceMsMean() - lower.serviceMsMean());
    }
}
