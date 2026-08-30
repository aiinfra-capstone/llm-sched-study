"""Shared scaffolding for the harness test suite.

Two things live here rather than being repeated per file:

  * the path to `contracts/`, because a test that checks conformance must check it
    against the *committed* schema and never against a copy pasted into a test, and
  * a validator factory, so a test names the contract it is asserting (`C-4 client`)
    instead of opening a file and hoping it is the right one.

`jsonschema` is a dev dependency, not a runtime one — the harness must stay importable
on a node that only has to generate a trace — so every conformance test goes through
`schema`, which skips rather than fails when it is absent.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = REPO_ROOT / "contracts"
SCHEMAS = CONTRACTS / "schemas"
EXAMPLES = CONTRACTS / "examples"

# The trace config every test starts from. Small, Poisson, single bucket: a test that
# wants burstiness or a length mix says so explicitly, so the reason it is exercising
# that path is visible in the test rather than inherited from a shared default.
BASE_TRACE_CONFIG: dict[str, Any] = {
    "gen_seed": 20260421,
    "n_requests": 24,
    "duration_s": 60,
    "arrival": {"process": "poisson", "lambda_base": 5.0},
    "length_dist": {"buckets": ["p128_o64", "p512_o128"], "weights": [0.6, 0.4]},
    "priority_mix": {"0": 0.7, "1": 0.3},
    "admissible": {"max_prompt": 2048, "max_output": 256, "timeout_ceiling_ms": 60000},
    "vocab_size": 128000,
}


@pytest.fixture
def trace_config() -> Callable[..., dict[str, Any]]:
    """Return a fresh copy of the base config, with top-level keys overridden."""

    def _make(**overrides: Any) -> dict[str, Any]:
        return {**json.loads(json.dumps(BASE_TRACE_CONFIG)), **overrides}

    return _make


@pytest.fixture
def schema() -> Callable[[str], Any]:
    """`schema("log_client")` -> a Draft 2020-12 validator for that contract artifact."""
    jsonschema = pytest.importorskip("jsonschema")

    def _validator(name: str) -> Any:
        path = SCHEMAS / f"{name}.schema.json"
        assert path.exists(), f"no such contract schema: {path}"
        return jsonschema.Draft202012Validator(json.loads(path.read_text()))

    return _validator


def assert_conforms(validator: Any, records: Iterable[Any], label: str = "record") -> None:
    """Validate a batch and report every violation at once, with its JSON pointer.

    One assertion per batch rather than per record: when a schema change breaks a
    dozen fields, the whole list is what you want to read, not the first one.
    """
    failures: list[str] = []
    for n, record in enumerate(records, start=1):
        for err in validator.iter_errors(record):
            where = "/".join(str(p) for p in err.absolute_path) or "<root>"
            failures.append(f"{label} {n} at {where}: {err.message}")
    assert not failures, "\n".join(failures)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def pending(module: str, attr: str, *, week: str, deliverable: str):
    """Import a module that a later week is supposed to deliver, or skip with the reason.

    `pytest.importorskip` alone is not enough here. Several of these packages already
    exist as a docstring and an empty `__all__` — `dataplane.figures` and
    `dataplane.pipeline` were created with the repository layout — so importing succeeds
    while nothing is implemented. Requiring one named entry point is what makes the skip
    track the work rather than the directory.
    """
    mod = pytest.importorskip(
        module, reason=f"{week}: {deliverable} not implemented yet ({module} is missing)"
    )
    if not hasattr(mod, attr):
        pytest.skip(
            f"{week}: {deliverable} not implemented yet ({module}.{attr} is missing)",
            allow_module_level=True,
        )
    return mod
