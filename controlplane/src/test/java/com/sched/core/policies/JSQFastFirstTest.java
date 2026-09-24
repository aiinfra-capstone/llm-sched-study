package com.sched.core.policies;

import static com.sched.Fixtures.node;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;

import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.v1.DispatchRequest;
import java.util.ArrayList;
import java.util.List;
import java.util.Random;
import java.util.stream.Collectors;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/**
 * jsq_fastfirst is the ordinal-bit control for H1: the same scores as JSQ, with
 * ties broken deterministically toward the highest-capability node instead of
 * at random. Any remaining WJSQ - jsq_fastfirst gap is calibration magnitude,
 * not one bit anyone could guess.
 */
class JSQFastFirstTest {
    private static final DispatchRequest ANY = DispatchRequest.getDefaultInstance();
    private final JSQFastFirst policy = new JSQFastFirst();

    @Test
    @DisplayName("the shortest queue wins, as in JSQ")
    void shortestQueueWins() {
        List<NodeView> nodes = List.of(node("busy", 5, 2, 100.0), node("quiet", 0, 1, 1.0));
        assertEquals("quiet", policy.choose(ANY, nodes, 0L, new Random(1)).chosen().orElseThrow());
    }

    @Test
    @DisplayName("a tied decision goes to the highest-capability node")
    void tiesGoToTheFastNode() {
        List<NodeView> nodes = List.of(node("slow", 2, 0, 1.0), node("fast", 2, 0, 1000.0));
        assertEquals("fast", policy.choose(ANY, nodes, 0L, new Random(1)).chosen().orElseThrow());
    }

    @Test
    @DisplayName("tie-breaking does not consult the draw")
    void tiesIgnoreTheRandomDraw() {
        // JSQ would split these by the draw; this arm must not, however the
        // draw falls. Two draws at opposite ends of [0, 1) must agree.
        List<NodeView> nodes = List.of(node("slow", 1, 0, 10.0), node("fast", 1, 0, 90.0));
        String low = policy.choose(ANY, nodes, 0L,
                new com.sched.Fixtures.FixedRandom(0.01)).chosen().orElseThrow();
        String high = policy.choose(ANY, nodes, 0L,
                new com.sched.Fixtures.FixedRandom(0.99)).chosen().orElseThrow();
        assertEquals("fast", low);
        assertEquals("fast", high);
    }

    @Test
    @DisplayName("scores are queue depth plus inflight, capability excluded")
    void scoreIsQueueDepthPlusInflight() {
        Policy.Choice choice = policy.choose(ANY, List.of(node("n", 3, 4, 10.0)), 0L, new Random(1));
        assertEquals(7.0, choice.scores().get("n"), 1e-9);
    }

    @Test
    @DisplayName("a deterministic choice reports no draw")
    void noDrawIsReported() {
        List<NodeView> nodes = List.of(node("slow", 1, 0, 10.0), node("fast", 1, 0, 90.0));
        assertNull(policy.choose(ANY, nodes, 0L, new Random(1)).tieBreakDraw());
    }

    @Test
    @DisplayName("an empty admissible set chooses nothing")
    void emptyAdmissibleSetYieldsNoChoice() {
        Policy.Choice choice = policy.choose(ANY, List.of(), 0L, new Random(1));
        assertFalse(choice.chosen().isPresent());
    }

    // The point of this arm is that it removes the random draw from a tie, so what is left
    // must not depend on anything else that varies between runs. The state store hands the
    // policy its nodes in whatever order it happens to hold them, and the tests above are
    // all written with the fast node in a convenient position, so they would pass on an
    // implementation that simply took the first tied node.

    @Test
    @DisplayName("the fast node wins a tie from any position in the candidate list")
    void tieResolutionIgnoresTheOrderTheStoreListsNodesIn() {
        NodeView fast = node("fast", 1, 1, 163.6);
        NodeView middle = node("middle", 1, 1, 120.0);
        NodeView slow = node("slow", 1, 1, 104.0);

        for (List<NodeView> order : permutations(List.of(fast, middle, slow))) {
            Policy.Choice choice = policy.choose(ANY, order, 0L, new Random(1));
            assertEquals("fast", choice.chosen().orElseThrow(),
                    "order " + ids(order) + " should not change a tie-break");
        }
    }

    @Test
    @DisplayName("equal capabilities fall back to the smallest node id, from any order")
    void equalCapabilitiesResolveByNodeIdNotByPosition() {
        NodeView a = node("alpha", 0, 2, 100.0);
        NodeView b = node("bravo", 2, 0, 100.0);
        NodeView c = node("charlie", 1, 1, 100.0);

        for (List<NodeView> order : permutations(List.of(a, b, c))) {
            Policy.Choice choice = policy.choose(ANY, order, 0L, new Random(1));
            assertEquals("alpha", choice.chosen().orElseThrow(),
                    "order " + ids(order) + " should not change which of three equals wins");
        }
    }

    private static List<List<NodeView>> permutations(List<NodeView> nodes) {
        if (nodes.size() <= 1) return List.of(nodes);
        List<List<NodeView>> out = new ArrayList<>();
        for (int i = 0; i < nodes.size(); i++) {
            List<NodeView> rest = new ArrayList<>(nodes);
            NodeView head = rest.remove(i);
            for (List<NodeView> tail : permutations(rest)) {
                List<NodeView> one = new ArrayList<>();
                one.add(head);
                one.addAll(tail);
                out.add(one);
            }
        }
        return out;
    }

    private static String ids(List<NodeView> nodes) {
        return nodes.stream().map(NodeView::nodeId).collect(Collectors.joining(","));
    }
}
