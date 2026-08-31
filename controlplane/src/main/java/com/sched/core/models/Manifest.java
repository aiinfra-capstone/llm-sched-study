package com.sched.core.models;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;

@JsonIgnoreProperties(ignoreUnknown = true)
public record Manifest(
        @JsonProperty("manifest_schema") Integer manifestSchema,
        @JsonProperty("run_id") String runId,
        @JsonProperty("nodes") List<SimNode> nodes) {
    @JsonIgnoreProperties(ignoreUnknown = true)
    public record SimNode(
            @JsonProperty("node_id") String nodeId,
            @JsonProperty("node_class") String nodeClass,
            @JsonProperty("engine_config") EngineConfig engineConfig) {
        @JsonIgnoreProperties(ignoreUnknown = true)
        public record EngineConfig(
                @JsonProperty("parallel") int parallel) {
        }

        // Helper method for the DES to quickly read the F-9 slot limit
        public int batchCapacity() {
            if (engineConfig == null)
                return 1; // Fallback
            return engineConfig.parallel();
        }
    }
}