"""F-20 / C-6 — the manifest is the reproducibility record, and the place a run is failed.

The manifest is the only per-run artifact that gets committed. If it can be emitted in a
shape that `contracts/check.py` rejects, that is discovered at the end of a measurement
week rather than at the start of one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dataplane.harness.manifest import SEND_LAG_THRESHOLD_MS, Validity, build, config_hash

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
