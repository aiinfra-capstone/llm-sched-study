package com.sched.core.models;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;

public record CostModelSnapshot(
                @JsonProperty("cost_model_schema") int costModelSchema,
                @JsonProperty("snapshot_id") String snapshotId,
                @JsonProperty("node_class") String nodeClass,
                @JsonProperty("measured_at_unix") long measuredAtUnix,
                @JsonProperty("calibration_run_ids") List<String> calibrationRunIds,
                @JsonProperty("form") String form,
                @JsonProperty("entries") List<CostEntry> entries,
                @JsonProperty("stochastic") Stochastic stochastic,
                @JsonProperty("admissibility") Admissibility admissibility,
                @JsonProperty("provenance") Provenance provenance) {

        public record CostEntry(
                        @JsonProperty("prompt_bucket") List<Integer> promptBucket,
                        @JsonProperty("output_bucket") List<Integer> outputBucket,
                        @JsonProperty("concurrency") int concurrency,
                        @JsonProperty("service_ms_mean") double serviceMsMean,
                        @JsonProperty("service_ms_p50") double serviceMsP50,
                        @JsonProperty("service_ms_p95") double serviceMsP95,
                        /**
                         * Boxed on purpose: absent and zero are different answers. A
                         * snapshot fitted before the split existed carries neither field,
                         * and a consumer must be able to tell that from a cell whose
                         * prefill really was measured at zero.
                         */
                        @JsonProperty("prefill_ms_mean") Double prefillMsMean,
                        @JsonProperty("decode_ms_mean") Double decodeMsMean,
                        @JsonProperty("tokens_per_s") double tokensPerS,
                        @JsonProperty("n_samples") int nSamples) {

                /** Whether this cell can say which part of its service time was prefill. */
                public boolean hasPhaseSplit() {
                        return prefillMsMean != null && decodeMsMean != null;
                }
        }

        public record Stochastic(
                        @JsonProperty("model") String model,
                        @JsonProperty("sigma") double sigma,
                        @JsonProperty("autocorr_time_s") double autocorrTimeS,
                        @JsonProperty("fit_r2") double fitR2) {
        }

        public record Admissibility(
                        @JsonProperty("max_prompt") int maxPrompt,
                        @JsonProperty("max_output") int maxOutput,
                        @JsonProperty("timeout_ceiling_ms") int timeoutCeilingMs) {
        }

        public record Provenance(
                        @JsonProperty("engine") String engine,
                        @JsonProperty("engine_version") String engineVersion,
                        @JsonProperty("quant") String quant,
                        @JsonProperty("gpu") String gpu,
                        @JsonProperty("driver") String driver,
                        @JsonProperty("prefix_caching") boolean prefixCaching,
                        @JsonProperty("engine_config") EngineConfig engineConfig) {

                public record EngineConfig(
                                @JsonProperty("ngl") int ngl,
                                @JsonProperty("threads") int threads,
                                @JsonProperty("parallel") int parallel) {
                }
        }
}