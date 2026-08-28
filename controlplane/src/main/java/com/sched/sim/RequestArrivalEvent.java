package com.sched.sim;

import com.sched.core.models.TraceRequest;
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.core.AdmissionFilter;
import com.sched.core.StalenessVeil;
import com.sched.core.InMemoryStateStore;
import com.sched.v1.DispatchRequest;
import java.util.List;
import java.util.Optional;
import java.util.Random;
import java.util.Collections;

public class RequestArrivalEvent extends SimulationEvent {
    private final TraceRequest traceReq;
    private final Policy policy;
    private final StalenessVeil veil;
    private final AdmissionFilter filter;
    private final DiscreteEventSimulator des;
    private final Random rng;
    private final ServiceSampler sampler;
    private final InMemoryStateStore store;

    public RequestArrivalEvent(long scheduledTimeNs, TraceRequest traceReq, Policy policy,
            StalenessVeil veil, AdmissionFilter filter,
            DiscreteEventSimulator des, Random rng,
            ServiceSampler sampler, InMemoryStateStore store) {
        super(scheduledTimeNs);
        this.traceReq = traceReq;
        this.policy = policy;
        this.veil = veil;
        this.filter = filter;
        this.des = des;
        this.rng = rng;
        this.sampler = sampler;
        this.store = store;
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

        // Feed the stale (F-8) and admissible (F-14) view to the policy
        List<NodeView> allNodes = veil.getAllNodes();
        List<NodeView> admissibleNodes = filter.filterAdmissible(allNodes, dispatchReq);

        Optional<String> chosenNode = policy.choose(dispatchReq, admissibleNodes, scheduledTimeNs, rng);

        if (chosenNode.isPresent()) {
            String nId = chosenNode.get();

            // Find the chosen node in the ground-truth store
            for (NodeView nv : store.getAllNodes()) {
                if (nv.nodeId().equals(nId)) {
                    // 1. Increment in-flight load
                    NodeView upd = new NodeView(
                            nv.nodeId(), nv.queueDepth(), nv.inflight() + 1,
                            nv.capabilityTokS(), nv.estimateAgeMs(), nv.isAdmissible());

                    // Update both ground-truth and historical veil
                    store.updateNode(upd);
                    veil.updateNode(upd);

                    // 2. Sample service time using C-3 stochastic profile
                    long serviceNs = sampler.sampleServiceNs(nId, traceReq.promptLen(), traceReq.outputLen(),
                            upd.inflight());

                    // Fallback to 100ms if parameters fall outside the calibrated cost model
                    // buckets
                    if (serviceNs < 0)
                        serviceNs = 100_000_000L;

                    // 3. Schedule the completion
                    des.scheduleEvent(new CompletionEvent(scheduledTimeNs + serviceNs, nId, store));
                    break;
                }
            }
        }
    }
}