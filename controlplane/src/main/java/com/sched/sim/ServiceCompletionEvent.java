package com.sched.sim;

import com.sched.core.DecisionLogger;
import com.sched.core.WorkerLogger;
import com.sched.core.ClientLogger;
import com.sched.core.models.SchedulerLogRecords.CompletionObservedRecord;
import com.sched.core.InMemoryStateStore;
import com.sched.core.StalenessVeil;
import com.sched.core.WorkerLogger.WorkerRecord;
import com.sched.core.ClientLogger.ClientRecord;

public class ServiceCompletionEvent extends SimulationEvent {
    private final SimNodeServer server;
    private final SimNodeServer.Admitted request;
    private final long startNs;
    private final long serviceNs;
    private final int concurrencyAtStart;
    private final DiscreteEventSimulator des;
    private final ServiceSampler sampler;
    private final InMemoryStateStore store;
    private final StalenessVeil veil;
    private final DecisionLogger logger;
    private final String runId;

    public ServiceCompletionEvent(long tNs, SimNodeServer server, SimNodeServer.Admitted request, long startNs, long serviceNs, int concurrencyAtStart, DiscreteEventSimulator des, ServiceSampler sampler, InMemoryStateStore store, StalenessVeil veil, DecisionLogger logger, String runId) {
        super(tNs);
        this.server = server;
        this.request = request;
        this.startNs = startNs;
        this.serviceNs = serviceNs;
        this.concurrencyAtStart = concurrencyAtStart;
        this.des = des;
        this.sampler = sampler;
        this.store = store;
        this.veil = veil;
        this.logger = logger;
        this.runId = runId;
    }

    @Override
    public void execute() {
        server.complete(scheduledTimeNs, des, sampler, store, veil, logger, runId);

        if (logger != null) {
            logger.logRecord(new CompletionObservedRecord(
                    "completion_observed", runId, request.req().reqId(), server.getNodeId(), "completion_rpc", 0L));
        }

        long queueWaitNs = startNs - request.admitNs();
        double kvOccupancy = (double) concurrencyAtStart / Math.max(1, server.getBatchCapacity());

        WorkerLogger workerLogger = des.getWorkerLogger();
        if (workerLogger != null) {
            workerLogger.logRecord(new WorkerRecord(
                runId, request.req().reqId(), server.getNodeId(), "llamacpp", queueWaitNs, serviceNs,
                request.req().promptLen(), request.req().outputLen(), concurrencyAtStart,
                request.inflightAtAdmit(),
                kvOccupancy,
                "ok"
            ));
        }

        ClientLogger clientLogger = des.getClientLogger();
        if (clientLogger != null) {
            // The client sees the engine's span plus the hops around it: request in over
            // gRPC, the decision, dispatch to the worker, and the F-11 direct return. The
            // cost model is fitted on service_ns alone and contains none of that, so the
            // measured per-request transport is added once, here, at the client boundary.
            // It deliberately does not enter serviceNs: the hop overlaps other requests
            // rather than occupying a batch slot, so charging it to the node would inflate
            // queueing that the hardware does not have.
            long overheadNs = des.getTransportOverhead().sampleNs();
            long e2e = scheduledTimeNs + overheadNs - (long)(request.req().arrivalOffsetS() * 1_000_000_000L);
            clientLogger.logRecord(new ClientRecord(
                runId, request.req().reqId(), request.req().arrivalOffsetS(), request.req().arrivalOffsetS(), 0.0, e2e, "ok",
                request.req().outputLen(), server.getNodeId(), server.getNodeId(), 0L
            ));
        }
    }
}
