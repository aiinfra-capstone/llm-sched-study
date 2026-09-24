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
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.v1.DispatchAck;
import com.sched.v1.DispatchRequest;

import io.grpc.stub.StreamObserver;

import java.io.File;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/**
 * Dispatch under concurrent arrivals.
 *
 * <p>A request that arrives while an earlier one is still being placed must still see that
 * earlier admission. On the first pair it did not: 367 of 11,144 consecutive same-node
 * decisions (3.3%) read the queue without the request before them, and both queue-aware
 * policies routed slightly worse than intended for it. Nothing in the suite fired two
 * dispatches at once, so nothing failed. The veil's concurrent-write test covers the map
 * behind the state, not read, decide and admit as one step.
 *
 * <p>The invariant this pins is the one that made the bug visible from a log alone. In
 * fixture mode nothing ever completes, so every admission stands: decision k, counted in
 * {@code decision_seq} order, must see exactly k requests already on the pool. Fewer means
 * it read the state before an earlier dispatch had recorded its admission.
 */
class SchedulerGrpcServiceConcurrencyTest {
    private static final String RUN_ID = "concurrent-run";
    private static final int REQUESTS = 200;
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final class TestClock implements Clock {
        @Override
        public long nowNs() {
            return 1_000_000_000L;
        }
    }

    private static final class CountingObs implements StreamObserver<DispatchAck> {
        private final AtomicInteger completed = new AtomicInteger();

        @Override
        public void onNext(DispatchAck value) {}

        @Override
        public void onError(Throwable t) {}

        @Override
        public void onCompleted() {
            completed.incrementAndGet();
        }
    }

    @Test
    @DisplayName("decision k sees the k admissions before it, however the dispatches interleave")
    void concurrentDispatchesEachSeeEveryAdmissionBeforeThem() throws Exception {
        File dir = Files.createTempDirectory("scheduler-concurrency").toFile();
        InMemoryStateStore store = new InMemoryStateStore();
        StalenessVeil veil = new StalenessVeil(0L, new TestClock());
        Map<String, com.sched.core.models.CostModelSnapshot.Admissibility> bounds = new HashMap<>();
        bounds.put("fast", new com.sched.core.models.CostModelSnapshot.Admissibility(4096, 4096, 60000));
        bounds.put("slow", new com.sched.core.models.CostModelSnapshot.Admissibility(4096, 4096, 60000));
        // JSQ, because the bug was measured on the queue-aware policies: they are the ones
        // whose choice changes when the queue they read is one request out of date.
        Policy pol = new com.sched.core.policies.JSQ();
        DecisionLogger logger = new DecisionLogger(dir.getAbsolutePath(), RUN_ID);
        Map<String, Integer> capacities = new HashMap<>();
        capacities.put("fast", 4);
        capacities.put("slow", 4);
        for (String id : List.of("fast", "slow")) {
            NodeView seed = new NodeView(id, 0, 0, id.equals("fast") ? 163.6 : 104.0, 0L, true);
            store.updateNode(seed);
            veil.updateNode(seed);
        }
        SchedulerGrpcService svc = new SchedulerGrpcService(
                store, veil, new AdmissionFilter(bounds), pol, logger, RUN_ID, "jsq", 0.0, 1,
                new HashMap<>(), capacities);

        // Every thread waits on one latch so the dispatches overlap rather than queue up
        // behind each other's start-up.
        ExecutorService pool = Executors.newFixedThreadPool(16);
        CountDownLatch start = new CountDownLatch(1);
        CountDownLatch done = new CountDownLatch(REQUESTS);
        CountingObs obs = new CountingObs();
        for (int i = 0; i < REQUESTS; i++) {
            final String reqId = String.format("r-%04d", i);
            pool.submit(() -> {
                try {
                    start.await();
                    svc.dispatch(DispatchRequest.newBuilder()
                            .setRunId(RUN_ID).setReqId(reqId).setOutputLen(64).setBucketId("b")
                            .addAllPromptTokenIds(Collections.nCopies(64, 1))
                            .build(), obs);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                } finally {
                    done.countDown();
                }
            });
        }
        start.countDown();
        assertTrue(done.await(60, TimeUnit.SECONDS), "every dispatch should return");
        pool.shutdown();
        logger.close();

        assertEquals(REQUESTS, obs.completed.get(), "every dispatch should complete cleanly");

        List<JsonNode> decisions = new ArrayList<>();
        for (String line : Files.readAllLines(new File(dir, "scheduler_" + RUN_ID + ".jsonl").toPath())) {
            JsonNode rec = MAPPER.readTree(line);
            if ("decision".equals(rec.path("type").asText())) decisions.add(rec);
        }
        assertEquals(REQUESTS, decisions.size(), "every dispatch should log one decision");
        decisions.sort((a, b) -> Long.compare(a.path("decision_seq").asLong(), b.path("decision_seq").asLong()));

        List<String> behind = new ArrayList<>();
        for (int k = 0; k < decisions.size(); k++) {
            JsonNode rec = decisions.get(k);
            int onPool = 0;
            for (JsonNode c : rec.path("candidates")) {
                onPool += c.path("queue_depth").asInt() + c.path("inflight").asInt();
            }
            if (onPool != k) {
                behind.add(rec.path("req_id").asText() + " at seq " + rec.path("decision_seq").asLong()
                        + " saw " + onPool + " admissions, not " + k);
            }
        }
        assertTrue(behind.isEmpty(),
                "a dispatch decided on a pool state missing an earlier admission: " + behind);

        // And the decision numbering is a sequence, not a set with holes or repeats.
        for (int k = 0; k < decisions.size(); k++) {
            assertEquals(k, decisions.get(k).path("decision_seq").asLong(),
                    "decision_seq should be dense and unique under concurrent dispatch");
        }
    }
}
