package com.sched.core;

import com.sched.core.interfaces.StateStore;
import java.util.List;
import java.util.Map;
import java.util.ArrayList;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.Comparator;

public class InMemoryStateStore implements StateStore {
    private final Map<String, NodeView> nodes = new ConcurrentHashMap<>();
    // Live admission/departure counters per node, kept here so dispatch can
    // see the same view of state that heartbeats later confirm. The DES
    // runs admit() in SimNodeServer and updates state from there; the live
    // path has no SimNodeServer, so the equivalent lives here.
    private final Map<String, AtomicInteger> inflight = new ConcurrentHashMap<>();
    private final Map<String, AtomicInteger> queueDepth = new ConcurrentHashMap<>();

    public void updateNode(NodeView view) {
        nodes.put(view.nodeId(), view);
        inflight.computeIfAbsent(view.nodeId(), k -> new AtomicInteger(view.inflight()));
        queueDepth.computeIfAbsent(view.nodeId(), k -> new AtomicInteger(view.queueDepth()));
    }

    public NodeView getNode(String nodeId) {
        return nodes.get(nodeId);
    }

    /**
     * Record that a request was admitted to a node. Increments inflight and,
     * if the node is at capacity, queue depth. Mirror of SimNodeServer.admit.
     */
    public NodeView admit(String nodeId, int capacity) {
        AtomicInteger inf = inflight.computeIfAbsent(nodeId, k -> new AtomicInteger(0));
        AtomicInteger qd = queueDepth.computeIfAbsent(nodeId, k -> new AtomicInteger(0));
        int newInflight = inf.incrementAndGet();
        int newQd = qd.get();
        if (newInflight > capacity) {
            newQd = qd.incrementAndGet();
            newInflight = capacity;
            // inflight stays at capacity, queue holds the excess
        }
        NodeView prev = nodes.get(nodeId);
        double capability = prev != null ? prev.capabilityTokS() : 0.0;
        NodeView upd = new NodeView(nodeId, newQd, newInflight, capability, 0L, true);
        nodes.put(nodeId, upd);
        veilRecord(upd);
        return upd;
    }

    /**
     * Record that a request finished on a node. Decrements inflight and, if
     * the queue had been holding, dequeues one and hands its slot to the engine.
     * Mirror of SimNodeServer.complete.
     */
    public NodeView complete(String nodeId, int capacity) {
        AtomicInteger inf = inflight.get(nodeId);
        AtomicInteger qd = queueDepth.get(nodeId);
        if (inf == null) return nodes.get(nodeId);
        int curQd = qd != null ? qd.get() : 0;
        int newQd = curQd;
        if (curQd > 0) {
            newQd = qd.decrementAndGet();
        }
        int newInflight = Math.max(0, inf.decrementAndGet());
        NodeView prev = nodes.get(nodeId);
        double capability = prev != null ? prev.capabilityTokS() : 0.0;
        NodeView upd = new NodeView(nodeId, newQd, newInflight, capability, 0L, true);
        nodes.put(nodeId, upd);
        veilRecord(upd);
        return upd;
    }

    private void veilRecord(NodeView v) {
        // Live store and StalenessVeil are separate components; the live
        // service updates both, the DES does not have a separate veil. Keep
        // the call here for symmetry with the rest of the package.
    }

    @Override
    public List<NodeView> getAllNodes() {
        List<NodeView> list = new ArrayList<>(nodes.values());
        list.sort(Comparator.comparing(NodeView::nodeId));
        return list;
    }
}