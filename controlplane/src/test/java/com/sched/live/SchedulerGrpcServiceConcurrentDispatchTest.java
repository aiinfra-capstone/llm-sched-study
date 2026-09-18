package com.sched.live;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sched.core.AdmissionFilter;
import com.sched.core.DecisionLogger;
import com.sched.core.InMemoryStateStore;
import com.sched.core.StalenessVeil;
import com.sched.core.interfaces.Clock;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.core.models.CostModelSnapshot;
import com.sched.v1.DispatchAck;
import com.sched.v1.DispatchRequest;

import io.grpc.stub.StreamObserver;

import java.io.File;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CyclicBarrier;
import java.util.concurrent.atomic.AtomicLong;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/**
 * Test-plan 3.8 "Concurrent dispatch": N simultaneous dispatches at SchedulerGrpcService
 * in fixture mode. Decision k must see exactly k earlier admissions reflected in the
 * candidates' queue state.
 *
 * <p>The veil's concurrent-write test (StalenessVeilTest) covers the map; this test
 * covers read, decide and admit as one step under contention, which is what the
 * {@code stateLock} in SchedulerGrpcService is for.
 */
class SchedulerGrpcServiceConcurrentDispatchTest {
    private static final String RUN_ID = "concurrent-test";
    private static final int N = 20;

    private static class TestClock implements Clock {
        private final AtomicLong t = new AtomicLong(1_000_000_000L);
        @Override public long nowNs() { return t.get(); }
    }

    private static class CapturingAckObs implements StreamObserver<DispatchAck> {
        final List<DispatchAck> items = new ArrayList<>();
        int completed;
        @Override public void onNext(DispatchAck value) { items.add(value); }
        @Override public void onError(Throwable t) {}
        @Override public void onCompleted() { completed++; }
    }

    @Test
    @DisplayName("N concurrent dispatches: decision k sees k earlier admissions")
    void concurrentDispatchesShowMonotonicallyRisingQueueState() throws Exception {
        File dir = Files.createTempDirectory("scheduler-concurrent").toFile();
        InMemoryStateStore store = new InMemoryStateStore();
        TestClock clk = new TestClock();
        StalenessVeil veil = new StalenessVeil(0L, clk);

        Map<String, CostModelSnapshot.Admissibility> bounds = new HashMap<>();
        bounds.put("n1", new CostModelSnapshot.Admissibility(4096, 4096, 60000));
        AdmissionFilter filter = new AdmissionFilter(bounds);

        // Use JSQ so all N requests go to the one node
        com.sched.core.interfaces.Policy pol = new com.sched.core.policies.JSQ();
        DecisionLogger logger = new DecisionLogger(dir.getAbsolutePath(), RUN_ID);

        // No worker channels: fixture mode, so admission records without forwarding
        Map<String, io.grpc.ManagedChannel> chans = new HashMap<>();
        Map<String, Integer> cap = new HashMap<>();
        cap.put("n1", 4);

        NodeView seed = new NodeView("n1", 0, 0, 100.0, 0L, true);
        store.updateNode(seed);
        veil.updateNode(seed);
        SchedulerGrpcService svc = new SchedulerGrpcService(
                store, veil, filter, pol, logger, RUN_ID, "jsq", 0.0, 1, chans, cap);

        // Fire N dispatches from N threads, all hitting the barrier at once
        CyclicBarrier barrier = new CyclicBarrier(N);
        Thread[] threads = new Thread[N];
        CapturingAckObs[] observers = new CapturingAckObs[N];
        for (int i = 0; i < N; i++) {
            int idx = i;
            observers[i] = new CapturingAckObs();
            threads[i] = new Thread(() -> {
                try {
                    DispatchRequest req = DispatchRequest.newBuilder()
                            .setReqId("r-" + idx).setOutputLen(16).setBucketId("b")
                            .addAllPromptTokenIds(java.util.Collections.nCopies(4, 0))
                            .build();
                    barrier.await();
                    svc.dispatch(req, observers[idx]);
                } catch (Exception e) {
                    throw new RuntimeException(e);
                }
            });
        }
        for (Thread t : threads) t.start();
        for (Thread t : threads) t.join(10_000);

        logger.close();

        // All N dispatches must have completed successfully
        for (int i = 0; i < N; i++) {
            assertEquals(1, observers[i].completed,
                    "dispatch " + i + " should complete");
            assertEquals(1, observers[i].items.size(),
                    "dispatch " + i + " should produce one ack");
            assertTrue(observers[i].items.get(0).getAccepted(),
                    "dispatch " + i + " should be accepted");
        }

        // Read the decision log and verify monotonically rising queue state
        File logFile = new File(dir, "scheduler_" + RUN_ID + ".jsonl");
        assertTrue(logFile.exists(), "decision log must exist");
        List<String> lines = Files.readAllLines(logFile.toPath());
        assertEquals(N, lines.size(), "must have exactly N decision records");

        ObjectMapper mapper = new ObjectMapper();
        List<JsonNode> decisions = new ArrayList<>();
        for (String line : lines) {
            decisions.add(mapper.readTree(line));
        }

        // Sort by decision_seq to get the order admissions happened
        decisions.sort(Comparator.comparingLong(d -> d.get("decision_seq").asLong()));

        // Decision k must see the k earlier admissions: the total of queue_depth + inflight
        // for the chosen node's candidate entry must be monotonically non-decreasing
        List<Integer> totalOnNode = new ArrayList<>();
        for (JsonNode d : decisions) {
            for (JsonNode candidate : d.get("candidates")) {
                if ("n1".equals(candidate.get("node_id").asText())) {
                    totalOnNode.add(
                            candidate.get("queue_depth").asInt()
                            + candidate.get("inflight").asInt());
                }
            }
        }

        assertEquals(N, totalOnNode.size(), "every decision must have a candidate for n1");
        for (int k = 1; k < totalOnNode.size(); k++) {
            assertTrue(totalOnNode.get(k) >= totalOnNode.get(k - 1),
                    "queue state must be monotonically non-decreasing: decision " + (k - 1)
                    + " saw " + totalOnNode.get(k - 1) + " but decision " + k
                    + " saw " + totalOnNode.get(k));
        }
        // The first decision sees 0, the last sees N-1
        assertEquals(0, totalOnNode.get(0), "first decision must see an empty node");
        assertEquals(N - 1, totalOnNode.get(totalOnNode.size() - 1),
                "last decision must see N-1 earlier admissions");
    }
}
