package com.sched.core.models;

import static com.sched.Fixtures.splitCell;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sched.core.models.CostModelSnapshot.Admissibility;
import com.sched.core.models.CostModelSnapshot.Provenance;
import com.sched.core.models.CostModelSnapshot.Stochastic;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * The live scheduler serves the snapshot the manifest names, never a newer one of its class.
 *
 * <p>It used to resolve every named snapshot to the newest one of the same node class, while
 * the simulator served the one named (F-21). A run whose manifest pinned an older calibration
 * was then routed on a cost model its manifest never mentioned, and the two vehicles priced
 * the same run on different snapshots. Both now load through {@code CostModelSnapshots.loadNamed}.
 */
class CostModelSnapshotsTest {
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String CLASS = "gtx1650ti_ngl99_p4_q4km_llama32_1b";
    private static final String OLDER = "cm_" + CLASS + "_older";
    private static final String NEWER = "cm_" + CLASS + "_newer";

    @TempDir
    Path dir;

    /** A snapshot whose reference cell's service time tells the two apart. */
    private static CostModelSnapshot snap(String id, String nodeClass, long measuredAt, double serviceMs) {
        return new CostModelSnapshot(
                1, id, nodeClass, measuredAt, List.of("cal_" + id), "lookup_table",
                List.of(splitCell(1, 128, 1, 64, 1, serviceMs, 0.2 * serviceMs, 0.8 * serviceMs)),
                new Stochastic("lognormal_multiplier", 0.1, 5.0, 0.0),
                new Admissibility(512, 128, 60_000),
                new Provenance("llamacpp", "b10569+p1", "Q4_K_M", "none", null, false,
                        new Provenance.EngineConfig(99, 6, 4)));
    }

    private void write(CostModelSnapshot s) throws Exception {
        Path classDir = Files.createDirectories(dir.resolve(s.nodeClass()));
        MAPPER.writeValue(classDir.resolve(s.snapshotId() + ".json").toFile(), s);
    }

    private void writeOlderAndNewer() throws Exception {
        write(snap(OLDER, CLASS, 1_788_000_000L, 1000.0));
        write(snap(NEWER, CLASS, 1_789_000_000L, 700.0));
    }

    @Test
    @DisplayName("a manifest naming the older snapshot of a class gets the older one")
    void theNamedSnapshotIsServedEvenWhenANewerOneOfItsClassExists() throws Exception {
        writeOlderAndNewer();

        Map<String, CostModelSnapshot> loaded =
                CostModelSnapshots.loadNamed(dir, Map.of("gtx1650ti", OLDER));

        CostModelSnapshot got = loaded.get("gtx1650ti");
        assertEquals(OLDER, got.snapshotId());
        assertEquals(1_788_000_000L, got.measuredAtUnix());
        assertEquals(1000.0, got.entries().get(0).serviceMsMean(), 1e-9,
                "the cell's contents are the older file's, not the newer one's under the old id");
    }

    @Test
    @DisplayName("two nodes of one class, each naming a different snapshot, each get their own")
    void eachNodeGetsTheSnapshotItNames() throws Exception {
        writeOlderAndNewer();
        write(snap("cm_rtx3050_only", "rtx3050_ngl99_p4_q4km_llama32_1b", 1_787_000_000L, 400.0));

        Map<String, CostModelSnapshot> loaded = CostModelSnapshots.loadNamed(dir, Map.of(
                "a", OLDER,
                "b", NEWER,
                "c", "cm_rtx3050_only"));

        assertEquals(Set.of("a", "b", "c"), loaded.keySet(), "one entry per node the manifest names");
        assertEquals(OLDER, loaded.get("a").snapshotId());
        assertEquals(NEWER, loaded.get("b").snapshotId());
        assertEquals("cm_rtx3050_only", loaded.get("c").snapshotId());
    }

    @Test
    @DisplayName("an id that is not in the directory refuses, naming the node and the id")
    void aMissingSnapshotIdRefusesRatherThanSwapping() throws Exception {
        writeOlderAndNewer();

        IllegalStateException e = assertThrows(IllegalStateException.class,
                () -> CostModelSnapshots.loadNamed(dir, Map.of(
                        "gtx1650ti", OLDER,
                        "rtx3050", "cm_" + CLASS + "_deleted")));

        assertTrue(e.getMessage().contains("rtx3050"), "names the node: " + e.getMessage());
        assertTrue(e.getMessage().contains("cm_" + CLASS + "_deleted"),
                "names the snapshot id: " + e.getMessage());
    }
}
