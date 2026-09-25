package com.sched.core.models;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.Map;
import java.util.stream.Stream;

/**
 * Loads the C-3 snapshots a run manifest names, for SimApp and the live scheduler alike.
 *
 * <p>Both vehicles used to carry their own copy of this loop, and the copies drifted: the
 * live scheduler swapped each named snapshot for the newest one of its node class, while
 * SimApp used the one the manifest named (F-21). A sim replay of a hardware run then served
 * a different cost model from the run it was replaying. One loader keeps them together.
 */
public final class CostModelSnapshots {
    private CostModelSnapshots() {
    }

    /**
     * The snapshot each node names, by node id.
     *
     * <p>Every {@code .json} file under {@code dir} is parsed and indexed by its snapshot id.
     * Each node gets exactly the snapshot its id names. A newer snapshot of the same node
     * class is never substituted, since the manifest is the record of what the run used.
     *
     * @throws IllegalStateException naming the node and the snapshot id when that id is not
     *         in {@code dir}, or naming the file when a snapshot under {@code dir} fails to
     *         parse
     */
    public static Map<String, CostModelSnapshot> loadNamed(Path dir, Map<String, String> nodeToSnapshotId) {
        Map<String, CostModelSnapshot> byId = indexById(dir);
        Map<String, CostModelSnapshot> named = new HashMap<>();
        for (Map.Entry<String, String> e : nodeToSnapshotId.entrySet()) {
            CostModelSnapshot snap = byId.get(e.getValue());
            if (snap == null) {
                String where = Files.isDirectory(dir) ? "" : " (that directory does not exist)";
                throw new IllegalStateException("node " + e.getKey() + " names snapshot "
                        + e.getValue() + ", which is not in " + dir + "/" + where);
            }
            named.put(e.getKey(), snap);
        }
        return named;
    }

    private static Map<String, CostModelSnapshot> indexById(Path dir) {
        Map<String, CostModelSnapshot> byId = new HashMap<>();
        if (!Files.isDirectory(dir)) return byId;
        try (Stream<Path> paths = Files.walk(dir)) {
            for (Path p : (Iterable<Path>) paths.filter(f -> f.toString().endsWith(".json"))::iterator) {
                CostModelSnapshot s = parse(p);
                byId.put(s.snapshotId(), s);
            }
        } catch (IOException e) {
            throw new IllegalStateException("failed to list C-3 snapshots in " + dir + ": " + e.getMessage(), e);
        }
        return byId;
    }

    private static CostModelSnapshot parse(Path p) {
        try {
            return CostModelParser.parse(p.toFile());
        } catch (IOException e) {
            throw new IllegalStateException("failed to load C-3 snapshot " + p + ": " + e.getMessage(), e);
        }
    }
}
