package com.sched.core;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.io.FileWriter;
import java.io.PrintWriter;
import java.io.IOException;
import java.io.File;
import java.io.UncheckedIOException;
import com.fasterxml.jackson.core.JsonProcessingException;

public class ClientLogger implements AutoCloseable {
    private final PrintWriter pw;
    private final File file;
    private final ObjectMapper mapper = new ObjectMapper();

    /** @throws UncheckedIOException when the log file cannot be opened (3.6). */
    public ClientLogger(String outputDir, String runId) {
        File dir = new File(outputDir);
        if (!dir.exists()) dir.mkdirs();
        this.file = new File(dir, "client_" + runId + ".jsonl");
        try {
            this.pw = new PrintWriter(new FileWriter(file, false)); // false = overwrite
        } catch (IOException e) {
            throw new UncheckedIOException("cannot open the client log " + file, e);
        }
    }

    public record ClientRecord(
        @JsonProperty("run_id") String runId,
        @JsonProperty("req_id") String reqId,
        @JsonProperty("intended_offset_s") double intendedOffsetS,
        @JsonProperty("actual_send_offset_s") double actualSendOffsetS,
        @JsonProperty("send_lag_ms") double sendLagMs,
        @JsonProperty("e2e_duration_ns") long e2eDurationNs,
        @JsonProperty("status") String status,
        @JsonProperty("output_tokens") int outputTokens,
        @JsonProperty("responding_node") String respondingNode,
        @JsonProperty("chosen_node_from_ack") String chosenNodeFromAck,
        @JsonProperty("dispatch_ack_ns") long dispatchAckNs
    ) {}

    public void logRecord(ClientRecord record) {
        String line;
        try {
            line = mapper.writeValueAsString(record);
        } catch (JsonProcessingException e) {
            throw new UncheckedIOException(e);
        }
        DecisionLogger.writeLine(pw, line, file);
    }

    public void close() {
        pw.close();
    }
}
