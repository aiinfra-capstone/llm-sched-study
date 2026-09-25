package com.sched.core;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;

import com.sched.core.interfaces.StateStore.NodeView;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/**
 * A req_id holds at most one slot in the pool. A second admit of the same req_id, to the
 * node it is on or to any other, counts nowhere and returns null so the caller can tell.
 */
class InMemoryStateStoreTest {

    private static InMemoryStateStore twoNodes() {
        InMemoryStateStore store = new InMemoryStateStore();
        store.updateNode(new NodeView("n1", 0, 0, 100.0, 0L, true));
        store.updateNode(new NodeView("n2", 0, 0, 100.0, 0L, true));
        return store;
    }

    private static void assertCounts(InMemoryStateStore store, String node, int inflight, int queued,
            String when) {
        NodeView n = store.getNode(node);
        assertEquals(inflight, n.inflight(), node + " inflight " + when);
        assertEquals(queued, n.queueDepth(), node + " queue depth " + when);
    }

    @Test
    @DisplayName("admitting a req_id again to the same node counts nothing and returns null")
    void aDuplicateAdmitToTheSameNodeIsNotCounted() {
        InMemoryStateStore store = twoNodes();

        assertNotNull(store.admit("n1", "r-1", 1));
        assertCounts(store, "n1", 1, 0, "after the first admit");

        assertNull(store.admit("n1", "r-1", 1), "a duplicate admit is reported as not counted");
        assertCounts(store, "n1", 1, 0, "after the duplicate");
        assertCounts(store, "n2", 0, 0, "after the duplicate");
    }

    @Test
    @DisplayName("admitting a req_id again to a second node counts nothing on either")
    void aDuplicateAdmitToAnotherNodeIsNotCounted() {
        InMemoryStateStore store = twoNodes();

        assertNotNull(store.admit("n1", "r-1", 1));
        assertNull(store.admit("n2", "r-1", 1), "the req_id already holds a slot on n1");
        assertCounts(store, "n1", 1, 0, "after the duplicate on n2");
        assertCounts(store, "n2", 0, 0, "after the duplicate on n2");

        // The slot stays where it was first admitted: completing it on n2 frees nothing,
        // completing it on n1 frees n1's.
        assertNull(store.complete("n2", "r-1", 1));
        assertCounts(store, "n2", 0, 0, "after a completion on the wrong node");
        assertNotNull(store.complete("n1", "r-1", 1));
        assertCounts(store, "n1", 0, 0, "after the completion on the node that held it");
    }

    @Test
    @DisplayName("a released req_id can be admitted again")
    void aReleasedReqIdIsAdmittedAgain() {
        InMemoryStateStore store = twoNodes();

        assertNotNull(store.admit("n1", "r-1", 1));
        assertNotNull(store.complete("n1", "r-1", 1));
        assertNotNull(store.admit("n2", "r-1", 1), "a req_id that holds no slot is admitted");
        assertCounts(store, "n1", 0, 0, "after the re-admit elsewhere");
        assertCounts(store, "n2", 1, 0, "after the re-admit elsewhere");
    }
}
