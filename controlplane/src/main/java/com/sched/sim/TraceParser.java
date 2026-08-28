package com.sched.sim;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sched.core.models.TraceRequest;
import java.io.BufferedReader;
import java.io.FileReader;
import java.io.IOException;
import java.util.ArrayList;
import java.util.List;

public class TraceParser {
    private static final ObjectMapper mapper = new ObjectMapper();

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