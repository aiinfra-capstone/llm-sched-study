package com.sched.core.policies;

import java.util.List;
import java.util.Optional;
import java.util.Random;
import java.util.concurrent.atomic.AtomicInteger;
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.v1.DispatchRequest;

public class RoundRobin implements Policy {

    // Explicit policy state injected from the outside to guarantee determinism during replay
    private final AtomicInteger counter;

    public RoundRobin(AtomicInteger initialCounter) {
        this.counter = initialCounter;
    }

    @Override
    public Optional<String> choose(DispatchRequest request, List<NodeView> admissibleNodes, long nowNs, Random rng) {
        // The architecture specifies that the AdmissionFilter applies F-14 outside the policy.
        // Therefore, we assume admissibleNodes contains ONLY nodes that can serve this request.

        if (admissibleNodes.isEmpty()) {
            return Optional.empty(); // Will trigger reject_reason = "no_admissible_node" in DispatchAck
        }

        // Standard round-robin logic over the admissible subset
        int index = Math.abs(counter.getAndIncrement()) % admissibleNodes.size();
        return Optional.of(admissibleNodes.get(index).nodeId());
    }
}