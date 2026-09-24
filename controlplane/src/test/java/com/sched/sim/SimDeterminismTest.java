package com.sched.sim;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sched.core.models.CostModelParser;
import com.sched.core.models.CostModelSnapshot;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.stream.Stream;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * F-20 inside the gate.
 *
 * <p>A dispatch sequence is a property of (trace, seed, policy) and of nothing else. That
 * was already checked by {@code tools/determinism_test.sh}, which is not part of
 * {@code mvn test} and so is not what anyone runs before pushing. This runs the same check
 * on the same entry point, {@link SimApp#main}, from a trace and a manifest built here.
 *
 * <p>The pool is built from committed C-3 snapshots copied into the test's own cost-model
 * directory, rather than read from {@code contracts/cost_models} in place. A calibration
 * promoted tomorrow changes which snapshot is newest for a class, and SimApp resolves a
 * node to the newest of its class; a test that let that happen would be asserting
 * yesterday's pool on tomorrow's numbers. Which snapshot is used does not matter here, so
 * the test takes the first of each class by name and copies it.
 */
class SimDeterminismTest {
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final int REQUESTS = 120;

    private record Pool(String slowId, String fastId) {}

    @Test
    @DisplayName("two runs of one manifest produce the same dispatch sequence")
    void twoRunsOfOneManifestAgree(@TempDir Path dir) throws IOException {
        Path costModels = dir.resolve("cost_models");
        Pool pool = copyOneSnapshotPerClass(costModels);
        Path trace = writeTrace(dir.resolve("trace.jsonl"));
        // JSQ, because it draws from the policy stream to break a tie. A policy that never
        // draws would agree across runs even if the seeding were broken.
        Path manifest = writeManifest(dir.resolve("manifest.json"), trace, pool, "jsq");

        List<String> first = runAndReadSequence(dir.resolve("out1"), trace, manifest, costModels);
        List<String> second = runAndReadSequence(dir.resolve("out2"), trace, manifest, costModels);

        assertEquals(REQUESTS, first.size(), "every request in the trace should be dispatched");
        assertEquals(first, second, "the same trace, seed and policy must give the same sequence");
    }

    @Test
    @DisplayName("the sequence being compared actually exercises the random stream")
    void theComparedSequenceContainsTieBreaks(@TempDir Path dir) throws IOException {
        Path costModels = dir.resolve("cost_models");
        Pool pool = copyOneSnapshotPerClass(costModels);
        Path trace = writeTrace(dir.resolve("trace.jsonl"));
        Path manifest = writeManifest(dir.resolve("manifest.json"), trace, pool, "jsq");

        SimApp.main(new String[] {
                trace.toString(), manifest.toString(), dir.resolve("out").toString(),
                "--cost-models", costModels.toString(), "--deterministic"});

        List<JsonNode> decisions = decisions(dir.resolve("out"));
        boolean anyDraw = decisions.stream().anyMatch(d -> !d.path("tie_break_draw").isNull());
        assertTrue(anyDraw,
                "JSQ on two nodes should tie at least once in " + REQUESTS + " requests; "
                + "without a tie the run above would agree with itself whatever the seeding did");
        assertFalse(decisions.isEmpty());
    }

    private static List<String> runAndReadSequence(Path out, Path trace, Path manifest, Path costModels)
            throws IOException {
        SimApp.main(new String[] {
                trace.toString(), manifest.toString(), out.toString(),
                "--cost-models", costModels.toString(), "--deterministic"});
        List<String> seq = new ArrayList<>();
        for (JsonNode d : decisions(out)) {
            // decide_duration_ns is wall clock and is deliberately not compared.
            seq.add(d.path("decision_seq").asLong() + " " + d.path("req_id").asText()
                    + " -> " + d.path("chosen_node").asText()
                    + " draw=" + d.path("tie_break_draw"));
        }
        return seq;
    }

    private static List<JsonNode> decisions(Path out) throws IOException {
        List<JsonNode> decisions = new ArrayList<>();
        try (Stream<Path> logs = Files.list(out)) {
            for (Path p : (Iterable<Path>) logs.filter(f -> f.getFileName().toString().startsWith("scheduler_"))
                    .sorted()::iterator) {
                for (String line : Files.readAllLines(p)) {
                    JsonNode rec = MAPPER.readTree(line);
                    if ("decision".equals(rec.path("type").asText())) decisions.add(rec);
                }
            }
        }
        decisions.sort(Comparator.comparingLong(d -> d.path("decision_seq").asLong()));
        return decisions;
    }

    /** Copy the first snapshot by name from each of the two GPU classes into `dest`. */
    private static Pool copyOneSnapshotPerClass(Path dest) throws IOException {
        Path root = contractsCostModels();
        String slowClass = "gtx1650ti_ngl99_p4_q4km_llama32_1b";
        String fastClass = "rtx3050_ngl99_p4_q4km_llama32_1b";
        return new Pool(copyFirst(root.resolve(slowClass), dest.resolve(slowClass)),
                copyFirst(root.resolve(fastClass), dest.resolve(fastClass)));
    }

    private static String copyFirst(Path classDir, Path dest) throws IOException {
        Files.createDirectories(dest);
        Path source;
        try (Stream<Path> files = Files.list(classDir)) {
            source = files.filter(p -> p.toString().endsWith(".json")).sorted().findFirst()
                    .orElseThrow(() -> new IllegalStateException("no snapshot in " + classDir));
        }
        Path target = dest.resolve(source.getFileName());
        Files.copy(source, target);
        CostModelSnapshot snap = CostModelParser.parse(target.toFile());
        return snap.snapshotId();
    }

    private static Path contractsCostModels() {
        Path here = Path.of("").toAbsolutePath();
        for (Path p = here; p != null; p = p.getParent()) {
            Path candidate = p.resolve("contracts").resolve("cost_models");
            if (Files.isDirectory(candidate)) return candidate;
        }
        throw new IllegalStateException("no contracts/cost_models above " + here);
    }

    private static Path writeTrace(Path path) throws IOException {
        StringBuilder sb = new StringBuilder();
        sb.append("{\"record\":\"header\",\"trace_schema\":1,\"gen_seed\":20260918,")
          .append("\"n_requests\":").append(REQUESTS).append("}\n");
        for (int i = 0; i < REQUESTS; i++) {
            // Arrivals close enough together that the pool builds a queue, so the policy has
            // a decision to make rather than an empty pool every time.
            sb.append(String.format(
                    "{\"record\":\"req\",\"req_id\":\"r%05d\",\"arrival_offset_s\":%.4f,"
                    + "\"prompt_len\":64,\"output_len\":64,\"bucket_id\":\"p128_o64\",\"priority\":0}%n",
                    i, i * 0.35));
        }
        Files.writeString(path, sb.toString());
        return path;
    }

    private static Path writeManifest(Path path, Path trace, Pool pool, String policy) throws IOException {
        String json = """
                {
                  "run_id": "determinism_fixture",
                  "started_unix": 1789600000,
                  "vehicle": "sim",
                  "config": {"seed": 20260918, "rate_scale": 1.0},
                  "trace_path": "%s",
                  "policy": "%s",
                  "lambda": 2.85,
                  "staleness_s": 0.0,
                  "warmup_s": 0.0,
                  "cost_model_snapshots": {"slow": "%s", "fast": "%s"},
                  "nodes": [
                    {"node_id": "slow", "role": "pool", "host": "h1", "engine": "llamacpp",
                     "model": "Llama-3.2-1B-Instruct", "quant": "Q4_K_M", "max_batch": 4,
                     "engine_config": {"parallel": 4, "ngl": 99, "threads": 6}},
                    {"node_id": "fast", "role": "pool", "host": "h2", "engine": "llamacpp",
                     "model": "Llama-3.2-1B-Instruct", "quant": "Q4_K_M", "max_batch": 4,
                     "engine_config": {"parallel": 4, "ngl": 99, "threads": 6}}
                  ]
                }
                """.formatted(trace.toString().replace("\\", "\\\\"), policy, pool.slowId(), pool.fastId());
        Files.writeString(path, json);
        return path;
    }
}
