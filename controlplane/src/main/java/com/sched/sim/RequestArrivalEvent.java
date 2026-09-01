package com.sched.sim;

import com.sched.core.models.TraceRequest;
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.core.AdmissionFilter;
import com.sched.core.StalenessVeil;
import com.sched.core.InMemoryStateStore;
import com.sched.core.DecisionLogger;
import com.sched.core.WorkerLogger;
import com.sched.core.ClientLogger;
import com.sched.core.models.SchedulerLogRecords.Candidate;
import com.sched.core.models.SchedulerLogRecords.DecisionRecord;
import com.sched.v1.DispatchRequest;
import java.util.List;
import java.util.Optional;
import java.util.Random;
import java.util.Collections;
import java.util.stream.Collectors;
import java.util.concurrent.atomic.AtomicLong;
import java.util.Set;

public class RequestArrivalEvent extends SimulationEvent {
    private final TraceRequest traceReq;
    private final Policy policy;
    private final StalenessVeil veil;
    private final AdmissionFilter filter;
    private final DiscreteEventSimulator des;
    private final Random rng;
    private final ServiceSampler sampler;
    private final InMemoryStateStore store;

    // Logging parameters
    private final DecisionLogger logger;
    private final String runId;
    private final String policyName;
    private final double stalenessParamS;
    private final AtomicLong decisionSeq;

    public RequestArrivalEvent(long scheduledTimeNs, TraceRequest traceReq, Policy policy,
            StalenessVeil veil, AdmissionFilter filter,
            DiscreteEventSimulator des, Random rng,
            ServiceSampler sampler, InMemoryStateStore store,
            DecisionLogger logger, String runId, String policyName,
            double stalenessParamS, AtomicLong decisionSeq) {
        super(scheduledTimeNs);
        this.traceReq = traceReq;
        this.policy = policy;
        this.veil = veil;
        this.filter = filter;
        this.des = des;
        this.rng = rng;
        this.sampler = sampler;
        this.store = store;
        this.logger = logger;
        this.runId = runId;
        this.policyName = policyName;
        this.stalenessParamS = stalenessParamS;
        this.decisionSeq = decisionSeq;
    }

    @Override
    public void execute() {
        DispatchRequest dispatchReq = DispatchRequest.newBuilder()
                .setReqId(traceReq.reqId())
                .setPriority(traceReq.priority())
                .setOutputLen(traceReq.outputLen())
                .setBucketId(traceReq.bucketId())
                .addAllPromptTokenIds(Collections.nCopies(traceReq.promptLen(), 0))
                .build();

        List<NodeView> allNodes = veil.getAllNodes();
        List<NodeView> admissibleNodes = filter.filterAdmissible(allNodes, dispatchReq);
        Set<String> admIds = admissibleNodes.stream().map(NodeView::nodeId).collect(Collectors.toSet());

        long startNs = System.nanoTime();
        Policy.Choice choice = policy.choose(dispatchReq, admissibleNodes, scheduledTimeNs, rng);
        long durationNs = System.nanoTime() - startNs;

        String chosenNode = choice.chosen().orElse(null);

        if (logger != null) {
            List<Candidate> candidates = allNodes.stream().map(nv -> {
                boolean isAdm = admIds.contains(nv.nodeId());
                Double score = choice.scores().get(nv.nodeId());
                return new Candidate(nv.nodeId(), nv.queueDepth(), nv.inflight(),
                        nv.capabilityTokS(), nv.estimateAgeMs(), isAdm, score);
            }).collect(Collectors.toList());

            DecisionRecord rec = new DecisionRecord(
                    "decision", runId, traceReq.reqId(), decisionSeq.getAndIncrement(),
                    policyName, stalenessParamS, durationNs, chosenNode, choice.tieBreakDraw(), candidates);
            logger.logRecord(rec);
        }

        if (chosenNode != null) {
            SimNodeServer server = des.getServer(chosenNode);
            if (server != null) {
                server.admit(traceReq, scheduledTimeNs, des, sampler, store, veil, logger, runId);
            }
        } else {
            ClientLogger clientLogger = des.getClientLogger();
            if (clientLogger != null) {
                long e2e = scheduledTimeNs - (long)(traceReq.arrivalOffsetS() * 1_000_000_000L);
                clientLogger.logRecord(new ClientLogger.ClientRecord(
                    runId, traceReq.reqId(), traceReq.arrivalOffsetS(), traceReq.arrivalOffsetS(), 0.0, e2e, "dropped",
                    traceReq.outputLen(), null, null, 0L
                ));
            }
        }
    }
}