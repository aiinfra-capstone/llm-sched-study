package com.sched.core.models;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.ArrayList;
import java.util.Comparator;
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
                        @JsonProperty("n_samples") int nSamples,
                        /**
                         * C-3 optional (D5): true when no sample was measured at this cell's
                         * own concurrency and the mean comes from the fallback. Null on a
                         * snapshot fitted before the flag existed.
                         */
                        @JsonProperty("thin") Boolean thin) {

                /** A cell from a snapshot fitted before {@code thin} existed. */
                public CostEntry(List<Integer> promptBucket, List<Integer> outputBucket,
                                int concurrency, double serviceMsMean, double serviceMsP50,
                                double serviceMsP95, Double prefillMsMean, Double decodeMsMean,
                                double tokensPerS, int nSamples) {
                        this(promptBucket, outputBucket, concurrency, serviceMsMean, serviceMsP50,
                                        serviceMsP95, prefillMsMean, decodeMsMean, tokensPerS, nSamples,
                                        null);
                }

                /** Whether this cell can say which part of its service time was prefill. */
                public boolean hasPhaseSplit() {
                        return prefillMsMean != null && decodeMsMean != null;
                }
        }

        /**
         * The C-3 mean service time for a request of {@code pLen} prompt and {@code oLen}
         * output tokens at concurrency {@code conc}, or -1 when no cell covers the shapes.
         *
         * <p>Exact concurrency when the grid has it, clamped to the grid's ends outside it, and
         * linear between the two measured concurrencies around it. ECT prices a request and
         * the simulator draws its service time from this one lookup, so the policy and the
         * vehicle it runs in cannot disagree about what a cell costs.
         */
        public double meanServiceMs(int pLen, int oLen, int conc) {
                List<CostEntry> candidates = new ArrayList<>();
                for (CostEntry e : entries) {
                        if (pLen >= e.promptBucket().get(0) && pLen <= e.promptBucket().get(1)
                                        && oLen >= e.outputBucket().get(0) && oLen <= e.outputBucket().get(1)) {
                                candidates.add(e);
                        }
                }
                if (candidates.isEmpty()) return -1;
                candidates.sort(Comparator.comparingInt(CostEntry::concurrency));
                for (CostEntry e : candidates) {
                        if (e.concurrency() == conc) return e.serviceMsMean();
                }
                CostEntry first = candidates.get(0);
                CostEntry last = candidates.get(candidates.size() - 1);
                if (conc <= first.concurrency()) return first.serviceMsMean();
                if (conc >= last.concurrency()) return last.serviceMsMean();
                for (int i = 0; i < candidates.size() - 1; i++) {
                        CostEntry lower = candidates.get(i);
                        CostEntry upper = candidates.get(i + 1);
                        if (lower.concurrency() < conc && conc < upper.concurrency()) {
                                double f = (double) (conc - lower.concurrency())
                                                / (double) (upper.concurrency() - lower.concurrency());
                                return lower.serviceMsMean() + f * (upper.serviceMsMean() - lower.serviceMsMean());
                        }
                }
                return first.serviceMsMean();
        }

        public record Stochastic(
                        @JsonProperty("model") String model,
                        @JsonProperty("sigma") double sigma,
                        @JsonProperty("autocorr_time_s") double autocorrTimeS,
                        @JsonProperty("fit_r2") double fitR2,
                        /**
                         * Whether {@code autocorr_time_s} is a measurement (resolved) or the
                         * instrument's floor (censored). The simulator reads neither: its noise
                         * is i.i.d. per request (0.3). Boxed so an older snapshot still loads.
                         */
                        @JsonProperty("tau_resolved") Boolean tauResolved,
                        @JsonProperty("tau_censored") Boolean tauCensored) {

                /** A stochastic block from a snapshot fitted before the τ flags existed. */
                public Stochastic(String model, double sigma, double autocorrTimeS, double fitR2) {
                        this(model, sigma, autocorrTimeS, fitR2, null, null);
                }
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