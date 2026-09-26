package com.sched.sim;

import static com.sched.RunFixtures.FAST_CLASS;
import static com.sched.RunFixtures.SLOW_CLASS;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.sched.ContractCheck;
import com.sched.JavaMain;
import com.sched.RunFixtures;
import com.sched.RunFixtures.Req;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * SimApp from its entry point, as the sweep and p4_validate run it: the manifest it writes,
 * the records it logs, and the runs it refuses (J2, J5 to J9).
 *
 * Each run is a child JVM. SimApp exits 1 on a refusal, which is the contract the Python
 * side reads, and an in-process call would end the test JVM with it.
 */
class SimAppTest {
    /** Far past any committed snapshot's max_prompt, so no node can admit it. */
    private static final int INADMISSIBLE = 1_000_000;

    private record Setup(Path dir, Path costModels, Path trace, Map<String, Object> manifest) {
        Path manifestPath() throws IOException {
            return RunFixtures.write(dir.resolve("manifest.json"), manifest);
        }

        JavaMain.Result run(Path out) throws IOException, InterruptedException {
            return JavaMain.run(SimApp.class, dir, trace.toString(), manifestPath().toString(),
                    out.toString(), "--cost-models", costModels.toString(), "--deterministic");
        }
    }

    private static Setup setup(Path dir, List<Req> reqs) throws IOException {
        Path costModels = dir.resolve("cost_models");
        String slow = RunFixtures.copySnapshot(SLOW_CLASS, costModels);
        String fast = RunFixtures.copySnapshot(FAST_CLASS, costModels);
        Path trace = RunFixtures.writeTrace(dir.resolve("trace.jsonl"), reqs);
        return new Setup(dir, costModels, trace, RunFixtures.manifest(trace, slow, fast, "jsq"));
    }

    private static Path only(Path out, String prefix) throws IOException {
        try (Stream<Path> files = Files.list(out)) {
            List<Path> hits = files.filter(p -> p.getFileName().toString().startsWith(prefix)).toList();
            assertEquals(1, hits.size(), prefix + " files in " + out + ": " + hits);
            return hits.get(0);
        }
    }

    @Test
    @DisplayName("J5, J2, J9: validity comes from the run, completions are sim_completion, a drop is a valid C-4 line")
    void aRunWithDropsWritesWhatHappened(@TempDir Path dir) throws Exception {
        // Two requests no node can admit: one inside warmup, which the replay would not
        // count either, and one after it.
        List<Req> reqs = new ArrayList<>(RunFixtures.steady(30, 0.5));
        reqs.add(new Req(1.25, INADMISSIBLE));
        reqs.add(new Req(20.0, INADMISSIBLE));
        reqs.sort(java.util.Comparator.comparingDouble(Req::offsetS));
        Setup s = setup(dir, reqs);
        s.manifest().put("warmup_s", 5.0);
        Path out = dir.resolve("out");

        JavaMain.Result r = s.run(out);
        assertEquals(0, r.exitCode(), r.output());

        JsonNode man = ContractCheck.MAPPER.readTree(out.resolve("manifest.json").toFile());
        assertEquals("simulator", man.get("vehicle").asText());
        assertEquals("fixture_sim", man.get("run_id").asText());
        assertEquals(RunFixtures.SEED, man.get("config").get("seed").asInt());
        JsonNode v = man.get("validity");
        assertEquals(1, v.get("dropped_requests").asInt(), "the drop inside warmup is not counted");
        assertFalse(v.get("valid").asBoolean(), "a dropped request invalidates the run, as on hardware");
        assertTrue(v.has("heartbeat_gaps") && v.get("heartbeat_gaps").isNull(),
                "no heartbeats in the DES: null, never 0");
        assertEquals(List.of("heartbeat_gaps"),
                ContractCheck.MAPPER.convertValue(v.get("unmeasured"), List.class));
        List<String> shas = new ArrayList<>();
        man.get("git_shas").fieldNames().forEachRemaining(shas::add);
        assertEquals(List.of("worker", "scheduler", "harness", "sim"), shas);

        // J2: every completion the simulator logs says it came from the simulator, with the
        // lag that is exact there by construction.
        List<JsonNode> sched = ContractCheck.readJsonl(only(out, "scheduler_"));
        List<JsonNode> observed = ContractCheck.ofType(sched, "completion_observed");
        assertEquals(30, observed.size(), "one per admitted request");
        JsonNode obsDef = ContractCheck.def("log_scheduler.schema.json", "completion_observed");
        for (JsonNode o : observed) {
            ContractCheck.assertConforms(o, obsDef, "completion_observed " + o.get("req_id"));
            assertEquals("sim_completion", o.get("source").asText());
            assertEquals(0, o.get("observed_lag_ns").asLong());
        }

        // J9: the client log, drops included, is C-4 on keys, types and enum values.
        List<JsonNode> client = ContractCheck.readJsonl(only(out, "client_"));
        JsonNode clientSchema = ContractCheck.schema("log_client.schema.json");
        for (JsonNode c : client) ContractCheck.assertConforms(c, clientSchema, "client " + c.get("req_id"));
        List<JsonNode> drops = client.stream().filter(c -> !"ok".equals(c.get("status").asText())).toList();
        assertEquals(List.of("r00003", "r00031"), drops.stream().map(c -> c.get("req_id").asText()).toList());
        for (JsonNode d : drops) {
            assertEquals("engine_error", d.get("status").asText());
            assertEquals(0, d.get("output_tokens").asInt());
        }
    }

