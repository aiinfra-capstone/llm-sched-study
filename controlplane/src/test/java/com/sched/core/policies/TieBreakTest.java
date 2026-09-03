package com.sched.core.policies;

import static com.sched.Fixtures.node;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.sched.core.interfaces.StateStore.NodeView;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/**
 * Policies.breakTie, which is most of what JSQ and WJSQ actually do.
 *
 * Queue depths are small integers, so two nodes scoring the same is the common case rather
 * than the exception, and for WJSQ before the scoring fix every idle node scored zero. That
 * makes the tie-break the thing that decides where requests go under light load, which is
 * exactly the regime H1 is about.
 */
class TieBreakTest {

    private static Map<String, Double> scores(Object... pairs) {
        Map<String, Double> out = new HashMap<>();
        for (int i = 0; i < pairs.length; i += 2) {
            out.put((String) pairs[i], ((Number) pairs[i + 1]).doubleValue());
        }
        return out;
    }

    @Test
    @DisplayName("the strictly best score wins whatever the draw is")
    void bestScoreWinsRegardlessOfDraw() {
        List<NodeView> nodes = List.of(node("slow", 0, 0, 1.0), node("fast", 0, 0, 100.0));
        Map<String, Double> s = scores("slow", 5.0, "fast", 1.0);

        for (double draw : new double[] {0.0, 0.25, 0.5, 0.75, 0.999}) {
            assertEquals("fast", Policies.breakTie(nodes, s, draw),
                    "a tie-break must never override a node that scored better");
        }
    }

    @Test
    @DisplayName("a tie is split by the draw, not by list order")
    void tieFollowsTheDrawAndNotListOrder() {
        List<NodeView> nodes = List.of(node("first", 0, 0, 1.0), node("second", 0, 0, 1.0));
        Map<String, Double> s = scores("first", 2.0, "second", 2.0);

        // This is the regression that matters. The old comparator compared one captured
        // scalar against itself, every comparison returned 0, and Stream.min kept whatever
        // it saw first, so "first" won every tie for the life of the run.
        assertEquals("first", Policies.breakTie(nodes, s, 0.10));
        assertEquals("second", Policies.breakTie(nodes, s, 0.90));
    }

    @Test
    @DisplayName("every tied node is reachable, and only the tied ones")
    void drawSweepReachesEveryTiedNodeAndNoOther() {
        List<NodeView> nodes = List.of(
                node("a", 0, 0, 1.0), node("b", 0, 0, 1.0), node("c", 0, 0, 1.0), node("loser", 9, 0, 1.0));
        Map<String, Double> s = scores("a", 1.0, "b", 1.0, "c", 1.0, "loser", 9.0);

        Map<String, Integer> hits = new HashMap<>();
        for (int i = 0; i < 1000; i++) {
            hits.merge(Policies.breakTie(nodes, s, i / 1000.0), 1, Integer::sum);
        }

        assertEquals(3, hits.size(), "exactly the three tied nodes should ever be chosen");
        for (String id : List.of("a", "b", "c")) {
            assertTrue(hits.getOrDefault(id, 0) > 300,
                    id + " should take roughly a third of a uniform sweep, got " + hits.get(id));
        }
    }

    @Test
    @DisplayName("scores within 1e-6 count as tied")
    void floatingPointNoiseDoesNotBreakATie() {
        List<NodeView> nodes = List.of(node("a", 0, 0, 1.0), node("b", 0, 0, 1.0));
        // (pending+1)/capability produces values like this; two nodes that are the same speed
        // to any meaningful precision must not be separated by the last bit of a double.
        Map<String, Double> s = scores("a", 0.01, "b", 0.01 + 1e-9);

        assertEquals("a", Policies.breakTie(nodes, s, 0.10));
        assertEquals("b", Policies.breakTie(nodes, s, 0.90));
    }

    @Test
    @DisplayName("a difference larger than the epsilon is a real difference")
    void differenceBeyondEpsilonIsNotATie() {
        List<NodeView> nodes = List.of(node("a", 0, 0, 1.0), node("b", 0, 0, 1.0));
        Map<String, Double> s = scores("a", 0.01, "b", 0.01 + 1e-3);

        assertEquals("a", Policies.breakTie(nodes, s, 0.99),
                "b is measurably worse, so no draw should reach it");
    }

    @Test
    @DisplayName("a draw of exactly 1.0 stays inside the list")
    void drawAtTheTopOfTheRangeDoesNotOverrun() {
        List<NodeView> nodes = List.of(node("a", 0, 0, 1.0), node("b", 0, 0, 1.0));
        Map<String, Double> s = scores("a", 1.0, "b", 1.0);

        // nextDouble() is [0,1), so 1.0 should never arrive, but the index arithmetic is
        // what stands between that assumption and an IndexOutOfBounds in a sweep.
        assertEquals("b", Policies.breakTie(nodes, s, 1.0));
    }

    @Test
    @DisplayName("a single candidate is returned without consulting the draw")
    void oneNodeIsAlwaysTheAnswer() {
        List<NodeView> nodes = List.of(node("only", 3, 1, 50.0));
        assertEquals("only", Policies.breakTie(nodes, scores("only", 4.0), 0.0));
        assertEquals("only", Policies.breakTie(nodes, scores("only", 4.0), 0.999));
    }
}
