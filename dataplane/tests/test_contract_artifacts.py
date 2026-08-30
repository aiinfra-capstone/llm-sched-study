"""The contract artifacts I own, checked from inside my own suite.

`contracts/check.py` already validates every example against its schema in CI. This file
is not a second copy of that: it guards the things check.py cannot, and it guards them
from the data plane's side of the seam, so a contract change that breaks my half fails in
my half's test run rather than in a job somebody else reads.

What is asserted here:

  * every schema is itself a well-formed Draft 2020-12 schema — a typo in a `$ref` makes
    a schema that validates everything, silently,
  * `check.py`'s PAIRS table covers every file in `contracts/examples/`, so an example
    added without a mapping cannot sit there unvalidated,
  * the C-1 clock discipline: no wire field is a bare timestamp. Every stamp on the wire
    names the host whose clock produced it, which is what stops somebody subtracting two
    of them (watch-list failure mode 3), and
  * the C-5 joined record carries one honest `transport_residual_ms` rather than the
    `transport_in`/`transport_out` split that would require cross-host subtraction.

The scheduler's own log (C-4 scheduler) is Aditya's to emit. It appears here only where
my pipeline has to *read* it, and never as an assertion about how he writes it.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys

import pytest
from conftest import CONTRACTS, EXAMPLES, REPO_ROOT, SCHEMAS, assert_conforms, read_jsonl

# The artifacts on my side of the seam: I emit the client and worker logs, I generate the
# trace, I produce the cost model, my launcher writes the manifest, my pipeline emits the
# joined record. `log_scheduler` is Aditya's emitter and is deliberately not in this list.
A_SIDE_SCHEMAS = [
    "trace",
    "cost_model",
    "log_client",
    "log_worker",
    "joined_record",
    "manifest",
]


@pytest.mark.parametrize("name", A_SIDE_SCHEMAS)
def test_schema_is_itself_valid(name: str) -> None:
    """A schema with a broken `$ref` or a misspelled keyword validates everything.

    `check_schema` is the only thing that catches that, and it is cheap.
    """
    jsonschema = pytest.importorskip("jsonschema")
    doc = json.loads((SCHEMAS / f"{name}.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(doc)


@pytest.mark.parametrize("name", A_SIDE_SCHEMAS)
def test_schema_declares_draft_2020_12(name: str) -> None:
    """`check.py` validates with Draft202012Validator regardless of what a schema says.

    So a schema that quietly declares draft-07 would be validated under rules it was not
    written for. Pin the dialect in the file, not just in the checker.
    """
    doc = json.loads((SCHEMAS / f"{name}.schema.json").read_text())
    assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def _load_checker():
    """Import contracts/check.py by path — it is a PEP 723 script, not an installed module."""
    spec = importlib.util.spec_from_file_location("contracts_check", CONTRACTS / "check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_example_is_covered_by_the_checker() -> None:
    """An example with no schema mapping is a file that looks checked and is not.

    The PAIRS table is read from the checker itself rather than re-listed here: a second
    list of the same thing is a second thing to forget to update.
    """
    mapped = {example for example, _schema in _load_checker().PAIRS}
    on_disk = {p.name for p in EXAMPLES.iterdir() if p.is_file()}
    missing = sorted(on_disk - mapped)
    assert not missing, f"unmapped example(s) in contracts/examples: {missing}"


def test_checker_maps_every_example_to_a_schema_that_exists() -> None:
    """A typo in the schema half of a PAIRS row makes check.py crash in CI, not fail
    usefully. Catch it here, where the message says which row."""
    for example, schema_name in _load_checker().PAIRS:
        assert (SCHEMAS / schema_name).exists(), f"{example} -> missing {schema_name}"


def test_contract_check_passes() -> None:
    """The gate itself, run from my suite so I find a break before pushing."""
    result = subprocess.run(
        [sys.executable, str(CONTRACTS / "check.py")],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_client_example_conforms(schema) -> None:
    """The committed C-4 client fixture is what Aditya develops his join against."""
    assert_conforms(schema("log_client"), read_jsonl(EXAMPLES / "client.sample.jsonl"), "client")


def test_worker_example_conforms(schema) -> None:
    assert_conforms(schema("log_worker"), read_jsonl(EXAMPLES / "worker.sample.jsonl"), "worker")


def test_trace_example_conforms(schema) -> None:
    assert_conforms(schema("trace"), read_jsonl(EXAMPLES / "trace.sample.jsonl"), "trace line")


def test_cost_model_example_conforms(schema) -> None:
    doc = json.loads((EXAMPLES / "cost_model.sample.json").read_text())
    assert_conforms(schema("cost_model"), [doc], "cost model")


# --------------------------------------------------------------------------------------
# C-1 — the half of the wire schema my components emit and consume
# --------------------------------------------------------------------------------------

PROTO_TEXT = (CONTRACTS / "scheduling.proto").read_text()


def _message(name: str) -> str:
    return PROTO_TEXT.split(f"message {name}")[1].split("}")[0]


def test_dispatch_request_carries_the_client_endpoint() -> None:
    """F-11: the worker answers the client directly, so the endpoint rides with the request.

    Take this field out and the scheduler is back in the response path, which is the one
    thing the whole seam is arranged to avoid.
    """
    assert "client_endpoint" in _message("DispatchRequest")
    assert "client_endpoint" in _message("ExecuteRequest")


def test_heartbeat_carries_a_sequence_number() -> None:
    """`seq` is how a heartbeat gap is detected at all; `validity.heartbeat_gaps` counts them."""
    body = _message("Heartbeat")
    for field in ("seq", "queue_depth", "inflight_count", "recent_tokens_per_s", "engine_state"):
        assert field in body, f"Heartbeat lost {field}"


def test_kv_occupancy_has_a_not_exposed_sentinel() -> None:
    """llama.cpp reports slot occupancy, not paged-KV occupancy.

    The contract says -1.0 means "the engine does not expose it". A worker reporting 0.0
    instead would be telling the scheduler the node is empty.
    """
    assert "-1.0" in _message("Heartbeat")


def test_completion_is_its_own_message() -> None:
    """Learning about completions only at the next heartbeat tick would be a second,
    uncontrolled staleness source sitting alongside the one injected for H3."""
    assert "message Completion" in PROTO_TEXT
    assert "rpc ReportCompletion" in PROTO_TEXT


def test_every_wire_timestamp_names_its_host() -> None:
    """Watch-list failure mode 3: somebody subtracts a worker stamp from a client stamp.

    The defence is naming. A field called `client_send_mono_ns` or `worker_mono_ns` is hard
    to subtract by accident; one called `timestamp_ns` is an invitation. Durations measured
    entirely on one host (`worker_service_ns`, `worker_queue_wait_ns`) are fine — they are
    not points in time.
    """
    stamps = re.findall(r"^\s*uint64\s+(\w+)\s*=", PROTO_TEXT, flags=re.MULTILINE)
    points_in_time = [f for f in stamps if f.endswith("_mono_ns")]
    unlabelled = [f for f in points_in_time if not f.startswith(("client_", "worker_", "sched_"))]
    assert not unlabelled, f"wire timestamps that do not name their host: {unlabelled}"
    assert points_in_time, "expected at least one host-labelled wire timestamp"


def test_no_wall_clock_field_on_the_wire() -> None:
    """A `unix_ns` / `epoch_ms` field is the shape of a cross-host subtraction waiting to
    happen. `started_unix` exists in the manifest for the lab notebook, and nowhere else."""
    offenders = re.findall(r"^\s*\w+\s+(\w*(?:unix|epoch|wall)\w*)\s*=", PROTO_TEXT, re.MULTILINE)
    assert not offenders, f"wall-clock fields on the wire: {offenders}"


# --------------------------------------------------------------------------------------
# C-5 — what my pipeline is allowed to emit
# --------------------------------------------------------------------------------------


def test_joined_record_has_one_residual_not_a_fabricated_split() -> None:
    """F-18 asks for transport in/out separately. Without synchronised clocks I cannot
    measure them apart, so the schema carries the sum, named `residual` so nobody mistakes
    it for a measurement. A schema growing `transport_in_ms` is the drift to catch."""
    props = json.loads((SCHEMAS / "joined_record.schema.json").read_text())["properties"]
    assert "transport_residual_ms" in props
    assert not {"transport_in_ms", "transport_out_ms"} & set(props)


def test_joined_record_keeps_is_warmup_derived_from_the_trace() -> None:
    """Watch-list failure mode 5: warmup discarded differently in the two vehicles.

    `is_warmup` is computed from `intended_offset_s`, which exists in both, rather than
    from run wall-clock, which exists in only one of them.
    """
    props = json.loads((SCHEMAS / "joined_record.schema.json").read_text())["properties"]
    assert props["is_warmup"]["type"] == "boolean"
    assert "intended_offset_s" in props


def test_prefill_and_decode_are_nullable_in_the_joined_record() -> None:
    """F-18 `partial` has to be representable. A backend that emits no timings block must
    produce nulls, not zeros — a zero prefill is a number somebody will plot."""
    props = json.loads((SCHEMAS / "joined_record.schema.json").read_text())["properties"]
    for field in ("prefill_ms", "decode_ms"):
        assert "null" in props[field]["type"], f"{field} cannot express 'not measured'"
