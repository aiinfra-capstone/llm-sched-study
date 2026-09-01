package com.sched.core.interfaces;

import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Random;
import com.sched.v1.DispatchRequest;
import com.sched.core.interfaces.StateStore.NodeView;

public interface Policy {
    record Choice(Optional<String> chosen, Map<String, Double> scores, Double tieBreakDraw) {}
    Choice choose(DispatchRequest request, List<NodeView> admissibleNodes, long nowNs, Random rng);
}