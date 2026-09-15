package com.sched.core.policies;

import static com.sched.Fixtures.node;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.v1.DispatchRequest;
import java.util.List;
import java.util.Random;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/**
 * static_weighted_wrr is StaticWeighted without the random stream: smooth
 * weighted round-robin, so its long-run share is the policy's share and not a
 * property of one random draw sequence.
 */
class StaticWeightedWRRTest {
    private static final DispatchRequest ANY = DispatchRequest.getDefaultInstance();

    @Test
    @DisplayName("traffic splits in proportion to capability, deterministically")
    void shareIsProportional() {
        StaticWeightedWRR policy = new StaticWeightedWRR(new AtomicInteger(0));
        List<NodeView> nodes = List.of(node("slow", 0, 0, 10.0), node("fast", 0, 0, 90.0));

        int fast = 0;
        for (int i = 0; i < 1000; i++) {
            if ("fast".equals(policy.choose(ANY, nodes, 0L, new Random(1)).chosen().orElseThrow())) fast++;
        }
        // Exactly 90% over whole smoothing periods, not roughly 90% of one stream.
        assertEquals(900, fast);
    }

    @Test
    @DisplayName("two instances from the same start produce the same sequence")
    void sequenceIsReproducible() {
        List<NodeView> nodes = List.of(node("slow", 0, 0, 10.0), node("fast", 0, 0, 90.0));
        StaticWeightedWRR first = new StaticWeightedWRR(new AtomicInteger(0));
        StaticWeightedWRR second = new StaticWeightedWRR(new AtomicInteger(0));

        for (int i = 0; i < 50; i++) {
            String a = first.choose(ANY, nodes, 0L, new Random(999)).chosen().orElseThrow();
            String b = second.choose(ANY, nodes, 0L, new Random(1)).chosen().orElseThrow();
            assertEquals(a, b, "the draw must not enter the sequence");
        }
    }

    @Test
    @DisplayName("the score is capability, queue ignored")
    void scoreIsCapability() {
        StaticWeightedWRR policy = new StaticWeightedWRR(new AtomicInteger(0));
        Policy.Choice choice = policy.choose(
                ANY, List.of(node("a", 500, 500, 42.0)), 0L, new Random(1));

        assertEquals(42.0, choice.scores().get("a"), 1e-9);
    }

    @Test
    @DisplayName("a deterministic choice reports no draw")
    void noDrawIsReported() {
        StaticWeightedWRR policy = new StaticWeightedWRR(new AtomicInteger(0));
        List<NodeView> nodes = List.of(node("slow", 0, 0, 10.0), node("fast", 0, 0, 90.0));
        assertNull(policy.choose(ANY, nodes, 0L, new Random(1)).tieBreakDraw());
    }

    @Test
    @DisplayName("a pool that reports no capability at all still dispatches")
    void fallsBackWhenAllCapabilitiesAreZero() {
        StaticWeightedWRR policy = new StaticWeightedWRR(new AtomicInteger(0));
        List<NodeView> nodes = List.of(node("a", 0, 0, 0.0), node("b", 0, 0, 0.0));

        Policy.Choice choice = policy.choose(ANY, nodes, 0L, new Random(1));
        assertTrue(choice.chosen().isPresent(), "a pool of cold heartbeats must not stall dispatch");
    }

    @Test
    @DisplayName("an empty admissible set chooses nothing")
    void emptyAdmissibleSetYieldsNoChoice() {
        assertFalse(new StaticWeightedWRR(new AtomicInteger(0))
                .choose(ANY, List.of(), 0L, new Random(1)).chosen().isPresent());
    }
}
