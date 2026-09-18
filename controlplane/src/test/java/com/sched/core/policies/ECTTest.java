package com.sched.core.policies;

import static com.sched.Fixtures.node;
import static com.sched.Fixtures.snapshot;
import static com.sched.Fixtures.splitCell;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.sched.core.models.CostModelSnapshot;
import com.sched.core.interfaces.StateStore.NodeView;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import com.sched.v1.DispatchRequest;

class ECTTest {

    private static final CostModelSnapshot SPLIT_SNAP = snapshot("split", 0.0, List.of(
            splitCell(1, 128, 1, 64, 1, 1000.0, 200.0, 800.0),
            splitCell(1, 128, 1, 64, 2, 2000.0, 200.0, 1750.0)));

    private static final CostModelSnapshot NO_SPLIT_SNAP = snapshot("flat", 0.0, List.of(
            com.sched.Fixtures.cell(1, 128, 1, 64, 1, 1000.0),
            com.sched.Fixtures.cell(1, 128, 1, 64, 2, 2000.0)));

    @Test
    @DisplayName("known mode prices from matching cell")
    void knownModePricesFromMatchingCell() {
        Map<String, CostModelSnapshot> snaps = Map.of("n1", SPLIT_SNAP);
        Map<String, Integer> caps = Map.of("n1", 4);
        ECT pol = new ECT(snaps, caps, ECT.MODE_KNOWN, 16);
        DispatchRequest req = com.sched.v1.DispatchRequest.getDefaultInstance();

        List<NodeView> nodes = List.of(node("n1", 0, 0, 100.0));
        var choice = pol.choose(req, nodes, 0L, new java.util.Random(1));

        assertEquals("n1", choice.chosen().orElseThrow());
    }

    @Test
    @DisplayName("unknown mode uses prior output length")
    void unknownModeUsesPriorOutputLen() {
        Map<String, CostModelSnapshot> snaps = Map.of("n1", SPLIT_SNAP);
        Map<String, Integer> caps = Map.of("n1", 4);
        ECT pol = new ECT(snaps, caps, ECT.MODE_UNKNOWN, 42);
        DispatchRequest req = com.sched.v1.DispatchRequest.getDefaultInstance();

        List<NodeView> nodes = List.of(node("n1", 0, 0, 100.0));
        var choice = pol.choose(req, nodes, 0L, new java.util.Random(1));

        assertEquals("n1", choice.chosen().orElseThrow());
    }

    @Test
    @DisplayName("fallback to scalar when cell missing in known mode")
    void fallbackToScalarWhenCellMissing() {
        Map<String, CostModelSnapshot> snaps = Map.of("n1", NO_SPLIT_SNAP);
        Map<String, Integer> caps = Map.of("n1", 4);
        ECT pol = new ECT(snaps, caps, ECT.MODE_KNOWN, 16);
        DispatchRequest req = com.sched.v1.DispatchRequest.getDefaultInstance();

        List<NodeView> nodes = List.of(node("n1", 2, 3, 10.0));
        var choice = pol.choose(req, nodes, 0L, new java.util.Random(1));

        assertEquals("n1", choice.chosen().orElseThrow());
        assertTrue(Double.isFinite(choice.scores().get("n1")));
    }

    @Test
    @DisplayName("fallback to scalar when cell missing in unknown mode")
    void fallbackToScalarWhenCellMissingUnknownMode() {
        Map<String, CostModelSnapshot> snaps = Map.of("n1", NO_SPLIT_SNAP);
        Map<String, Integer> caps = Map.of("n1", 4);
        ECT pol = new ECT(snaps, caps, ECT.MODE_UNKNOWN, 42);
        DispatchRequest req = com.sched.v1.DispatchRequest.getDefaultInstance();

        List<NodeView> nodes = List.of(node("n1", 2, 3, 10.0));
        var choice = pol.choose(req, nodes, 0L, new java.util.Random(1));

        assertEquals("n1", choice.chosen().orElseThrow());
        assertTrue(Double.isFinite(choice.scores().get("n1")));
    }

    @Test
    @DisplayName("empty admissible set yields no choice")
    void emptyAdmissibleSetYieldsNoChoice() {
        Map<String, CostModelSnapshot> snaps = Map.of("n1", SPLIT_SNAP);
        Map<String, Integer> caps = Map.of("n1", 4);
        ECT pol = new ECT(snaps, caps, ECT.MODE_KNOWN, 16);
        DispatchRequest req = com.sched.v1.DispatchRequest.getDefaultInstance();

        var choice = pol.choose(req, List.of(), 0L, new java.util.Random(1));

        assertFalse(choice.chosen().isPresent());
        assertTrue(choice.scores().isEmpty());
    }

