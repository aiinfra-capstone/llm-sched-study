package com.sched.live;

import static com.sched.RunFixtures.FAST_CLASS;
import static com.sched.RunFixtures.SLOW_CLASS;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.sched.ContractCheck;
import com.sched.JavaMain;
import com.sched.RunFixtures;
import com.sched.v1.BeginRun;
import com.sched.v1.ExecuteAck;
import com.sched.v1.Heartbeat;
import com.sched.v1.SchedulerGrpc;
import com.sched.v1.WorkerGrpc;
import io.grpc.ManagedChannel;
import io.grpc.ManagedChannelBuilder;
import io.grpc.Server;
import io.grpc.ServerBuilder;
import io.grpc.stub.StreamObserver;
import java.io.IOException;
import java.net.ServerSocket;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * LiveSchedulerApp from its command line (J11), and the heartbeat summary its shutdown hook
 * writes (J4). Run as a child JVM: its refusals are System.exit(2), and a real SIGTERM is the
 * only way to run the shutdown hook the harness depends on.
 */
class LiveSchedulerAppTest {

    private record Setup(Path dir, Path costModels, Map<String, Object> manifest) {
        String[] args(String... extra) throws IOException {
            List<String> a = new ArrayList<>(List.of(
                    RunFixtures.write(dir.resolve("manifest.json"), manifest).toString(),
                    "--cost-models", costModels.toString(),
                    "--log-dir", dir.resolve("logs").toString()));
            a.addAll(List.of(extra));
            return a.toArray(String[]::new);
        }
    }

    private static Setup setup(Path dir) throws IOException {
        Path costModels = dir.resolve("cost_models");
        String slow = RunFixtures.copySnapshot(SLOW_CLASS, costModels);
        String fast = RunFixtures.copySnapshot(FAST_CLASS, costModels);
        Path trace = RunFixtures.writeTrace(dir.resolve("trace.jsonl"), RunFixtures.steady(4, 1.0));
        return new Setup(dir, costModels, RunFixtures.manifest(trace, slow, fast, "round_robin"));
    }

    private static int freePort() throws IOException {
        try (ServerSocket s = new ServerSocket(0)) {
            return s.getLocalPort();
        }
    }

    /** A worker that records every BeginRun it is sent. */
    private static Server fakeWorker(List<BeginRun> begun) throws IOException {
        return ServerBuilder.forPort(0).addService(new WorkerGrpc.WorkerImplBase() {
            @Override
            public void begin(BeginRun request, StreamObserver<ExecuteAck> responseObserver) {
                begun.add(request);
                responseObserver.onNext(ExecuteAck.newBuilder().build());
                responseObserver.onCompleted();
            }
        }).build().start();
    }

    @Test
    @DisplayName("J11: a malformed --worker refuses the run with exit 2")
    void aMalformedWorkerArgumentIsRefused(@TempDir Path dir) throws Exception {
        Setup s = setup(dir);
        List<List<String>> bad = List.of(
                List.of("slow"),                                 // no '='
                List.of("=127.0.0.1:5000"),                      // no node id
                List.of("slow=127.0.0.1"),                       // no port
                List.of("slow=127.0.0.1:abc"),                   // not a number
                List.of("slow=127.0.0.1:0"),                     // out of range
                List.of("slow=127.0.0.1:70000"),
                List.of("ghost=127.0.0.1:5000"),                 // not in the manifest's pool
                List.of("slow=127.0.0.1:5000", "slow=127.0.0.1:5001")); // given twice
        for (List<String> workers : bad) {
            List<String> extra = new ArrayList<>();
            for (String w : workers) extra.addAll(List.of("--worker", w));
            JavaMain.Result r = JavaMain.run(LiveSchedulerApp.class, dir, s.args(extra.toArray(String[]::new)));
            assertEquals(2, r.exitCode(), workers + ":\n" + r.output());
            assertTrue(r.output().contains("--worker"), workers + ":\n" + r.output());
        }
    }

    @Test
    @DisplayName("J11: a manifest with no config.seed does not start")
    void aManifestWithoutASeedDoesNotStart(@TempDir Path dir) throws Exception {
        Setup s = setup(dir);
        RunFixtures.config(s.manifest()).remove("seed");
        JavaMain.Result r = JavaMain.run(LiveSchedulerApp.class, dir, s.args());
        assertEquals(2, r.exitCode(), r.output());
        assertTrue(r.output().contains("config.seed"), r.output());
    }

