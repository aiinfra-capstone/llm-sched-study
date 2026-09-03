package com.sched.core.policies;

import static com.sched.Fixtures.node;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
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
 * The two policies that complete H1's 2x2: RoundRobin knows neither queue nor capability,
 * StaticWeighted knows capability but not queue.
 */
class RoundRobinAndStaticWeightedTest {
    private static final DispatchRequest ANY = DispatchRequest.getDefaultInstance();

    @Test
    @DisplayName("round robin cycles in list order and ignores both queue and capability")
    void roundRobinCycles() {
        RoundRobin policy = new RoundRobin(new AtomicInteger(0));
        List<NodeView> nodes = List.of(node("a", 99, 99, 1.0), node("b", 0, 0, 1000.0));

        assertEquals("a", policy.choose(ANY, nodes, 0L, new Random(1)).chosen().orElseThrow());
        assertEquals("b", policy.choose(ANY, nodes, 0L, new Random(1)).chosen().orElseThrow());
        assertEquals("a", policy.choose(ANY, nodes, 0L, new Random(1)).chosen().orElseThrow());
    }

    @Test
    @DisplayName("round robin marks the node it picked and no other")
    void roundRobinScoresIdentifyTheChoice() {
        RoundRobin policy = new RoundRobin(new AtomicInteger(0));
        List<NodeView> nodes = List.of(node("a", 0, 0, 1.0), node("b", 0, 0, 1.0));

        Policy.Choice choice = policy.choose(ANY, nodes, 0L, new Random(1));
        assertEquals(1.0, choice.scores().get("a"), 1e-9);
        assertEquals(0.0, choice.scores().get("b"), 1e-9);
    }

    @Test
    @DisplayName("round robin survives the counter passing Integer.MAX_VALUE")
    void roundRobinHandlesCounterOverflow() {
        // getAndIncrement wraps to Integer.MIN_VALUE, and Math.abs(Integer.MIN_VALUE) is
        // still negative. A run long enough to reach it should not die on a negative index.
        RoundRobin policy = new RoundRobin(new AtomicInteger(Integer.MAX_VALUE));
        List<NodeView> nodes = List.of(node("a", 0, 0, 1.0), node("b", 0, 0, 1.0));

        policy.choose(ANY, nodes, 0L, new Random(1));
        Policy.Choice afterWrap = policy.choose(ANY, nodes, 0L, new Random(1));
        assertTrue(afterWrap.chosen().isPresent(), "the counter wrapping must not stop dispatch");
    }

    @Test
    @DisplayName("static weighted splits traffic in proportion to capability")
    void staticWeightedIsProportional() {
        StaticWeighted policy = new StaticWeighted();
        List<NodeView> nodes = List.of(node("slow", 0, 0, 10.0), node("fast", 0, 0, 90.0));
        Random rng = new Random(3);

        int fast = 0;
        for (int i = 0; i < 2000; i++) {
            if ("fast".equals(policy.choose(ANY, nodes, 0L, rng).chosen().orElseThrow())) fast++;
        }
        // 90 of 100 tok/s, so about 90% of the traffic.
        assertTrue(fast > 1700 && fast < 1900, "expected roughly 1800 of 2000 to the fast node, got " + fast);
    }

    @Test
    @DisplayName("static weighted ignores queue depth entirely")
    void staticWeightedIgnoresQueueDepth() {
        StaticWeighted policy = new StaticWeighted();
        Policy.Choice choice = policy.choose(
                ANY, List.of(node("a", 500, 500, 42.0)), 0L, new Random(1));

        assertEquals(42.0, choice.scores().get("a"), 1e-9, "the score is capability, nothing else");
    }

    @Test
    @DisplayName("a pool that reports no capability at all still dispatches")
    void staticWeightedFallsBackWhenAllCapabilitiesAreZero() {
        StaticWeighted policy = new StaticWeighted();
        List<NodeView> nodes = List.of(node("a", 0, 0, 0.0), node("b", 0, 0, 0.0));

        Policy.Choice choice = policy.choose(ANY, nodes, 0L, new Random(1));
        assertTrue(choice.chosen().isPresent(), "a pool of cold heartbeats must not stall dispatch");
    }

    @Test
    @DisplayName("both policies decline an empty admissible set")
    void emptyAdmissibleSetYieldsNoChoice() {
        assertFalse(new RoundRobin(new AtomicInteger(0))
                .choose(ANY, List.of(), 0L, new Random(1)).chosen().isPresent());
        assertFalse(new StaticWeighted()
                .choose(ANY, List.of(), 0L, new Random(1)).chosen().isPresent());
    }
}
