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
    // Every request admitted and not yet released, mapped to the node it was admitted to.
    // A dispatch whose Execute blew its deadline after the worker accepted it is rolled
    // back by the scheduler and later completed by the worker. Both go through this map,
    // so only the first of the two releases the slot.
    //
    // An entry whose completion never arrives, say because its worker died mid-request,
    // stays here. That leak is bounded by one run: every run starts its own scheduler
    // process, so the map never outlives the requests of a single run.
    private final Map<String, String> admitted = new ConcurrentHashMap<>();

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
     * Record that request {@code reqId} was admitted to a node.
     *
     * <p>Returns null, and counts nothing, when {@code reqId} already holds a slot on any
     * node. A duplicate req_id on a second node would otherwise hold a slot there that no
     * completion ever releases, since the completion releases by req_id on the first node.
     * An empty req_id cannot be tracked and falls back to {@link #admit(String, int)}.
     */
    public NodeView admit(String nodeId, String reqId, int capacity) {
        if (reqId == null || reqId.isEmpty()) return admit(nodeId, capacity);
        if (admitted.putIfAbsent(reqId, nodeId) != null) return null;
        return admit(nodeId, capacity);
    }

    /**
     * Release the slot request {@code reqId} holds on a node, whether it finished or its
     * dispatch was rolled back. Returns null, and changes nothing, when that req_id holds no
     * slot on that node: it was already released, or it was never admitted here. An empty
     * req_id falls back to {@link #complete(String, int)}.
     */
    public NodeView complete(String nodeId, String reqId, int capacity) {
        if (reqId == null || reqId.isEmpty()) return complete(nodeId, capacity);
        if (!admitted.remove(reqId, nodeId)) return null;
        return complete(nodeId, capacity);
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