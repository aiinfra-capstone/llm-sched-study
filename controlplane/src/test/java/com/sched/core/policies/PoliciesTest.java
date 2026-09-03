package com.sched.core.policies;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.sched.core.interfaces.Policy;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/**
 * F-1: all five policies selectable from one config value, with no code change between runs.
 * The names are also a C-4 enum, so a typo here is a log that fails schema validation after
 * the run rather than a scheduler that fails to start before it.
 */
class PoliciesTest {

    @Test
    @DisplayName("all five C-6 names resolve")
    void everyNameResolves() {
        assertInstanceOf(RoundRobin.class, Policies.fromName("round_robin", new AtomicInteger(0), 0.0));
        assertInstanceOf(JSQ.class, Policies.fromName("jsq", new AtomicInteger(0), 0.0));
        assertInstanceOf(StaticWeighted.class, Policies.fromName("static_weighted", new AtomicInteger(0), 0.0));
        assertInstanceOf(WJSQ.class, Policies.fromName("wjsq", new AtomicInteger(0), 0.0));
        assertInstanceOf(Threshold.class, Policies.fromName("threshold", new AtomicInteger(0), 10.0));
    }

    @Test
    @DisplayName("an unknown name fails at startup and says what the five are")
    void unknownNameIsRejected() {
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> Policies.fromName("least_loaded", new AtomicInteger(0), 0.0));

        assertTrue(e.getMessage().contains("least_loaded"), "the message should name the offending value");
        assertTrue(e.getMessage().contains("round_robin"), "and list what is allowed");
    }

    @Test
    @DisplayName("the empty string is not a policy")
    void emptyNameIsRejected() {
        assertThrows(IllegalArgumentException.class,
                () -> Policies.fromName("", new AtomicInteger(0), 0.0));
    }

    @Test
    @DisplayName("the threshold value reaches the policy")
    void thresholdCutoffIsWired() {
        // A Threshold built with the wrong T is a policy that silently measures something
        // other than the arm it is named after, which no log would reveal.
        Policy strict = Policies.fromName("threshold", new AtomicInteger(0), 1000.0);
        Policy loose = Policies.fromName("threshold", new AtomicInteger(0), 1.0);

        var nodes = java.util.List.of(com.sched.Fixtures.node("n", 0, 0, 50.0));
        var req = com.sched.v1.DispatchRequest.getDefaultInstance();

        assertEquals(0.0, strict.choose(req, nodes, 0L, new java.util.Random(1)).scores().get("n"), 1e-9);
        assertEquals(1.0, loose.choose(req, nodes, 0L, new java.util.Random(1)).scores().get("n"), 1e-9);
    }
}
