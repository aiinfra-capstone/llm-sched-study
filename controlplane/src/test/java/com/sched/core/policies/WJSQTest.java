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
 * WJSQ is the capability-aware arm of H1, so the whole substitution hypothesis is measured
 * against whether this policy uses the capability it is given.
 */
class WJSQTest {
    private static final DispatchRequest ANY = DispatchRequest.getDefaultInstance();
    private final WJSQ policy = new WJSQ();

    @Test
    @DisplayName("an idle fast node beats an idle slow one")
    void idlePoolRoutesToTheFasterNode() {
        // The bug this pins: scoring pending/capability gives 0/100 == 0/1 == 0.0, so a GPU
        // and a CPU were indistinguishable exactly when the choice was free, and the tie-break
        // then handed the request to whichever came first in the list. Scoring
        // (pending+1)/capability makes the idle GPU 0.01 against the idle CPU's 1.0.
        List<NodeView> nodes = List.of(node("cpu", 0, 0, 1.0), node("gpu", 0, 0, 100.0));

        for (double draw : new double[] {0.0, 0.5, 0.99}) {
            Policy.Choice choice = policy.choose(ANY, nodes, 0L, new Fixtures.FixedRandom(draw));
            assertEquals("gpu", choice.chosen().orElseThrow(),
                    "an idle capability-weighted pool must prefer the fast node at every draw");
        }
    }

    @Test
    @DisplayName("the score is (pending + 1) / capability")
    void scoreIsExpectedClearanceTime() {
        List<NodeView> nodes = List.of(node("cpu", 0, 0, 1.0), node("gpu", 0, 0, 100.0));
        Policy.Choice choice = policy.choose(ANY, nodes, 0L, new Random(1));

        assertEquals(1.0, choice.scores().get("cpu"), 1e-9);
        assertEquals(0.01, choice.scores().get("gpu"), 1e-9);
    }

    @Test
    @DisplayName("queue depth and in-flight both count as pending")
    void pendingCountsQueuedAndInflight() {
        List<NodeView> nodes = List.of(node("n", 2, 3, 10.0));
        Policy.Choice choice = policy.choose(ANY, nodes, 0L, new Random(1));

        assertEquals((2 + 3 + 1) / 10.0, choice.scores().get("n"), 1e-9);
    }

    @Test
    @DisplayName("enough queued work overrides raw speed")
    void aLoadedFastNodeLosesToAnIdleSlowOne() {
        // 101/100 against 1/1: the fast node is now the worse bet, which is the behaviour the
        // policy exists to produce and the reason it is not just "always pick the GPU".
        List<NodeView> nodes = List.of(node("gpu", 100, 0, 100.0), node("cpu", 0, 0, 1.0));
        Policy.Choice choice = policy.choose(ANY, nodes, 0L, new Random(1));

        assertEquals("cpu", choice.chosen().orElseThrow());
    }

    @Test
    @DisplayName("a one-node pool still reports a score")
    void singleNodePoolPopulatesTheScoreMap() {
        // The second bug in this file: scores were built as a side effect inside the
        // comparator, and Stream.min over one element performs no comparisons, so a one-node
        // pool logged an empty scores map. That is the pool this study has been running on,
        // and C-4 wants the score of every candidate.
        Policy.Choice choice = policy.choose(ANY, List.of(node("only", 1, 1, 20.0)), 0L, new Random(1));

        assertEquals(1, choice.scores().size());
        assertEquals(3 / 20.0, choice.scores().get("only"), 1e-9);
    }

    @Test
    @DisplayName("every candidate is scored, not just the winner")
    void allCandidatesAppearInTheScoreMap() {
        List<NodeView> nodes = List.of(node("a", 0, 0, 1.0), node("b", 5, 0, 50.0), node("c", 2, 1, 10.0));
        Policy.Choice choice = policy.choose(ANY, nodes, 0L, new Random(1));

        assertEquals(3, choice.scores().size(), "F-3 wants the full candidate set in the decision record");
    }

    @Test
    @DisplayName("a node reporting no capability is not divided by zero")
    void zeroCapabilityIsFloored() {
        List<NodeView> nodes = List.of(node("dead", 0, 0, 0.0), node("alive", 0, 0, 10.0));
        Policy.Choice choice = policy.choose(ANY, nodes, 0L, new Random(1));

        assertTrue(Double.isFinite(choice.scores().get("dead")), "a stale heartbeat must not produce infinity");
        assertEquals("alive", choice.chosen().orElseThrow());
    }

    @Test
    @DisplayName("an empty admissible set chooses nothing and reports the draw as absent")
    void emptyAdmissibleSetYieldsNoChoice() {
        Policy.Choice choice = policy.choose(ANY, List.of(), 0L, new Random(1));

        assertFalse(choice.chosen().isPresent());
        assertTrue(choice.scores().isEmpty());
        assertEquals(null, choice.tieBreakDraw());
    }

    @Test
    @DisplayName("the reported draw is the one that chose the node")
    void reportedDrawMatchesTheDecision() {
        // C-4 requires tie_break_draw on every decision record. It is only worth recording if
        // it is the value that actually selected, which is why the tie-break indexes on it
        // rather than calling nextInt separately.
        List<NodeView> nodes = List.of(node("a", 0, 0, 5.0), node("b", 0, 0, 5.0));

        Policy.Choice low = policy.choose(ANY, nodes, 0L, new Fixtures.FixedRandom(0.10));
        assertEquals(0.10, low.tieBreakDraw(), 1e-12);
        assertEquals("a", low.chosen().orElseThrow());

        Policy.Choice high = policy.choose(ANY, nodes, 0L, new Fixtures.FixedRandom(0.90));
        assertEquals(0.90, high.tieBreakDraw(), 1e-12);
        assertEquals("b", high.chosen().orElseThrow());
    }

    @Test
    @DisplayName("one seeded run reproduces another, which is F-20")
    void sameSeedGivesTheSameSequence() {
        List<NodeView> nodes = List.of(node("a", 0, 0, 5.0), node("b", 0, 0, 5.0), node("c", 0, 0, 5.0));

        StringBuilder first = new StringBuilder();
        Random r1 = new Random(20260903);
        for (int i = 0; i < 50; i++) first.append(policy.choose(ANY, nodes, 0L, r1).chosen().orElseThrow());

        StringBuilder second = new StringBuilder();
        Random r2 = new Random(20260903);
        for (int i = 0; i < 50; i++) second.append(policy.choose(ANY, nodes, 0L, r2).chosen().orElseThrow());

        assertEquals(first.toString(), second.toString());
    }

    @Test
    @DisplayName("across many draws a tied pool is actually spread")
    void tiedPoolIsSpreadRatherThanPinned() {
        List<NodeView> nodes = List.of(node("a", 0, 0, 5.0), node("b", 0, 0, 5.0));
        Random rng = new Random(7);

        int a = 0;
        for (int i = 0; i < 400; i++) {
            if ("a".equals(policy.choose(ANY, nodes, 0L, rng).chosen().orElseThrow())) a++;
        }

        // The old comparator would have made this exactly 400.
        assertTrue(a > 150 && a < 250, "expected a roughly even split, node a took " + a + " of 400");
    }
}
