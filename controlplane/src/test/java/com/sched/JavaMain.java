package com.sched;

import static org.junit.jupiter.api.Assertions.fail;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;

/**
 * A main class run in its own JVM, on this test's classpath.
 *
 * SimApp and LiveSchedulerApp refuse a bad run with System.exit, which is the behaviour the
 * harness relies on (it reads the exit code). Called in-process that exit would end the test
 * JVM along with every test after it, so the refusals are exercised in a child process and
 * the exit code is what the test asserts.
 */
public final class JavaMain {
    private JavaMain() {}

    public record Result(int exitCode, String output) {}

    /** A started child JVM whose output is collected as it arrives. */
    public static final class Running {
        public final Process process;
        private final StringBuilder output = new StringBuilder();
        private final Thread pump;

        Running(Process process) {
            this.process = process;
            this.pump = new Thread(() -> {
                try (BufferedReader r = new BufferedReader(
                        new InputStreamReader(process.getInputStream(), StandardCharsets.UTF_8))) {
                    String line;
                    while ((line = r.readLine()) != null) {
                        synchronized (output) {
                            output.append(line).append('\n');
                            output.notifyAll();
                        }
                    }
                } catch (IOException e) {
                    throw new UncheckedIOException(e);
                }
            });
            pump.setDaemon(true);
            pump.start();
        }

        public String output() {
            synchronized (output) {
                return output.toString();
            }
        }

        /** Wait until a printed line contains {@code marker}; fail if the process exits first. */
        public void awaitLine(String marker, Duration timeout) throws InterruptedException {
            long deadline = System.nanoTime() + timeout.toNanos();
            synchronized (output) {
                while (!output.toString().contains(marker)) {
                    long left = deadline - System.nanoTime();
                    if (left <= 0 || (!process.isAlive() && !pump.isAlive())) {
                        fail("no line containing \"" + marker + "\" (alive: " + process.isAlive()
                                + "):\n" + output);
                    }
                    output.wait(Math.max(1, Math.min(200, TimeUnit.NANOSECONDS.toMillis(left))));
                }
            }
        }

        /** SIGTERM, as the harness stops the scheduler, then wait for the exit code. */
        public int terminate(Duration timeout) throws InterruptedException {
            process.destroy();
            if (!process.waitFor(timeout.toMillis(), TimeUnit.MILLISECONDS)) {
                process.destroyForcibly();
                fail("the process did not exit on SIGTERM:\n" + output());
            }
            pump.join(timeout.toMillis());
            return process.exitValue();
        }
    }

    /** Start {@code main} with {@code args}; stdout and stderr are merged. */
    public static Running start(Class<?> main, Path workDir, String... args) throws IOException {
        List<String> cmd = new ArrayList<>();
        cmd.add(Path.of(System.getProperty("java.home"), "bin", "java").toString());
        cmd.add("-cp");
        cmd.add(System.getProperty("java.class.path"));
        cmd.add(main.getName());
        cmd.addAll(List.of(args));
        Process p = new ProcessBuilder(cmd).directory(workDir.toFile()).redirectErrorStream(true).start();
        return new Running(p);
    }

    /** Run to completion and return the exit code and everything it printed. */
    public static Result run(Class<?> main, Path workDir, String... args)
            throws IOException, InterruptedException {
        Running r = start(main, workDir, args);
        if (!r.process.waitFor(120, TimeUnit.SECONDS)) {
            r.process.destroyForcibly();
            fail(main.getSimpleName() + " did not exit within 120 s:\n" + r.output());
        }
        r.pump.join(10_000);
        return new Result(r.process.exitValue(), r.output());
    }
}
