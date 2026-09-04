package com.sched.core.policies;

import java.util.List;
import java.util.Map;
import java.util.HashMap;
import java.util.Optional;
import java.util.Random;
import java.util.concurrent.atomic.AtomicInteger;
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.v1.DispatchRequest;

public class RoundRobin implements Policy {
    private final AtomicInteger counter;

    public RoundRobin(AtomicInteger initialCounter) {
        this.counter = initialCounter;
    }

    @Override
    public Choice choose(DispatchRequest request, List<NodeView> admissibleNodes, long nowNs, Random rng) {
        if (admissibleNodes.isEmpty()) {
            return new Choice(Optional.empty(), new HashMap<>(), null);
        }
        // floorMod, not Math.abs: Math.abs(Integer.MIN_VALUE) is itself, negative, so
        // once the counter wraps past Integer.MAX_VALUE the index goes negative and
        // get() throws. Identical to the old expression for every non-negative count.
        int index = Math.floorMod(counter.getAndIncrement(), admissibleNodes.size());
        String chosen = admissibleNodes.get(index).nodeId();
        
        Map<String, Double> scores = new HashMap<>();
        for (int i = 0; i < admissibleNodes.size(); i++) {
            scores.put(admissibleNodes.get(i).nodeId(), i == index ? 1.0 : 0.0);
        }
        return new Choice(Optional.of(chosen), scores, null);
    }
}