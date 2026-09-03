package com.sched.sim;

import static com.sched.Fixtures.cell;
import static com.sched.Fixtures.snapshot;
import static com.sched.Fixtures.splitCell;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sched.core.ClientLogger;
import com.sched.core.InMemoryStateStore;
import com.sched.core.StalenessVeil;
import com.sched.core.WorkerLogger;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.core.models.CostModelSnapshot;
import com.sched.core.models.TraceRequest;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Random;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * The node model: a fixed slot count, a queue behind it, and the rule for what happens to a
 * request already running when the batch around it changes.
 *
 * Completion times are read back out of the client log rather than asserted on an internal
 * field, because the log is what the pipeline joins and the figures are drawn from. A test
 * that agreed with a private variable but disagreed with the log would be checking the wrong
 * thing.
 */
class SimNodeServerTest {

    private static final double MS = 1_000_000.0;

    /** Two rows of one bucket, with the phase split the invariance rule needs. */
    private static CostModelSnapshot withSplit() {
        return snapshot("split", 0.0, List.of(
                splitCell(1, 128, 1, 64, 1, 1000.0, 200.0, 800.0),
                splitCell(1, 128, 1, 64, 2, 2000.0, 200.0, 1800.0)));
    }

    /** The same numbers with no split recorded, which is every snapshot fitted before it existed. */
    private static CostModelSnapshot withoutSplit() {
        return snapshot("flat", 0.0, List.of(
                cell(1, 128, 1, 64, 1, 1000.0),
                cell(1, 128, 1, 64, 2, 2000.0)));
    }

    private static TraceRequest req(String id, double arrivalS, int promptLen, int outputLen) {
        return new TraceRequest("request", id, arrivalS, promptLen, outputLen, "p64_o32", 0);
    }

    /** One node, one snapshot, loggers pointed at a temp dir, everything wired as SimApp wires it. */
    private static final class Harness {
        final DiscreteEventSimulator des = new DiscreteEventSimulator(new SimClock());
        final InMemoryStateStore store = new InMemoryStateStore();
        final StalenessVeil veil;
        final ServiceSampler sampler;
        final SimNodeServer server;
        final ClientLogger clientLogger;
        final WorkerLogger workerLogger;
        final Path dir;

        Harness(Path dir, CostModelSnapshot snap, int batchCapacity) {
            this.dir = dir;
            this.veil = new StalenessVeil(0L, new SimClock());
            this.sampler = new ServiceSampler(Map.of("n1", snap), new Random(1));
            this.sampler.setDeterministic(true);
            this.server = new SimNodeServer("n1", batchCapacity);
            this.clientLogger = new ClientLogger(dir.toString(), "t");
            this.workerLogger = new WorkerLogger(dir.toString(), "t");
            des.setLoggers(workerLogger, clientLogger);
            des.addServer(server);

            NodeView seed = new NodeView("n1", 0, 0, 100.0, 0L, true);
            store.updateNode(seed);
            veil.seed(seed, 0L);
        }

        void admit(TraceRequest r, double atMs) {
            server.admit(r, (long) (atMs * MS), des, sampler, store, veil, null, "t");
        }

        /** req_id -> completion time in ms, recovered from the client log. */
        Map<String, Double> completionsMs() throws IOException {
            des.run();
            clientLogger.close();
            workerLogger.close();

            ObjectMapper mapper = new ObjectMapper();
            Map<String, Double> out = new java.util.LinkedHashMap<>();
            for (String line : Files.readAllLines(dir.resolve("client_t.jsonl"))) {
                if (line.isBlank()) continue;
                JsonNode n = mapper.readTree(line);
                // e2e is completion minus arrival, so completion is e2e plus arrival back again.
                double e2eMs = n.get("e2e_duration_ns").asLong() / MS;
                double arrivalMs = n.get("intended_offset_s").asDouble() * 1000.0;
                out.put(n.get("req_id").asText(), e2eMs + arrivalMs);
            }
            return out;
        }
    }

    @Test
    @DisplayName("a request that fits a free slot starts at once")
    void requestUnderCapacityStartsImmediately(@TempDir Path dir) throws IOException {
        Harness h = new Harness(dir, withSplit(), 4);
        h.admit(req("r1", 0.0, 64, 32), 0.0);

        assertEquals(1000.0, h.completionsMs().get("r1"), 1e-6);
    }

    @Test
    @DisplayName("prefill does not stretch when a neighbour arrives; decode does")
    void concurrencyChangeScalesOnlyTheDecodeRemainder(@TempDir Path dir) throws IOException {
        // r1 starts at 0 with a 1000 ms service time of which the first 200 ms is prefill.
        // r2 arrives at 100 ms, so r1 is 100 ms into its prompt evaluation with 100 ms of it
        // left, and 800 ms of decode after that. Concurrency going 1 -> 2 doubles the cell
        // mean, so the decode remainder doubles to 1600 ms and the prefill remainder does not
        // move: 100 + 100 + 1600 = 1800 ms.
        Harness h = new Harness(dir, withSplit(), 4);
        h.admit(req("r1", 0.0, 64, 32), 0.0);
        h.admit(req("r2", 0.1, 64, 32), 100.0);

        assertEquals(1800.0, h.completionsMs().get("r1"), 1e-6);
    }

    @Test
    @DisplayName("without a split the whole remainder scales, which is the old behaviour")
    void missingSplitReproducesUniformScaling(@TempDir Path dir) throws IOException {
        // Identical numbers, identical timing, only the phase split removed. The whole 900 ms
        // remainder doubles to 1800, so r1 lands at 1900 rather than 1800. That 100 ms gap is
        // exactly the prompt evaluation the old code was inflating, and it is the reason a
        // snapshot with no split has to behave as it always did rather than guess a share.
        Harness h = new Harness(dir, withoutSplit(), 4);
        h.admit(req("r1", 0.0, 64, 32), 0.0);
        h.admit(req("r2", 0.1, 64, 32), 100.0);

        assertEquals(1900.0, h.completionsMs().get("r1"), 1e-6);
    }

