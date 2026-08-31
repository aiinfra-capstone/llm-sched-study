package com.sched.core.models;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.File;
import java.io.IOException;

public class ManifestParser {
    private static final ObjectMapper mapper = new ObjectMapper();

    public static Manifest parse(String filePath) throws IOException {
        return mapper.readValue(new File(filePath), Manifest.class);
    }
}