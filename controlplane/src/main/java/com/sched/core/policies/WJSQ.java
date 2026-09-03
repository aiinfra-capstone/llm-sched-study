package com.sched.core.policies;

import java.util.List;
import java.util.Map;
import java.util.HashMap;
import java.util.Optional;
import java.util.Random;
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.v1.DispatchRequest;

public class WJSQ implements Policy {
    @Override
    public Choice choose(DispatchRequest request, List<NodeView> admissibleNodes, long nowNs, Random rng) {
        if (admissibleNodes.isEmpty()) {
            return new Choice(Optional.empty(), new HashMap<>(), null);
        }

        // (pending + 1) / capability, not pending / capability. The score has to answer
        // "when would this request finish here", and the request being placed is the +1.
        // Without it every idle node scores 0 no matter how fast it is, so a 100 tok/s GPU
        // and a 1 tok/s CPU are indistinguishable exactly when the choice is free, and the
        // capability weighting only starts working once the pool is already loaded. With
        // it an idle GPU scores 0.01 against the idle CPU's 1.0 and wins, which is the
        // whole point of a capability-weighted policy.
        Map<String, Double> scores = new HashMap<>();
        for (NodeView n : admissibleNodes) {
            double pending = n.queueDepth() + n.inflight();
            double capability = Math.max(n.capabilityTokS(), 0.001);
            scores.put(n.nodeId(), (pending + 1.0) / capability);
        }

        double draw = rng.nextDouble();
        String bestNode = Policies.breakTie(admissibleNodes, scores, draw);

        return new Choice(Optional.of(bestNode), scores, draw);
    }
}
