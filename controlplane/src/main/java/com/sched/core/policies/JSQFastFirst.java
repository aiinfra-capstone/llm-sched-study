package com.sched.core.policies;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.HashMap;
import java.util.Optional;
import java.util.Random;
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.v1.DispatchRequest;

/**
 * JSQ with ties broken deterministically toward the highest-capability node.
 *
 * <p>Queue depths are small integers, so 30 to 53% of JSQ decisions are ties
 * broken at random. WJSQ at a 1.57 ratio behaves close to JSQ with ties sent
 * to the fast node, so part of WJSQ - JSQ may be one ordinal bit anyone can
 * guess. This arm isolates that bit: same scores as JSQ, no random draw.
 */
public class JSQFastFirst implements Policy {
    @Override
    public Choice choose(DispatchRequest request, List<NodeView> admissibleNodes, long nowNs, Random rng) {
        if (admissibleNodes.isEmpty()) {
            return new Choice(Optional.empty(), new HashMap<>(), null);
        }

        Map<String, Double> scores = new HashMap<>();
        for (NodeView n : admissibleNodes) {
            scores.put(n.nodeId(), (double) (n.queueDepth() + n.inflight()));
        }

        double best = Double.POSITIVE_INFINITY;
        for (NodeView n : admissibleNodes) {
            double sc = scores.get(n.nodeId());
            if (sc < best) best = sc;
        }
        List<NodeView> tied = new ArrayList<>();
        for (NodeView n : admissibleNodes) {
            if (scores.get(n.nodeId()) <= best + 1e-6) tied.add(n);
        }
        if (tied.size() == 1) {
            return new Choice(Optional.of(tied.get(0).nodeId()), scores, null);
        }
        // Fastest first, and among equal capabilities the smallest node id, so the choice
        // never depends on the order the state store happens to list its nodes in.
        NodeView fast = tied.get(0);
        for (NodeView n : tied) {
            if (n.capabilityTokS() > fast.capabilityTokS()
                    || (n.capabilityTokS() == fast.capabilityTokS()
                        && n.nodeId().compareTo(fast.nodeId()) < 0)) {
                fast = n;
            }
        }
        return new Choice(Optional.of(fast.nodeId()), scores, null);
    }
}
