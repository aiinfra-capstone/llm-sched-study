package com.sched.core;

import static com.sched.Fixtures.node;
import static com.sched.Fixtures.snapshot;
import static com.sched.Fixtures.splitCell;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import static org.junit.jupiter.api.Assertions.assertTrue;

import com.sched.core.models.CostModelParser;
import com.sched.core.models.CostModelSnapshot;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
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

    // ------------------------------------------------- the two copies of this definition
    //
    // Capability is computed twice, here and in tools/pool_load.py, and the two have to
    // agree or the pool a policy sees is not the pool the analysis reports. Everything
    // above is synthetic, so the two could drift apart without any of it failing. These
    // pin the numbers the first pair actually ran under, against the snapshots it ran
    // under, which the Python side pins as well.
    //
    // Historical snapshots on purpose: both classes are recalibrated as the study goes on
    // (G2 moved the 1650 Ti on 2026-09-17), and a test that pinned whatever is newest
    // would fail on every recalibration while telling nobody anything. What is fixed here
    // is what those two files say, and that never changes again.

    private static final String FIRST_PAIR_1650TI =
            "008_cm_gtx1650ti_ngl99_p4_q4km_llama32_1b_20260831T153652Z_008.json";
    private static final String FIRST_PAIR_3050 =
            "008_cm_rtx3050_ngl99_p4_q4km_llama32_1b_20260914T200053Z_008.json";

    private static Path costModels() {
        Path here = Path.of("").toAbsolutePath();
        for (Path p = here; p != null; p = p.getParent()) {
            Path candidate = p.resolve("contracts").resolve("cost_models");
            if (Files.isDirectory(candidate)) return candidate;
        }
        throw new IllegalStateException("no contracts/cost_models above " + here);
    }

    private static CostModelSnapshot committed(String nodeClass, String file) throws IOException {
        return CostModelParser.parse(costModels().resolve(nodeClass).resolve(file).toFile());
    }

    @Test
    @DisplayName("the first pair's GTX 1650 Ti capability is 103.9472, as tools/pool_load.py has it")
    void firstPairSlowNodeCapabilityIsPinned() throws IOException {
        CostModelSnapshot snap = committed("gtx1650ti_ngl99_p4_q4km_llama32_1b", FIRST_PAIR_1650TI);

        assertEquals(103.9472, Capability.referenceTokS(snap), 1e-4);
        assertEquals(103.9472, Capability.resolve("gtx1650ti", snap, Map.of()), 1e-4);
        assertTrue(Capability.usesServiceRate(snap),
                "the first pair ran on the service-rate definition, not the decode fallback");
    }

    @Test
    @DisplayName("the first pair's RTX 3050 capability is 163.6071, as tools/pool_load.py has it")
    void firstPairFastNodeCapabilityIsPinned() throws IOException {
        CostModelSnapshot snap = committed("rtx3050_ngl99_p4_q4km_llama32_1b", FIRST_PAIR_3050);

        assertEquals(163.6071, Capability.referenceTokS(snap), 1e-4);
        assertEquals(163.6071, Capability.resolve("rtx3050", snap, Map.of()), 1e-4);
        assertTrue(Capability.usesServiceRate(snap));
    }

    @Test
    @DisplayName("the ratio the first pair's policies were given is 1.5740")
    void firstPairCapabilityRatioIsPinned() throws IOException {
        double slow = Capability.referenceTokS(
                committed("gtx1650ti_ngl99_p4_q4km_llama32_1b", FIRST_PAIR_1650TI));
        double fast = Capability.referenceTokS(
                committed("rtx3050_ngl99_p4_q4km_llama32_1b", FIRST_PAIR_3050));

        // What static_weighted split traffic on and what wjsq divided its queue by. The
        // pool's measured service ratio is about 2.6, so this understating it is a finding
        // (results.md, the capability threat), not a bug to fix here.
        assertEquals(1.5740, fast / slow, 1e-4);
    }

    @Test
    @DisplayName("decode-only capability of the same snapshot is the pre-fix definition, and differs")
    void decodeOnlyModeOnTheRealSnapshotDiffers() throws IOException {
        CostModelSnapshot snap = committed("rtx3050_ngl99_p4_q4km_llama32_1b", FIRST_PAIR_3050);

        double service = Capability.referenceTokS(snap, Capability.MODE_SERVICE);
        double decode = Capability.referenceTokS(snap, Capability.MODE_DECODE);

        assertTrue(decode > service,
                "decode tok/s counts only decode time, so it is the larger number");
        // S3 exists because these two differ on real hardware. If they ever came out equal
        // on a committed snapshot, the sensitivity arm would be measuring nothing.
        assertTrue(decode - service > 1.0, "decode " + decode + " against service " + service);
    }
}