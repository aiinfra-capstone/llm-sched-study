# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.21", "grpcio-tools>=1.60"]
# ///
"""Contract conformance check — the guard on the six frozen artifacts (C-1..C-6).

Runs in CI on every PR. Four jobs:

  1. Every example in contracts/examples/ validates against its schema. This is
     what makes the fixture-first pattern of §11 trustworthy: if A's fake
     scheduler and B's fake worker both emit records that pass here, the two
     halves will join when they meet for real.

  2. scheduling.proto compiles. §12.2's mitigation for schema drift is a version
     field plus a loader that rejects unknown versions loudly — this is the
     cheaper half: the wire schema is never merged in a state that does not build.

  3. The second copy of C-1 is wire-identical to the frozen one. The control
     plane's Maven build compiles its own `scheduling.proto`, so the repository
     now holds the frozen artifact twice while `contract-v1` pins only one of
     them by content hash. Two files that are meant to be one file will drift,
     and the drift is invisible until a field number means different things on
     the two ends of a socket.

  4. Every property a *strict* record on the far side of the seam has to accept
     is declared there. This is §12's failure mode 2 turned into a test: a C-3
     field added on this side is a field Jackson throws on over there, and in
     Week 3 that took three weeks and 50 unreadable snapshots to surface. A name
     bound over there that no schema defines is printed as a NOTE rather than
     failed — that is a bug in the reader, not a non-conforming artifact.
     Name-level rather than a type checker, which is enough for both.

There is also a fifth thing it will do on request, which is not part of the CI gate:
validate an arbitrary file against the contract it claims to be. That exists because the
seam has two sides and only one of them runs this repository's test suite. Handing
somebody "your 42 records are invalid" is worth much less than handing them the command
that says so before they push.

Usage:
    uv run contracts/check.py                          # the four jobs above
    uv run contracts/check.py --validate FILE [FILE…]  # one file against its contract
    uv run contracts/check.py --validate FILE --schema log_scheduler.schema.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).parent
REPO = ROOT.parent
SCHEMAS = ROOT / "schemas"
EXAMPLES = ROOT / "examples"

# The frozen C-1 artifact, and the copy the control plane's Maven build compiles.
# `contract-v1` hashes the first; the socket speaks the second.
PROTO = ROOT / "scheduling.proto"
PROTO_COPIES: list[Path] = [REPO / "controlplane" / "src" / "main" / "proto" / "scheduling.proto"]

# Records on the far side of the seam that bind one of our schemas by field name.
# `strict` means the record has no @JsonIgnoreProperties(ignoreUnknown = true), so
# Jackson throws on any property the record does not declare — a missing name there is
# not a warning, it is every real file failing to parse. That is exactly how 0 of 50
# committed C-3 snapshots stopped loading in Week 3.
JAVA_BINDINGS: list[tuple[str, str, bool]] = [
    (
        "controlplane/src/main/java/com/sched/core/models/CostModelSnapshot.java",
        "cost_model.schema.json",
        True,
    ),
    (
        "controlplane/src/main/java/com/sched/core/models/Manifest.java",
        "manifest.schema.json",
        False,
    ),
]

# example file -> schema file.  JSONL examples are validated line by line.
PAIRS: list[tuple[str, str]] = [
    ("trace.sample.jsonl", "trace.schema.json"),
    ("manifest.engine_gap.sample.json", "manifest.schema.json"),
    ("cost_model.sample.json", "cost_model.schema.json"),
    ("client.sample.jsonl", "log_client.schema.json"),
    ("scheduler.sample.jsonl", "log_scheduler.schema.json"),
    ("worker.sample.jsonl", "log_worker.schema.json"),
    ("manifest.sample.json", "manifest.schema.json"),
]


def check_examples() -> list[str]:
    failures: list[str] = []
    for example_name, schema_name in PAIRS:
        example, schema_path = EXAMPLES / example_name, SCHEMAS / schema_name
        if not example.exists():
            failures.append(f"{example_name}: missing")
            continue

        validator = Draft202012Validator(json.loads(schema_path.read_text()))

        if example.suffix == ".jsonl":
            records = [
                (n, json.loads(line))
                for n, line in enumerate(example.read_text().splitlines(), start=1)
                if line.strip()
            ]
        else:
            records = [(1, json.loads(example.read_text()))]

        for lineno, record in records:
            for err in validator.iter_errors(record):
                where = "/".join(str(p) for p in err.absolute_path) or "<root>"
                failures.append(f"{example_name}:{lineno} at {where}: {err.message}")

        print(f"  {example_name:28s} -> {schema_name:28s} {len(records)} record(s)")
    return failures


def check_proto() -> list[str]:
    from grpc_tools import protoc

    with tempfile.TemporaryDirectory() as out:
        rc = protoc.main(
            [
                "protoc",
                f"-I{ROOT}",
                f"--python_out={out}",
                f"--grpc_python_out={out}",
                str(PROTO),
            ]
        )
    if rc != 0:
        return ["scheduling.proto: does not compile (see protoc output above)"]
    print("  scheduling.proto             -> compiles")
    return []


def _wire_shape(path: Path) -> dict[str, object]:
    """Everything about a .proto that two ends of a socket have to agree on.

    Comments, whitespace, field order, `package`, and every `option` are excluded
    deliberately. The control plane's copy carries `java_package` and friends and
    formats its fields differently, and neither of those changes a byte on the wire.
    What does change bytes is a field number, a type, a repeated/optional label, or the
    request or response type of an RPC — so those are what get compared.
    """
    from google.protobuf import descriptor_pb2
    from grpc_tools import protoc

    with tempfile.TemporaryDirectory() as out:
        desc = Path(out) / "d.bin"
        rc = protoc.main(["protoc", f"-I{path.parent}", f"--descriptor_set_out={desc}", str(path)])
        if rc != 0:
            raise ValueError(f"{path}: does not compile")
        fds = descriptor_pb2.FileDescriptorSet()
        fds.ParseFromString(desc.read_bytes())

    messages: dict[str, dict[int, tuple[str, int, int, str]]] = {}
    services: dict[str, dict[str, tuple[str, str, bool, bool]]] = {}
    for f in fds.file:
        for m in f.message_type:
            messages[m.name] = {
                fld.number: (fld.name, fld.type, fld.label, fld.type_name) for fld in m.field
            }
        for svc in f.service:
            services[svc.name] = {
                meth.name: (
                    meth.input_type.rsplit(".", 1)[-1],
                    meth.output_type.rsplit(".", 1)[-1],
                    meth.client_streaming,
                    meth.server_streaming,
                )
                for meth in svc.method
            }
    return {"messages": messages, "services": services}


def check_proto_copies() -> list[str]:
    """The frozen C-1 and the copy Maven compiles must describe the same wire.

    `contract-v1` pins `contracts/scheduling.proto` by content hash. It cannot pin a
    second file it does not know about, so without this the tag would keep certifying a
    proto that nothing on the socket actually speaks.
    """
    failures: list[str] = []
    try:
        frozen = _wire_shape(PROTO)
    except ValueError as exc:
        return [str(exc)]

    for copy in PROTO_COPIES:
        rel = copy.relative_to(REPO)
        if not copy.exists():
            print(f"  {rel!s:28s} -> absent (nothing to drift)")
            continue
        try:
            other = _wire_shape(copy)
        except ValueError as exc:
            failures.append(str(exc))
            continue

        for kind in ("messages", "services"):
            mine, theirs = frozen[kind], other[kind]
            for name in sorted(set(mine) | set(theirs)):
                if name not in theirs:
                    failures.append(f"{rel}: {kind[:-1]} {name!r} is missing")
                elif name not in mine:
                    failures.append(f"{rel}: {kind[:-1]} {name!r} is not in the frozen C-1")
                elif mine[name] != theirs[name]:
                    for key in sorted(set(mine[name]) | set(theirs[name])):
                        a, b = mine[name].get(key), theirs[name].get(key)
                        if a != b:
                            failures.append(
                                f"{rel}: {kind[:-1]} {name}[{key!r}] is {b!r}, frozen C-1 "
                                f"says {a!r}"
                            )
        if not failures:
            print(f"  {rel!s:28s} -> wire-identical to the frozen C-1")
    return failures


def _schema_property_names(node: object) -> set[str]:
    """Every property name anywhere in a schema, at any depth."""
    names: set[str] = set()
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            names |= set(props)
        for value in node.values():
            names |= _schema_property_names(value)
    elif isinstance(node, list):
        for item in node:
            names |= _schema_property_names(item)
    return names


def check_java_bindings() -> list[str]:
    """§12 failure mode 2, as a test rather than as a hope.

    Two directions, and they fail in opposite ways. A schema property a *strict* record
    does not declare makes Jackson throw on every real file — loud, but only once someone
    runs it against real data, which in Week 3 was three weeks after the field was added.
    A record field the schema never defined is worse: it parses, it is null forever, and
    whatever reads it quietly does the wrong thing.
    """
    failures: list[str] = []
    for java_rel, schema_name, strict in JAVA_BINDINGS:
        java = REPO / java_rel
        if not java.exists():
            print(f"  {java.name:28s} -> absent (not yet written)")
            continue
        declared = set(re.findall(r'@JsonProperty\("([^"]+)"\)', java.read_text()))
        schema = _schema_property_names(json.loads((SCHEMAS / schema_name).read_text()))

        # A name the schema never defined is a bug in the reader, not a violation of the
        # contract, so it is reported and not failed. The one exception is a strict record:
        # there, a schema property the record does not declare means every committed
        # example fails to parse, and an artifact that cannot be read by the far side of
        # the seam is not conforming in any useful sense.
        undefined = sorted(declared - schema)
        missing = sorted(schema - declared) if strict else []
        for name in missing:
            failures.append(
                f"{java_rel}: does not declare {name!r} from {schema_name}, and the "
                "record is strict — Jackson throws on every file that carries it"
            )
        for name in undefined:
            print(
                f"  NOTE {java.name}: binds {name!r}, which {schema_name} does not "
                "define — it parses to null on every file"
            )
        if not undefined and not missing:
            print(f"  {java.name:28s} -> {len(declared)} binding(s) agree with {schema_name}")
    return failures


# --------------------------------------------------------------------------------------
# --validate: one arbitrary file against the contract it claims to be
# --------------------------------------------------------------------------------------

# Filename to schema. The C-4 log names are fixed by the contract itself
# (`scheduler_{run_id}.jsonl` and so on), so detection is reliable for exactly the files
# people most often need to check, and `--schema` covers the rest. Order matters: the
# first pattern that matches wins, so the specific ones come before `*trace*`. Both
# the contract's own naming (`scheduler_{run_id}.jsonl`) and the sample files
# (`scheduler.sample.jsonl`) match, since people check both.
SCHEMA_BY_PATTERN: list[tuple[str, str]] = [
    (r"^scheduler[_.].*\.jsonl$", "log_scheduler.schema.json"),
    (r"^worker[_.].*\.jsonl$", "log_worker.schema.json"),
    (r"^client[_.].*\.jsonl$", "log_client.schema.json"),
    (r"^manifest.*\.json$", "manifest.schema.json"),
    (r"^(cost_model|cm_).*\.json$", "cost_model.schema.json"),
    (r".*trace.*\.jsonl$", "trace.schema.json"),
]


def detect_schema(path: Path) -> str | None:
    """Which contract a file is claiming to be, from its name. None when unguessable."""
    for pattern, schema in SCHEMA_BY_PATTERN:
        if re.match(pattern, path.name):
            return schema
    return None


def _records(path: Path) -> list[tuple[int, object]]:
    """(line number, record) for a JSON or JSONL file. Blank lines skipped."""
    if path.suffix == ".jsonl":
        return [
            (n, json.loads(line))
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
            if line.strip()
        ]
    return [(1, json.loads(path.read_text(encoding="utf-8")))]


def _messages(validator: Draft202012Validator, record: object) -> list[str]:
    """Readable errors for one record, including through a `oneOf`.

    Two of the six schemas are a `oneOf` over record types: C-2 discriminates header from
    request from admissibility, and C-4's scheduler log discriminates decision from
    completion. jsonschema reports a failure there by printing the whole instance and
    saying it "is not valid under any of the given schemas", which tells a reader nothing
    about which branch they were aiming at or what was wrong with it.

    So when a `oneOf` fails, group the sub-errors by branch and report only the branch
    that came closest. That branch is almost always the one intended: a record that meant
    to be a decision fails the completion branch on `type` alone and fails the decision
    branch on the fields actually wrong. Fewest errors is the better guess, and reporting
    every branch would bury the real problem under the alternatives nobody meant.
    """
    out: list[str] = []
    for err in validator.iter_errors(record):
        if not err.context:
            where = "/".join(str(p) for p in err.absolute_path) or "<root>"
            out.append(f"at {where}: {err.message}")
            continue
        branches: dict[object, list[str]] = {}
        for sub in err.context:
            key = sub.schema_path[0] if sub.schema_path else 0
            where = "/".join(str(p) for p in sub.absolute_path) or "<root>"
            branches.setdefault(key, []).append(f"at {where}: {sub.message}")
        best = min(branches.values(), key=len)
        out.extend(best)
    return out


def validate_file(path: Path, schema_name: str | None = None) -> list[str]:
    """Validate one file, printing a per-file report. Returns its failures."""
    schema_name = schema_name or detect_schema(path)
    if schema_name is None:
        print(f"  {path.name}: cannot tell which contract this is; pass --schema")
        return [f"{path.name}: no schema given and none could be inferred from the name"]

    schema_path = SCHEMAS / schema_name
    if not schema_path.exists():
        print(f"  {path.name}: no such schema {schema_name}")
        return [f"{path.name}: unknown schema {schema_name!r}"]

    validator = Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))
    try:
        records = _records(path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  {path.name}: {type(exc).__name__}: {exc}")
        return [f"{path.name}: {exc}"]

    # Grouped by message rather than listed per record: 42 records failing the same way
    # is one problem, and printing it 42 times hides the second problem underneath it.
    seen: dict[str, tuple[int, int]] = {}
    failures: list[str] = []
    for lineno, record in records:
        msgs = _messages(validator, record)
        for m in msgs:
            count, first = seen.get(m, (0, lineno))
            seen[m] = (count + 1, first)
        failures.extend(f"{path.name}:{lineno} {m}" for m in msgs)

    bad = len({ln for ln, rec in records if _messages(validator, rec)})
    status = "ok" if not seen else f"{bad}/{len(records)} record(s) fail"
    print(f"  {path.name:34s} -> {schema_name:28s} {len(records):4d} record(s)  {status}")
    for message, (count, first) in sorted(seen.items(), key=lambda kv: -kv[1][0]):
        times = "1 record" if count == 1 else f"{count} records"
        print(f"      [{times}, first at line {first}]  {message}")
    return failures


def validate_paths(paths: list[Path], schema_name: str | None = None) -> int:
    print("Validating against the frozen contracts:")
    failures: list[str] = []
    for path in paths:
        failures += validate_file(path, schema_name)
    print()
    if failures:
        print(f"FAIL — {len(failures)} conformance error(s) across {len(paths)} file(s).")
        return 1
    print(f"OK — {len(paths)} file(s) conform.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Contract conformance check for the six frozen artifacts (C-1..C-6)"
    )
    ap.add_argument(
        "--validate",
        nargs="+",
        type=Path,
        metavar="FILE",
        help="validate these files against the contract their names imply, and exit. "
        "Not part of the CI gate: this is for checking output before it is pushed.",
    )
    ap.add_argument(
        "--schema",
        help="schema file name to validate against, overriding detection "
        "(e.g. log_scheduler.schema.json)",
    )
    args = ap.parse_args(argv)

    if args.validate:
        return validate_paths(args.validate, args.schema)

    print("C-2..C-6 — examples against schemas:")
    failures = check_examples()
    print("\nC-1 — wire schema:")
    failures += check_proto()
    failures += check_proto_copies()
    print("\nC-3 / C-6 — the seam's other reader:")
    failures += check_java_bindings()

    if failures:
        print(f"\nFAIL — {len(failures)} contract violation(s):", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print("\nOK — all six contract artifacts conform.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
