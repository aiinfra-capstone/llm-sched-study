package com.sched.core;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.io.FileWriter;
import java.io.PrintWriter;
import java.io.IOException;
import java.io.File;
import java.io.UncheckedIOException;
import com.fasterxml.jackson.core.JsonProcessingException;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

public class WorkerLogger implements AutoCloseable {
    private final String outputDir;
    private final String runId;
    private final Map<String, PrintWriter> writers = new ConcurrentHashMap<>();
    private final ObjectMapper mapper = new ObjectMapper();

    public WorkerLogger(String outputDir, String runId) {
        this.outputDir = outputDir;
        this.runId = runId;
    }

    public record WorkerRecord(
        @JsonProperty("run_id") String runId,
        @JsonProperty("req_id") String reqId,
        @JsonProperty("node_id") String nodeId,
        @JsonProperty("engine") String engine,
        @JsonProperty("queue_wait_ns") long queueWaitNs,
        @JsonProperty("service_ns") long serviceNs,
        @JsonProperty("prompt_tokens") int promptTokens,
        @JsonProperty("output_tokens") int outputTokens,
        @JsonProperty("batch_size_at_admission") int batchSizeAtAdmission,
        @JsonProperty("inflight_at_admission") int inflightAtAdmission,
        @JsonProperty("kv_occupancy_at_admission") double kvOccupancyAtAdmission,
        @JsonProperty("status") String status
    ) {}

    public void logRecord(WorkerRecord record) {
        PrintWriter pw = writers.computeIfAbsent(record.nodeId(), id -> {
            File f = fileFor(id);
            f.getParentFile().mkdirs();
            try {
                return new PrintWriter(new FileWriter(f, false)); // false = overwrite
            } catch (IOException e) {
                throw new UncheckedIOException("cannot open the worker log " + f, e);
            }
        });
        String line;
        try {
            line = mapper.writeValueAsString(record);
        } catch (JsonProcessingException e) {
            throw new UncheckedIOException(e);
        }
        DecisionLogger.writeLine(pw, line, fileFor(record.nodeId()));
    }

    private File fileFor(String nodeId) {
        return new File(new File(outputDir), "worker_" + nodeId + "_" + runId + ".jsonl");
    }

    public void close() {
        for (PrintWriter pw : writers.values()) {
            pw.close();
        }
    }
}
