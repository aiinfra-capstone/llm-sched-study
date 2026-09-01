package com.sched.core;

import com.sched.core.interfaces.StateStore;
import java.util.List;
import java.util.Map;
import java.util.ArrayList;
import java.util.concurrent.ConcurrentHashMap;
import java.util.Comparator;

public class InMemoryStateStore implements StateStore {
    private final Map<String, NodeView> nodes = new ConcurrentHashMap<>();

    public void updateNode(NodeView view) {
        nodes.put(view.nodeId(), view);
    }

    @Override
    public List<NodeView> getAllNodes() {
        List<NodeView> list = new ArrayList<>(nodes.values());
        list.sort(Comparator.comparing(NodeView::nodeId));
        return list;
    }
}