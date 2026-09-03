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
import java.util.Set;

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
    private final java.util.Map<String, io.grpc.ManagedChannel> workerChannels;

    public SchedulerGrpcService(InMemoryStateStore store, StalenessVeil veil,
            AdmissionFilter filter, Policy policy,
            DecisionLogger logger, String runId,
            String policyName, double stalenessParamS, int rngSeed) {
        this(store, veil, filter, policy, logger, runId, policyName, stalenessParamS, rngSeed, java.util.Collections.emptyMap());
    }

    public SchedulerGrpcService(InMemoryStateStore store, StalenessVeil veil,
            AdmissionFilter filter, Policy policy,
            DecisionLogger logger, String runId,
            String policyName, double stalenessParamS, int rngSeed,
            java.util.Map<String, io.grpc.ManagedChannel> workerChannels) {
        this.store = store;
        this.veil = veil;
        this.filter = filter;
        this.policy = policy;
        this.rng = new Random(rngSeed);
        this.logger = logger;
        this.decisionSeq = new AtomicLong(0);
        this.runId = runId;
        this.policyName = policyName;
        this.stalenessParamS = stalenessParamS;
        this.workerChannels = workerChannels != null ? new java.util.HashMap<>(workerChannels) : new java.util.HashMap<>();
    }

    @Override
    public StreamObserver<Heartbeat> streamHeartbeat(StreamObserver<BeginRun> responseObserver) {
        return new StreamObserver<Heartbeat>() {
            @Override
            public void onNext(Heartbeat beat) {
                // Capability stays on the C-3 measurement the scheduler was seeded with.
                // The heartbeat refreshes queue depth and inflight only. Taking the live
                // throughput EWMA here would weight static_weighted and wjsq by a live
                // queue signal instead of the calibrated capability H1 compares on,
                // while the DES keeps the C-3 value for the whole run.
                double capability = 0.0;
                NodeView known = store.getNode(beat.getNodeId());
                if (known != null) capability = known.capabilityTokS();
                if (capability <= 0.0) capability = beat.getRecentTokensPerS();
                NodeView nv = new NodeView(
                        beat.getNodeId(), beat.getQueueDepth(), beat.getInflightCount(),
                        capability, 0L, true);
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

        List<NodeView> allNodes = veil.getAllNodes();
        List<NodeView> admissibleNodes = filter.filterAdmissible(allNodes, req);
        Set<String> admIds = admissibleNodes.stream().map(NodeView::nodeId).collect(Collectors.toSet());

        Policy.Choice choice = policy.choose(req, admissibleNodes, System.nanoTime(), rng);
        long durationNs = System.nanoTime() - startNs;

        String chosenNode = choice.chosen().orElse(null);
        long seq = decisionSeq.getAndIncrement();

        if (logger != null) {
            List<Candidate> candidates = allNodes.stream().map(nv -> {
                boolean isAdm = admIds.contains(nv.nodeId());
                Double score = choice.scores().get(nv.nodeId());
                return new Candidate(nv.nodeId(), nv.queueDepth(), nv.inflight(),
                        nv.capabilityTokS(), nv.estimateAgeMs(), isAdm, score);
            }).collect(Collectors.toList());

            DecisionRecord rec = new DecisionRecord(
                    "decision", runId, req.getReqId(), seq,
                    policyName, stalenessParamS, durationNs, chosenNode, choice.tieBreakDraw(), candidates);
            logger.logRecord(rec);
        }

        // Forward to chosen worker via Worker.Execute. The worker delivers direct
        // to the client under F-11 and reports completion separately, so the
        // scheduler is in the request path but not the response path.
        boolean forwarded = false;
        String forwardError = null;
        if (chosenNode != null && workerChannels.containsKey(chosenNode)) {
            io.grpc.ManagedChannel ch = workerChannels.get(chosenNode);
            try {
                com.sched.v1.WorkerGrpc.WorkerBlockingStub stub = com.sched.v1.WorkerGrpc.newBlockingStub(ch)
                        .withDeadlineAfter(5, java.util.concurrent.TimeUnit.SECONDS);
                com.sched.v1.ExecuteRequest exec = com.sched.v1.ExecuteRequest.newBuilder()
                        .setRunId(req.getRunId().isEmpty() ? runId : req.getRunId())
                        .setReqId(req.getReqId())
                        .addAllPromptTokenIds(req.getPromptTokenIdsList())
                        .setOutputLen(req.getOutputLen())
                        .setPriority(req.getPriority())
                        .setBucketId(req.getBucketId())
                        .setClientEndpoint(req.getClientEndpoint())
                        .setDecisionSeq((int) seq)
                        .build();
                stub.execute(exec);
                forwarded = true;
            } catch (Exception e) {
                forwardError = e.getMessage();
                System.err.println("Failed to forward Execute to worker " + chosenNode + ": " + forwardError);
            }
        } else if (chosenNode != null && !workerChannels.isEmpty()) {
            forwardError = "no channel for " + chosenNode + " (known: " + workerChannels.keySet() + ")";
            System.err.println("No channel for chosen node " + chosenNode + " (known: " + workerChannels.keySet() + ")");
        } else if (chosenNode != null) {
            // No worker channels configured (fixture mode): decision is logged,
            // nothing to forward to, treat as delivered for the smoke path.
            forwarded = true;
        }

        DispatchAck.Builder ackBuilder = DispatchAck.newBuilder().setReqId(req.getReqId());
        if (chosenNode != null && forwarded) {
            // Put node_id, not endpoint, so it joins against worker log's node_id
            ackBuilder.setChosenNode(chosenNode);
            ackBuilder.setAccepted(true);
        } else if (chosenNode != null) {
            ackBuilder.setAccepted(false);
            ackBuilder.setRejectReason(forwardError != null ? forwardError : "worker forward failed");
        } else {
            ackBuilder.setAccepted(false);
            ackBuilder.setRejectReason("No admissible nodes available");
        }

        responseObserver.onNext(ackBuilder.build());
        responseObserver.onCompleted();
    }
}