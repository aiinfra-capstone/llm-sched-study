package com.sched.core.models;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.sched.core.models.CostModelSnapshot.CostEntry;
import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * The far side of the seam, read against the committed artifacts rather than against a
 * hand-built string.
 *
 * C-3 and C-6 are the only two things the data plane hands the control plane, and the failure
 * mode they are frozen against is silent: a field the record does not declare parses to null
 * forever and whatever reads it quietly does the wrong thing. contracts/check.py catches a
 * name mismatch; these catch what the values mean once parsed.
 */
class ContractParsingTest {

    private static final Path CONTRACTS = contractsDir();
    private static final Path EXAMPLES = CONTRACTS.resolve("examples");
    private static final Path COST_MODELS = CONTRACTS.resolve("cost_models");

    /**
     * Walk up from the working directory to find contracts/.
     *
     * Surefire runs with the module as the working directory, so "../contracts" is right
     * today. Hard-coding that makes the test fail confusingly the first time someone runs it
     * from the repo root or from an IDE, and a contract test that is annoying to run is a
     * contract test that stops being run.
     */
    private static Path contractsDir() {
        Path here = Path.of("").toAbsolutePath();
        for (Path p = here; p != null; p = p.getParent()) {
            Path candidate = p.resolve("contracts");
            if (Files.isDirectory(candidate.resolve("examples"))) return candidate;
        }
        throw new IllegalStateException("no contracts/ directory above " + here);
    }

    @Test
    @DisplayName("the committed C-3 example parses with every block populated")
    void costModelSampleParses() throws IOException {
        CostModelSnapshot snap = CostModelParser.parse(EXAMPLES.resolve("cost_model.sample.json").toFile());

        assertEquals(1, snap.costModelSchema());
        assertNotNull(snap.snapshotId());
        assertNotNull(snap.nodeClass());
        assertTrue(snap.measuredAtUnix() > 0, "an unstamped snapshot cannot be ordered in a series");
        assertFalse(snap.entries().isEmpty());
        assertNotNull(snap.stochastic(), "F-22 needs the stochastic block to give the DES its variance");
        assertNotNull(snap.admissibility(), "F-13 turns on these bounds");
        assertNotNull(snap.provenance());
    }

    @Test
    @DisplayName("a schema version other than 1 is refused rather than guessed at")
    void wrongSchemaVersionIsRefused(@TempDir Path dir) throws IOException {
        String json = Files.readString(EXAMPLES.resolve("cost_model.sample.json"))
                .replaceFirst("\"cost_model_schema\"\\s*:\\s*1", "\"cost_model_schema\": 2");
        File f = dir.resolve("v2.json").toFile();
        Files.writeString(f.toPath(), json);

        IOException e = assertThrows(IOException.class, () -> CostModelParser.parse(f));
        assertTrue(e.getMessage().contains("cost_model_schema"));
    }

    @Test
    @DisplayName("every committed snapshot in the repo parses")
    void everyCommittedSnapshotParses() throws IOException {
        // These are the files a real run is served by. If one of them stops parsing, every
        // simulation that names it fails at load, and that should be caught here rather than
        // three minutes into a sweep.
        int parsed = 0;
        try (var walk = Files.walk(COST_MODELS)) {
            for (Path p : walk.filter(x -> x.toString().endsWith(".json")).toList()) {
                CostModelSnapshot snap = CostModelParser.parse(p.toFile());
                assertEquals(1, snap.costModelSchema(), p.getFileName().toString());
                assertFalse(snap.entries().isEmpty(), p.getFileName().toString());
                parsed++;
            }
        }
        assertTrue(parsed >= 50, "expected the full committed series, parsed " + parsed);
    }

    @Test
    @DisplayName("the committed snapshots carry the phase split")
    void committedSnapshotsHaveAPhaseSplit() throws IOException {
        // The split was backfilled from stored calibration observations, and the invariance
        // rule in SimNodeServer silently degrades to whole-span scaling without it.
        CostModelSnapshot snap = CostModelParser.parse(COST_MODELS
                .resolve("gtx1650ti_ngl99_p4_q4km_llama32_1b")
                .resolve("008_cm_gtx1650ti_ngl99_p4_q4km_llama32_1b_20260831T153652Z_008.json").toFile());

        for (CostEntry e : snap.entries()) {
            assertTrue(e.hasPhaseSplit(), "every cell of the anchor snapshot should carry prefill and decode");
            assertTrue(e.prefillMsMean() + e.decodeMsMean() <= e.serviceMsMean() + 1e-6,
                    "the two phases cannot exceed the span they were measured inside");
        }
    }

