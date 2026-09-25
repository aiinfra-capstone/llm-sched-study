package com.sched.live;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.sched.core.AdmissionFilter;
import com.sched.core.DecisionLogger;
import com.sched.core.InMemoryStateStore;
import com.sched.core.StalenessVeil;
import com.sched.core.interfaces.Clock;
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.v1.Completion;
import com.sched.v1.DispatchAck;
import com.sched.v1.DispatchRequest;
import com.sched.v1.ExecuteAck;

import io.grpc.stub.StreamObserver;

import java.io.File;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicLong;

import org.junit.jupiter.api.Test;

class SchedulerGrpcServiceClosedLoopTest {
    private static final String RUN_ID = "closedloop-run";

    private static class TestClock implements Clock {
        private final AtomicLong t = new AtomicLong(1_000_000_000L);
        void advanceMs(long ms) { t.addAndGet(ms * 1_000_000L); }
        @Override public long nowNs() { return t.get(); }
    }

    private static class CapturingAckObs<T> implements StreamObserver<T> {
        final List<T> items = new ArrayList<>();
        int completed;
        int errors;
        @Override public void onNext(T value) { items.add(value); }
        @Override public void onError(Throwable t) { errors++; }
        @Override public void onCompleted() { completed++; }
    }

    private static DecisionLogger recorderLogger(File dir) {
        return new DecisionLogger(dir.getAbsolutePath(), RUN_ID);
    }

    @Test
    void dispatchIncramentsInflightAndQueueSoPolicySeesTheBurst() throws Exception {
        File dir = Files.createTempDirectory("scheduler-closed-loop").toFile();
        InMemoryStateStore store = new InMemoryStateStore();
        TestClock clk = new TestClock();
        StalenessVeil veil = new StalenessVeil(0L, clk);
        Map<String, com.sched.core.models.CostModelSnapshot.Admissibility> bounds = new HashMap<>();
        bounds.put("n1", new com.sched.core.models.CostModelSnapshot.Admissibility(4096, 4096, 60000));
        AdmissionFilter filter = new AdmissionFilter(bounds);
        Policy pol = new com.sched.core.policies.RoundRobin(new java.util.concurrent.atomic.AtomicInteger(0));
        DecisionLogger logger = recorderLogger(dir);
        Map<String, io.grpc.ManagedChannel> chans = new HashMap<>();
        // No channel: dispatch takes the fixture path and admit() still runs,
        // because the test is about state updates, not about Execute.
        Map<String, Integer> cap = new HashMap<>();
        cap.put("n1", 2);
        // Seed both the store and the veil so the policy sees the node.
        NodeView seed = new NodeView("n1", 0, 0, 100.0, 0L, true);
        store.updateNode(seed);
        veil.updateNode(seed);
        SchedulerGrpcService svc = new SchedulerGrpcService(
                store, veil, filter, pol, logger, RUN_ID, "round_robin", 0.0, 1, chans, cap);

        for (int i = 0; i < 5; i++) {
            DispatchRequest req = DispatchRequest.newBuilder()
                    .setReqId("r-" + i).setOutputLen(16).setBucketId("b")
                    .addAllPromptTokenIds(java.util.Collections.nCopies(4, 0))
                    .build();
            CapturingAckObs<DispatchAck> obs = new CapturingAckObs<>();
            svc.dispatch(req, obs);
            assertEquals(1, obs.completed, "each dispatch should complete cleanly");
            assertEquals(1, obs.items.size(), "each dispatch should produce one ack");
        }

        NodeView after = store.getNode("n1");
        assertNotNull(after);
        assertEquals(2, after.inflight(), "inflight saturates at capacity");
        assertEquals(3, after.queueDepth(), "queue holds the excess");
    }

