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

        // Queue depths are small integers, so ties are the common case rather than the
        // exception, and how they are broken is most of what this policy does.
        double draw = rng.nextDouble();
        String bestNode = Policies.breakTie(admissibleNodes, scores, draw);

        return new Choice(Optional.of(bestNode), scores, draw);
    }
}
