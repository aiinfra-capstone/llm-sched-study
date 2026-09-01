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
            return new Choice(Optional.empty(), scores, null);
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