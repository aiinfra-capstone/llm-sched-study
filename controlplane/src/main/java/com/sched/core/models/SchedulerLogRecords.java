package com.sched.core.models;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;

public interface SchedulerLogRecords {

    record Candidate(
            @JsonProperty("node_id") String nodeId,
            @JsonProperty("queue_depth") int queueDepth,
            @JsonProperty("inflight") int inflight,
            @JsonProperty("capability_tok_s") double capabilityTokS,
            @JsonProperty("estimate_age_ms") long estimateAgeMs,
            @JsonProperty("admissible") boolean admissible,
            @JsonProperty("score") Double score // Can be null if rejected before scoring
    ) {
    }

    record DecisionRecord(
            @JsonProperty("type") String type, // Always "decision"
            @JsonProperty("run_id") String runId,
            @JsonProperty("req_id") String reqId,
            @JsonProperty("decision_seq") long decisionSeq,
            @JsonProperty("policy") String policy,
            @JsonProperty("staleness_param_s") double stalenessParamS,
            @JsonProperty("decide_duration_ns") long decideDurationNs,
            @JsonProperty("chosen_node") String chosenNode,
            @JsonProperty("tie_break_draw") double tieBreakDraw,
            @JsonProperty("candidates") List<Candidate> candidates) {
    }

    record CompletionObservedRecord(
            @JsonProperty("type") String type, // Always "completion_observed"
            @JsonProperty("run_id") String runId,
            @JsonProperty("req_id") String reqId,
            @JsonProperty("node_id") String nodeId,
            @JsonProperty("source") String source, // e.g., "completion_rpc" or "sim_event"
            @JsonProperty("observed_lag_ns") long observedLagNs) {
    }
}