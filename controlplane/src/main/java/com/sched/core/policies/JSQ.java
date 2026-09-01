package com.sched.core.policies;

import java.util.List;
import java.util.Map;
import java.util.HashMap;
import java.util.Optional;
import java.util.Random;
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.v1.DispatchRequest;

public class JSQ implements Policy {
    @Override
    public Choice choose(DispatchRequest request, List<NodeView> admissibleNodes, long nowNs, Random rng) {
        if (admissibleNodes.isEmpty()) {
            return new Choice(Optional.empty(), new HashMap<>(), null);
        }

        Map<String, Double> scores = new HashMap<>();
        for (NodeView n : admissibleNodes) {
            scores.put(n.nodeId(), (double) (n.queueDepth() + n.inflight()));
        }

        double draw = rng.nextDouble();
        String bestNode = admissibleNodes.stream()
            .min(java.util.Comparator.comparingDouble((NodeView n) -> scores.get(n.nodeId()))
            .thenComparing(n -> draw))
            .map(NodeView::nodeId).get();

        return new Choice(Optional.of(bestNode), scores, draw);
    }
}