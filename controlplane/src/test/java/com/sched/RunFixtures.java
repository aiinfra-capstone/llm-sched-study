package com.sched;

import com.sched.core.models.CostModelParser;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;

/**
 * The files a real SimApp or LiveSchedulerApp run reads: a cost-model tree, a C-2 trace and a
 * C-6 manifest, built in a test's own directory.
 *
 * The snapshots are committed ones copied into the test's tree, so a calibration promoted
 * later does not change what a test runs on. The manifest is a plain map, so a test that is
 * about one missing or wrong field removes or changes exactly that one.
 */
public final class RunFixtures {
    public static final String SLOW_CLASS = "gtx1650ti_ngl99_p4_q4km_llama32_1b";
    public static final String FAST_CLASS = "rtx3050_ngl99_p4_q4km_llama32_1b";
    public static final int SEED = 20260926;

    private RunFixtures() {}

    /** Copy the first committed snapshot of {@code nodeClass} into {@code root}; returns its id. */
    public static String copySnapshot(String nodeClass, Path root) throws IOException {
        Path classDir = ContractCheck.contracts().resolve("cost_models").resolve(nodeClass);
        Path source;
        try (Stream<Path> files = Files.list(classDir)) {
            source = files.filter(p -> p.toString().endsWith(".json")).sorted().findFirst()
                    .orElseThrow(() -> new IllegalStateException("no snapshot in " + classDir));
        }
        Path dest = root.resolve(nodeClass);
        Files.createDirectories(dest);
        Path target = dest.resolve(source.getFileName());
        Files.copy(source, target);
        return CostModelParser.parse(target.toFile()).snapshotId();
    }

    /** One trace request: when it arrives and how long its prompt is. Output is 64 tokens. */
    public record Req(double offsetS, int promptLen) {}

    /** Requests every {@code spacingS} seconds with a 64-token prompt. */
    public static List<Req> steady(int n, double spacingS) {
        List<Req> out = new ArrayList<>();
        for (int i = 0; i < n; i++) out.add(new Req(i * spacingS, 64));
        return out;
    }

    public static Path writeTrace(Path path, List<Req> reqs) throws IOException {
        return writeTrace(path, reqs, 1);
    }

    /** A C-2 trace whose header carries {@code traceSchema}. */
    public static Path writeTrace(Path path, List<Req> reqs, int traceSchema) throws IOException {
        StringBuilder sb = new StringBuilder();
        sb.append("{\"record\":\"header\",\"trace_schema\":").append(traceSchema)
          .append(",\"gen_seed\":3,\"n_requests\":").append(reqs.size()).append("}\n");
        for (int i = 0; i < reqs.size(); i++) {
            Req r = reqs.get(i);
            sb.append(String.format(
                    "{\"record\":\"req\",\"req_id\":\"r%05d\",\"arrival_offset_s\":%.4f,"
                    + "\"prompt_len\":%d,\"output_len\":64,\"bucket_id\":\"p128_o64\",\"priority\":0}%n",
                    i, r.offsetS(), r.promptLen()));
        }
        Files.writeString(path, sb.toString());
        return path;
    }

    public static String sha256(Path file) throws IOException {
        try {
            return HexFormat.of().formatHex(
                    MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(file)));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    public static Map<String, Object> node(String nodeId, String host) {
        Map<String, Object> n = new LinkedHashMap<>();
        n.put("node_id", nodeId);
        n.put("role", "pool");
        n.put("host", host);
        n.put("engine", "llamacpp");
        n.put("model", "Llama-3.2-1B-Instruct");
        n.put("quant", "Q4_K_M");
        n.put("max_batch", 4);
        n.put("engine_config", Map.of("parallel", 4, "ngl", 99, "threads", 6));
        return n;
    }

    /**
     * A manifest for a two-node pool, "slow" and "fast", on the snapshots named. Every map in
     * it is mutable, so a test can drop the seed or change the policy before writing it.
     */
    public static Map<String, Object> manifest(Path trace, String slowId, String fastId, String policy)
            throws IOException {
        Map<String, Object> config = new LinkedHashMap<>();
        config.put("seed", SEED);
        config.put("rate_scale", 1.0);
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("run_id", "fixture");
        m.put("started_unix", 1789600000L);
        m.put("vehicle", "hardware");
        m.put("config_hash", "0".repeat(64));
        m.put("config", config);
        m.put("trace_path", trace.toString());
        m.put("trace_sha256", sha256(trace));
        m.put("policy", policy);
        m.put("lambda", 2.0);
        m.put("staleness_s", 0.0);
        m.put("warmup_s", 0.0);
        m.put("duration_s", 60.0);
        Map<String, String> snaps = new LinkedHashMap<>();
        snaps.put("slow", slowId);
        snaps.put("fast", fastId);
        m.put("cost_model_snapshots", snaps);
        m.put("nodes", new ArrayList<>(List.of(node("slow", "h1"), node("fast", "h2"))));
        return m;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> config(Map<String, Object> manifest) {
        return (Map<String, Object>) manifest.get("config");
    }

    public static Path write(Path path, Map<String, Object> manifest) throws IOException {
        Files.writeString(path, ContractCheck.json(manifest));
        return path;
    }
}
