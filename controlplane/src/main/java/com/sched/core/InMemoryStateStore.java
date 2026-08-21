package com.sched.core;

import com.sched.core.interfaces.StateStore;
import java.util.List;
import java.util.Map;
import java.util.ArrayList;
import java.util.concurrent.ConcurrentHashMap;

public class InMemoryStateStore implements StateStore {
    private final Map<String, NodeView> nodes = new ConcurrentHashMap<>();

    /**
     * Updates or adds a node's current state.
     * Live path: Called when a Heartbeat or Completion gRPC arrives.
     * Sim path: Called by SimulationEvents in the DES.
     */
    public void updateNode(NodeView view) {
        nodes.put(view.nodeId(), view);
    }

    @Override
    public List<NodeView> getAllNodes() {
        return new ArrayList<>(nodes.values());
    }
}