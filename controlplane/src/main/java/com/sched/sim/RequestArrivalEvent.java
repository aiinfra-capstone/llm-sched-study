package com.sched.sim;

import com.sched.core.models.TraceRequest;
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.core.AdmissionFilter;
import com.sched.core.StalenessVeil;
import com.sched.core.InMemoryStateStore;
import com.sched.core.DecisionLogger;
import com.sched.core.models.SchedulerLogRecords.Candidate;
import com.sched.core.models.SchedulerLogRecords.DecisionRecord;
import com.sched.v1.DispatchRequest;
import java.util.List;
import java.util.Optional;
import java.util.Random;
import java.util.Collections;
import java.util.stream.Collectors;
import java.util.concurrent.atomic.AtomicLong;

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

        // Track exactly how long the policy takes to execute
        long startNs = System.nanoTime();
        Optional<String> chosenNodeOpt = policy.choose(dispatchReq, admissibleNodes, scheduledTimeNs, rng);
        long durationNs = System.nanoTime() - startNs;

        String chosenNode = chosenNodeOpt.orElse("DROP");

        // Write the decision record with the full snapshot of candidate nodes
        if (logger != null) {
            List<Candidate> candidates = allNodes.stream().map(nv -> {
                boolean isAdm = admissibleNodes.contains(nv);
                return new Candidate(nv.nodeId(), nv.queueDepth(), nv.inflight(),
                        nv.capabilityTokS(), nv.estimateAgeMs(), isAdm, null);
            }).collect(Collectors.toList());

            DecisionRecord rec = new DecisionRecord(
                    "decision", runId, traceReq.reqId(), decisionSeq.getAndIncrement(),
                    policyName, stalenessParamS, durationNs, chosenNode, 0.0, candidates);
            logger.logRecord(rec);
        }

        if (chosenNodeOpt.isPresent()) {
            String nId = chosenNodeOpt.get();
            for (NodeView nv : store.getAllNodes()) {
                if (nv.nodeId().equals(nId)) {
                    NodeView upd = new NodeView(
                            nv.nodeId(), nv.queueDepth(), nv.inflight() + 1,
                            nv.capabilityTokS(), nv.estimateAgeMs(), nv.isAdmissible());

                    store.updateNode(upd);
                    veil.updateNode(upd);

                    long serviceNs = sampler.sampleServiceNs(nId, traceReq.promptLen(), traceReq.outputLen(),
                            upd.inflight());
                    if (serviceNs < 0)
                        serviceNs = 100_000_000L;

                    // Schedule completion event, passing the logger forward
                    des.scheduleEvent(new CompletionEvent(scheduledTimeNs + serviceNs, nId, store, logger, runId,
                            traceReq.reqId()));
                    break;
                }
            }
        } else {
            System.out.println("[" + scheduledTimeNs + " ns] Dropped " + traceReq.reqId() + " (no admissible nodes)");
        }
    }
}