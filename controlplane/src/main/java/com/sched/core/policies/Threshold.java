package com.sched.core.policies;

import java.util.List;
import java.util.Map;
import java.util.HashMap;
import java.util.Optional;
import java.util.Random;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Collectors;
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.v1.DispatchRequest;

public class Threshold implements Policy {
    private final double cutoffT;
    private final AtomicInteger counter;

    public Threshold(double cutoffT, AtomicInteger initialCounter) {
        this.cutoffT = cutoffT;
        this.counter = initialCounter;
    }

    @Override
    public Choice choose(DispatchRequest request, List<NodeView> admissibleNodes, long nowNs, Random rng) {
        if (admissibleNodes.isEmpty()) {
            return new Choice(Optional.empty(), new HashMap<>(), null);
        }

        Map<String, Double> scores = new HashMap<>();
        List<NodeView> strongNodes = admissibleNodes.stream()
                .filter(n -> {
                    boolean ok = n.capabilityTokS() >= cutoffT;
                    scores.put(n.nodeId(), ok ? 1.0 : 0.0);
                    return ok;
                })
                .collect(Collectors.toList());

        if (strongNodes.isEmpty()) {
            for (NodeView n : admissibleNodes) {
                if (!scores.containsKey(n.nodeId())) {
                    scores.put(n.nodeId(), 0.0);
                }
            }
            // No node clears the cutoff, so serve on the fastest one there is rather than
            // rejecting. A request the pool could have answered is a scheduling decision,
            // not an admission decision: F-13 admissibility is about whether a node can run
            // the shape at all, and every node here has already passed it. Dropping instead
            // would make this arm produce no latency at all whenever T sits above the pool,
            // which reads as a policy with perfect tail behaviour and zero throughput.
            NodeView best = admissibleNodes.get(0);
            for (NodeView n : admissibleNodes) {
                if (n.capabilityTokS() > best.capabilityTokS()) best = n;
            }
            return new Choice(Optional.of(best.nodeId()), scores, null);
        }

        for (NodeView n : admissibleNodes) {
            if (!scores.containsKey(n.nodeId())) {
                scores.put(n.nodeId(), 0.0);
            }
        }

        int index = Math.abs(counter.getAndIncrement()) % strongNodes.size();
        return new Choice(Optional.of(strongNodes.get(index).nodeId()), scores, null);
    }
}