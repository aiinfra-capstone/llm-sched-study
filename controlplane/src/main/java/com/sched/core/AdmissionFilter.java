package com.sched.core;

import java.util.List;
import java.util.stream.Collectors;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.v1.DispatchRequest;

public class AdmissionFilter {
    /**
     * Applies F-14 constraint outside the policy, ensuring no policy is penalized
     * for producing infinite-latency outcomes.
     */
    public List<NodeView> filterAdmissible(List<NodeView> allNodes, DispatchRequest request) {
        return allNodes.stream()
                .filter(NodeView::isAdmissible)
                // In Week 3, we will add the precise logic checking prompt/output lengths
                // against the node's timeout ceiling based on the C-3 Cost Model.
                .collect(Collectors.toList());
    }
}