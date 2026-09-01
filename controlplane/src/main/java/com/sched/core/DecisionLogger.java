package com.sched.core;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sched.core.models.SchedulerLogRecords.Record;
import java.io.FileWriter;
import java.io.PrintWriter;
import java.io.IOException;
import java.io.File;

public class DecisionLogger {
    private PrintWriter pw;
    private final ObjectMapper mapper = new ObjectMapper();

    public DecisionLogger(String outputDir, String runId) {
        try {
            File dir = new File(outputDir);
            if (!dir.exists()) dir.mkdirs();
            File f = new File(dir, "scheduler_" + runId + ".jsonl");
            this.pw = new PrintWriter(new FileWriter(f, false)); // false = truncate
        } catch (IOException e) {
            e.printStackTrace();
        }
    }

    public synchronized void logRecord(Record record) {
        try {
            if (pw != null) {
                pw.println(mapper.writeValueAsString(record));
                pw.flush();
            }
        } catch (Exception e) {
            e.printStackTrace();
        }
    }

    public void close() {
        if (pw != null) {
            pw.close();
        }
    }
}