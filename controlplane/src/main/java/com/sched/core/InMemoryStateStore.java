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
     * Record that a request was admitted to a node. Mirror of SimNodeServer.admit.
     *
     * The inflight counter holds every request on the node, running or waiting. The view
     * splits it at the node's slot count: up to {@code capacity} are in flight and the
     * rest are queued. Deriving both from one count keeps them consistent however admits
     * and completions interleave; two counters moved separately let a completion report
     * seven in flight on a four-slot node.
     */
    public NodeView admit(String nodeId, int capacity) {
        AtomicInteger inf = inflight.computeIfAbsent(nodeId, k -> new AtomicInteger(0));
        queueDepth.computeIfAbsent(nodeId, k -> new AtomicInteger(0));
        return publish(nodeId, inf.incrementAndGet(), capacity);
    }

    /**
     * Record that a request finished on a node. Mirror of SimNodeServer.complete: the
     * finish frees a slot, and a queued request, if there is one, takes it.
     */
    public NodeView complete(String nodeId, int capacity) {
        AtomicInteger inf = inflight.get(nodeId);
        if (inf == null) return nodes.get(nodeId);
        return publish(nodeId, inf.updateAndGet(v -> Math.max(0, v - 1)), capacity);
    }

    private NodeView publish(String nodeId, int onNode, int capacity) {
        // A capacity of 0 means the manifest gave no slot count, and every request then
        // counts as queued, which is what admit did before the split was derived.
        int newInflight = Math.min(onNode, Math.max(0, capacity));
        int newQd = onNode - newInflight;
        queueDepth.get(nodeId).set(newQd);
        NodeView prev = nodes.get(nodeId);
        double capability = prev != null ? prev.capabilityTokS() : 0.0;
        NodeView upd = new NodeView(nodeId, newQd, newInflight, capability, 0L, true);
        nodes.put(nodeId, upd);
        return upd;
    }

    @Override
    public List<NodeView> getAllNodes() {
        List<NodeView> list = new ArrayList<>(nodes.values());
        list.sort(Comparator.comparing(NodeView::nodeId));
        return list;
    }
}