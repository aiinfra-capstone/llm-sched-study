package com.sched.core;

import com.sched.core.interfaces.StateStore;
import com.sched.core.interfaces.Clock;
import java.util.List;
import java.util.Map;
import java.util.ArrayList;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentSkipListMap;
import java.util.Comparator;

public class StalenessVeil implements StateStore {
    // Both maps must be safe for concurrent structural modification. The outer
    // Map is a ConcurrentHashMap; the inner was a plain TreeMap, and a writer
    // doing a put while a reader walks floorEntry can leave the red-black tree
    // in an inconsistent state. ConcurrentSkipListMap gives the same floorEntry
    // semantics under concurrent put, so heartbeat writers and dispatch readers
    // no longer race.
    private final Map<String, ConcurrentSkipListMap<Long, NodeView>> hist = new ConcurrentHashMap<>();
    private final long staleNs;
    private final Clock clk;

    public StalenessVeil(long staleNs, Clock clk) {
        this.staleNs = staleNs;
        this.clk = clk;
    }

    public void seed(NodeView v, long atNs) {
        hist.computeIfAbsent(v.nodeId(), k -> new ConcurrentSkipListMap<>()).put(atNs, v);
    }

    public void updateNode(NodeView nv) {
        long curr = clk.nowNs();
        ConcurrentSkipListMap<Long, NodeView> h =
            hist.computeIfAbsent(nv.nodeId(), k -> new ConcurrentSkipListMap<>());
        h.put(curr, nv);

        // getAllNodes serves floorEntry(now - staleNs), and both clocks in use here only
        // move forward, so once an entry is older than the entry that horizon currently
        // lands on it can never be served again. Keeping that one and dropping what is
        // strictly older bounds the history at the staleness window instead of letting it
        // grow for the length of the run.
        Long horizon = h.floorKey(curr - staleNs);
        if (horizon != null) {
            h.headMap(horizon, false).clear();
        }
    }

    @Override
    public List<NodeView> getAllNodes() {
        long now = clk.nowNs();
        long target = now - staleNs;
        List<NodeView> views = new ArrayList<>();

        for (ConcurrentSkipListMap<Long, NodeView> h : hist.values()) {
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