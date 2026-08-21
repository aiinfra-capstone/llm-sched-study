package com.sched.core.models;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.File;
import java.io.IOException;

public class CostModelParser {
    // A single, thread-safe ObjectMapper instance for the application
    private static final ObjectMapper mapper = new ObjectMapper();

    /**
     * Parses a C-3 JSON snapshot file into a CostModelSnapshot record.
     */
    public static CostModelSnapshot parse(String filePath) throws IOException {
        return mapper.readValue(new File(filePath), CostModelSnapshot.class);
    }
}