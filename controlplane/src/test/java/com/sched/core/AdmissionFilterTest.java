package com.sched.core;

import static com.sched.Fixtures.node;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.core.models.CostModelSnapshot.Admissibility;
import com.sched.v1.DispatchRequest;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;
import java.util.stream.IntStream;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/**
 * F-13 and F-14: admissibility is decided outside the policy, so that every policy sees the
 * same candidate set and a comparison between them is about routing rather than about which
 * one happened to refuse more work.
 */
class AdmissionFilterTest {

    private static DispatchRequest request(int promptLen, int outputLen) {
        return DispatchRequest.newBuilder()
                .addAllPromptTokenIds(IntStream.range(0, promptLen).boxed().collect(Collectors.toList()))
                .setOutputLen(outputLen)
                .build();
    }

    private static AdmissionFilter filter() {
        return new AdmissionFilter(Map.of(
                "small", new Admissibility(512, 128, 60_000),
                "large", new Admissibility(2048, 256, 60_000)));
    }

    @Test
    @DisplayName("a request inside both bounds is admissible everywhere")
    void withinBoundsPassesEverywhere() {
        List<NodeView> out = filter().filterAdmissible(
                List.of(node("small", 0, 0, 10.0), node("large", 0, 0, 10.0)), request(100, 64));

        assertEquals(2, out.size());
    }

    @Test
    @DisplayName("a prompt past a node's envelope removes that node and only that node")
    void promptBeyondEnvelopeExcludesTheNode() {
        List<NodeView> out = filter().filterAdmissible(
                List.of(node("small", 0, 0, 10.0), node("large", 0, 0, 10.0)), request(1024, 64));

        assertEquals(1, out.size());
        assertEquals("large", out.get(0).nodeId());
    }

    @Test
    @DisplayName("an output length past the envelope excludes the node too")
    void outputBeyondEnvelopeExcludesTheNode() {
        List<NodeView> out = filter().filterAdmissible(
                List.of(node("small", 0, 0, 10.0), node("large", 0, 0, 10.0)), request(100, 200));

        assertEquals(1, out.size());
        assertEquals("large", out.get(0).nodeId());
    }

    @Test
    @DisplayName("the bounds are inclusive")
    void boundsAreInclusive() {
        assertEquals(1, filter().filterAdmissible(List.of(node("small", 0, 0, 10.0)), request(512, 128)).size());
        assertTrue(filter().filterAdmissible(List.of(node("small", 0, 0, 10.0)), request(513, 128)).isEmpty());
    }

    @Test
    @DisplayName("a node with no cost model profile is never admissible")
    void unprofiledNodeIsRefused() {
        // A node we have not calibrated is a node whose service time we would have to invent,
        // so it does not get traffic. This is the check that keeps the simulator honest once
        // the pool grows.
        List<NodeView> out = filter().filterAdmissible(
                List.of(node("stranger", 0, 0, 10.0)), request(10, 10));

        assertTrue(out.isEmpty());
    }

    @Test
    @DisplayName("a node the state store already marked down stays out")
    void unhealthyNodeIsRefused() {
        NodeView down = new NodeView("small", 0, 0, 10.0, 0L, false);
        assertTrue(filter().filterAdmissible(List.of(down), request(10, 10)).isEmpty());
    }

    @Test
    @DisplayName("an empty pool filters to an empty set rather than failing")
    void emptyPoolIsEmpty() {
        assertTrue(filter().filterAdmissible(List.of(), request(10, 10)).isEmpty());
    }
}
