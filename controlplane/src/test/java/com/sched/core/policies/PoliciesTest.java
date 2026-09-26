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
 * F-1: all eight policies selectable from one config value, with no code change between runs.
 * The names are also a C-4 enum, so a typo here is a log that fails schema validation after
 * the run rather than a scheduler that fails to start before it.
 */
class PoliciesTest {

    @Test
    @DisplayName("all eight C-6 names resolve")
    void everyNameResolves() {
        assertInstanceOf(RoundRobin.class, Policies.fromName("round_robin", new AtomicInteger(0), 0.0));
        assertInstanceOf(JSQ.class, Policies.fromName("jsq", new AtomicInteger(0), 0.0));
        assertInstanceOf(JSQFastFirst.class, Policies.fromName("jsq_fastfirst", new AtomicInteger(0), 0.0));
        assertInstanceOf(StaticWeighted.class, Policies.fromName("static_weighted", new AtomicInteger(0), 0.0));
        assertInstanceOf(StaticWeightedWRR.class, Policies.fromName("static_weighted_wrr", new AtomicInteger(0), 0.0));
        assertInstanceOf(WJSQ.class, Policies.fromName("wjsq", new AtomicInteger(0), 0.0));
        assertInstanceOf(Threshold.class, Policies.fromName("threshold", new AtomicInteger(0), 10.0));
        assertInstanceOf(ECT.class, Policies.fromName("ect", new AtomicInteger(0), 0.0,
                java.util.Map.of(), java.util.Map.of(),
                java.util.Map.of("ect_mode", ECT.MODE_KNOWN, "output_len_prior", 16)));
    }

    @Test
    @DisplayName("an unknown name fails at startup and says what the eight are")
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

    @Test
    @DisplayName("ECT takes its mode and prior from the run's config and refuses without them")
    void ectStatesItsModelInTheConfig() {
        // J12, from the config side: the scheduler refuses at startup rather than pricing
        // with a mode and prior the manifest never recorded.
        java.util.Map<String, Object> none = java.util.Map.of();
        for (java.util.Map<String, Object> config : java.util.List.of(
                none, java.util.Map.<String, Object>of("ect_mode", 1))) {
            IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                    () -> Policies.fromName("ect", new AtomicInteger(0), 0.0, java.util.Map.of(),
                            java.util.Map.of(), config), "config " + config);
            assertTrue(e.getMessage().contains("ect_mode"), e.getMessage());
        }
        assertThrows(IllegalArgumentException.class,
                () -> Policies.fromName("ect", new AtomicInteger(0), 0.0), "no config at all");
        assertThrows(IllegalArgumentException.class,
                () -> Policies.fromName("ect", new AtomicInteger(0), 0.0, java.util.Map.of(),
                        java.util.Map.of(), java.util.Map.of("ect_mode", "unknown")),
                "unknown mode with no prior");
        IllegalArgumentException text = assertThrows(IllegalArgumentException.class,
                () -> Policies.fromName("ect", new AtomicInteger(0), 0.0, java.util.Map.of(),
                        java.util.Map.of(), java.util.Map.of("ect_mode", "unknown", "output_len_prior", "44")));
        assertTrue(text.getMessage().contains("must be a number"), text.getMessage());

        // Manifests recorded under the older key names replay as they ran.
        assertInstanceOf(ECT.class, Policies.fromName("ect", new AtomicInteger(0), 0.0,
                java.util.Map.of(), java.util.Map.of(),
                java.util.Map.of("p6_mode", "unknown", "ect_prior_output_len", 44)));
    }
}
