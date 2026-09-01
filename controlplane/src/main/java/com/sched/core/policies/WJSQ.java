package com.sched.core.policies;

import java.util.List;
import java.util.Map;
import java.util.HashMap;
import java.util.Optional;
import java.util.Random;
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.v1.DispatchRequest;

public class WJSQ implements Policy {
    @Override
    public Choice choose(DispatchRequest request, List<NodeView> admissibleNodes, long nowNs, Random rng) {
        if (admissibleNodes.isEmpty()) {
            return new Choice(Optional.empty(), new HashMap<>(), null);
        }

        Map<String, Double> scores = new HashMap<>();
        double draw = rng.nextDouble();

        String bestNode = admissibleNodes.stream()
            .min(java.util.Comparator.comparingDouble((NodeView n) -> {
                double pending = n.queueDepth() + n.inflight();
                double capability = Math.max(n.capabilityTokS(), 0.001);
                double score = pending / capability;
                scores.put(n.nodeId(), score);
                return score;
            }).thenComparing(n -> draw))
            .map(NodeView::nodeId).get();

        return new Choice(Optional.of(bestNode), scores, draw);
    }
}