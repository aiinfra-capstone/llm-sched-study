package com.sched.live;

import io.grpc.stub.StreamObserver;
import com.sched.v1.SchedulerGrpc;
import com.sched.v1.Heartbeat;
import com.sched.v1.BeginRun;
import com.sched.v1.DispatchRequest;
import com.sched.v1.DispatchAck;
import com.sched.core.InMemoryStateStore;
import com.sched.core.StalenessVeil;
import com.sched.core.AdmissionFilter;
import com.sched.core.DecisionLogger;
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.core.models.SchedulerLogRecords.Candidate;
import com.sched.core.models.SchedulerLogRecords.DecisionRecord;
import java.util.List;
import java.util.Optional;
import java.util.Random;
import java.util.stream.Collectors;
import java.util.concurrent.atomic.AtomicLong;

public class SchedulerGrpcService extends SchedulerGrpc.SchedulerImplBase {
    private final InMemoryStateStore store;
    private final StalenessVeil veil;
    private final AdmissionFilter filter;
    private final Policy policy;
    private final Random rng;
    private final DecisionLogger logger;
    private final AtomicLong decisionSeq;
    private final String runId;
    private final String policyName;
    private final double stalenessParamS;

    public SchedulerGrpcService(InMemoryStateStore store, StalenessVeil veil,
            AdmissionFilter filter, Policy policy,
            DecisionLogger logger, String runId,
            String policyName, double stalenessParamS) {
        this.store = store;
        this.veil = veil;
        this.filter = filter;
        this.policy = policy;
        this.rng = new Random(42);
        this.logger = logger;
        this.decisionSeq = new AtomicLong(0);
        this.runId = runId;
        this.policyName = policyName;
        this.stalenessParamS = stalenessParamS;
    }

    @Override
    public StreamObserver<Heartbeat> streamHeartbeat(StreamObserver<BeginRun> responseObserver) {
        return new StreamObserver<Heartbeat>() {
            @Override
            public void onNext(Heartbeat beat) {
                System.out.println("Heartbeat | Node: " + beat.getNodeId() + " | Q: " + beat.getQueueDepth());
                NodeView nv = new NodeView(
                        beat.getNodeId(), beat.getQueueDepth(), beat.getInflightCount(),
                        beat.getRecentTokensPerS(), 0L, true);
                // Update ground truth and historical veil
                store.updateNode(nv);
                veil.updateNode(nv);
            }

            @Override
            public void onError(Throwable t) {
                System.err.println("Heartbeat stream error: " + t.getMessage());
            }

            @Override
            public void onCompleted() {
                responseObserver.onCompleted();
            }
        };
    }

    @Override
    public void dispatch(DispatchRequest req, StreamObserver<DispatchAck> responseObserver) {
        long startNs = System.nanoTime();

        // 1. Fetch F-8 stale state and filter for F-14 admissibility
        List<NodeView> allNodes = veil.getAllNodes();
        List<NodeView> admissibleNodes = filter.filterAdmissible(allNodes, req);

        // 2. Execute the exact same F-21 policy logic used in the simulator
        Optional<String> chosenNodeOpt = policy.choose(req, admissibleNodes, System.nanoTime(), rng);
        long durationNs = System.nanoTime() - startNs;

        String chosenNode = chosenNodeOpt.orElse("DROP");

        // 3. Log the C-4 decision record
        if (logger != null) {
            List<Candidate> candidates = allNodes.stream().map(nv -> {
                boolean isAdm = admissibleNodes.contains(nv);
                return new Candidate(nv.nodeId(), nv.queueDepth(), nv.inflight(),
                        nv.capabilityTokS(), nv.estimateAgeMs(), isAdm, null);
            }).collect(Collectors.toList());

            DecisionRecord rec = new DecisionRecord(
                    "decision", runId, req.getReqId(), decisionSeq.getAndIncrement(),
                    policyName, stalenessParamS, durationNs, chosenNode, 0.0, candidates);
            logger.logRecord(rec);
        }

        // 4. Return F-1 wire schema acknowledgment
        DispatchAck.Builder ackBuilder = DispatchAck.newBuilder().setReqId(req.getReqId());
        if (chosenNodeOpt.isPresent()) {
            ackBuilder.setChosenNode(chosenNodeOpt.get());
            ackBuilder.setAccepted(true);
        } else {
            ackBuilder.setAccepted(false);
            ackBuilder.setRejectReason("No admissible nodes available");
        }

        responseObserver.onNext(ackBuilder.build());
        responseObserver.onCompleted();
    }
}