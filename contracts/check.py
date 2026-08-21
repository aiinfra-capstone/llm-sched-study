# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.21", "grpcio-tools>=1.60"]
# ///
"""Contract conformance check — the guard on the six frozen artifacts (C-1..C-6).

Runs in CI on every PR. Two jobs:

  1. Every example in contracts/examples/ validates against its schema. This is
     what makes the fixture-first pattern of §11 trustworthy: if A's fake
     scheduler and B's fake worker both emit records that pass here, the two
     halves will join when they meet for real.

  2. scheduling.proto compiles. §12.2's mitigation for schema drift is a version
     field plus a loader that rejects unknown versions loudly — this is the
     cheaper half: the wire schema is never merged in a state that does not build.

Usage:  uv run contracts/check.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).parent
SCHEMAS = ROOT / "schemas"
EXAMPLES = ROOT / "examples"

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
                str(ROOT / "scheduling.proto"),
            ]
        )
    if rc != 0:
        return ["scheduling.proto: does not compile (see protoc output above)"]
    print("  scheduling.proto             -> compiles")
    return []


def main() -> int:
    print("C-2..C-6 — examples against schemas:")
    failures = check_examples()
    print("\nC-1 — wire schema:")
    failures += check_proto()

    if failures:
        print(f"\nFAIL — {len(failures)} contract violation(s):", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print("\nOK — all six contract artifacts conform.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
