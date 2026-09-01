package com.sched.core.policies;

import java.util.List;
import java.util.Map;
import java.util.HashMap;
import java.util.Optional;
import java.util.Random;
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.v1.DispatchRequest;

public class StaticWeighted implements Policy {
    @Override
    public Choice choose(DispatchRequest req, List<NodeView> nodes, long t, Random rng) {
        if (nodes.isEmpty()) {
            return new Choice(Optional.empty(), new HashMap<>(), null);
        }

        Map<String, Double> scores = new HashMap<>();
        for (NodeView n : nodes) {
            scores.put(n.nodeId(), n.capabilityTokS());
        }

        double tot = nodes.stream().mapToDouble(NodeView::capabilityTokS).sum();
        if (tot <= 0) {
            return new Choice(Optional.of(nodes.get(rng.nextInt(nodes.size())).nodeId()), scores, null);
        }

        double draw = rng.nextDouble();
        double target = draw * tot;
        double cum = 0.0;

        for (NodeView n : nodes) {
            cum += n.capabilityTokS();
            if (target <= cum) {
                return new Choice(Optional.of(n.nodeId()), scores, draw);
            }
        }

        return new Choice(Optional.of(nodes.get(nodes.size() - 1).nodeId()), scores, draw);
    }
}