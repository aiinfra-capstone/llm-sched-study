package com.sched.core;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.sched.core.ClientLogger.ClientRecord;
import com.sched.core.WorkerLogger.WorkerRecord;
import com.sched.core.models.SchedulerLogRecords.CompletionObservedRecord;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * J10: a log that cannot be written stops the run instead of dropping records.
 *
 * Each logger used to catch the IOException, leave its writer null and log nothing, so a
 * run whose log directory was wrong finished with no decisions on disk and a manifest that
 * said it was valid. A PrintWriter also swallows write errors of its own, so a closed or
 * failed stream lost records the same way.
 */
class LoggersTest {

    private static final ClientRecord CLIENT = new ClientRecord("run1", "r000001", 1.5, 1.5, 0.0,
            1_000_000_000L, "ok", 32, "n1", "n1", 0L);
    private static final WorkerRecord WORKER = new WorkerRecord("run1", "r000001", "n1", "llamacpp",
            0L, 1_000_000_000L, 64, 32, 1, 0, 0.25, "ok");
    private static final CompletionObservedRecord OBSERVED =
            new CompletionObservedRecord("completion_observed", "run1", "r000001", "n1", "sim_completion", 0L);

    /** A path that exists as a regular file, so no log can be opened beneath it. */
    private static String notADirectory(Path dir) throws IOException {
        Path file = dir.resolve("not_a_dir");
        Files.writeString(file, "");
        return file.toString();
    }

    @Test
    @DisplayName("the scheduler and client logs refuse to start when their file cannot be opened")
    void decisionAndClientLoggersThrowOnOpen(@TempDir Path dir) throws IOException {
        String bad = notADirectory(dir);
        UncheckedIOException d = assertThrows(UncheckedIOException.class, () -> new DecisionLogger(bad, "run1"));
        assertTrue(d.getMessage().contains("scheduler_run1.jsonl"), d.getMessage());
        UncheckedIOException c = assertThrows(UncheckedIOException.class, () -> new ClientLogger(bad, "run1"));
        assertTrue(c.getMessage().contains("client_run1.jsonl"), c.getMessage());
    }

    @Test
    @DisplayName("the worker log, opened per node on first use, throws on that first record")
    void workerLoggerThrowsOnFirstRecord(@TempDir Path dir) throws IOException {
        WorkerLogger w = new WorkerLogger(notADirectory(dir), "run1");
        UncheckedIOException e = assertThrows(UncheckedIOException.class, () -> w.logRecord(WORKER));
        assertTrue(e.getMessage().contains("worker_n1_run1.jsonl"), e.getMessage());
    }

    @Test
    @DisplayName("a record written to a closed log throws rather than vanishing")
    void aClosedLogRefusesARecord(@TempDir Path dir) throws IOException {
        DecisionLogger d = new DecisionLogger(dir.toString(), "run1");
        d.logRecord(OBSERVED);
        d.close();
        assertThrows(UncheckedIOException.class, () -> d.logRecord(OBSERVED));

        ClientLogger c = new ClientLogger(dir.toString(), "run1");
        c.logRecord(CLIENT);
        c.close();
        assertThrows(UncheckedIOException.class, () -> c.logRecord(CLIENT));

        WorkerLogger w = new WorkerLogger(dir.toString(), "run1");
        w.logRecord(WORKER);
        w.close();
        assertThrows(UncheckedIOException.class, () -> w.logRecord(WORKER));

        // What was written before the close is on disk, one line each.
        assertEquals(1, Files.readAllLines(dir.resolve("scheduler_run1.jsonl")).size());
        assertEquals(1, Files.readAllLines(dir.resolve("client_run1.jsonl")).size());
        assertEquals(1, Files.readAllLines(dir.resolve("worker_n1_run1.jsonl")).size());
    }

    @Test
    @DisplayName("a missing log directory is created, as before")
    void aMissingDirectoryIsCreated(@TempDir Path dir) {
        Path nested = dir.resolve("a").resolve("b");
        new DecisionLogger(nested.toString(), "run1").close();
        new ClientLogger(nested.toString(), "run1").close();
        assertTrue(Files.isRegularFile(nested.resolve("scheduler_run1.jsonl")));
        assertTrue(Files.isRegularFile(nested.resolve("client_run1.jsonl")));
    }
}
