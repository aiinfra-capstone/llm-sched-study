package com.sched.core.interfaces;

import java.util.List;

public interface StateStore {
    /**
     * Represents the scheduler's belief about a node's state.
     */
    record NodeView(
            String nodeId,
            int queueDepth,
            int inflight,
            double capabilityTokS,
            long estimateAgeMs,
            boolean isAdmissible) {
    }

    /**
     * Returns the current view of all nodes in the pool.
     */
    List<NodeView> getAllNodes();
}