    @Test
    @DisplayName("scores all candidates when cell missing")
    void scoresAllCandidatesWhenCellMissing() {
        Map<String, CostModelSnapshot> snaps = Map.of("n1", NO_SPLIT_SNAP);
        Map<String, Integer> caps = Map.of("n1", 4);
        ECT pol = new ECT(snaps, caps, ECT.MODE_KNOWN, 16);
        DispatchRequest req = com.sched.v1.DispatchRequest.getDefaultInstance();

        List<NodeView> nodes = List.of(node("n1", 1, 0, 50.0), node("n2", 0, 1, 30.0));
        var choice = pol.choose(req, nodes, 0L, new java.util.Random(1));

        assertEquals(2, choice.scores().size());
        assertTrue(Double.isFinite(choice.scores().get("n1")));
        assertTrue(Double.isFinite(choice.scores().get("n2")));
    }

    @Test
    @DisplayName("known mode uses request output length")
    void knownModeUsesRequestOutputLen() {
        Map<String, CostModelSnapshot> snaps = Map.of("n1", SPLIT_SNAP);
        Map<String, Integer> caps = Map.of("n1", 4);
        ECT pol = new ECT(snaps, caps, ECT.MODE_KNOWN, 16);
        DispatchRequest req = com.sched.v1.DispatchRequest.newBuilder()
                .setOutputLen(32)
                .build();

        List<NodeView> nodes = List.of(node("n1", 0, 0, 100.0));
        var choice = pol.choose(req, nodes, 0L, new java.util.Random(1));

        assertEquals("n1", choice.chosen().orElseThrow());
    }

    // ---- Tests added for test-plan 3.8 coverage gaps ----

    /**
     * On two nodes where scalar capability and the cost model disagree, ECT picks the
     * lower predicted completion. This is the core pricing value proposition of P6.
     */
    @Test
    @DisplayName("ECT picks lower predicted completion when scalar capability disagrees")
    void ectPicksLowerCompletionOverHigherCapability() {
        // Node A: low scalar capability (10 tok/s) but a fast cost-model cell (500 ms service)
        CostModelSnapshot snapA = snapshot("a", 0.0, List.of(
                splitCell(1, 128, 1, 64, 1, 500.0, 100.0, 400.0)));
        // Node B: high scalar capability (200 tok/s) but a slow cost-model cell (3000 ms service)
        CostModelSnapshot snapB = snapshot("b", 0.0, List.of(
                splitCell(1, 128, 1, 64, 1, 3000.0, 500.0, 2500.0)));

        Map<String, CostModelSnapshot> snaps = Map.of("a", snapA, "b", snapB);
        Map<String, Integer> caps = Map.of("a", 4, "b", 4);
        ECT pol = new ECT(snaps, caps, ECT.MODE_KNOWN, 16);
        DispatchRequest req = DispatchRequest.newBuilder().setOutputLen(32)
                .addAllPromptTokenIds(java.util.Collections.nCopies(64, 0)).build();

        // Node B has higher capability but slower predicted completion
        List<NodeView> nodes = List.of(node("a", 0, 0, 10.0), node("b", 0, 0, 200.0));
        var choice = pol.choose(req, nodes, 0L, new java.util.Random(1));

        assertEquals("a", choice.chosen().orElseThrow(),
                "ECT must pick the node with lower predicted completion, not higher scalar capability");
        assertTrue(choice.scores().get("a") < choice.scores().get("b"),
                "node a's score (predicted ms) must be lower than node b's");
    }

