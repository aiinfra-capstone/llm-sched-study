"""F-20 / C-6 — the manifest is the reproducibility record, and the place a run is failed.

The manifest is the only per-run artifact that gets committed. If it can be emitted in a
shape that `contracts/check.py` rejects, that is discovered at the end of a measurement
week rather than at the start of one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dataplane.harness.manifest import (
    SEND_LAG_THRESHOLD_MS,
    Validity,
    build,
    config_hash,
    git_shas,
    unsynced_hosts,
)

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
CONFIG = {"duration_s": 600.0, "warmup_s": 30.0, "arrival": {"lambda_base": 3.5}}


def _nodes() -> list[dict]:
    """A real pool block, taken from the committed C-6 example rather than invented here."""
    sample = json.loads((CONTRACTS / "examples" / "manifest.sample.json").read_text())
    return sample["nodes"]


def test_emitted_manifest_conforms_to_c6() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    man = build(
        run_id="run_0001",
        config=CONFIG,
        trace_path="traces/t.jsonl",
        trace_sha256="0" * 64,
        validity=Validity(max_send_lag_ms=3.1),
        nodes=_nodes(),
    )
    validator = jsonschema.Draft202012Validator(
        json.loads((CONTRACTS / "schemas" / "manifest.schema.json").read_text())
    )
    errors = list(validator.iter_errors(man))
    assert not errors, [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in errors]


def test_empty_pool_is_refused() -> None:
    """Under F-9a the node block IS the experimental condition. The harness must not guess it."""
    with pytest.raises(ValueError, match="non-empty `nodes`"):
        build(
            run_id="run_0001",
            config=CONFIG,
            trace_path="t.jsonl",
            trace_sha256="0" * 64,
            validity=Validity(),
            nodes=[],
        )


def test_send_lag_breach_invalidates_the_run() -> None:
    clean = Validity(max_send_lag_ms=SEND_LAG_THRESHOLD_MS - 1)
    assert clean.valid and not clean.reasons()

    drifted = Validity(max_send_lag_ms=91.4, send_lag_violations=3)
    assert not drifted.valid
    assert "open-loop" in " ".join(drifted.reasons())


def test_colocated_nodes_invalidates_the_run() -> None:
    """Two logical nodes on one host reintroduce exactly the confound F-9a removes."""
    assert not Validity(colocated_nodes=1).valid


def test_heartbeat_gaps_are_reported_but_not_fatal() -> None:
    """A stale estimate is what H3 is about; it does not ruin the measurement."""
    v = Validity(heartbeat_gaps=7)
    assert v.valid
    assert v.to_dict()["heartbeat_gaps"] == 7


def test_config_hash_is_order_independent() -> None:
    """Same config, same hash, on any machine — so `config_hash` can identify a condition."""
    assert config_hash({"a": 1, "b": 2}) == config_hash({"b": 2, "a": 1})
    assert config_hash({"a": 1}) != config_hash({"a": 2})


# --------------------------------------------------------------------------------------
# Every value the schema constrains, emitted by `build` and checked against it
# --------------------------------------------------------------------------------------

POLICIES = ["round_robin", "jsq", "static_weighted", "wjsq", "threshold"]


def _manifest(**kw):
    defaults = {
        "run_id": "run_0001",
        "config": CONFIG,
        "trace_path": "traces/t.jsonl",
        "trace_sha256": "0" * 64,
        "validity": Validity(),
        "nodes": _nodes(),
    }
    return build(**{**defaults, **kw})


@pytest.mark.parametrize("policy", POLICIES)
def test_every_policy_name_is_emittable(policy: str, schema) -> None:
    """F-1: all five policies are selectable from one config value. The manifest is where
    that value is recorded, and its enum is the only thing that keeps the recorded name
    and the implemented name from diverging."""
    errors = list(schema("manifest").iter_errors(_manifest(policy=policy)))
    assert not errors, [e.message for e in errors]


@pytest.mark.parametrize("vehicle", ["hardware", "simulator"])
def test_both_vehicles_are_emittable(vehicle: str, schema) -> None:
    """F-24 requires every simulated figure to be labelled, and `vehicle` is what the
    figure scripts read to do it. A manifest that cannot say `simulator` cannot be
    labelled automatically, and labelling by hand fails exactly once — in the report."""
    errors = list(schema("manifest").iter_errors(_manifest(vehicle=vehicle)))
    assert not errors, [e.message for e in errors]


@pytest.mark.parametrize("status", ["full", "partial"])
def test_f18_status_round_trips(status: str, schema) -> None:
    """`partial` is the honest answer for a backend that emits no timings block. It has to
    be emittable, or the pressure is to fake the split instead."""
    man = _manifest(f18_status=status)
    assert man["f18_status"] == status
    assert not list(schema("manifest").iter_errors(man))


def test_f18_status_is_omitted_rather_than_nulled() -> None:
    """An absent field says "not asserted". A null says "asserted to be nothing", and the
    schema's enum would reject it — which is the right outcome, so never emit one."""
    assert "f18_status" not in _manifest()


