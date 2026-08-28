package com.sched.core.models;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;

@JsonIgnoreProperties(ignoreUnknown = true)
public record TraceRequest(
        @JsonProperty("record") String record,
        @JsonProperty("req_id") String reqId,
        @JsonProperty("arrival_offset_s") double arrivalOffsetS,
        @JsonProperty("prompt_len") int promptLen,
        @JsonProperty("output_len") int outputLen,
        @JsonProperty("bucket_id") String bucketId,
        @JsonProperty("priority") int priority) {
}