    @Test
    @DisplayName("a request whose prefill is already finished scales in full")
    void arrivalAfterPrefillScalesEverything(@TempDir Path dir) throws IOException {
        // r2 arrives at 500 ms, well past r1's 200 ms of prefill, so there is no prompt
        // evaluation left to protect: 500 remaining, all decode, doubled to 1000, landing at
        // 1500. The rule has to be about what is left rather than about the phase totals.
        Harness h = new Harness(dir, withSplit(), 4);
        h.admit(req("r1", 0.0, 64, 32), 0.0);
        h.admit(req("r2", 0.5, 64, 32), 500.0);

        assertEquals(1500.0, h.completionsMs().get("r1"), 1e-6);
    }

    @Test
    @DisplayName("beyond the slot count requests queue instead of running")
    void requestsBeyondCapacityQueue(@TempDir Path dir) throws IOException {
        Harness h = new Harness(dir, withSplit(), 1);
        h.admit(req("r1", 0.0, 64, 32), 0.0);
        h.admit(req("r2", 0.0, 64, 32), 0.0);

        assertEquals(1, h.server.queueDepth(), "the second request should be waiting, not running");
        assertEquals(1, h.server.inflight());

        Map<String, Double> done = h.completionsMs();
        assertEquals(1000.0, done.get("r1"), 1e-6);
        // r2 only starts when the slot frees, and then runs alone at concurrency 1.
        assertEquals(2000.0, done.get("r2"), 1e-6);
    }

    @Test
    @DisplayName("the queue drains in arrival order")
    void queueIsFirstInFirstOut(@TempDir Path dir) throws IOException {
        Harness h = new Harness(dir, withSplit(), 1);
        h.admit(req("r1", 0.0, 64, 32), 0.0);
        h.admit(req("r2", 0.0, 64, 32), 0.0);
        h.admit(req("r3", 0.0, 64, 32), 0.0);

        Map<String, Double> done = h.completionsMs();
        assertEquals(List.of("r1", "r2", "r3"), new ArrayList<>(done.keySet()));
    }

    @Test
    @DisplayName("an uncalibrated shape is refused loudly rather than priced at 100 ms")
    void uncalibratedShapeThrows(@TempDir Path dir) {
        // The old code substituted 100 ms whenever the cost model had no cell, which turns a
        // hole in the measurement into a plausible-looking latency. It is reachable whenever a
        // snapshot's admissibility is wider than the grid it actually sampled, which is the
        // case for the committed 1B snapshot today.
        Harness h = new Harness(dir, withSplit(), 4);

        IllegalStateException e = assertThrows(IllegalStateException.class,
                () -> h.admit(req("huge", 0.0, 4096, 32), 0.0));

        assertTrue(e.getMessage().contains("4096"), "the message should name the shape it could not price");
        assertTrue(e.getMessage().contains("n1"), "and the node it could not price it for");
    }

    @Test
    @DisplayName("the store and the veil see the node's queue state")
    void stateIsPublishedToTheStore(@TempDir Path dir) {
        // The policy reads the pool through the veil, so a node whose depth never reaches it
        // is a node every policy believes is idle.
        Harness h = new Harness(dir, withSplit(), 1);
        h.admit(req("r1", 0.0, 64, 32), 0.0);
        h.admit(req("r2", 0.0, 64, 32), 0.0);

        NodeView seen = h.store.getAllNodes().get(0);
        assertEquals(1, seen.inflight());
        assertEquals(1, seen.queueDepth());
        assertEquals(1, h.veil.getAllNodes().get(0).queueDepth());
    }

    @Test
    @DisplayName("the worker log records the batch size the request was admitted into")
    void workerLogCarriesAdmissionState(@TempDir Path dir) throws IOException {
        Harness h = new Harness(dir, withSplit(), 4);
        h.admit(req("r1", 0.0, 64, 32), 0.0);
        h.admit(req("r2", 0.1, 64, 32), 100.0);
        h.completionsMs();

        ObjectMapper mapper = new ObjectMapper();
        Map<String, Integer> inflightAtAdmit = new java.util.HashMap<>();
        for (String line : Files.readAllLines(dir.resolve("worker_n1_t.jsonl"))) {
            if (line.isBlank()) continue;
            JsonNode n = mapper.readTree(line);
            inflightAtAdmit.put(n.get("req_id").asText(), n.get("inflight_at_admission").asInt());
        }

        assertEquals(0, inflightAtAdmit.get("r1"), "r1 found the node empty");
        assertEquals(1, inflightAtAdmit.get("r2"), "r2 arrived alongside r1");
    }

    @Test
    @DisplayName("measured transport is added once at the client boundary and not to service")
    void transportOverheadLandsOnTheClientOnly(@TempDir Path dir) throws IOException {
        Harness h = new Harness(dir, withSplit(), 4);
        h.des.setTransportOverhead(new TransportOverhead(5.86, 0.0, new Random(1), true));
        h.admit(req("r1", 0.0, 64, 32), 0.0);

        assertEquals(1005.86, h.completionsMs().get("r1"), 1e-6);

        // The node's own span must not carry it: the hop overlaps other requests rather than
        // occupying a batch slot, so charging it to the node would inflate queueing the
        // hardware does not have.
        ObjectMapper mapper = new ObjectMapper();
        JsonNode w = mapper.readTree(Files.readAllLines(dir.resolve("worker_n1_t.jsonl")).get(0));
        assertEquals(1_000_000_000L, w.get("service_ns").asLong());
    }
}
