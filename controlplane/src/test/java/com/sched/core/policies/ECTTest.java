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
}