    private static SchedulerGrpcService oneNodeService(
            File dir, InMemoryStateStore store, Map<String, io.grpc.ManagedChannel> chans) {
        TestClock clk = new TestClock();
        StalenessVeil veil = new StalenessVeil(0L, clk);
        Map<String, com.sched.core.models.CostModelSnapshot.Admissibility> bounds = new HashMap<>();
        bounds.put("n1", new com.sched.core.models.CostModelSnapshot.Admissibility(4096, 4096, 60000));
        AdmissionFilter filter = new AdmissionFilter(bounds);
        Policy pol = new com.sched.core.policies.RoundRobin(new java.util.concurrent.atomic.AtomicInteger(0));
        Map<String, Integer> cap = new HashMap<>();
        cap.put("n1", 1);
        NodeView seed = new NodeView("n1", 0, 0, 100.0, 0L, true);
        store.updateNode(seed);
        veil.updateNode(seed);
        return new SchedulerGrpcService(
                store, veil, filter, pol, recorderLogger(dir), RUN_ID, "round_robin", 0.0, 1, chans, cap);
    }

    private static void complete(SchedulerGrpcService svc, String reqId) {
        Completion done = Completion.newBuilder()
                .setRunId(RUN_ID).setNodeId("n1").setReqId(reqId).setStatus("ok").setServiceNs(123L)
                .build();
        CapturingAckObs<ExecuteAck> obs = new CapturingAckObs<>();
        svc.reportCompletion(done, obs);
        assertEquals(1, obs.completed, "reportCompletion should complete cleanly for " + reqId);
        assertEquals(1, obs.items.size(), "reportCompletion should produce one ack for " + reqId);
    }

    private static DispatchAck dispatch(SchedulerGrpcService svc, String reqId) {
        DispatchRequest req = DispatchRequest.newBuilder()
                .setReqId(reqId).setOutputLen(16).setBucketId("b")
                .addAllPromptTokenIds(java.util.Collections.nCopies(4, 0))
                .build();
        CapturingAckObs<DispatchAck> obs = new CapturingAckObs<>();
        svc.dispatch(req, obs);
        assertEquals(1, obs.items.size(), "each dispatch should produce one ack");
        return obs.items.get(0);
    }

    private static void assertSlots(InMemoryStateStore store, int inflight, int queued, String when) {
        NodeView n = store.getNode("n1");
        assertEquals(inflight, n.inflight(), "inflight " + when);
        assertEquals(queued, n.queueDepth(), "queue depth " + when);
    }

    /**
     * A worker on a local port that takes every request it is sent and records its req_id.
     * The reply for a req_id in {@code replyFails} fails after the worker has taken it.
     */
    private static io.grpc.Server startWorker(List<String> accepted, java.util.Set<String> replyFails)
            throws java.io.IOException {
        return io.grpc.ServerBuilder.forPort(0)
                .addService(new com.sched.v1.WorkerGrpc.WorkerImplBase() {
                    @Override
                    public void execute(com.sched.v1.ExecuteRequest request,
                            StreamObserver<ExecuteAck> responseObserver) {
                        accepted.add(request.getReqId());
                        if (replyFails.contains(request.getReqId())) {
                            responseObserver.onError(io.grpc.Status.UNAVAILABLE
                                    .withDescription("reply lost after the worker accepted")
                                    .asRuntimeException());
                            return;
                        }
                        responseObserver.onNext(ExecuteAck.newBuilder()
                                .setReqId(request.getReqId()).setQueued(true).build());
                        responseObserver.onCompleted();
                    }
                })
                .build()
                .start();
    }

    private static io.grpc.ManagedChannel channelTo(io.grpc.Server worker) {
        return io.grpc.ManagedChannelBuilder.forAddress("127.0.0.1", worker.getPort()).usePlaintext().build();
    }

