package com.sched.core;

import static com.sched.Fixtures.node;
import static com.sched.Fixtures.snapshot;
import static com.sched.Fixtures.splitCell;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import com.sched.core.models.CostModelParser;
import com.sched.core.models.CostModelSnapshot;
import java.io.File;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

class CapabilityTest {

    private static final CostModelSnapshot SPLIT_SNAP = snapshot("split", 0.0, List.of(
            splitCell(1, 128, 1, 64, 1, 1000.0, 200.0, 800.0),
            splitCell(1, 128, 1, 64, 2, 2000.0, 200.0, 1750.0)));

    private static final CostModelSnapshot NO_SPLIT_SNAP = snapshot("flat", 0.0, List.of(
            com.sched.Fixtures.cell(1, 128, 1, 64, 1, 1000.0),
            com.sched.Fixtures.cell(1, 128, 1, 64, 2, 2000.0)));

    @Test
    @DisplayName("reference cell at concurrency 1")
    void referenceCellAtConcurrencyOne() {
        CostModelSnapshot.CostEntry cell = Capability.referenceCell(SPLIT_SNAP, 1);
        assertEquals(1, cell.concurrency());
        assertEquals(1000.0, cell.serviceMsMean(), 1e-9);
    }

    @Test
    @DisplayName("reference cell interpolates concurrency")
    void referenceCellInterpolatesConcurrency() {
        CostModelSnapshot.CostEntry cell = Capability.referenceCell(SPLIT_SNAP, 3);
        assertEquals(2, cell.concurrency());
        assertEquals(2000.0, cell.serviceMsMean(), 1e-9);
    }

    @Test
    @DisplayName("resolve with capability_override")
    void resolveWithOverride() {
        Map<String, Object> config = Map.of(
                "capability_override", Map.of("n1", 50.0));
        double cap = Capability.resolve("n1", SPLIT_SNAP, config);
        assertEquals(50.0, cap, 1e-9);
    }

    @Test
    @DisplayName("resolve with capability_concurrency")
    void resolveWithConcurrency() {
        Map<String, Object> config = Map.of(
                "capability_concurrency", 2);
        double cap = Capability.resolve("n1", SPLIT_SNAP, config);
        assertEquals(100.0 * 1750.0 / 2000.0, cap, 1e-9);
    }

    @Test
    @DisplayName("resolve decode_only mode")
    void resolveDecodeOnlyMode() {
        Map<String, Object> config = Map.of("capability_mode", "decode");
        double cap = Capability.resolve("n1", SPLIT_SNAP, config);
        assertEquals(100.0, cap, 1e-9);
    }

    @Test
    @DisplayName("resolve default service mode")
    void resolveDefaultServiceMode() {
        Map<String, Object> config = Map.of();
        double cap = Capability.resolve("n1", SPLIT_SNAP, config);
        assertEquals(100.0 * 800.0 / 1000.0, cap, 1e-9);
    }

    @Test
    @DisplayName("resolve no snapshot returns zero")
    void resolveNoSnapshot() {
        Map<String, Object> config = Map.of();
        double cap = Capability.resolve("n1", null, config);
        assertEquals(0.0, cap, 1e-9);
    }

    @Test
    @DisplayName("resolve decode mode with null snapshot returns zero")
    void resolveDecodeModeNullSnapshot() {
        Map<String, Object> config = Map.of("capability_mode", "decode");
        double cap = Capability.resolve("n1", null, config);
        assertEquals(0.0, cap, 1e-9);
    }

    @Test
    @DisplayName("resolve capability_override missing node falls through")
    void resolveOverrideMissingNodeFallsThrough() {
        Map<String, Object> config = Map.of(
                "capability_override", Map.of("other_node", 50.0));
        double cap = Capability.resolve("n1", SPLIT_SNAP, config);
        assertEquals(100.0 * 800.0 / 1000.0, cap, 1e-9);
    }

    @Test
    @DisplayName("resolve unknown mode defaults to service")
    void resolveUnknownModeDefaultsToService() {
        Map<String, Object> config = Map.of("capability_mode", "unknown_mode");
        double cap = Capability.resolve("n1", SPLIT_SNAP, config);
        assertEquals(100.0 * 800.0 / 1000.0, cap, 1e-9);
    }

    @Test
    @DisplayName("referenceTokS decode mode")
    void referenceTokSDecodeMode() {
        double cap = Capability.referenceTokS(SPLIT_SNAP, Capability.MODE_DECODE);
        assertEquals(100.0, cap, 1e-9);
    }

    @Test
    @DisplayName("referenceTokS service mode with split")
    void referenceTokSServiceModeWithSplit() {
        double cap = Capability.referenceTokS(SPLIT_SNAP, Capability.MODE_SERVICE);
        assertEquals(100.0 * 800.0 / 1000.0, cap, 1e-9);
    }

    @Test
    @DisplayName("usesServiceRate detects phase split")
    void usesServiceRateDetectsSplit() {
        assertEquals(true, Capability.usesServiceRate(SPLIT_SNAP));
    }

    @Test
    @DisplayName("usesServiceRate no split")
    void usesServiceRateNoSplit() {
        assertEquals(false, Capability.usesServiceRate(NO_SPLIT_SNAP));
    }

    /**
     * Cross-language drift guard: the same two numbers test_pool_load.py pins to on the
     * Python side. A change here without a matching change there means the two copies
     * disagree, and three of the five policies route on a number one of them is wrong about.
     */
    @Test
    @DisplayName("capability pinned to the first pair's _008 snapshots (103.9472 / 163.6070)")
    void capabilityPinnedToFirstPairSnapshots() throws Exception {
        // user.dir is controlplane/; contracts are at the repo root (one level up)
        File repoRoot = new File(System.getProperty("user.dir")).getParentFile();
        File slowFile = new File(repoRoot,
                "contracts/cost_models/gtx1650ti_ngl99_p4_q4km_llama32_1b/"
                + "008_cm_gtx1650ti_ngl99_p4_q4km_llama32_1b_20260831T153652Z_008.json");
        File fastFile = new File(repoRoot,
                "contracts/cost_models/rtx3050_ngl99_p4_q4km_llama32_1b/"
                + "008_cm_rtx3050_ngl99_p4_q4km_llama32_1b_20260914T200053Z_008.json");

        CostModelSnapshot slowSnap = CostModelParser.parse(slowFile);
        CostModelSnapshot fastSnap = CostModelParser.parse(fastFile);

        double slow = Capability.referenceTokS(slowSnap);
        double fast = Capability.referenceTokS(fastSnap);

        assertEquals(1039472L, (long) Math.floor(slow * 1e4),
                "GTX 1650 Ti capability must pin to 103.9472 tok/s (floor*1e4 = 1039472)");
        assertEquals(1636070L, (long) Math.floor(fast * 1e4),
                "RTX 3050 capability must pin to 163.6070 tok/s (floor*1e4 = 1636070)");
    }
}