package com.sched.sim;

import static com.sched.Fixtures.snapshot;
import static com.sched.Fixtures.splitCell;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;

import com.sched.core.AdmissionFilter;
import com.sched.core.Capability;
import com.sched.core.DecisionLogger;
import com.sched.core.InMemoryStateStore;
import com.sched.core.StalenessVeil;
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.core.models.CostModelSnapshot;
import com.sched.core.models.TraceRequest;
import com.sched.core.policies.Policies;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.File;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/**
 * Test-plan 3.8 "Determinism": two simulator runs of one manifest produce an identical
 * dispatch sequence, asserted in the Java suite. The event loop, the policy, and the
 * service sampler are all seeded from the manifest's seed, so the only question is whether
 * anything non-deterministic leaks in (thread scheduling, HashMap iteration order, etc.).
 */
class SimulatorDeterminismTest {

    private static final CostModelSnapshot SNAP_A = snapshot("a", 0.1, List.of(
            splitCell(1, 512, 1, 256, 1, 600.0, 120.0, 480.0),
            splitCell(1, 512, 1, 256, 2, 900.0, 130.0, 770.0),
            splitCell(1, 512, 1, 256, 3, 1200.0, 135.0, 1065.0),
            splitCell(1, 512, 1, 256, 4, 1500.0, 140.0, 1360.0)));

    private static final CostModelSnapshot SNAP_B = snapshot("b", 0.1, List.of(
            splitCell(1, 512, 1, 256, 1, 400.0, 80.0, 320.0),
            splitCell(1, 512, 1, 256, 2, 700.0, 90.0, 610.0),
            splitCell(1, 512, 1, 256, 3, 1000.0, 100.0, 900.0),
            splitCell(1, 512, 1, 256, 4, 1300.0, 110.0, 1190.0)));

    /** A small synthetic trace: 10 requests arriving over 5 seconds. */
    private static List<TraceRequest> syntheticTrace() {
        List<TraceRequest> reqs = new ArrayList<>();
        Random gen = new Random(12345);
        for (int i = 0; i < 10; i++) {
            double arrival = i * 0.5;
            int promptLen = 32 + gen.nextInt(64);
            int outputLen = 16 + gen.nextInt(48);
            reqs.add(new TraceRequest(String.valueOf(i + 1), "req-" + i, arrival, promptLen, outputLen,
                    "p128_o64", 0));
        }
        return reqs;
    }

    /**
     * Run one DES pass and return the dispatch sequence as (reqId, chosenNode) pairs.
     */
    private List<String> runOnce(int seed, File outputDir) throws Exception {
        String runId = "det-" + seed;
        SimClock clk = new SimClock();
        DiscreteEventSimulator des = new DiscreteEventSimulator(clk);
        InMemoryStateStore store = new InMemoryStateStore();
        StalenessVeil veil = new StalenessVeil(0L, clk);

        Map<String, CostModelSnapshot> snaps = Map.of("nodeA", SNAP_A, "nodeB", SNAP_B);
        Map<String, CostModelSnapshot.Admissibility> bounds = new HashMap<>();
        bounds.put("nodeA", SNAP_A.admissibility());
        bounds.put("nodeB", SNAP_B.admissibility());
        AdmissionFilter filter = new AdmissionFilter(bounds);

        Random policyRng = new Random(seed);
        Map<String, Random> serviceRng = new HashMap<>();
        serviceRng.put("nodeA", new Random(seed + 100));
        serviceRng.put("nodeB", new Random(seed + 101));
        ServiceSampler sampler = new ServiceSampler(snaps, new Random(seed + 1000), serviceRng);

        DecisionLogger logger = new DecisionLogger(outputDir.getAbsolutePath(), runId);

        // Seed both nodes
        for (Map.Entry<String, CostModelSnapshot> e : snaps.entrySet()) {
            double cap = Capability.referenceTokS(e.getValue());
            NodeView nv = new NodeView(e.getKey(), 0, 0, cap, 0L, true);
            store.updateNode(nv);
            veil.seed(nv, 0L);
            des.addServer(new SimNodeServer(e.getKey(), 4));
        }

        Map<String, Integer> capacities = Map.of("nodeA", 4, "nodeB", 4);
        Policy pol = Policies.fromName("jsq", new AtomicInteger(0), 0.0, snaps, capacities);
        AtomicLong seq = new AtomicLong(0);

        for (TraceRequest rq : syntheticTrace()) {
            long arrNs = (long) (rq.arrivalOffsetS() * 1_000_000_000L);
            des.scheduleEvent(new RequestArrivalEvent(
                    arrNs, rq, pol, veil, filter, des, policyRng, sampler, store,
                    logger, runId, "jsq", 0.0, seq));
        }

        des.run();
        logger.close();

        // Read the decision log and extract the dispatch sequence
        File logFile = new File(outputDir, "scheduler_" + runId + ".jsonl");
        List<String> lines = Files.readAllLines(logFile.toPath());
        ObjectMapper mapper = new ObjectMapper();
        List<JsonNode> decisions = new ArrayList<>();
        for (String line : lines) {
            JsonNode node = mapper.readTree(line);
            if ("decision".equals(node.get("type").asText())) {
                decisions.add(node);
            }
        }
        decisions.sort(Comparator.comparingLong(d -> d.get("decision_seq").asLong()));

        List<String> sequence = new ArrayList<>();
        for (JsonNode d : decisions) {
            String reqId = d.get("req_id").asText();
            String chosen = d.has("chosen_node") && !d.get("chosen_node").isNull()
                    ? d.get("chosen_node").asText() : "NONE";
            sequence.add(reqId + "->" + chosen);
        }
        return sequence;
    }

    @Test
    @DisplayName("two simulator runs with the same seed produce identical dispatch sequences")
    void twoRunsProduceIdenticalSequence() throws Exception {
        File dir1 = Files.createTempDirectory("sim-det-1").toFile();
        File dir2 = Files.createTempDirectory("sim-det-2").toFile();

        List<String> seq1 = runOnce(42, dir1);
        List<String> seq2 = runOnce(42, dir2);

        assertFalse(seq1.isEmpty(), "the simulation must produce decisions");
        assertEquals(seq1.size(), seq2.size(),
                "both runs must produce the same number of decisions");
        assertEquals(seq1, seq2,
                "both runs must produce an identical dispatch sequence element by element");
    }
}
