package com.sched.core.models;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sched.core.ClientLogger.ClientRecord;
import com.sched.core.WorkerLogger.WorkerRecord;
import com.sched.core.models.SchedulerLogRecords.Candidate;
import com.sched.core.models.SchedulerLogRecords.CompletionObservedRecord;
import com.sched.core.models.SchedulerLogRecords.DecisionRecord;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/**
 * C-4 from the writing side.
 *
 * contracts/check.py validates the committed sample files against these schemas and checks
 * the two records the control plane READS. Nothing checked the records it WRITES, and every
 * C-4 schema sets additionalProperties: false, so a renamed field here does not fail loudly.
 * It produces a log that the pipeline joins into nulls and a figure that quietly loses a
 * column, which is the failure mode this repo is most afraid of.
 *
 * These compare the JSON Jackson actually emits against the schema's own property list, so
 * the check is on the bytes that reach disk rather than on the annotations.
 */
class LogRecordSchemaTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static Path schemas() {
        Path here = Path.of("").toAbsolutePath();
        for (Path p = here; p != null; p = p.getParent()) {
            Path candidate = p.resolve("contracts").resolve("schemas");
            if (Files.isDirectory(candidate)) return candidate;
        }
        throw new IllegalStateException("no contracts/schemas above " + here);
    }

    private static JsonNode schema(String name) throws IOException {
        return MAPPER.readTree(schemas().resolve(name).toFile());
    }

    private static Set<String> names(JsonNode arrayOrObject) {
        Set<String> out = new TreeSet<>();
        arrayOrObject.fieldNames().forEachRemaining(out::add);
        return out;
    }

    private static Set<String> required(JsonNode node) {
        Set<String> out = new TreeSet<>();
        node.get("required").forEach(n -> out.add(n.asText()));
        return out;
    }

    private static Set<String> emitted(Object record) throws IOException {
        return names(MAPPER.readTree(MAPPER.writeValueAsString(record)));
    }

    /** Everything the schema allows, and nothing it does not; every required key present. */
    private static void assertConforms(Object record, JsonNode def, String what) throws IOException {
        Set<String> keys = emitted(record);
        Set<String> allowed = names(def.get("properties"));

        Set<String> extra = new TreeSet<>(keys);
        extra.removeAll(allowed);
        assertTrue(extra.isEmpty(),
                what + " emits " + extra + ", which the schema forbids (additionalProperties: false)");

        Set<String> missing = new TreeSet<>(required(def));
        missing.removeAll(keys);
        assertTrue(missing.isEmpty(), what + " is missing required " + missing);
    }

    @Test
    @DisplayName("a decision record matches the C-4 decision schema exactly")
    void decisionRecordConforms() throws IOException {
        DecisionRecord rec = new DecisionRecord("decision", "run1", "r000001", 0L, "wjsq",
                0.0, 12345L, "n1", 0.5,
                List.of(new Candidate("n1", 0, 1, 100.0, 0L, true, 0.02)));

        assertConforms(rec, schema("log_scheduler.schema.json").get("$defs").get("decision"), "DecisionRecord");
    }

    @Test
    @DisplayName("a candidate matches the C-4 candidate schema exactly")
    void candidateConforms() throws IOException {
        // F-3 in full. Without estimate_age_ms per candidate, H3 is unanalysable, so this is
        // the one nested shape the whole staleness result depends on.
        JsonNode candidateSchema = schema("log_scheduler.schema.json")
                .get("$defs").get("decision").get("properties").get("candidates").get("items");

        assertConforms(new Candidate("n1", 2, 1, 100.0, 45L, true, 0.03), candidateSchema, "Candidate");
    }

    @Test
    @DisplayName("a completion_observed record matches its schema exactly")
    void completionObservedConforms() throws IOException {
        CompletionObservedRecord rec =
                new CompletionObservedRecord("completion_observed", "run1", "r000001", "n1", "sim_event", 0L);

        assertConforms(rec, schema("log_scheduler.schema.json").get("$defs").get("completion_observed"),
                "CompletionObservedRecord");
    }

    @Test
    @DisplayName("a worker record matches the C-4 worker schema")
    void workerRecordConforms() throws IOException {
        WorkerRecord rec = new WorkerRecord("run1", "r000001", "n1", "llamacpp",
                0L, 1_000_000_000L, 64, 32, 1, 0, 0.25, "ok");

        assertConforms(rec, schema("log_worker.schema.json"), "WorkerRecord");
    }

    @Test
    @DisplayName("a client record matches the C-4 client schema")
    void clientRecordConforms() throws IOException {
        ClientRecord rec = new ClientRecord("run1", "r000001", 1.5, 1.5, 0.0,
                1_000_000_000L, "ok", 32, "n1", "n1", 0L);

        assertConforms(rec, schema("log_client.schema.json"), "ClientRecord");
    }

    @Test
    @DisplayName("the type discriminators are the literals the schema pins")
    void typeDiscriminatorsAreExact() throws IOException {
        // The scheduler log is a oneOf keyed on `type`, so a record carrying anything else
        // matches neither branch and fails validation for the whole file rather than the line.
        JsonNode defs = schema("log_scheduler.schema.json").get("$defs");

        assertEquals("decision", defs.get("decision").get("properties").get("type").get("const").asText());
        assertEquals("completion_observed",
                defs.get("completion_observed").get("properties").get("type").get("const").asText());
    }

    @Test
    @DisplayName("the policy name written to the log is one the schema's enum allows")
    void policyNamesMatchTheSchemaEnum() throws IOException {
        // Same five names as Policies.fromName. If the two lists ever diverge, a run completes
        // and then its log fails validation, which is the most expensive moment to find out.
        Set<String> allowed = new TreeSet<>();
        schema("log_scheduler.schema.json").get("$defs").get("decision")
                .get("properties").get("policy").get("enum").forEach(n -> allowed.add(n.asText()));

        assertEquals(Set.of("round_robin", "jsq", "static_weighted", "wjsq", "threshold"), allowed);

        for (String name : allowed) {
            com.sched.core.policies.Policies.fromName(
                    name, new java.util.concurrent.atomic.AtomicInteger(0), 1.0);
        }
    }

    @Test
    @DisplayName("a rejected dispatch still serialises, with nulls rather than omissions")
    void rejectedDispatchStillConforms() throws IOException {
        // chosen_node and tie_break_draw are nullable in the schema but still required, so the
        // no-admissible-node path has to emit them as null rather than drop them.
        DecisionRecord rec = new DecisionRecord("decision", "run1", "r000001", 7L, "threshold",
                30.0, 900L, null, null, List.of());

        JsonNode def = schema("log_scheduler.schema.json").get("$defs").get("decision");
        assertConforms(rec, def, "rejected DecisionRecord");

        JsonNode json = MAPPER.readTree(MAPPER.writeValueAsString(rec));
        assertTrue(json.get("chosen_node").isNull());
        assertTrue(json.get("tie_break_draw").isNull());
    }
}
