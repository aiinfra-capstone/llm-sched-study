package com.sched.core.policies;

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
 * {@code service + (queueDepth+1)*service/capacity}, where capacity is the node's slot
 * count from the manifest. A priced node without one is refused at construction.
 *
 * <p>Nothing about the model is defaulted. The mode ({@code known} or {@code unknown}) comes
 * from the run's config, and in {@code unknown} mode so does the output-length prior, so a
 * manifest always states what ECT assumed.
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

    /**
     * @param mode {@link #MODE_KNOWN} or {@link #MODE_UNKNOWN}; anything else is refused
     * @param priorOutputLen the output length assumed in unknown mode, or null in known mode
     * @throws IllegalArgumentException when the mode is not one of the two, unknown mode has
     *     no positive prior, or a node with a snapshot has no positive capacity
     */
    public ECT(Map<String, CostModelSnapshot> snaps, Map<String, Integer> capacities, String mode,
            Integer priorOutputLen) {
        this.snaps = snaps != null ? snaps : Map.of();
        this.capacities = capacities != null ? capacities : Map.of();
        if (!MODE_KNOWN.equals(mode) && !MODE_UNKNOWN.equals(mode)) {
            throw new IllegalArgumentException(
                "ect_mode must be '" + MODE_KNOWN + "' or '" + MODE_UNKNOWN + "', got " + mode);
        }
        this.mode = mode;
        if (MODE_UNKNOWN.equals(mode) && (priorOutputLen == null || priorOutputLen <= 0)) {
            throw new IllegalArgumentException(
                "ect_mode 'unknown' needs a positive output_len_prior, got " + priorOutputLen);
        }
        this.priorOutputLen = priorOutputLen != null ? priorOutputLen : 0;
        for (String nodeId : this.snaps.keySet()) {
            Integer cap = this.capacities.get(nodeId);
            if (cap == null || cap <= 0) {
                throw new IllegalArgumentException(
                    "ECT prices " + nodeId + " but has no slot count for it (capacity " + cap + ")");
            }
        }
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
        double service = snap == null ? -1 : snap.meanServiceMs(promptLen, outputLen, n.inflight() + 1);
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
        int cap = capacities.get(n.nodeId());
        if (n.inflight() < cap) {
            return service;
        }
        return service + (n.queueDepth() + 1) * service / cap;
    }
}
