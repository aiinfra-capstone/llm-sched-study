package com.sched.core.policies;

import java.util.List;
import java.util.Optional;
import java.util.Random;
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.v1.DispatchRequest;

public class StaticWeighted implements Policy {
    @Override
    public Optional<String> choose(DispatchRequest req, List<NodeView> nodes, long t, Random rng) {
        if (nodes.isEmpty()) {
            return Optional.empty();
        }

        double tot = nodes.stream()
                .mapToDouble(NodeView::capabilityTokS)
                .sum();

        if (tot <= 0) {
            return Optional.of(nodes.get(rng.nextInt(nodes.size())).nodeId());
        }

        double draw = rng.nextDouble() * tot;
        double cum = 0.0;

        for (NodeView n : nodes) {
            cum += n.capabilityTokS();
            if (draw <= cum) {
                return Optional.of(n.nodeId());
            }
        }

        return Optional.of(nodes.get(nodes.size() - 1).nodeId());
    }
}