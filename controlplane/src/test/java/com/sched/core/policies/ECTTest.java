package com.sched.core.policies;

import static com.sched.Fixtures.node;
import static com.sched.Fixtures.snapshot;
import static com.sched.Fixtures.splitCell;
import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
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

    // ---------------------------------------------------------------- pricing (P6)
    //
    // The tests above are single-node, so `assertEquals("n1", chosen)` holds whatever the
    // scores are: an ECT that returned a constant would pass them. These put two nodes in
    // and assert on the numbers, because pricing per request from the cost model is the
    // only reason this policy exists.

    /** Slow per token, but its cells say short outputs are cheap here. */
    private static final CostModelSnapshot PRICED_SLOW = snapshot("priced_slow", 0.0, List.of(
            splitCell(1, 128, 1, 64, 1, 400.0, 100.0, 300.0),
            splitCell(1, 128, 65, 128, 1, 2000.0, 100.0, 1900.0)));

    /** Fast per token, and its cells say so for long outputs. */
    private static final CostModelSnapshot PRICED_FAST = snapshot("priced_fast", 0.0, List.of(
            splitCell(1, 128, 1, 64, 1, 900.0, 300.0, 600.0),
            splitCell(1, 128, 65, 128, 1, 1200.0, 300.0, 900.0)));

    private static DispatchRequest request(int promptLen, int outputLen) {
        return DispatchRequest.newBuilder()
                .addAllPromptTokenIds(java.util.Collections.nCopies(promptLen, 1))
                .setOutputLen(outputLen)
                .build();
    }

    @Test
    @DisplayName("the lower predicted completion wins even when the scalar capability disagrees")
    void pricingBeatsScalarCapability() {
        Map<String, CostModelSnapshot> snaps = Map.of("slow", PRICED_SLOW, "fast", PRICED_FAST);
        Map<String, Integer> caps = Map.of("slow", 4, "fast", 4);
        ECT pol = new ECT(snaps, caps, ECT.MODE_KNOWN, 16);
        // "slow" carries the lower capability, which is what wjsq and static_weighted would
        // route on, and the higher one for a long output. For a short output its own cost
        // model says it finishes first, and that is the call ECT is supposed to make.
        List<NodeView> nodes = List.of(node("slow", 0, 0, 50.0), node("fast", 0, 0, 150.0));

        var shortOut = pol.choose(request(64, 32), nodes, 0L, new java.util.Random(1));
        assertEquals("slow", shortOut.chosen().orElseThrow(),
                "a 32-token output is priced at 400 ms on slow against 900 ms on fast");
        assertEquals(400.0, shortOut.scores().get("slow"), 1e-9);
        assertEquals(900.0, shortOut.scores().get("fast"), 1e-9);

        var longOut = pol.choose(request(64, 96), nodes, 0L, new java.util.Random(1));
        assertEquals("fast", longOut.chosen().orElseThrow(),
                "a 96-token output is priced at 2000 ms on slow against 1200 ms on fast");
        assertEquals(2000.0, longOut.scores().get("slow"), 1e-9);
        assertEquals(1200.0, longOut.scores().get("fast"), 1e-9);
    }

    @Test
    @DisplayName("output length moves a known-mode score, and can move the choice")
    void outputLengthMovesTheScore() {
        Map<String, CostModelSnapshot> snaps = Map.of("slow", PRICED_SLOW);
        Map<String, Integer> caps = Map.of("slow", 4);
        ECT pol = new ECT(snaps, caps, ECT.MODE_KNOWN, 16);
        List<NodeView> nodes = List.of(node("slow", 0, 0, 50.0));

        double shortScore = pol.choose(request(64, 32), nodes, 0L, new java.util.Random(1))
                .scores().get("slow");
        double longScore = pol.choose(request(64, 96), nodes, 0L, new java.util.Random(1))
                .scores().get("slow");

        assertEquals(400.0, shortScore, 1e-9);
        assertEquals(2000.0, longScore, 1e-9);
        assertTrue(longScore > shortScore, "a longer output must price higher, not the same");
    }

    @Test
    @DisplayName("the prior replaces the request's length in unknown mode, and changes the score")
    void unknownModePricesFromThePriorNotTheRequest() {
        Map<String, CostModelSnapshot> snaps = Map.of("slow", PRICED_SLOW);
        Map<String, Integer> caps = Map.of("slow", 4);
        List<NodeView> nodes = List.of(node("slow", 0, 0, 50.0));
        // One request, 96 output tokens, priced by each mode in turn.
        DispatchRequest req = request(64, 96);

        double known = new ECT(snaps, caps, ECT.MODE_KNOWN, 32)
                .choose(req, nodes, 0L, new java.util.Random(1)).scores().get("slow");
        double unknown = new ECT(snaps, caps, ECT.MODE_UNKNOWN, 32)
                .choose(req, nodes, 0L, new java.util.Random(1)).scores().get("slow");

        assertEquals(2000.0, known, 1e-9, "known mode reads output_len 96, the long-output cell");
        assertEquals(400.0, unknown, 1e-9, "unknown mode reads the prior 32, the short-output cell");
        assertNotEquals(known, unknown,
                "if the prior did not change the score, the two ECT arms measure the same thing");
    }

    @Test
    @DisplayName("a node with no cell is priced in milliseconds, comparable with a priced node")
    void fallbackIsAPredictedCompletionInMilliseconds() {
        // The bug this pins: the fallback used to return (pending+1)/capability, a unitless
        // number around 0.02, and every unpriced node beat every priced one on a comparison
        // that was really between units. Asserting the score is finite did not catch it.
        Map<String, CostModelSnapshot> snaps = Map.of("priced", PRICED_FAST);
        Map<String, Integer> caps = Map.of("priced", 4, "unpriced", 4);
        ECT pol = new ECT(snaps, caps, ECT.MODE_KNOWN, 16);
        // 100 tok/s and 64 tokens to produce, nothing pending: one second of decoding.
        List<NodeView> nodes = List.of(node("priced", 0, 0, 150.0), node("unpriced", 0, 0, 100.0));

        var choice = pol.choose(request(64, 96), nodes, 0L, new java.util.Random(1));

        assertEquals(1200.0, choice.scores().get("priced"), 1e-9);
        assertEquals(960.0, choice.scores().get("unpriced"), 1e-9,
                "(0 pending + 1) * 96 tokens / 100 tok/s = 0.96 s, in ms");
        assertEquals("unpriced", choice.chosen().orElseThrow(),
                "on these numbers the unpriced node really is faster, and the comparison is in one unit");

        // And a node that is genuinely slower loses, which a unitless fallback could not do.
        List<NodeView> slowUnpriced = List.of(node("priced", 0, 0, 150.0), node("unpriced", 2, 2, 20.0));
        var second = pol.choose(request(64, 96), slowUnpriced, 0L, new java.util.Random(1));
        assertEquals(24000.0, second.scores().get("unpriced"), 1e-9,
                "(4 pending + 1) * 96 tokens / 20 tok/s = 24 s, in ms");
        assertEquals("priced", second.chosen().orElseThrow());
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

    @Test
    @DisplayName("nothing is defaulted: a bad mode, a missing prior or a missing slot count is refused")
    void refusesWhatItOnceDefaulted() {
        // J12. Each of these used to become known mode, a prior of 16 or a capacity of 4
        // without a word, so a manifest could name one model and the run price another.
        Map<String, CostModelSnapshot> snaps = Map.of("n1", SPLIT_SNAP);
        Map<String, Integer> caps = Map.of("n1", 4);
        for (String mode : java.util.Arrays.asList(null, "", "prior", "KNOWN")) {
            IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                    () -> new ECT(snaps, caps, mode, 16), "mode " + mode);
            assertTrue(e.getMessage().contains("ect_mode"), e.getMessage());
        }
        for (Integer prior : java.util.Arrays.asList(null, 0, -3)) {
            assertThrows(IllegalArgumentException.class,
                    () -> new ECT(snaps, caps, ECT.MODE_UNKNOWN, prior), "prior " + prior);
        }
        for (Map<String, Integer> noSlots : List.of(Map.<String, Integer>of(), Map.of("n1", 0))) {
            IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                    () -> new ECT(snaps, noSlots, ECT.MODE_KNOWN, null), "capacities " + noSlots);
            assertTrue(e.getMessage().contains("n1"), "the message names the node: " + e.getMessage());
        }

        // Known mode reads each request's own length and needs no prior; a node with no
        // snapshot is priced by the scalar fallback and needs no slot count.
        assertDoesNotThrow(() -> new ECT(snaps, caps, ECT.MODE_KNOWN, null));
        assertDoesNotThrow(() -> new ECT(Map.of(), Map.of(), ECT.MODE_KNOWN, null));
    }
}
