package com.sched.core.models;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;
import java.util.Map;

@JsonIgnoreProperties(ignoreUnknown = true)
public record Manifest(
    @JsonProperty("run_id") String runId,
    @JsonProperty("started_unix") Long startedUnix,
    @JsonProperty("vehicle") String vehicle,
    @JsonProperty("config_hash") String configHash,
    @JsonProperty("config") Map<String, Object> config,
    @JsonProperty("trace_path") String tracePath,
    @JsonProperty("trace_sha256") String traceSha256,
    @JsonProperty("policy") String policy,
    @JsonProperty("lambda") Double lambdaValue,
    @JsonProperty("staleness_s") Double stalenessS,
    @JsonProperty("warmup_s") Double warmupS,
    @JsonProperty("duration_s") Double durationS,
    @JsonProperty("cost_model_snapshots") Map<String, String> costModelSnapshots,
    @JsonProperty("nodes") List<SimNode> nodes,
    @JsonProperty("git_shas") Map<String, String> gitShas,
    @JsonProperty("validity") Map<String, Object> validity,
    @JsonProperty("clock_sync") Map<String, Object> clockSync
) {
    @JsonIgnoreProperties(ignoreUnknown = true)
    public record SimNode(
        @JsonProperty("node_id") String nodeId,
        @JsonProperty("role") String role,
        @JsonProperty("host") String host,
        @JsonProperty("engine") String engine,
        @JsonProperty("engine_version") String engineVersion,
        @JsonProperty("model") String model,
        @JsonProperty("quant") String quant,
        @JsonProperty("gpu") String gpu,
        @JsonProperty("prefix_caching") Boolean prefixCaching,
        @JsonProperty("max_batch") Integer maxBatch,
        @JsonProperty("engine_config") EngineConfig engineConfig
    ) {
        @JsonIgnoreProperties(ignoreUnknown = true)
        public record EngineConfig(
            @JsonProperty("parallel") int parallel,
            @JsonProperty("ngl") Integer ngl,
            @JsonProperty("threads") Integer threads
        ) {}

        public int batchCapacity() {
            if (engineConfig == null) return 1;
            return engineConfig.parallel();
        }
        
        public String role() {
            return role != null ? role : "pool";
        }
    }
}