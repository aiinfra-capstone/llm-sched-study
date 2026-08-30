package com.sched.core.policies;

import java.util.List;
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
    public Optional<String> choose(DispatchRequest request, List<NodeView> admissibleNodes, long nowNs, Random rng) {
        List<NodeView> strongNodes = admissibleNodes.stream()
                .filter(n -> n.capabilityTokS() >= cutoffT)
                .collect(Collectors.toList());

        if (strongNodes.isEmpty()) {
            return Optional.empty();
        }

        int index = Math.abs(counter.getAndIncrement()) % strongNodes.size();
        return Optional.of(strongNodes.get(index).nodeId());
    }
}