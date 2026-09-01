package com.sched.core;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.io.FileWriter;
import java.io.PrintWriter;
import java.io.IOException;
import java.io.File;

public class ClientLogger {
    private final PrintWriter pw;
    private final ObjectMapper mapper = new ObjectMapper();

    public ClientLogger(String outputDir, String runId) {
        try {
            File dir = new File(outputDir);
            if (!dir.exists()) dir.mkdirs();
            File f = new File(dir, "client_" + runId + ".jsonl");
            this.pw = new PrintWriter(new FileWriter(f, false)); // false = overwrite
        } catch (IOException e) {
            throw new RuntimeException(e);
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
        try {
            pw.println(mapper.writeValueAsString(record));
            pw.flush();
        } catch (Exception e) {
            e.printStackTrace();
        }
    }

    public void close() {
        pw.close();
    }
}
