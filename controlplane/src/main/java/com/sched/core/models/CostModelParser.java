package com.sched.core.models;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.File;
import java.io.IOException;

public class CostModelParser {
    private static final ObjectMapper mapper = new ObjectMapper();

    public static CostModelSnapshot parse(File file) throws IOException {
        CostModelSnapshot snap = mapper.readValue(file, CostModelSnapshot.class);

        // §12.2 strict schema version validation
        if (snap.costModelSchema() != 1) {
            throw new IOException(
                    "cost_model_schema " + snap.costModelSchema() + " is not 1 in file: " + file.getName());
        }

        return snap;
    }
}