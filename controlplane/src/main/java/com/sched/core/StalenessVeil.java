package com.sched.core;

import com.sched.core.interfaces.StateStore;
import java.util.List;

public class StalenessVeil implements StateStore {
    private final StateStore realStore;
    private final long stalenessNs;

    public StalenessVeil(StateStore realStore, long stalenessNs) {
        this.realStore = realStore;
        this.stalenessNs = stalenessNs;
    }

    @Override
    public List<NodeView> getAllNodes() {
        // TODO: Later in the project, this will read from a time-ordered snapshot
        // history
        // to return the node states exactly as they were 'stalenessNs' ago.
        // For Week 1 testing, it acts as a transparent pass-through.
        return realStore.getAllNodes();
    }
}