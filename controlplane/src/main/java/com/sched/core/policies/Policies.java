package com.sched.core.policies;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.StateStore.NodeView;

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

    /**
     * Pick uniformly among the nodes that share the best score.
     *
     * A comparator cannot do this. `thenComparing(n -> draw)` compares one captured scalar
     * against itself, every comparison returns 0, and Stream.min keeps whichever element it
     * saw first, so ties resolve on list order and the pool's first node absorbs all of
     * them. That is a real bias whenever scores collapse, which for WJSQ is every idle
     * moment. The draw is the one the decision record already reports as `tie_break_draw`,
     * so the log says exactly which value chose the node.
     */
    static String breakTie(List<NodeView> nodes, Map<String, Double> scores, double draw) {
        double best = Double.POSITIVE_INFINITY;
        for (NodeView n : nodes) {
            double sc = scores.get(n.nodeId());
            if (sc < best) best = sc;
        }
        List<NodeView> tied = new ArrayList<>();
        for (NodeView n : nodes) {
            if (scores.get(n.nodeId()) <= best + 1e-6) tied.add(n);
        }
        int idx = (int) (draw * tied.size());
        if (idx >= tied.size()) idx = tied.size() - 1;
        if (idx < 0) idx = 0;
        return tied.get(idx).nodeId();
    }
}
