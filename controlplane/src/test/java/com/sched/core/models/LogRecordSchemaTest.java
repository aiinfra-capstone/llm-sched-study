package com.sched.core.models;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.sched.ContractCheck;
import com.sched.core.ClientLogger.ClientRecord;
import com.sched.core.WorkerLogger.WorkerRecord;
import com.sched.core.models.SchedulerLogRecords.Candidate;
import com.sched.core.models.SchedulerLogRecords.CompletionObservedRecord;
import com.sched.core.models.SchedulerLogRecords.DecisionRecord;
import com.sched.core.models.SchedulerLogRecords.HeartbeatSummaryRecord;
import java.io.IOException;
import java.util.List;
import java.util.Map;
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

    private static JsonNode schema(String name) throws IOException {
        return ContractCheck.schema(name);
    }

    /** Keys, types and enum or const values, on the JSON Jackson actually emits (J1). */
    private static void assertConforms(Object record, JsonNode def, String what) throws IOException {
        ContractCheck.assertConforms(record, def, what);
    }

    private static List<String> problems(Object record, JsonNode def) throws IOException {
        return ContractCheck.problems(
                ContractCheck.MAPPER.readTree(ContractCheck.MAPPER.writeValueAsString(record)), def, "record");
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
    @DisplayName("a completion_observed record matches its schema exactly, from either vehicle")
    void completionObservedConforms() throws IOException {
        JsonNode def = schema("log_scheduler.schema.json").get("$defs").get("completion_observed");
        for (String source : List.of("completion_rpc", "sim_completion")) {
            CompletionObservedRecord rec =
                    new CompletionObservedRecord("completion_observed", "run1", "r000001", "n1", source, 0L);
            assertConforms(rec, def, "CompletionObservedRecord from " + source);
        }
    }

    @Test
    @DisplayName("a heartbeat_summary record matches its schema exactly, at either point")
    void heartbeatSummaryConforms() throws IOException {
        JsonNode def = schema("log_scheduler.schema.json").get("$defs").get("heartbeat_summary");
        for (String at : List.of("end_run", "shutdown")) {
            assertConforms(new HeartbeatSummaryRecord("heartbeat_summary", "run1", "n1", 812L, 3L, 0L, at),
                    def, "HeartbeatSummaryRecord at " + at);
        }
        assertFalse(problems(new HeartbeatSummaryRecord("heartbeat_summary", "run1", "n1", 812L, 3L, 0L,
                "end_of_run"), def).isEmpty(), "an `at` outside the enum");
    }

    @Test
    @DisplayName("a value outside a schema enum fails, not only a wrong key")
    void offEnumValuesAreCaught() throws IOException {
        // Key names match in every one of these. Before J1 all four passed.
        JsonNode defs = schema("log_scheduler.schema.json").get("$defs");
        assertFalse(problems(new CompletionObservedRecord("completion_observed", "run1", "r1", "n1",
                "sim_event", 0L), defs.get("completion_observed")).isEmpty(), "source sim_event");
        assertFalse(problems(new CompletionObservedRecord("completion", "run1", "r1", "n1",
                "completion_rpc", 0L), defs.get("completion_observed")).isEmpty(), "type completion");
        assertFalse(problems(new DecisionRecord("decision", "run1", "r1", 0L, "least_loaded", 0.0, 1L,
                "n1", 0.5, List.of()), defs.get("decision")).isEmpty(), "policy least_loaded");
        assertFalse(problems(new ClientRecord("run1", "r1", 1.5, 1.5, 0.0, 1L, "dropped", 0, null, null, 0L),
                schema("log_client.schema.json")).isEmpty(), "client status dropped with null nodes");
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
        assertEquals("heartbeat_summary",
                defs.get("heartbeat_summary").get("properties").get("type").get("const").asText());
    }

    @Test
    @DisplayName("the policy name written to the log is one the schema's enum allows")
    void policyNamesMatchTheSchemaEnum() throws IOException {
        // Same eight names as Policies.fromName. If the two lists ever diverge, a run completes
        // and then its log fails validation, which is the most expensive moment to find out.
        Set<String> allowed = new TreeSet<>();
        schema("log_scheduler.schema.json").get("$defs").get("decision")
                .get("properties").get("policy").get("enum").forEach(n -> allowed.add(n.asText()));

        assertEquals(Set.of("round_robin", "jsq", "jsq_fastfirst", "static_weighted",
                "static_weighted_wrr", "wjsq", "threshold", "ect"), allowed);

        // ECT states its mode in the run's config and has no default (3.5); the other seven
        // ignore the key.
        for (String name : allowed) {
            com.sched.core.policies.Policies.fromName(
                    name, new java.util.concurrent.atomic.AtomicInteger(0), 1.0,
                    Map.of(), Map.of(), Map.of("ect_mode", "known"));
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

        JsonNode json = ContractCheck.MAPPER.readTree(ContractCheck.MAPPER.writeValueAsString(rec));
        assertTrue(json.get("chosen_node").isNull());
        assertTrue(json.get("tie_break_draw").isNull());
    }
}
