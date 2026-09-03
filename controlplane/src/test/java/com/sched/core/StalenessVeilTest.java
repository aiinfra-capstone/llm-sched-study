package com.sched.core;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.sched.core.interfaces.Clock;
import com.sched.core.interfaces.StateStore.NodeView;
import java.util.List;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/**
 * F-8. The veil is the whole of H3: it serves the scheduler a node's state as it was s
 * seconds ago, so that "the estimate was stale" becomes a controlled variable rather than an
 * accident of timing.
 */
class StalenessVeilTest {

    /** A clock the test moves by hand, because H3 is entirely about when things are read. */
    private static final class ManualClock implements Clock {
        private long now;

        @Override
        public long nowNs() {
            return now;
        }

        void advanceMs(long ms) {
            now += ms * 1_000_000L;
        }
    }

    private static NodeView view(String id, int queueDepth) {
        return new NodeView(id, queueDepth, 0, 100.0, 0L, true);
    }

    @Test
    @DisplayName("with no staleness the newest state is served")
    void zeroStalenessServesTheLatest() {
        ManualClock clock = new ManualClock();
        StalenessVeil veil = new StalenessVeil(0L, clock);

        veil.updateNode(view("n", 1));
        clock.advanceMs(100);
        veil.updateNode(view("n", 7));

        assertEquals(7, veil.getAllNodes().get(0).queueDepth());
    }

    @Test
    @DisplayName("a veil of s seconds serves the state from s seconds ago")
    void veilServesTheOlderState() {
        ManualClock clock = new ManualClock();
        StalenessVeil veil = new StalenessVeil(1_000_000_000L, clock);

        veil.updateNode(view("n", 1));
        clock.advanceMs(2000);
        veil.updateNode(view("n", 9));

        // The 9 landed just now, so a one second veil should still be showing the 1.
        assertEquals(1, veil.getAllNodes().get(0).queueDepth());
    }

    @Test
    @DisplayName("once the veil passes, the newer state becomes visible")
    void newerStateAppearsAfterTheVeilElapses() {
        ManualClock clock = new ManualClock();
        StalenessVeil veil = new StalenessVeil(1_000_000_000L, clock);

        veil.updateNode(view("n", 1));
        clock.advanceMs(2000);
        veil.updateNode(view("n", 9));
        clock.advanceMs(1500);

        assertEquals(9, veil.getAllNodes().get(0).queueDepth());
    }

    @Test
    @DisplayName("the reported age is how old the served estimate actually is")
    void ageReflectsTheServedEstimate() {
        // C-4 records estimate_age_ms per candidate, and without it H3 is unanalysable: you
        // cannot tell a bad policy from a stale one.
        ManualClock clock = new ManualClock();
        StalenessVeil veil = new StalenessVeil(1_000_000_000L, clock);

        veil.updateNode(view("n", 1));
        clock.advanceMs(2500);

        assertEquals(2500L, veil.getAllNodes().get(0).estimateAgeMs());
    }

    @Test
    @DisplayName("before any history exists the earliest seed is served rather than nothing")
    void fallsBackToTheEarliestEntry() {
        // At the start of a run the veil is looking further back than the run is old. Serving
        // the seed keeps the pool dispatchable instead of stalling the first s seconds.
        ManualClock clock = new ManualClock();
        StalenessVeil veil = new StalenessVeil(10_000_000_000L, clock);

        veil.seed(view("n", 3), 0L);
        clock.advanceMs(100);

        List<NodeView> nodes = veil.getAllNodes();
        assertEquals(1, nodes.size());
        assertEquals(3, nodes.get(0).queueDepth());
    }

    @Test
    @DisplayName("nodes come back in a stable order")
    void orderIsDeterministic() {
        // Policies tie-break on a draw indexed into this list, so its order is part of F-20.
        ManualClock clock = new ManualClock();
        StalenessVeil veil = new StalenessVeil(0L, clock);

        veil.updateNode(view("zulu", 1));
        veil.updateNode(view("alpha", 1));
        veil.updateNode(view("mike", 1));

        assertEquals(List.of("alpha", "mike", "zulu"),
                veil.getAllNodes().stream().map(NodeView::nodeId).toList());
    }

    @Test
    @DisplayName("an empty veil reports an empty pool")
    void emptyVeilIsEmpty() {
        assertTrue(new StalenessVeil(0L, new ManualClock()).getAllNodes().isEmpty());
    }

    @Test
    @DisplayName("the in-memory store keeps only the newest state and sorts it")
    void stateStoreKeepsLatestPerNode() {
        InMemoryStateStore store = new InMemoryStateStore();
        store.updateNode(view("b", 1));
        store.updateNode(view("a", 1));
        store.updateNode(view("b", 5));

        List<NodeView> all = store.getAllNodes();
        assertEquals(List.of("a", "b"), all.stream().map(NodeView::nodeId).toList());
        assertEquals(5, all.get(1).queueDepth());
    }
}
