package com.sched.core.policies;

import static com.sched.Fixtures.node;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.sched.Fixtures;
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.v1.DispatchRequest;
import java.util.List;
import java.util.Random;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/**
 * JSQ is the calibration-blind arm of H1. The hypothesis is that queue depth substitutes for
 * knowing how fast a node is, so it matters that this policy genuinely knows nothing else.
 */
class JSQTest {
    private static final DispatchRequest ANY = DispatchRequest.getDefaultInstance();
    private final JSQ policy = new JSQ();

    @Test
    @DisplayName("the shortest queue wins")
    void shortestQueueWins() {
        List<NodeView> nodes = List.of(node("busy", 5, 2, 100.0), node("quiet", 0, 1, 1.0));
        assertEquals("quiet", policy.choose(ANY, nodes, 0L, new Random(1)).chosen().orElseThrow());
    }

    @Test
    @DisplayName("capability is ignored entirely")
    void capabilityDoesNotEnterTheScore() {
        // If this ever starts mattering, JSQ has stopped being the blind arm and H1 no longer
        // measures what it says it measures.
        List<NodeView> slowFirst = List.of(node("a", 2, 0, 1.0), node("b", 2, 0, 1000.0));
        Policy.Choice choice = policy.choose(ANY, slowFirst, 0L, new Random(1));

        assertEquals(2.0, choice.scores().get("a"), 1e-9);
        assertEquals(2.0, choice.scores().get("b"), 1e-9);
    }

    @Test
    @DisplayName("queued and in-flight are both queue")
    void scoreIsQueueDepthPlusInflight() {
        Policy.Choice choice = policy.choose(ANY, List.of(node("n", 3, 4, 10.0)), 0L, new Random(1));
        assertEquals(7.0, choice.scores().get("n"), 1e-9);
    }

    @Test
    @DisplayName("equal queues are split by the draw")
    void equalQueuesAreSplitRandomly() {
        // Integer queue depths mean ties are the normal case here, not an edge case.
        List<NodeView> nodes = List.of(node("a", 1, 0, 10.0), node("b", 1, 0, 10.0));

        assertEquals("a", policy.choose(ANY, nodes, 0L, new Fixtures.FixedRandom(0.10)).chosen().orElseThrow());
        assertEquals("b", policy.choose(ANY, nodes, 0L, new Fixtures.FixedRandom(0.90)).chosen().orElseThrow());
    }

    @Test
    @DisplayName("an idle pool is spread rather than pinned to the first node")
    void idlePoolIsSpread() {
        List<NodeView> nodes = List.of(node("a", 0, 0, 10.0), node("b", 0, 0, 10.0));
        Random rng = new Random(11);

        int a = 0;
        for (int i = 0; i < 400; i++) {
            if ("a".equals(policy.choose(ANY, nodes, 0L, rng).chosen().orElseThrow())) a++;
        }
        assertTrue(a > 150 && a < 250, "expected a roughly even split, node a took " + a + " of 400");
    }

    @Test
    @DisplayName("every candidate is scored")
    void allCandidatesAreScored() {
        List<NodeView> nodes = List.of(node("a", 0, 0, 1.0), node("b", 1, 0, 1.0), node("c", 2, 0, 1.0));
        assertEquals(3, policy.choose(ANY, nodes, 0L, new Random(1)).scores().size());
    }

    @Test
    @DisplayName("an empty admissible set chooses nothing")
    void emptyAdmissibleSetYieldsNoChoice() {
        Policy.Choice choice = policy.choose(ANY, List.of(), 0L, new Random(1));
        assertFalse(choice.chosen().isPresent());
        assertTrue(choice.scores().isEmpty());
    }

    @Test
    @DisplayName("the reported draw is the one that chose the node")
    void reportedDrawMatchesTheDecision() {
        List<NodeView> nodes = List.of(node("a", 0, 0, 1.0), node("b", 0, 0, 1.0));
        Policy.Choice choice = policy.choose(ANY, nodes, 0L, new Fixtures.FixedRandom(0.75));

        assertEquals(0.75, choice.tieBreakDraw(), 1e-12);
        assertEquals("b", choice.chosen().orElseThrow());
    }
}
