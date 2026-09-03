package com.sched.live;

import static org.junit.jupiter.api.Assertions.assertEquals;
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

    @Test
    void completionDrainsQueueAndWritesCompletionObserved() throws Exception {
        File dir = Files.createTempDirectory("scheduler-completion").toFile();
        InMemoryStateStore store = new InMemoryStateStore();
        TestClock clk = new TestClock();
        StalenessVeil veil = new StalenessVeil(0L, clk);
        Map<String, com.sched.core.models.CostModelSnapshot.Admissibility> bounds = new HashMap<>();
        bounds.put("n1", new com.sched.core.models.CostModelSnapshot.Admissibility(4096, 4096, 60000));
        AdmissionFilter filter = new AdmissionFilter(bounds);
        Policy pol = new com.sched.core.policies.RoundRobin(new java.util.concurrent.atomic.AtomicInteger(0));
        DecisionLogger logger = recorderLogger(dir);
        Map<String, io.grpc.ManagedChannel> chans = new HashMap<>();
        Map<String, Integer> cap = new HashMap<>();
        cap.put("n1", 1);
        store.updateNode(new NodeView("n1", 0, 0, 100.0, 0L, true));
        SchedulerGrpcService svc = new SchedulerGrpcService(
                store, veil, filter, pol, logger, RUN_ID, "round_robin", 0.0, 1, chans, cap);

        store.admit("n1", 1);
        store.admit("n1", 1);
        NodeView before = store.getNode("n1");
        assertEquals(1, before.queueDepth(), "one request should be queued after two admits at capacity one");

        Completion done = Completion.newBuilder()
                .setRunId(RUN_ID).setNodeId("n1").setReqId("r-completed").setStatus("ok").setServiceNs(123L)
                .build();
        CapturingAckObs<ExecuteAck> obs = new CapturingAckObs<>();
        svc.reportCompletion(done, obs);
        assertEquals(1, obs.completed, "reportCompletion should complete cleanly");
        assertEquals(1, obs.items.size(), "reportCompletion should produce one ack");

        NodeView after = store.getNode("n1");
        assertEquals(0, after.queueDepth(), "queue should be drained after one completion at capacity one");

        File logFile = new File(dir, "scheduler_" + RUN_ID + ".jsonl");
        assertTrue(logFile.exists(), "scheduler log should be created");
        List<String> lines = Files.readAllLines(logFile.toPath());
        boolean found = false;
        for (String line : lines) {
            if (line.contains("\"completion_observed\"")) {
                found = true;
                break;
            }
        }
        assertTrue(found, "log should contain a completion_observed record");
    }
}
