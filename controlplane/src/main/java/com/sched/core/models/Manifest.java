package com.sched.core.models;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;
import java.util.Map;

@JsonIgnoreProperties(ignoreUnknown = true)
public record Manifest(
    @JsonProperty("manifest_schema") Integer manifestSchema,
    @JsonProperty("run_id") String runId,
    @JsonProperty("cost_model_snapshots") Map<String, String> costModelSnapshots,
    @JsonProperty("nodes") List<SimNode> nodes
) {
    @JsonIgnoreProperties(ignoreUnknown = true)
    public record SimNode(
        @JsonProperty("node_id") String nodeId,
        @JsonProperty("node_class") String nodeClass,
        @JsonProperty("engine_config") EngineConfig engineConfig
    ) {
        @JsonIgnoreProperties(ignoreUnknown = true)
        public record EngineConfig(
            @JsonProperty("parallel") int parallel
        ) {}

        public int batchCapacity() {
            if (engineConfig == null) return 1;
            return engineConfig.parallel();
        }
    }
}