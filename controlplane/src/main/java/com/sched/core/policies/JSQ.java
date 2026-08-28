package com.sched.core.policies;

import java.util.Comparator;
import java.util.List;
import java.util.Optional;
import java.util.Random;
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.v1.DispatchRequest;

public class JSQ implements Policy {
    @Override
    public Optional<String> choose(DispatchRequest request, List<NodeView> admissibleNodes, long nowNs, Random rng) {
        if (admissibleNodes.isEmpty())
            return Optional.empty();

        return admissibleNodes.stream()
                // F-4: Account for both queue depth and in-flight count
                .min(Comparator.comparingDouble((NodeView n) -> (double) (n.queueDepth() + n.inflight()))
                        // Tie-breaker using injected RNG
                        .thenComparing(n -> rng.nextDouble()))
                .map(NodeView::nodeId);
    }
}