    /**
     * A completion releases the slot its own request holds, once, and only that. A request
     * the scheduler never admitted, or one already released, frees nothing: under the old
     * count any completion drained the queue, so a stray or repeated one handed a queued
     * request a slot the pool did not have.
     */
    @Test
    void completionDrainsQueueAndWritesCompletionObserved() throws Exception {
        File dir = Files.createTempDirectory("scheduler-completion").toFile();
        InMemoryStateStore store = new InMemoryStateStore();
        SchedulerGrpcService svc = oneNodeService(dir, store, new HashMap<>());

        // Fixture mode: no worker channels, so each dispatch is admitted by its req_id.
        assertTrue(dispatch(svc, "r-running").getAccepted());
        assertTrue(dispatch(svc, "r-queued").getAccepted());
        assertSlots(store, 1, 1, "after two admits at capacity one");

        complete(svc, "r-stranger");
        assertSlots(store, 1, 1, "after a completion for a request never admitted");

        complete(svc, "r-running");
        assertSlots(store, 1, 0, "after the running request finished and the queued one took its slot");

        complete(svc, "r-running");
        assertSlots(store, 1, 0, "after the same completion arrived a second time");

        complete(svc, "r-queued");
        assertSlots(store, 0, 0, "after both admitted requests finished");

        File logFile = new File(dir, "scheduler_" + RUN_ID + ".jsonl");
        assertTrue(logFile.exists(), "scheduler log should be created");
        List<String> observed = Files.readAllLines(logFile.toPath()).stream()
                .filter(line -> line.contains("\"completion_observed\""))
                .toList();
        // Every completion the worker reported is on record, released or not, so the log
        // still shows what arrived even where the count ignored it.
        assertEquals(4, observed.size(), "one completion_observed record per reported completion");
        assertTrue(observed.stream().anyMatch(line -> line.contains("\"r-running\"")),
                "the record names the request that finished");
    }

    /**
     * A worker that accepts a request but whose answer never reaches the scheduler: the
     * forward fails, the scheduler rolls the admission back, and the worker later reports a
     * completion for a request it did run. Under the old count that completion released a
     * second slot, one a later request was holding, and the next dispatch was placed on a
     * node the scheduler believed had room. Here the late completion must change nothing.
     *
     * A forward that errors reaches the same rollback as one that times out, without
     * waiting out the scheduler's five-second deadline.
     */
    @Test
    void aForwardRolledBackAfterTheWorkerAcceptedReleasesItsSlotOnce() throws Exception {
        List<String> accepted = java.util.Collections.synchronizedList(new ArrayList<>());
        io.grpc.Server worker = startWorker(accepted, java.util.Set.of("r-lost"));
        io.grpc.ManagedChannel channel = channelTo(worker);
        try {
            File dir = Files.createTempDirectory("scheduler-rollback").toFile();
            InMemoryStateStore store = new InMemoryStateStore();
            Map<String, io.grpc.ManagedChannel> chans = new HashMap<>();
            chans.put("n1", channel);
            SchedulerGrpcService svc = oneNodeService(dir, store, chans);

            DispatchAck lost = dispatch(svc, "r-lost");
            assertFalse(lost.getAccepted(), "a forward that failed is not reported as accepted");
            assertSlots(store, 0, 0, "after the failed forward was rolled back");

            assertTrue(dispatch(svc, "r-held").getAccepted());
            assertTrue(dispatch(svc, "r-waiting").getAccepted());
            assertSlots(store, 1, 1, "with one request running and one queued");

            complete(svc, "r-lost");
            assertSlots(store, 1, 1, "after the late completion of the rolled-back request");

            complete(svc, "r-held");
            assertSlots(store, 1, 0, "after the running request finished");
            complete(svc, "r-waiting");
            assertSlots(store, 0, 0, "after every request that holds a slot finished");

            assertEquals(List.of("r-lost", "r-held", "r-waiting"), accepted,
                    "the worker received every forward, the failed one included");
        } finally {
            channel.shutdownNow();
            worker.shutdownNow();
        }
    }

    /** The service's own per-node counter. Nothing reads it back, so the test reads the field. */
    private static long serviceInflight(SchedulerGrpcService svc, String node) throws Exception {
        java.lang.reflect.Field f = SchedulerGrpcService.class.getDeclaredField("inflight");
        f.setAccessible(true);
        @SuppressWarnings("unchecked")
        Map<String, AtomicLong> counters = (Map<String, AtomicLong>) f.get(svc);
        return counters.get(node).get();
    }

