package com.sched.core;

import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.v1.DispatchRequest;
import com.sched.core.models.CostModelSnapshot.Admissibility;

public class AdmissionFilter {

    // Maps a Node ID to its specific hardware admissibility bounds loaded from C-3
    private final Map<String, Admissibility> nodeAdmissibilityMap;

    public AdmissionFilter(Map<String, Admissibility> nodeAdmissibilityMap) {
        this.nodeAdmissibilityMap = nodeAdmissibilityMap;
    }

    /**
     * Applies F-14 and F-13 constraints outside the policy.
     */
    public List<NodeView> filterAdmissible(List<NodeView> allNodes, DispatchRequest request) {
        int promptLen = request.getPromptTokenIdsCount();
        int outputLen = request.getOutputLen();

        return allNodes.stream()
                .filter(NodeView::isAdmissible) // General health check from the StateStore
                .filter(node -> {
                    Admissibility bounds = nodeAdmissibilityMap.get(node.nodeId());
                    // If we have no hardware profile for this node, it is not admissible
                    if (bounds == null)
                        return false;

                    // Enforce the C-3 cost model limits
                    return promptLen <= bounds.maxPrompt() &&
                            outputLen <= bounds.maxOutput();
                })
                .collect(Collectors.toList());
    }
}