package com.sched.core;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.BufferedWriter;
import java.io.FileWriter;
import java.io.IOException;

public class DecisionLogger {
    private final ObjectMapper mapper;
    private final BufferedWriter writer;

    public DecisionLogger(String runId) throws IOException {
        this.mapper = new ObjectMapper();
        // File naming convention strictly defined by the spec
        String fileName = "scheduler_" + runId + ".jsonl";

        // The 'true' flag opens the FileWriter in append mode
        this.writer = new BufferedWriter(new FileWriter(fileName, true));
    }

    /**
     * Synchronized to prevent overlapping JSON writes if multiple gRPC threads
     * make a dispatch decision at the exact same millisecond.
     */
    public synchronized void logRecord(Object record) {
        try {
            writer.write(mapper.writeValueAsString(record));
            writer.newLine();
            writer.flush(); // Flush immediately to prevent data loss on crash
        } catch (IOException e) {
            System.err.println("Failed to write to scheduler log: " + e.getMessage());
        }
    }

    public void close() throws IOException {
        writer.close();
    }
}