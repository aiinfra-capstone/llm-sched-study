package com.sched.core.policies;

import java.util.concurrent.atomic.AtomicInteger;
import com.sched.core.interfaces.Policy;

public final class Policies {
    /** F-1: all five selectable from one config value, no code change between runs. */
    public static Policy fromName(String name, AtomicInteger rrCounter, double thresholdT) {
        return switch (name) {
            case "round_robin"     -> new RoundRobin(rrCounter);
            case "jsq"             -> new JSQ();
            case "static_weighted" -> new StaticWeighted();
            case "wjsq"            -> new WJSQ();
            case "threshold"       -> new Threshold(thresholdT, rrCounter);
            default -> throw new IllegalArgumentException(
                "policy '" + name + "' is not one of the five C-6 names: round_robin, "
                + "jsq, static_weighted, wjsq, threshold");
        };
    }
}