    @Test
    @DisplayName("J11: a worker that cannot be told the run begins stops the run before any dispatch")
    void anUnreachableWorkerStopsTheRun(@TempDir Path dir) throws Exception {
        Setup s = setup(dir);
        JavaMain.Result r = JavaMain.run(LiveSchedulerApp.class, dir,
                s.args("--worker", "slow=127.0.0.1:" + freePort(), "--port", String.valueOf(freePort())));
        assertEquals(1, r.exitCode(), r.output());
        assertTrue(r.output().contains("BeginRun to slow failed"), r.output());
        assertTrue(!r.output().contains("Live Control Plane active"), r.output());
    }

    @Test
    @DisplayName("J11 and J4: BeginRun reaches every worker; SIGTERM writes the heartbeat summaries")
    void beginRunThenSummariesAtShutdown(@TempDir Path dir) throws Exception {
        Setup s = setup(dir);
        List<BeginRun> slowBegun = Collections.synchronizedList(new ArrayList<>());
        List<BeginRun> fastBegun = Collections.synchronizedList(new ArrayList<>());
        Server slow = fakeWorker(slowBegun);
        Server fast = fakeWorker(fastBegun);
        int port = freePort();
        JavaMain.Running app = JavaMain.start(LiveSchedulerApp.class, dir, s.args(
                "--worker", "slow=127.0.0.1:" + slow.getPort(),
                "--worker", "fast=127.0.0.1:" + fast.getPort(),
                "--port", String.valueOf(port)));
        ManagedChannel channel = null;
        try {
            app.awaitLine("Live Control Plane active", Duration.ofSeconds(90));

            // Announced before the first dispatch, with what the run is.
            for (List<BeginRun> begun : List.of(slowBegun, fastBegun)) {
                assertEquals(1, begun.size(), app.output());
                assertEquals("fixture", begun.get(0).getRunId());
                assertEquals(s.manifest().get("trace_sha256"), begun.get(0).getTraceSha256());
                assertEquals(s.manifest().get("config_hash"), begun.get(0).getConfigHash());
            }

            // Beats as a worker sends them. slow skips 6 to 8; fast sends one, its baseline.
            channel = ManagedChannelBuilder.forAddress("127.0.0.1", port).usePlaintext().build();
            CountDownLatch done = new CountDownLatch(1);
            StreamObserver<Heartbeat> beats = SchedulerGrpc.newStub(channel).streamHeartbeat(
                    new StreamObserver<BeginRun>() {
                        @Override public void onNext(BeginRun value) { }
                        @Override public void onError(Throwable t) { done.countDown(); }
                        @Override public void onCompleted() { done.countDown(); }
                    });
            for (long seq : new long[] {4, 5, 9}) beats.onNext(beat("slow", seq));
            beats.onNext(beat("fast", 70));
            beats.onCompleted();
            // The server answers the stream's end after it has taken every beat in order.
            assertTrue(done.await(30, TimeUnit.SECONDS), "the scheduler never closed the heartbeat stream");
        } finally {
            if (channel != null) channel.shutdownNow();
            app.terminate(Duration.ofSeconds(30));
            slow.shutdownNow();
            fast.shutdownNow();
        }

        List<JsonNode> summaries = ContractCheck.ofType(
                ContractCheck.readJsonl(dir.resolve("logs").resolve("scheduler_fixture.jsonl")), "heartbeat_summary");
        JsonNode def = ContractCheck.def("log_scheduler.schema.json", "heartbeat_summary");
        for (JsonNode sum : summaries) ContractCheck.assertConforms(sum, def, "heartbeat_summary");
        assertEquals(List.of("fast", "slow"), summaries.stream().map(x -> x.get("node_id").asText()).toList(),
                app.output());
        for (JsonNode sum : summaries) assertEquals("shutdown", sum.get("at").asText());
        assertEquals(0, summaries.get(0).get("missed_beats").asLong());
        assertEquals(3, summaries.get(1).get("missed_beats").asLong());
        assertEquals(9, summaries.get(1).get("last_seq").asLong());
    }

    private static Heartbeat beat(String node, long seq) {
        return Heartbeat.newBuilder().setRunId("fixture").setNodeId(node).setSeq(seq)
                .setRecentTokensPerS(100.0).setKvOccupancyFrac(-1.0).setEngineState("ready").build();
    }
}