def test_git_shas_carry_all_four_components() -> None:
    """In this monorepo all four resolve to one commit. The field stays four-valued
    because `scheduler` and `sim` are Aditya's and may yet move to their own repo — a
    manifest that already has the shape survives that without a contract change."""
    assert set(git_shas()) == {"worker", "scheduler", "harness", "sim"}


def test_git_shas_accept_overrides() -> None:
    """Which is how a manifest records a control plane living in another repo."""
    assert git_shas(sim="deadbee")["sim"] == "deadbee"


def test_git_shas_never_raise_outside_a_checkout(tmp_path: Path) -> None:
    """An unversioned directory should still be able to produce a manifest; it just
    cannot claim provenance for it."""
    assert git_shas(root=tmp_path)["harness"] == "unknown"


def test_lambda_is_taken_from_the_arrival_process() -> None:
    """`lambda` is the load axis of the whole study. Reading it from the config's arrival
    block rather than accepting it as a free field means it cannot disagree with the trace
    that was actually replayed."""
    assert _manifest()["lambda"] == CONFIG["arrival"]["lambda_base"]


def test_an_explicit_lambda_overrides_the_arrival_base() -> None:
    """MMPP has two rates; a run set that wants to be labelled by an effective lambda says
    so explicitly rather than being mislabelled by `lambda_base`."""
    config = {**CONFIG, "lambda": 9.25}
    assert _manifest(config=config)["lambda"] == 9.25


def test_started_unix_is_recorded_but_never_used_for_a_duration() -> None:
    """The one wall-clock read in the harness. It exists so a run can be found in a lab
    notebook. Every duration in the analysis comes from a single machine's monotonic
    clock, so nothing downstream may subtract this."""
    man = _manifest(started_unix=1777400000)
    assert man["started_unix"] == 1777400000
    assert man["duration_s"] == CONFIG["duration_s"]


def test_warmup_and_staleness_default_to_zero() -> None:
    """A run with no warmup is a valid run. A missing field would make `is_warmup`
    undefined in the join, which is worse."""
    man = _manifest(config={"duration_s": 60.0})
    assert man["warmup_s"] == 0.0
    assert man["staleness_s"] == 0.0


def test_config_is_carried_verbatim() -> None:
    """The manifest is the reproducibility record. A summarised config is not one."""
    assert _manifest()["config"] == CONFIG


def test_config_hash_is_64_hex_characters() -> None:
    """SHA-256, so a condition can be identified by its hash across machines."""
    digest = config_hash(CONFIG)
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)


def test_config_hash_is_stable_across_nesting_order() -> None:
    """Canonicalisation has to reach nested objects, or two identical conditions written
    by two different launchers get two different hashes."""
    a = {"outer": {"a": 1, "b": [1, 2]}, "z": 0}
    b = {"z": 0, "outer": {"b": [1, 2], "a": 1}}
    assert config_hash(a) == config_hash(b)


def test_list_order_still_changes_the_hash() -> None:
    """Order matters inside a list — `buckets` and `weights` are positional, so two
    configs that differ only in bucket order are genuinely different conditions."""
    assert config_hash({"b": [1, 2]}) != config_hash({"b": [2, 1]})


# --------------------------------------------------------------------------------------
# The validity block: every field is a count of something that should be zero
# --------------------------------------------------------------------------------------


def test_engine_restarts_invalidate_the_run() -> None:
    """A pool that restarted mid-run was not the pool the manifest describes."""
    assert not Validity(engine_restarts=1).valid


def test_dropped_requests_invalidate_the_run() -> None:
    assert not Validity(dropped_requests=1).valid


def test_valid_is_computed_and_cannot_be_set() -> None:
    """A run is failed by the harness from measurements, not by a human deciding a plot
    looks fine. `valid` being a property rather than a field is what enforces that."""
    v = Validity(send_lag_violations=2)
    assert "valid" not in vars(v)
    assert v.to_dict()["valid"] is False


def test_reasons_are_empty_exactly_when_the_run_is_valid() -> None:
    assert Validity().reasons() == []
    assert Validity(heartbeat_gaps=3).reasons() == []  # reported, not fatal
    assert Validity(colocated_nodes=1).reasons()