    /**
     * A req_id counts once in the pool. A client that retries a dispatch under the same
     * req_id, or two dispatches that share one, must not take a second slot, on the node the
     * first went to or on any other, and the service's own counter must move only when the
     * store counted. Before M1 a duplicate on a second node took a slot there that no
     * completion would ever release, since the completion names the node the first ran on.
     */
    @Test
    void aDuplicateReqIdTakesNoSecondSlotOnAnyNode() throws Exception {
        List<String> acceptedA = java.util.Collections.synchronizedList(new ArrayList<>());
        List<String> acceptedB = java.util.Collections.synchronizedList(new ArrayList<>());
        io.grpc.Server workerA = startWorker(acceptedA, java.util.Set.of());
        io.grpc.Server workerB = startWorker(acceptedB, java.util.Set.of());
        io.grpc.ManagedChannel chanA = channelTo(workerA);
        io.grpc.ManagedChannel chanB = channelTo(workerB);
        try {
            File dir = Files.createTempDirectory("scheduler-duplicate").toFile();
            InMemoryStateStore store = new InMemoryStateStore();
            StalenessVeil veil = new StalenessVeil(0L, new TestClock());
            Map<String, com.sched.core.models.CostModelSnapshot.Admissibility> bounds = new HashMap<>();
            Map<String, Integer> cap = new HashMap<>();
            for (String n : List.of("n1", "n2")) {
                bounds.put(n, new com.sched.core.models.CostModelSnapshot.Admissibility(4096, 4096, 60000));
                cap.put(n, 1);
                NodeView seed = new NodeView(n, 0, 0, 100.0, 0L, true);
                store.updateNode(seed);
                veil.updateNode(seed);
            }
            // The policy is scripted so each dispatch lands where the case needs it.
            java.util.Iterator<String> script = List.of("n1", "n2", "n1").iterator();
            Policy pol = (req, nodes, now, rng) ->
                    new Policy.Choice(java.util.Optional.of(script.next()), new HashMap<>(), null);
            Map<String, io.grpc.ManagedChannel> chans = new HashMap<>();
            chans.put("n1", chanA);
            chans.put("n2", chanB);
            SchedulerGrpcService svc = new SchedulerGrpcService(store, veil, new AdmissionFilter(bounds),
                    pol, recorderLogger(dir), RUN_ID, "scripted", 0.0, 1, chans, cap);

            assertTrue(dispatch(svc, "r-dup").getAccepted());
            assertEquals(1, store.getNode("n1").inflight(), "n1 after the first admit");
            assertEquals(1L, serviceInflight(svc, "n1"), "service counter for n1 after the first admit");

            // Both duplicates are refused by name, and neither reaches a worker: a second
            // Execute would have the worker run a request no slot accounts for.
            DispatchAck toOther = dispatch(svc, "r-dup"); // to n2
            DispatchAck toSame = dispatch(svc, "r-dup"); // back to n1
            for (DispatchAck dup : List.of(toOther, toSame)) {
                assertFalse(dup.getAccepted(), "a duplicate req_id is not accepted");
                assertEquals("duplicate req_id", dup.getRejectReason());
            }
            assertEquals(List.of("r-dup"), acceptedA, "n1's worker got the first Execute only");
            assertEquals(List.of(), acceptedB, "n2's worker got no Execute");

            for (String n : List.of("n1", "n2")) {
                int expected = n.equals("n1") ? 1 : 0;
                assertEquals(expected, store.getNode(n).inflight(), n + " inflight after both duplicates");
                assertEquals(0, store.getNode(n).queueDepth(), n + " queue depth after both duplicates");
                assertEquals(expected, veil.getAllNodes().stream()
                        .filter(v -> v.nodeId().equals(n)).findFirst().orElseThrow().inflight(),
                        n + " as the policy sees it after both duplicates");
                assertEquals((long) expected, serviceInflight(svc, n),
                        "service counter for " + n + " after both duplicates");
            }

            // The one slot r-dup holds is released by one completion, on the node it ran on.
            complete(svc, "r-dup");
            assertEquals(0, store.getNode("n1").inflight(), "n1 after r-dup finished");
            assertEquals(0L, serviceInflight(svc, "n1"), "service counter for n1 after r-dup finished");
        } finally {
            chanA.shutdownNow();
            chanB.shutdownNow();
            workerA.shutdownNow();
            workerB.shutdownNow();
        }
    }
}
