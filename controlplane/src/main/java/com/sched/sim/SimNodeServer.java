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
    private final java.util.List<Running> active = new java.util.ArrayList<>();

    public record Admitted(TraceRequest req, long admitNs, int inflightAtAdmit) {}

    private static class Running {
        Admitted admitted;
        long startNs;
        long serviceNs;
        int concurrency;
        double meanMs;
        ServiceCompletionEvent event;
        Running(Admitted a, long sNs, long svcNs, int conc, double mean, ServiceCompletionEvent ev) {
            admitted = a; startNs = sNs; serviceNs = svcNs; concurrency = conc; meanMs = mean; event = ev;
        }
    }

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
        int prevBusy = busy;
        busy++;
        int concurrency = busy;
        updateStore(store, veil);

        // Re-evaluate service time for already-running requests when batch composition changes
        if (prevBusy > 0) {
            reevaluateActive(nowNs, concurrency, sampler, des, store, veil, logger, runId);
        }

        double meanMs = sampler.getMeanMs(nodeId, request.req().promptLen(), request.req().outputLen(), concurrency);
        long serviceNs = sampler.sampleServiceNs(nodeId, request.req().promptLen(), request.req().outputLen(), concurrency);
        if (meanMs < 0 || serviceNs < 0) {
            // The old behaviour here was to substitute 100 ms and carry on, which turns a
            // hole in the cost model into a plausible-looking latency that no measurement
            // supports. It is reachable whenever the snapshot's admissibility bounds are
            // wider than the grid that was actually sampled: AdmissionFilter passes the
            // request on max_prompt/max_output, then no bucket covers it. Say so instead.
            throw new IllegalStateException(String.format(
                "node %s has no cost model cell for prompt=%d output=%d concurrency=%d. "
                + "The snapshot's admissibility bounds are wider than its calibrated grid, "
                + "so the request was admitted and then had no measured service time. "
                + "Calibrate the missing cell or narrow admissibility to what was sampled.",
                nodeId, request.req().promptLen(), request.req().outputLen(), concurrency));
        }

        ServiceCompletionEvent ev = new ServiceCompletionEvent(nowNs + serviceNs, this, request, nowNs, serviceNs, concurrency, des, sampler, store, veil, logger, runId);
        active.add(new Running(request, nowNs, serviceNs, concurrency, meanMs, ev));
        des.scheduleEvent(ev);
    }

    public void complete(long nowNs, DiscreteEventSimulator des, ServiceSampler sampler, InMemoryStateStore store, StalenessVeil veil, com.sched.core.DecisionLogger logger, String runId) {
        // Remove the entry whose event fires now (the request that just completed)
        java.util.Iterator<Running> it = active.iterator();
        while (it.hasNext()) {
            Running r = it.next();
            if (!r.event.isCancelled() && r.event.getScheduledTimeNs() == nowNs) {
                it.remove();
                break;
            }
        }
        // Clean any cancelled leftovers (rescheduled events)
        active.removeIf(r -> r.event.isCancelled());
        busy--;
        if (busy < 0) busy = 0;
        updateStore(store, veil);
        if (!active.isEmpty() && busy > 0) {
            reevaluateActive(nowNs, busy, sampler, des, store, veil, logger, runId);
        }
        if (!waiting.isEmpty()) {
            Admitted next = waiting.removeFirst();
            start(next, nowNs, des, sampler, store, veil, logger, runId);
        }
    }

    private void reevaluateActive(long nowNs, int newConcurrency, ServiceSampler sampler, DiscreteEventSimulator des, InMemoryStateStore store, StalenessVeil veil, com.sched.core.DecisionLogger logger, String runId) {
        for (Running r : new java.util.ArrayList<>(active)) {
            if (r.event.isCancelled()) continue;
            long remaining = r.event.getScheduledTimeNs() - nowNs;
            if (remaining <= 0) continue;
            double newMean = sampler.getMeanMs(nodeId, r.admitted.req().promptLen(), r.admitted.req().outputLen(), newConcurrency);
            if (newMean < 0 || r.meanMs <= 0) continue;
            double scale = newMean / r.meanMs;
            // Only stretch if concurrency increased (scale>1); shrink if concurrency decreased
            // Apply scaling to remaining time to model contention change
            if (Math.abs(scale - 1.0) < 1e-9) continue;
            long newRemaining = (long)(remaining * scale);
            if (newRemaining < 1_000_000L) newRemaining = 1_000_000L;
            r.event.cancel();
            ServiceCompletionEvent newEv = new ServiceCompletionEvent(nowNs + newRemaining, this, r.admitted, r.startNs, r.serviceNs, r.concurrency, des, sampler, store, veil, logger, runId);
            r.event = newEv;
            r.meanMs = newMean;
            // keep original serviceNs for logging; update scheduled time via new event
            des.scheduleEvent(newEv);
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