def test_colocated_reason_names_the_confound() -> None:
    """The message is what I will read at 2am. It should say why, not just what."""
    assert "contention" in " ".join(Validity(colocated_nodes=2).reasons())


def test_send_lag_is_rounded_but_not_truncated() -> None:
    """Three decimals of a millisecond is well below anything that matters, and it keeps
    the manifest diffable."""
    assert Validity(max_send_lag_ms=3.14159).to_dict()["max_send_lag_ms"] == 3.142


def test_validity_block_conforms_on_its_own(schema) -> None:
    """The client writes `validity.json` alone when the launcher owns the manifest. That
    file still has to be the same shape the manifest will embed."""
    errors = list(schema("manifest").iter_errors(_manifest(validity=Validity(heartbeat_gaps=4))))
    assert not errors, [e.message for e in errors]


def test_the_engine_gap_example_is_a_shape_i_can_emit(schema) -> None:
    """F-9b's probe node is a `role: engine_gap_probe` entry, not a pool member. The
    committed example proves the schema allows it; this proves `build` does too."""
    sample = json.loads((CONTRACTS / "examples" / "manifest.engine_gap.sample.json").read_text())
    man = _manifest(nodes=sample["nodes"])
    errors = list(schema("manifest").iter_errors(man))
    assert not errors, [e.message for e in errors]


def test_an_unmeasured_clock_is_not_a_failed_one() -> None:
    """Absent means nobody looked, and a single-host run has no clock block by design.

    Counting that as an unsynchronised host would make every single-host run in the study
    look suspect, which is the opposite of what the field is for.
    """
    assert unsynced_hosts(None) == 0
    assert unsynced_hosts({}) == 0
    assert unsynced_hosts({"reference": "box-a", "hosts": {}}) == 0


def test_unsynchronised_hosts_are_counted_but_do_not_fail_the_run() -> None:
    """The rate error is the only clock term the pipeline acts on, and it is sub-millisecond.

    A constant offset cancels in a difference of single-host durations, and there is no
    cross-host subtraction anywhere in C-4 or C-5. So an undisciplined host costs the
    *evidence* that durations were comparable, not the durations. It is recorded, not fatal.
    """
    block = {
        "reference": "box-a",
        "hosts": {
            "box-a": {"method": "chrony", "synchronised": True, "rate_error_ppm": 1.2},
            "box-b": {"method": "chrony", "synchronised": False},
            # No `synchronised` key at all: a host that never reported is not a host that
            # reported success, so it counts against us rather than for us.
            "box-c": {"method": "none"},
        },
    }
    assert unsynced_hosts(block) == 2

    validity = Validity(clock_unsynced_hosts=2)
    assert validity.valid is True
    assert validity.to_dict()["clock_unsynced_hosts"] == 2
    assert validity.reasons() == []


def test_a_measured_clock_block_travels_on_the_manifest() -> None:
    """C-6 carries the measurement so the claim can be checked rather than taken."""
    jsonschema = pytest.importorskip("jsonschema")
    block = {
        "reference": "box-a",
        "measured_unix": 1788376140,
        "hosts": {
            "box-a": {"method": "chrony", "synchronised": True, "rate_error_ppm": 0.4},
            "box-b": {"method": "chrony", "synchronised": True, "rate_error_ppm": -0.3},
        },
        "max_abs_offset_ms": 1.4,
        "max_rate_error_ppm": 0.8,
        "ok": True,
    }
    man = build(
        run_id="run_0009",
        config=CONFIG,
        trace_path="traces/t.jsonl",
        trace_sha256="0" * 64,
        validity=Validity(clock_unsynced_hosts=unsynced_hosts(block)),
        nodes=_nodes(),
        clock_sync=block,
    )
    assert man["clock_sync"]["reference"] == "box-a"
    assert man["validity"]["clock_unsynced_hosts"] == 0

    validator = jsonschema.Draft202012Validator(
        json.loads((CONTRACTS / "schemas" / "manifest.schema.json").read_text())
    )
    assert not list(validator.iter_errors(man))


def test_a_manifest_with_no_clock_measurement_omits_the_block_entirely() -> None:
    """A zeroed block would read as "the clocks agreed". Absence is the honest record."""
    man = build(
        run_id="run_0010",
        config=CONFIG,
        trace_path="traces/t.jsonl",
        trace_sha256="0" * 64,
        validity=Validity(),
        nodes=_nodes(),
    )
    assert "clock_sync" not in man
    assert man["validity"]["clock_unsynced_hosts"] == 0
