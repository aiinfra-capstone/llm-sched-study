package com.sched.core;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.FileWriter;
import java.io.PrintWriter;
import java.io.IOException;
import java.io.File;
import java.io.UncheckedIOException;
import com.fasterxml.jackson.core.JsonProcessingException;

public class DecisionLogger {
    private final PrintWriter pw;
    private final File file;
    private final ObjectMapper mapper = new ObjectMapper();

    /**
     * @throws UncheckedIOException when the log file cannot be opened. A scheduler that ran
     *     without its log would produce a run nobody can join, so it does not start (3.6).
     */
    public DecisionLogger(String outputDir, String runId) {
        File dir = new File(outputDir);
        if (!dir.exists()) dir.mkdirs();
        this.file = new File(dir, "scheduler_" + runId + ".jsonl");
        try {
            this.pw = new PrintWriter(new FileWriter(file, false));
        } catch (IOException e) {
            throw new UncheckedIOException("cannot open the scheduler log " + file, e);
        }
    }

    public synchronized void logRecord(Object record) {
        String line;
        try {
            line = mapper.writeValueAsString(record);
        } catch (JsonProcessingException e) {
            throw new UncheckedIOException(e);
        }
        writeLine(pw, line, file);
    }

    public synchronized void close() {
        pw.close();
    }

    /**
     * Write one line or throw. A PrintWriter never throws on its own, so a full disk or a
     * closed stream would otherwise drop records without a word and the run would look
     * complete (3.6).
     */
    static void writeLine(PrintWriter pw, String line, File f) {
        pw.println(line);
        pw.flush();
        if (pw.checkError()) {
            throw new UncheckedIOException(new IOException("could not write to " + f));
        }
    }
}