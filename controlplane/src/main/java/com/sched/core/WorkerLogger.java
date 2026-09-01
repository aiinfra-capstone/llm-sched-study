package com.sched.core;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.io.FileWriter;
import java.io.PrintWriter;
import java.io.IOException;
import java.io.File;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

public class WorkerLogger {
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
            try {
                File dir = new File(outputDir);
                if (!dir.exists()) dir.mkdirs();
                File f = new File(dir, "worker_" + id + "_" + runId + ".jsonl");
                return new PrintWriter(new FileWriter(f, false)); // false = overwrite
            } catch (IOException e) {
                throw new RuntimeException(e);
            }
        });
        try {
            pw.println(mapper.writeValueAsString(record));
            pw.flush();
        } catch (Exception e) {
            e.printStackTrace();
        }
    }

    public void close() {
        for (PrintWriter pw : writers.values()) {
            pw.close();
        }
    }
}
