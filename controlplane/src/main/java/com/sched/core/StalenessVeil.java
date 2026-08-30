package com.sched.core;

import com.sched.core.interfaces.StateStore;
import com.sched.core.interfaces.Clock;
import java.util.List;
import java.util.Map;
import java.util.ArrayList;
import java.util.TreeMap;
import java.util.concurrent.ConcurrentHashMap;

public class StalenessVeil implements StateStore {
    private final Map<String, TreeMap<Long, NodeView>> hist = new ConcurrentHashMap<>();
    private final long staleNs;
    private final Clock clk;

    public StalenessVeil(long staleNs, Clock clk) {
        this.staleNs = staleNs;
        this.clk = clk;
    }

    public void updateNode(NodeView nv) {
        long curr = clk.nowNs();
        hist.computeIfAbsent(nv.nodeId(), k -> new TreeMap<>()).put(curr, nv);
    }

    @Override
    public List<NodeView> getAllNodes() {
        long tgt = clk.nowNs() - staleNs;
        List<NodeView> views = new ArrayList<>();

        for (TreeMap<Long, NodeView> h : hist.values()) {
            Map.Entry<Long, NodeView> e = h.floorEntry(tgt);
            if (e != null) {
                views.add(e.getValue());
            }
        }
        return views;
    }
}