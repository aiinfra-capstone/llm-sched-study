package com.sched.sim;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sched.core.models.TraceRequest;
import java.io.BufferedReader;
import java.io.FileReader;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;

public class TraceParser {
    private static final ObjectMapper mapper = new ObjectMapper();

    /** The only C-2 version this parser reads (trace.schema.json, trace_schema const). */
    public static final int TRACE_SCHEMA = 1;

    /**
     * Parse a trace only if it is the one the manifest names (3.2): its SHA-256 over the
     * whole file equals {@code expectedSha256}, and its header carries a trace_schema this
     * parser knows. A replay of a different trace would still run and still look like a
     * replay of the hardware run, so a mismatch refuses instead.
     *
     * @throws IllegalArgumentException on a hash or schema mismatch, or no header line
     */
    public static List<TraceRequest> parseVerified(String filePath, String expectedSha256)
            throws IOException {
        byte[] bytes = Files.readAllBytes(Path.of(filePath));
        String actual;
        try {
            actual = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
        if (expectedSha256 == null || !expectedSha256.equalsIgnoreCase(actual)) {
            throw new IllegalArgumentException("trace " + filePath + " has sha256 " + actual
                + " but the manifest names " + expectedSha256);
        }
        String first;
        try (BufferedReader br = new BufferedReader(new FileReader(filePath))) {
            first = br.readLine();
        }
        com.fasterxml.jackson.databind.JsonNode header = first == null ? null : mapper.readTree(first);
        if (header == null || !"header".equals(header.path("record").asText())) {
            throw new IllegalArgumentException("trace " + filePath + " does not start with a header record");
        }
        if (!header.path("trace_schema").isInt() || header.get("trace_schema").asInt() != TRACE_SCHEMA) {
            throw new IllegalArgumentException("trace " + filePath + " has trace_schema "
                + header.get("trace_schema") + "; this simulator reads " + TRACE_SCHEMA);
        }
        return parse(filePath);
    }

    public static List<TraceRequest> parse(String filePath) throws IOException {
        List<TraceRequest> requests = new ArrayList<>();
        try (BufferedReader br = new BufferedReader(new FileReader(filePath))) {
            String line;
            while ((line = br.readLine()) != null) {
                TraceRequest req = mapper.readValue(line, TraceRequest.class);
                if ("req".equals(req.record())) {
                    requests.add(req);
                }
            }
        }
        return requests;
    }
}