"""Week 4 — the results pipeline: a pure function of (manifest, three logs) -> one Parquet.

Skipped until `dataplane.pipeline.join` lands. Note that `pyproject.toml` already declares
a `pipeline = "dataplane.pipeline.join:main"` entry point, so the module is committed to
by name before it exists — one of these tests is simply that the promise resolves.

The join inputs here are the **committed contract examples**, not fabricated records. That
is the point of §11's fixture-first plan: if my client fixture and Aditya's scheduler
fixture both pass their schemas, the two halves join when they meet for real. All four
examples describe one request — `run_0142` / `r000417` — so they are a complete join by
construction, and a change to any of them that breaks the join breaks it here.

Three properties matter more than the row count:

  * `transport_residual_ms` is a residual, not a measurement. It is what is left after the
    single-host durations, and it is named that way so nobody decomposes it.
  * `is_warmup` comes from `intended_offset_s`, which exists in both vehicles, never from
    run wall-clock, which exists in one.
  * The pipeline has no network and no engine, so Aditya can hand me a directory of
    simulator logs and it processes them with zero changes.
"""

from __future__ import annotations

import json

import pytest
from conftest import EXAMPLES, pending, read_jsonl

pytestmark = pytest.mark.forward

join_mod = pending("dataplane.pipeline.join", "join", week="Week 4", deliverable="results pipeline")

MANIFEST = json.loads((EXAMPLES / "manifest.sample.json").read_text())
CLIENT = read_jsonl(EXAMPLES / "client.sample.jsonl")
SCHEDULER = read_jsonl(EXAMPLES / "scheduler.sample.jsonl")
WORKER = read_jsonl(EXAMPLES / "worker.sample.jsonl")


def _join(manifest=None, client=None, scheduler=None, worker=None):
    return join_mod.join(
        manifest=manifest or MANIFEST,
        client=client if client is not None else CLIENT,
        scheduler=scheduler if scheduler is not None else SCHEDULER,
        worker=worker if worker is not None else WORKER,
    )


# --------------------------------------------------------------------------------------
# The shape of what comes out
# --------------------------------------------------------------------------------------


def test_the_entry_point_declared_in_pyproject_resolves() -> None:
    """`pipeline = "dataplane.pipeline.join:main"` is a promise the packaging makes."""
    assert callable(join_mod.main)


def test_a_joined_row_conforms_to_c5(schema) -> None:
    rows = _join()
    assert rows
    errors = [e.message for row in rows for e in schema("joined_record").iter_errors(row)]
    assert not errors, errors


def test_one_row_per_client_record() -> None:
    """The client log is the spine: it has a record for every request, including the ones
    that failed. A join that dropped failures would silently condition every latency
    distribution on success."""
    assert len(_join()) == len(CLIENT)


def test_run_level_fields_come_from_the_manifest() -> None:
    """`policy`, `lambda`, `staleness_s` are properties of the run, not of a request. They
    are stamped onto every row so a figure can group by them without a second file."""
    row = _join()[0]
    assert row["policy"] == MANIFEST["policy"]
    assert row["lambda"] == MANIFEST["lambda"]
    assert row["staleness_s"] == MANIFEST["staleness_s"]
    assert row["node_count"] == len(MANIFEST["nodes"])


# --------------------------------------------------------------------------------------
# Clock discipline — the thing that quietly invalidates measurement studies
# --------------------------------------------------------------------------------------


def test_the_residual_is_what_is_left_over(schema) -> None:
    """transport_residual_ms = e2e - queue_wait - service - decide_us/1000.

    F-18 asks for transport in and out separately. Without synchronised clocks I can only
    measure the sum of everything not accounted for by single-host durations. Reporting it
    as one residual, and naming it that, is the honest version.
    """
    row = _join()[0]
    expected = row["e2e_ms"] - row["queue_wait_ms"] - row["service_ms"] - row["decide_us"] / 1000.0
    assert row["transport_residual_ms"] == pytest.approx(expected, abs=1e-6)


def test_the_join_never_subtracts_across_hosts() -> None:
    """Watch-list failure mode 3, checked at the only place it can happen. The wire's
    host-labelled stamps exist for gap detection; if the pipeline reads one, somebody has
    computed a duration across two unsynchronised clocks."""
    with open(join_mod.__file__) as fh:
        source = fh.read()
    for forbidden in ("client_send_mono_ns", "worker_mono_ns", "started_unix"):
        assert forbidden not in source, f"the join reads {forbidden}"


def test_is_warmup_comes_from_the_trace_offset() -> None:
    """Watch-list failure mode 5: warmup discarded differently in the two vehicles. Both
    have `intended_offset_s`; only one has a wall clock."""
    manifest = {**MANIFEST, "warmup_s": 60.0}
    for row in _join(manifest=manifest):
        assert row["is_warmup"] == (row["intended_offset_s"] < 60.0)