    @Test
    @DisplayName("a cell with no split says so instead of reporting zero")
    void absentSplitIsDistinguishableFromZero() {
        // Boxed on purpose. "never measured" and "measured at zero" are different answers and
        // a primitive double cannot tell them apart.
        CostEntry without = new CostEntry(java.util.List.of(1, 128), java.util.List.of(1, 64),
                1, 1000.0, 1000.0, 1200.0, null, null, 100.0, 8);
        CostEntry zero = new CostEntry(java.util.List.of(1, 128), java.util.List.of(1, 64),
                1, 1000.0, 1000.0, 1200.0, 0.0, 1000.0, 100.0, 8);

        assertFalse(without.hasPhaseSplit());
        assertNull(without.prefillMsMean());
        assertTrue(zero.hasPhaseSplit());
        assertEquals(0.0, zero.prefillMsMean());
    }

    @Test
    @DisplayName("the committed C-6 example parses with the fields the simulator reads")
    void manifestSampleParses() throws IOException {
        Manifest m = ManifestParser.parse(EXAMPLES.resolve("manifest.sample.json").toString());

        assertNotNull(m.runId());
        assertNotNull(m.policy(), "the policy name selects the arm and must survive the round trip");
        assertNotNull(m.costModelSnapshots());
        assertFalse(m.nodes().isEmpty());
        assertNotNull(m.vehicle(), "F-24 stamps every simulated figure from this field");
    }

    @Test
    @DisplayName("an unknown manifest field is ignored rather than fatal")
    void unknownManifestFieldsAreTolerated(@TempDir Path dir) throws IOException {
        // C-6 grows over time and the two halves do not deploy in lockstep, so a manifest
        // written by a newer harness has to stay readable by an older scheduler.
        String json = Files.readString(EXAMPLES.resolve("manifest.sample.json"))
                .replaceFirst("\\{", "{\"a_field_from_next_week\": 42,");
        Path f = dir.resolve("future.json");
        Files.writeString(f, json);

        assertNotNull(ManifestParser.parse(f.toString()).runId());
    }

    @Test
    @DisplayName("a manifest with no transport measurement reports zero, and that means absent")
    void absentTransportOverheadIsZero() throws IOException {
        // Absence has to mean "not measured", never "measured at zero". SimApp reads exactly
        // this to decide whether to apply an overhead at all.
        Manifest m = ManifestParser.parse(EXAMPLES.resolve("manifest.sample.json").toString());

        assertEquals(0.0, m.transportOverheadMeanMs(), 1e-9);
        assertEquals(0.0, m.transportOverheadSdMs(), 1e-9);
    }

    @Test
    @DisplayName("a measured transport block reaches the accessors")
    void transportOverheadIsRead(@TempDir Path dir) throws IOException {
        String json = Files.readString(EXAMPLES.resolve("manifest.sample.json"))
                .replaceFirst("\\{", "{\"transport_overhead\": {\"mean_ms\": 5.86, \"sd_ms\": 2.655, "
                        + "\"n_samples\": 759, \"source\": \"C-5 transport_residual_ms\"},");
        Path f = dir.resolve("with_overhead.json");
        Files.writeString(f, json);

        Manifest m = ManifestParser.parse(f.toString());
        assertEquals(5.86, m.transportOverheadMeanMs(), 1e-9);
        assertEquals(2.655, m.transportOverheadSdMs(), 1e-9);
    }

    @Test
    @DisplayName("a node's batch capacity is its engine's slot count")
    void batchCapacityComesFromParallel() throws IOException {
        // llama.cpp --parallel is exactly SimNodeServer's slot count, which is what makes the
        // node model exact rather than approximate.
        Manifest m = ManifestParser.parse(EXAMPLES.resolve("manifest.sample.json").toString());
        Manifest.SimNode node = m.nodes().get(0);

        assertEquals(node.engineConfig().parallel(), node.batchCapacity());
    }

    @Test
    @DisplayName("a node with no engine config falls back to a single slot")
    void batchCapacityDefaultsToOne() {
        Manifest.SimNode node = new Manifest.SimNode(
                "n", "pool", "h", "llamacpp", "v", "m", "q", "gpu", false, 4, null);
        assertEquals(1, node.batchCapacity(), "a missing slot count must not mean unlimited");
    }

    @Test
    @DisplayName("a node with no role is a pool member")
    void roleDefaultsToPool() {
        // The F-9b engine-gap probe is the only non-pool role, and it has to opt in explicitly
        // rather than a missing field quietly removing a node from the comparison.
        Manifest.SimNode node = new Manifest.SimNode(
                "n", null, "h", "llamacpp", "v", "m", "q", "gpu", false, 4, null);
        assertEquals("pool", node.role());
    }
}
