package com.sched.core.interfaces;

import java.util.List;
import java.util.Optional;
import java.util.Random;
import com.sched.v1.DispatchRequest;
import com.sched.core.interfaces.StateStore.NodeView;

public interface Policy {
    /**
     * A pure function to select the optimal node for a given request.
     * 
     * @param request         The incoming DispatchRequest from the trace/client.
     * @param admissibleNodes The current view of nodes, pre-filtered for
     *                        admissibility (F-14) and aged by StalenessVeil (F-8).
     * @param nowNs           Current monotonic time (provided by the Clock
     *                        interface).
     * @param rng             Injected RNG for deterministic tie-breaking.
     * @return The chosen Node ID, or Optional.empty() if no node can handle the
     *         request.
     */
    Optional<String> choose(DispatchRequest request, List<NodeView> admissibleNodes, long nowNs, Random rng);
}