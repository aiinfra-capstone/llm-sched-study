package com.sched.core;

import com.sched.core.interfaces.StateStore;
import com.sched.core.interfaces.Clock;
import java.util.List;
import java.util.Map;
import java.util.ArrayList;
import java.util.TreeMap;
import java.util.concurrent.ConcurrentHashMap;
import java.util.Comparator;

public class StalenessVeil implements StateStore {
    private final Map<String, TreeMap<Long, NodeView>> hist = new ConcurrentHashMap<>();
    private final long staleNs;
    private final Clock clk;

    public StalenessVeil(long staleNs, Clock clk) {
        this.staleNs = staleNs;
        this.clk = clk;
    }

    public void seed(NodeView v, long atNs) {
        hist.computeIfAbsent(v.nodeId(), k -> new TreeMap<>()).put(atNs, v);
    }

    public void updateNode(NodeView nv) {
        long curr = clk.nowNs();
        hist.computeIfAbsent(nv.nodeId(), k -> new TreeMap<>()).put(curr, nv);
    }

    @Override
    public List<NodeView> getAllNodes() {
        long now = clk.nowNs();
        long target = now - staleNs;
        List<NodeView> views = new ArrayList<>();

        for (TreeMap<Long, NodeView> h : hist.values()) {
            Map.Entry<Long, NodeView> at = h.floorEntry(target);
            if (at == null) at = h.firstEntry();
            if (at == null) continue;
            NodeView v = at.getValue();
            long ageMs = (now - at.getKey()) / 1_000_000L;
            views.add(new NodeView(v.nodeId(), v.queueDepth(), v.inflight(),
                                   v.capabilityTokS(), ageMs, v.isAdmissible()));
        }
        views.sort(Comparator.comparing(NodeView::nodeId));
        return views;
    }
}