package com.sched.sim;

import com.sched.core.InMemoryStateStore;
import com.sched.core.interfaces.StateStore.NodeView;

public class CompletionEvent extends SimulationEvent {
    private final String nId;
    private final InMemoryStateStore store;

    public CompletionEvent(long tNs, String nId, InMemoryStateStore store) {
        super(tNs);
        this.nId = nId;
        this.store = store;
    }

    @Override
    public void execute() {
        for (NodeView nv : store.getAllNodes()) {
            if (nv.nodeId().equals(nId)) {
                int inf = Math.max(0, nv.inflight() - 1);

                NodeView upd = new NodeView(
                        nv.nodeId(),
                        nv.queueDepth(),
                        inf,
                        nv.capabilityTokS(),
                        nv.estimateAgeMs(),
                        nv.isAdmissible());

                store.updateNode(upd);
                break;
            }
        }
    }
}