    @Test
    @DisplayName("J5: a run where every request was admitted is valid")
    void aCleanRunIsValid(@TempDir Path dir) throws Exception {
        Setup s = setup(dir, RunFixtures.steady(20, 0.5));
        Path out = dir.resolve("out");
        JavaMain.Result r = s.run(out);
        assertEquals(0, r.exitCode(), r.output());
        JsonNode v = ContractCheck.MAPPER.readTree(out.resolve("manifest.json").toFile()).get("validity");
        assertEquals(0, v.get("dropped_requests").asInt());
        assertTrue(v.get("valid").asBoolean());
    }

    @Test
    @DisplayName("J6: a manifest that cannot be written exits non-zero and leaves no manifest")
    void aManifestWriteFailureExitsNonZero(@TempDir Path dir) throws Exception {
        Setup s = setup(dir, RunFixtures.steady(10, 0.5));
        Path out = dir.resolve("out");
        // A non-empty directory where the manifest goes: the rename over it fails.
        Path blocker = out.resolve("manifest.json");
        Files.createDirectories(blocker);
        Files.writeString(blocker.resolve("keep"), "x");

        JavaMain.Result r = s.run(out);
        assertNotEquals(0, r.exitCode(), r.output());
        assertTrue(Files.isDirectory(blocker) && Files.exists(blocker.resolve("keep")),
                "nothing replaced what was there");
        assertFalse(Files.exists(out.resolve("manifest.json.tmp")), "the temporary file is removed");
        // The fallback that once wrote the hardware manifest as the sim's own is gone.
        String hardware = Files.readString(s.manifestPath());
        try (Stream<Path> files = Files.walk(out)) {
            for (Path f : files.filter(Files::isRegularFile).toList()) {
                assertNotEquals(hardware, Files.readString(f), f + " is a copy of the hardware manifest");
            }
        }
    }

    @Test
    @DisplayName("J7: a manifest with no config.seed is refused")
    void aManifestWithoutASeedIsRefused(@TempDir Path dir) throws Exception {
        Setup s = setup(dir, RunFixtures.steady(10, 0.5));
        RunFixtures.config(s.manifest()).remove("seed");
        Path out = dir.resolve("out");
        JavaMain.Result r = s.run(out);
        assertNotEquals(0, r.exitCode(), r.output());
        assertTrue(r.output().contains("config.seed"), r.output());
        assertFalse(Files.exists(out.resolve("manifest.json")));
    }

    @Test
    @DisplayName("J8: a trace whose sha256 is not the manifest's is refused")
    void aTraceWithTheWrongHashIsRefused(@TempDir Path dir) throws Exception {
        Setup s = setup(dir, RunFixtures.steady(10, 0.5));
        s.manifest().put("trace_sha256", "0".repeat(64));
        Path out = dir.resolve("out");
        JavaMain.Result r = s.run(out);
        assertNotEquals(0, r.exitCode(), r.output());
        assertTrue(r.output().contains("sha256"), r.output());
        assertFalse(Files.exists(out.resolve("manifest.json")));

        s.manifest().remove("trace_sha256");
        r = s.run(out);
        assertNotEquals(0, r.exitCode(), "a manifest naming no hash at all:\n" + r.output());
    }

    @Test
    @DisplayName("J8: a trace of another C-2 version is refused, even with its own hash named")
    void aTraceOfAnotherSchemaIsRefused(@TempDir Path dir) throws Exception {
        Setup s = setup(dir, RunFixtures.steady(10, 0.5));
        RunFixtures.writeTrace(s.trace(), RunFixtures.steady(10, 0.5), 2);
        s.manifest().put("trace_sha256", RunFixtures.sha256(s.trace()));
        Path out = dir.resolve("out");
        JavaMain.Result r = s.run(out);
        assertNotEquals(0, r.exitCode(), r.output());
        assertTrue(r.output().contains("trace_schema"), r.output());
        assertFalse(Files.exists(out.resolve("manifest.json")));
    }
}
