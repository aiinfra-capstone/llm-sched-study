package com.sched.core.policies;

import static com.sched.Fixtures.node;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.v1.DispatchRequest;
import java.util.HashSet;
import java.util.List;
import java.util.Random;
import java.util.Set;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/** Threshold(T) is H2's limit policy: one bit of calibration and nothing else. */
class ThresholdTest {
    private static final DispatchRequest ANY = DispatchRequest.getDefaultInstance();

    private static Threshold at(double t) {
        return new Threshold(t, new AtomicInteger(0));
    }

    @Test
    @DisplayName("only nodes at or above T are used")
    void onlyStrongNodesAreChosen() {
        List<NodeView> nodes = List.of(node("weak", 0, 0, 5.0), node("strong", 9, 9, 50.0));
        Policy.Choice choice = at(10.0).choose(ANY, nodes, 0L, new Random(1));

        assertEquals("strong", choice.chosen().orElseThrow(),
                "queue state is irrelevant to Threshold; only the capability bit counts");
    }

    @Test
    @DisplayName("a node exactly at T counts as strong")
    void thresholdIsInclusive() {
        List<NodeView> nodes = List.of(node("edge", 0, 0, 10.0));
        assertEquals("edge", at(10.0).choose(ANY, nodes, 0L, new Random(1)).chosen().orElseThrow());
    }

    @Test
    @DisplayName("with no node above T it serves on the fastest one instead of dropping")
    void fallsBackToFastestAdmissibleNode() {
        // The bug this pins: returning Optional.empty() dropped the request. Every node here
        // has already passed F-13 admissibility, so refusing to serve is a scheduling
        // decision wearing an admission decision's clothes, and it would have made this arm
        // report perfect tail latency at zero throughput whenever T sat above the pool.
        List<NodeView> nodes = List.of(node("slow", 0, 0, 3.0), node("less-slow", 0, 0, 7.0));
        Policy.Choice choice = at(100.0).choose(ANY, nodes, 0L, new Random(1));

        assertTrue(choice.chosen().isPresent(), "a servable request must not be dropped");
        assertEquals("less-slow", choice.chosen().orElseThrow());
    }

    @Test
    @DisplayName("the fallback still scores every candidate")
    void fallbackReportsScores() {
        List<NodeView> nodes = List.of(node("a", 0, 0, 3.0), node("b", 0, 0, 7.0));
        Policy.Choice choice = at(100.0).choose(ANY, nodes, 0L, new Random(1));

        assertEquals(2, choice.scores().size());
        assertEquals(0.0, choice.scores().get("a"), 1e-9, "nothing cleared the cutoff");
        assertEquals(0.0, choice.scores().get("b"), 1e-9);
    }

    @Test
    @DisplayName("scores are the one-bit calibration, 1.0 strong and 0.0 weak")
    void scoresAreTheCapabilityBit() {
        List<NodeView> nodes = List.of(node("weak", 0, 0, 5.0), node("strong", 0, 0, 50.0));
        Policy.Choice choice = at(10.0).choose(ANY, nodes, 0L, new Random(1));

        assertEquals(0.0, choice.scores().get("weak"), 1e-9);
        assertEquals(1.0, choice.scores().get("strong"), 1e-9);
    }

    @Test
    @DisplayName("several strong nodes are used in rotation")
    void strongNodesAreRotated() {
        List<NodeView> nodes = List.of(node("s1", 0, 0, 50.0), node("s2", 0, 0, 50.0));
        Threshold policy = at(10.0);

        Set<String> seen = new HashSet<>();
        for (int i = 0; i < 4; i++) {
            seen.add(policy.choose(ANY, nodes, 0L, new Random(1)).chosen().orElseThrow());
        }
        assertEquals(Set.of("s1", "s2"), seen, "a single strong node must not absorb everything");
    }

    @Test
    @DisplayName("an empty admissible set chooses nothing")
    void emptyAdmissibleSetYieldsNoChoice() {
        Policy.Choice choice = at(10.0).choose(ANY, List.of(), 0L, new Random(1));

        // This is the one refusal that stays. There is genuinely nothing to serve on.
        assertFalse(choice.chosen().isPresent());
        assertTrue(choice.scores().isEmpty());
    }
}
