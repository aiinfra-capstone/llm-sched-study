package com.sched.core.policies;

import java.util.List;
import java.util.Map;
import java.util.HashMap;
import java.util.Optional;
import java.util.Random;
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.v1.DispatchRequest;

public class StaticWeightedWRR implements Policy {
    private final Map<String, Double> current = new HashMap<>();

    public StaticWeightedWRR() {
    }

    @Override
    public Choice choose(DispatchRequest req, List<NodeView> nodes, long t, Random rng) {
        if (nodes.isEmpty()) {
            return new Choice(Optional.empty(), new HashMap<>(), null);
        }

        Map<String, Double> scores = new HashMap<>();
        double tot = 0.0;
        for (NodeView n : nodes) {
            double cap = n.capabilityTokS();
            scores.put(n.nodeId(), cap);
            tot += cap;
        }
        if (tot <= 0) {
            return new Choice(Optional.of(nodes.get(0).nodeId()), scores, null);
        }

        // Smooth weighted round-robin: no random draw, so the long-run share is
        // the policy's share and not a property of one random stream. Each call
        // credits every node its capability, serves the highest credit, and
        // debits the total. Over N decisions node i is served capability_i /
        // total * N times (rounded to whole requests).
        double total = tot;
        String best = null;
        double bestCredit = Double.NEGATIVE_INFINITY;
        for (NodeView n : nodes) {
            double credit = current.getOrDefault(n.nodeId(), 0.0)
                    + Math.max(n.capabilityTokS(), 0.0);
            current.put(n.nodeId(), credit);
            if (credit > bestCredit) {
                bestCredit = credit;
                best = n.nodeId();
            }
        }
        current.put(best, current.get(best) - total);
        return new Choice(Optional.of(best), scores, null);
    }
}