def test_a_zero_warmup_marks_nothing() -> None:
    for row in _join(manifest={**MANIFEST, "warmup_s": 0.0}):
        assert row["is_warmup"] is False


# --------------------------------------------------------------------------------------
# Gaps stay gaps
# --------------------------------------------------------------------------------------


def test_a_missing_scheduler_record_becomes_null_not_zero() -> None:
    """My fake scheduler deliberately writes no decision record, because it cannot fill in
    a candidates array honestly. The pipeline has to survive that gap without inventing a
    queue depth of zero — which is a plausible number that would go straight into a
    figure."""
    rows = _join(scheduler=[])
    assert rows[0]["chosen_node"] is None or rows[0]["decide_us"] is None


def test_a_partial_f18_run_leaves_prefill_and_decode_null() -> None:
    """If a backend emits no timings block I log `service_ns` only. The join must carry
    that absence through rather than filling it."""
    worker = [{k: v for k, v in WORKER[0].items() if k not in ("prefill_ns", "decode_ns")}]
    row = _join(worker=worker)[0]
    assert row["prefill_ms"] is None and row["decode_ms"] is None
    assert row["service_ms"] > 0


def test_a_failed_request_still_produces_a_row() -> None:
    """A timeout is a data point about a 100x-heterogeneous pool, not a missing row."""
    client = [{**CLIENT[0], "status": "timeout", "responding_node": ""}]
    rows = _join(client=client, worker=[])
    assert len(rows) == 1
    assert rows[0]["status"] == "timeout"


# --------------------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------------------


def test_logs_from_a_different_run_are_refused() -> None:
    """Joining run_0142's client log against run_0143's worker log produces rows that look
    fine and describe nothing."""
    with pytest.raises(ValueError, match="run_id"):
        _join(worker=[{**WORKER[0], "run_id": "run_9999"}])


def test_a_manifest_naming_a_different_trace_is_refused() -> None:
    """The trace SHA-256 is the workload's identity. A manifest pointing at another trace
    attributes these results to a workload that did not produce them."""
    rows_ok = _join()
    assert rows_ok
    with pytest.raises(ValueError):
        join_mod.join(
            manifest={**MANIFEST, "trace_sha256": "f" * 64},
            client=CLIENT,
            scheduler=SCHEDULER,
            worker=WORKER,
            trace_sha256="0" * 64,
        )


def test_an_invalid_run_is_marked_rather_than_silently_joined() -> None:
    """A run whose load generator drifted is not a data point about scheduling. The
    pipeline may still join it — for diagnosis — but it must not present it as analysable
    without saying so."""
    manifest = {**MANIFEST, "validity": {**MANIFEST["validity"], "valid": False}}
    with pytest.raises(ValueError, match="invalid"):
        join_mod.join(manifest=manifest, client=CLIENT, scheduler=SCHEDULER, worker=WORKER)


# --------------------------------------------------------------------------------------
# The vehicle boundary — the whole reason the pipeline is a pure function
# --------------------------------------------------------------------------------------


def test_simulator_logs_process_with_zero_changes() -> None:
    """Aditya hands me a directory of DES logs and this runs on them unchanged. That is
    what keeps the hardware/simulator comparison a comparison rather than two pipelines."""
    rows = _join(manifest={**MANIFEST, "vehicle": "simulator"})
    assert all(row["run_id"] == MANIFEST["run_id"] for row in rows)


def test_the_pipeline_imports_nothing_from_the_transport() -> None:
    """No network, no engine, laptop-runnable. `dataplane.proto` runs protoc on import;
    the pipeline acquiring that dependency by accident would make the analysis stage need a
    gRPC toolchain to open a log file."""
    with open(join_mod.__file__) as fh:
        source = fh.read()
    for forbidden in ("import grpc", "from dataplane.proto", "httpx"):
        assert forbidden not in source


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------


def test_the_parquet_round_trips(tmp_path) -> None:
    """C-5 is Parquet because the figure scripts consume Parquet only. A file the reader
    cannot open is a stage boundary that does not hold."""
    pd = pytest.importorskip("pandas")
    out = tmp_path / "joined.parquet"
    join_mod.write_parquet(_join(), out)
    assert len(pd.read_parquet(out)) == len(CLIENT)


def test_nullable_columns_survive_the_parquet_round_trip(tmp_path) -> None:
    """The nulls are load-bearing: a `prefill_ms` that becomes 0.0 on the way through
    Parquet is the F-18 gap turning into a measurement."""
    pd = pytest.importorskip("pandas")
    worker = [{k: v for k, v in WORKER[0].items() if k not in ("prefill_ns", "decode_ns")}]
    out = tmp_path / "joined.parquet"
    join_mod.write_parquet(_join(worker=worker), out)
    assert pd.read_parquet(out)["prefill_ms"].isna().all()
