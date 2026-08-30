package com.sched.core.policies;

import java.util.Comparator;
import java.util.List;
import java.util.Optional;
import java.util.Random;
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.v1.DispatchRequest;

public class WJSQ implements Policy {
    @Override
    public Optional<String> choose(DispatchRequest request, List<NodeView> admissibleNodes, long nowNs, Random rng) {
        if (admissibleNodes.isEmpty())
            return Optional.empty();

        return admissibleNodes.stream()
                .min(Comparator.comparingDouble((NodeView n) -> {
                    double pending = n.queueDepth() + n.inflight();
                    // Prevent division by zero if a node capability is reported as 0
                    double capability = Math.max(n.capabilityTokS(), 0.001);
                    return pending / capability;
                }).thenComparing(n -> rng.nextDouble()))
                .map(NodeView::nodeId);
    }
}