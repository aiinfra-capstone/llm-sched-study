package com.sched.live;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.sched.ContractCheck;
import com.sched.core.AdmissionFilter;
import com.sched.core.DecisionLogger;
import com.sched.core.InMemoryStateStore;
import com.sched.core.StalenessVeil;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.core.models.CostModelSnapshot.Admissibility;
import com.sched.v1.BeginRun;
import com.sched.v1.Completion;
import com.sched.v1.DispatchAck;
import com.sched.v1.DispatchRequest;
import com.sched.v1.ExecuteAck;
import com.sched.v1.Heartbeat;
import io.grpc.stub.StreamObserver;
import java.io.IOException;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * The two scheduler-log records the live scheduler measures itself: completion_observed's
 * lag (J3) and heartbeat_summary's sequence counts (J4). Both are read by the harness, the
 * first by the join and the second into validity.heartbeat_gaps.
 */
class SchedulerLiveRecordsTest {
    private static final String RUN_ID = "live-records";

    private static final class Sink<T> implements StreamObserver<T> {
        final List<T> items = new ArrayList<>();
        @Override public void onNext(T value) { items.add(value); }
        @Override public void onError(Throwable t) { throw new AssertionError(t); }
        @Override public void onCompleted() { }
    }

    private static SchedulerGrpcService service(Path dir, DecisionLogger logger) {
        InMemoryStateStore store = new InMemoryStateStore();
        StalenessVeil veil = new StalenessVeil(0L, System::nanoTime);
        Map<String, Admissibility> bounds = new HashMap<>();
        for (String n : List.of("n1", "n2")) {
            bounds.put(n, new Admissibility(4096, 4096, 60000));
            NodeView seed = new NodeView(n, 0, 0, 100.0, 0L, true);
            store.updateNode(seed);
            veil.updateNode(seed);
        }
        return new SchedulerGrpcService(store, veil, new AdmissionFilter(bounds),
                new com.sched.core.policies.RoundRobin(new java.util.concurrent.atomic.AtomicInteger(0)),
                logger, RUN_ID, "round_robin", 0.0, 1, Map.of(), Map.of("n1", 4, "n2", 4));
    }

    private static List<JsonNode> log(Path dir) throws IOException {
        return ContractCheck.readJsonl(dir.resolve("scheduler_" + RUN_ID + ".jsonl"));
    }

    @Test
    @DisplayName("J3: a live completion records a measured, non-zero observed_lag_ns")
    void aLiveCompletionRecordsItsLag(@TempDir Path dir) throws IOException {
        DecisionLogger logger = new DecisionLogger(dir.toString(), RUN_ID);
        SchedulerGrpcService svc = service(dir, logger);

        Sink<DispatchAck> ack = new Sink<>();
        svc.dispatch(DispatchRequest.newBuilder().setReqId("r1").setOutputLen(16).setBucketId("b")
                .addAllPromptTokenIds(java.util.Collections.nCopies(4, 0)).build(), ack);
        String node = ack.items.get(0).getChosenNode();
        svc.reportCompletion(Completion.newBuilder().setRunId(RUN_ID).setNodeId(node).setReqId("r1")
                .setStatus("ok").setServiceNs(123L).build(), new Sink<ExecuteAck>());
        logger.close();

        List<JsonNode> observed = ContractCheck.ofType(log(dir), "completion_observed");
        assertEquals(1, observed.size());
        JsonNode rec = observed.get(0);
        ContractCheck.assertConforms(rec,
                ContractCheck.def("log_scheduler.schema.json", "completion_observed"), "completion_observed");
        assertEquals("completion_rpc", rec.get("source").asText());
        assertEquals(node, rec.get("node_id").asText());
        // Written as 0 before 3.1, which is also what the simulator writes by construction,
        // so the two vehicles' records could not be told apart by the value.
        assertTrue(rec.get("observed_lag_ns").asLong() > 0, "lag " + rec.get("observed_lag_ns"));
    }

    @Test
    @DisplayName("J4: forward jumps count missed beats, a seq at or below the last counts a regression")
    void heartbeatSequencesAreCountedPerNode(@TempDir Path dir) throws IOException {
        DecisionLogger logger = new DecisionLogger(dir.toString(), RUN_ID);
        SchedulerGrpcService svc = service(dir, logger);
        StreamObserver<Heartbeat> beats = svc.streamHeartbeat(new Sink<BeginRun>());
        // n1: 3 is the baseline (the emitter outlives each scheduler), 5 skips 4, 9 skips
        // 6 to 8, the repeated 9 and the drop to 2 are two restarts of the emitter, and 3
        // follows 2 with nothing missed.
        for (long seq : new long[] {3, 5, 9, 9, 2, 3}) beats.onNext(beat("n1", seq));
        // n2: one beat is only a baseline.
        beats.onNext(beat("n2", 40));
        svc.writeHeartbeatSummaries("end_run");
        beats.onNext(beat("n2", 43));
        svc.writeHeartbeatSummaries("shutdown");
        logger.close();

        List<JsonNode> summaries = ContractCheck.ofType(log(dir), "heartbeat_summary");
        JsonNode def = ContractCheck.def("log_scheduler.schema.json", "heartbeat_summary");
        for (JsonNode s : summaries) ContractCheck.assertConforms(s, def, "heartbeat_summary");

        // One record per node that sent a beat, at each point, in node order.
        List<String> order = summaries.stream()
                .map(s -> s.get("at").asText() + " " + s.get("node_id").asText()).toList();
        assertEquals(List.of("end_run n1", "end_run n2", "shutdown n1", "shutdown n2"), order);

        JsonNode n1 = summaries.get(2);
        assertEquals(3, n1.get("last_seq").asLong());
        assertEquals(1 + 3, n1.get("missed_beats").asLong());
        assertEquals(2, n1.get("seq_regressions").asLong());

        // A running total: the shutdown record includes what the end_run record counted.
        assertEquals(0, summaries.get(1).get("missed_beats").asLong());
        assertEquals(2, summaries.get(3).get("missed_beats").asLong());
        assertEquals(RUN_ID, summaries.get(3).get("run_id").asText());
    }

    @Test
    @DisplayName("J4: a scheduler that heard no beat writes no summary, and no logger means no write")
    void noBeatMeansNoSummary(@TempDir Path dir) throws IOException {
        DecisionLogger logger = new DecisionLogger(dir.toString(), RUN_ID);
        service(dir, logger).writeHeartbeatSummaries("shutdown");
        logger.close();
        assertTrue(ContractCheck.ofType(log(dir), "heartbeat_summary").isEmpty(),
                "no record, so the harness writes heartbeat_gaps null rather than 0");

        SchedulerGrpcService unlogged = service(dir, null);
        unlogged.streamHeartbeat(new Sink<BeginRun>()).onNext(beat("n1", 1));
        unlogged.writeHeartbeatSummaries("shutdown");
    }

    private static Heartbeat beat(String node, long seq) {
        return Heartbeat.newBuilder().setRunId(RUN_ID).setNodeId(node).setSeq(seq)
                .setRecentTokensPerS(100.0).setKvOccupancyFrac(-1.0).setEngineState("ready").build();
    }
}
