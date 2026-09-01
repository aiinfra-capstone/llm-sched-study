package com.sched.sim;

import java.util.ArrayDeque;
import com.sched.core.models.TraceRequest;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.core.InMemoryStateStore;
import com.sched.core.StalenessVeil;

public final class SimNodeServer {
    private final String nodeId;
    private final int batchCapacity;
    private final ArrayDeque<Admitted> waiting = new ArrayDeque<>();
    private int busy = 0;

    public record Admitted(TraceRequest req, long admitNs, int inflightAtAdmit) {}

    public SimNodeServer(String nodeId, int batchCapacity) {
        this.nodeId = nodeId;
        this.batchCapacity = batchCapacity;
    }

    public String getNodeId() { return nodeId; }
    public int queueDepth() { return waiting.size(); }
    public int inflight() { return busy; }
    public int getBatchCapacity() { return batchCapacity; }

    public void admit(TraceRequest request, long nowNs, DiscreteEventSimulator des, ServiceSampler sampler, InMemoryStateStore store, StalenessVeil veil, com.sched.core.DecisionLogger logger, String runId) {
        Admitted admitted = new Admitted(request, nowNs, busy);
        if (busy < batchCapacity) {
            start(admitted, nowNs, des, sampler, store, veil, logger, runId);
        } else {
            waiting.addLast(admitted);
            updateStore(store, veil);
        }
    }

    private void start(Admitted request, long nowNs, DiscreteEventSimulator des, ServiceSampler sampler, InMemoryStateStore store, StalenessVeil veil, com.sched.core.DecisionLogger logger, String runId) {
        busy++;
        int concurrency = busy;
        updateStore(store, veil);

        long serviceNs = sampler.sampleServiceNs(nodeId, request.req().promptLen(), request.req().outputLen(), concurrency);
        if (serviceNs < 0) serviceNs = 100_000_000L;

        des.scheduleEvent(new ServiceCompletionEvent(nowNs + serviceNs, this, request, nowNs, serviceNs, concurrency, des, sampler, store, veil, logger, runId));
    }

    public void complete(long nowNs, DiscreteEventSimulator des, ServiceSampler sampler, InMemoryStateStore store, StalenessVeil veil, com.sched.core.DecisionLogger logger, String runId) {
        busy--;
        updateStore(store, veil);
        if (!waiting.isEmpty()) {
            Admitted next = waiting.removeFirst();
            start(next, nowNs, des, sampler, store, veil, logger, runId);
        }
    }

    private void updateStore(InMemoryStateStore store, StalenessVeil veil) {
        for (NodeView nv : store.getAllNodes()) {
            if (nv.nodeId().equals(nodeId)) {
                NodeView upd = new NodeView(nv.nodeId(), queueDepth(), inflight(), nv.capabilityTokS(), nv.estimateAgeMs(), nv.isAdmissible());
                store.updateNode(upd);
                veil.updateNode(upd);
                break;
            }
        }
    }
}
