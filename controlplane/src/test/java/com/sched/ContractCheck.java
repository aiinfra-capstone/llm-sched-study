package com.sched;

import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

/**
 * A record checked against its C-4 schema definition on keys, types and values.
 *
 * Key names alone let a record through that says {@code "source": "sim_event"} or
 * {@code "status": "dropped"}, and every C-4 schema pins those with an enum. The check here
 * walks each emitted field against its property: the JSON type, then the enum or const, then
 * into nested objects and arrays of objects. It is not a full JSON Schema validator (no $ref,
 * no oneOf, no numeric bounds); it covers what the control plane's records use.
 */
public final class ContractCheck {
    public static final ObjectMapper MAPPER = new ObjectMapper();

    private ContractCheck() {}

    /** The repository's contracts/ directory, found by walking up from the working directory. */
    public static Path contracts() {
        Path here = Path.of("").toAbsolutePath();
        for (Path p = here; p != null; p = p.getParent()) {
            Path candidate = p.resolve("contracts");
            if (Files.isDirectory(candidate.resolve("schemas"))) return candidate;
        }
        throw new IllegalStateException("no contracts/schemas above " + here);
    }

    public static JsonNode schema(String file) throws IOException {
        return MAPPER.readTree(contracts().resolve("schemas").resolve(file).toFile());
    }

    /** One entry of a schema's {@code $defs}, such as the scheduler log's "decision". */
    public static JsonNode def(String file, String name) throws IOException {
        return schema(file).get("$defs").get(name);
    }

    /** Serialise with the mapper the loggers use, then check the bytes that would reach disk. */
    public static void assertConforms(Object record, JsonNode def, String what) throws IOException {
        assertConforms(MAPPER.readTree(MAPPER.writeValueAsString(record)), def, what);
    }

    public static void assertConforms(JsonNode record, JsonNode def, String what) {
        List<String> problems = new ArrayList<>();
        check(record, def, what, problems);
        assertTrue(problems.isEmpty(), String.join("; ", problems));
    }

    /** What is wrong with the record, empty when it conforms. */
    public static List<String> problems(JsonNode record, JsonNode def, String what) {
        List<String> problems = new ArrayList<>();
        check(record, def, what, problems);
        return problems;
    }

    private static void check(JsonNode value, JsonNode def, String where, List<String> out) {
        if (def.has("type") && !typeMatches(value, def.get("type"))) {
            out.add(where + " is " + value.getNodeType() + ", schema says " + def.get("type"));
            return;
        }
        if (def.has("const") && !same(def.get("const"), value)) {
            out.add(where + " is " + value + ", schema pins " + def.get("const"));
        }
        if (def.has("enum")) {
            boolean found = false;
            for (JsonNode allowed : def.get("enum")) found |= same(allowed, value);
            if (!found) out.add(where + " is " + value + ", not one of " + def.get("enum"));
        }
        if (value.isObject() && def.has("properties")) {
            JsonNode props = def.get("properties");
            Set<String> keys = new TreeSet<>();
            value.fieldNames().forEachRemaining(keys::add);
            for (String key : keys) {
                if (!props.has(key)) {
                    out.add(where + " emits " + key + ", which the schema forbids");
                } else {
                    check(value.get(key), props.get(key), where + "." + key, out);
                }
            }
            if (def.has("required")) {
                for (JsonNode r : def.get("required")) {
                    if (!value.has(r.asText())) out.add(where + " is missing required " + r.asText());
                }
            }
        }
        if (value.isArray() && def.has("items")) {
            for (int i = 0; i < value.size(); i++) {
                check(value.get(i), def.get("items"), where + "[" + i + "]", out);
            }
        }
    }

    private static boolean typeMatches(JsonNode value, JsonNode type) {
        if (type.isArray()) {
            for (Iterator<JsonNode> it = type.elements(); it.hasNext(); ) {
                if (typeMatches(value, it.next())) return true;
            }
            return false;
        }
        return switch (type.asText()) {
            case "string" -> value.isTextual();
            case "integer" -> value.isIntegralNumber()
                    || (value.isNumber() && value.decimalValue().stripTrailingZeros().scale() <= 0);
            case "number" -> value.isNumber();
            case "boolean" -> value.isBoolean();
            case "null" -> value.isNull();
            case "array" -> value.isArray();
            case "object" -> value.isObject();
            default -> false;
        };
    }

    private static boolean same(JsonNode a, JsonNode b) {
        if (a.isNumber() && b.isNumber()) return a.decimalValue().compareTo(b.decimalValue()) == 0;
        return a.equals(b);
    }

    /** Every line of a JSONL file, parsed. */
    public static List<JsonNode> readJsonl(Path file) throws IOException {
        List<JsonNode> out = new ArrayList<>();
        for (String line : Files.readAllLines(file)) {
            if (!line.isBlank()) out.add(MAPPER.readTree(line));
        }
        return out;
    }

    /** Records of one scheduler log type, in file order. */
    public static List<JsonNode> ofType(List<JsonNode> records, String type) {
        return records.stream().filter(r -> type.equals(r.path("type").asText())).toList();
    }

    /** Convenience for building the small JSON documents the tests write. */
    public static String json(Map<String, ?> value) throws IOException {
        return MAPPER.writerWithDefaultPrettyPrinter().writeValueAsString(value);
    }
}
