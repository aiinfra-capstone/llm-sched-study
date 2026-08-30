package com.sched.sim;

import com.sched.core.InMemoryStateStore;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.core.DecisionLogger;
import com.sched.core.models.SchedulerLogRecords.CompletionObservedRecord;

public class CompletionEvent extends SimulationEvent {
    private final String nId;
    private final InMemoryStateStore store;
    private final DecisionLogger logger;
    private final String runId;
    private final String reqId;

    public CompletionEvent(long tNs, String nId, InMemoryStateStore store, DecisionLogger logger, String runId,
            String reqId) {
        super(tNs);
        this.nId = nId;
        this.store = store;
        this.logger = logger;
        this.runId = runId;
        this.reqId = reqId;
    }

    @Override
    public void execute() {
        for (NodeView nv : store.getAllNodes()) {
            if (nv.nodeId().equals(nId)) {
                int inf = Math.max(0, nv.inflight() - 1);

                NodeView upd = new NodeView(
                        nv.nodeId(), nv.queueDepth(), inf,
                        nv.capabilityTokS(), nv.estimateAgeMs(), nv.isAdmissible());

                store.updateNode(upd);

                // Write the completion record to the C-4 log
                if (logger != null) {
                    logger.logRecord(new CompletionObservedRecord(
                            "completion_observed", runId, reqId, nId, "sim_event", 0L));
                }
                break;
            }
        }
    }
}