    @Test
    @DisplayName("known and unknown modes give different scores for the same request")
    void knownAndUnknownModesGiveDifferentScores() {
        // Snapshot with TWO output buckets so different outputLen values land in
        // different cells with different service times.
        CostModelSnapshot multiOut = snapshot("multi", 0.0, List.of(
                splitCell(1, 128, 1, 64, 1, 1000.0, 200.0, 800.0),
                splitCell(1, 128, 65, 128, 1, 1500.0, 300.0, 1200.0)));

        Map<String, CostModelSnapshot> snaps = Map.of("n1", multiOut);
        Map<String, Integer> caps = Map.of("n1", 4);

        // Request with outputLen=80 (lands in [65,128] bucket => 1500 ms)
        // Prior=32 (lands in [1,64] bucket => 1000 ms)
        DispatchRequest req = DispatchRequest.newBuilder().setOutputLen(80)
                .addAllPromptTokenIds(java.util.Collections.nCopies(64, 0)).build();

        ECT known = new ECT(snaps, caps, ECT.MODE_KNOWN, 32);
        ECT unknown = new ECT(snaps, caps, ECT.MODE_UNKNOWN, 32);

        List<NodeView> nodes = List.of(node("n1", 0, 0, 100.0));
        double scoreKnown = known.choose(req, nodes, 0L, new java.util.Random(1)).scores().get("n1");
        double scoreUnknown = unknown.choose(req, nodes, 0L, new java.util.Random(1)).scores().get("n1");

        assertTrue(Math.abs(scoreKnown - scoreUnknown) > 1e-9,
                "known mode (outputLen=80->1500ms) and unknown mode (prior=32->1000ms) must produce different scores, "
                + "got known=" + scoreKnown + " unknown=" + scoreUnknown);
    }

    @Test
    @DisplayName("changed output length changes known-mode score")
    void changedOutputLenChangesKnownModeScore() {
        // Snapshot with two output buckets so different outputLen values land in
        // different cells with different service times.
        CostModelSnapshot multiOut = snapshot("multi", 0.0, List.of(
                splitCell(1, 128, 1, 64, 1, 1000.0, 200.0, 800.0),
                splitCell(1, 128, 65, 128, 1, 1500.0, 300.0, 1200.0)));

        Map<String, CostModelSnapshot> snaps = Map.of("n1", multiOut);
        Map<String, Integer> caps = Map.of("n1", 4);
        ECT pol = new ECT(snaps, caps, ECT.MODE_KNOWN, 16);

        // outputLen=32 lands in [1,64] bucket (1000 ms)
        DispatchRequest reqSmall = DispatchRequest.newBuilder().setOutputLen(32)
                .addAllPromptTokenIds(java.util.Collections.nCopies(64, 0)).build();
        // outputLen=80 lands in [65,128] bucket (1500 ms)
        DispatchRequest reqLarge = DispatchRequest.newBuilder().setOutputLen(80)
                .addAllPromptTokenIds(java.util.Collections.nCopies(64, 0)).build();

        List<NodeView> nodes = List.of(node("n1", 0, 0, 100.0));
        double scoreSmall = pol.choose(reqSmall, nodes, 0L, new java.util.Random(1)).scores().get("n1");
        double scoreLarge = pol.choose(reqLarge, nodes, 0L, new java.util.Random(1)).scores().get("n1");

        assertTrue(Math.abs(scoreSmall - scoreLarge) > 1e-9,
                "different output lengths must produce different known-mode scores, "
                + "got 32=" + scoreSmall + " 80=" + scoreLarge);
    }

    /**
     * When a cell is missing the fallback must equal (pending+1) * outputLen / capability * 1000.0
     * in milliseconds, not merely be finite. A one-node pool asserts nothing about pricing.
     */
    @Test
    @DisplayName("fallback score matches exact ms calculation, not only finite")
    void fallbackScoreMatchesExactMsCalculation() {
        // n2 has NO snapshot in the map, so its cells are all missing
        Map<String, CostModelSnapshot> snaps = Map.of();
        Map<String, Integer> caps = Map.of("n2", 4);
        ECT pol = new ECT(snaps, caps, ECT.MODE_KNOWN, 16);

        int outputLen = 32;
        double capability = 50.0;
        int queueDepth = 3;
        int inflight = 2;
        double pending = queueDepth + inflight;

        DispatchRequest req = DispatchRequest.newBuilder().setOutputLen(outputLen)
                .addAllPromptTokenIds(java.util.Collections.nCopies(64, 0)).build();
        List<NodeView> nodes = List.of(node("n2", queueDepth, inflight, capability));
        double actualScore = pol.choose(req, nodes, 0L, new java.util.Random(1)).scores().get("n2");

        double expectedMs = (pending + 1.0) * Math.max(outputLen, 1) / Math.max(capability, 0.001) * 1000.0;
        assertEquals(expectedMs, actualScore, 1e-6,
                "fallback score must be (pending+1)*outputLen/capability*1000 ms exactly");
    }
}