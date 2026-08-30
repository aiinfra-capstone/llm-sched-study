"""The three lines that only run when the module is the program, not the import.

`gen-trace` and `replay` are installed console scripts, and `test_end_to_end.py` drives
those. But the modules also carry `if __name__ == "__main__": raise SystemExit(main())`,
which is the form anyone actually types on a node that has the checkout but not the
install (`python -m dataplane.harness.replay ...`). A console script never executes that
line, so nothing was checking that the two entry points agree.

`runpy` executes them in this process, which is also the only way the line is visible to
coverage — a subprocess is measured by nobody.
"""

from __future__ import annotations

import importlib
import json
import runpy
import sys
import warnings
from pathlib import Path

import pytest
from conftest import read_jsonl

MODULES = ["dataplane.harness.gen_trace", "dataplane.harness.replay"]


def _run_module(module: str, argv: list[str]) -> int:
    """Execute `python -m <module> <argv>` in-process; return the exit status."""
    saved = sys.argv
    sys.argv = [module, *argv]
    try:
        with warnings.catch_warnings():
            # runpy warns when the module is already in sys.modules, which it always is
            # here — the suite imported it. The warning is about the double execution,
            # which is the thing being tested.
            warnings.simplefilter("ignore", RuntimeWarning)
            runpy.run_module(module, run_name="__main__", alter_sys=True)
    except SystemExit as exc:  # the `raise SystemExit(main())` under test
        return int(exc.code or 0)
    finally:
        sys.argv = saved
    raise AssertionError(f"{module} ran as __main__ without exiting")


def test_gen_trace_runs_as_a_module(tmp_path: Path, trace_config) -> None:
    """`python -m dataplane.harness.gen_trace` writes the same trace the script does."""
    config = tmp_path / "c.json"
    config.write_text(json.dumps(trace_config()))
    out = tmp_path / "t.jsonl"

    assert _run_module("dataplane.harness.gen_trace", [str(config), "-o", str(out)]) == 0

    header, *body = read_jsonl(out)
    assert header["record"] == "header"
    assert len(body) == header["n_requests"] == 24


def test_the_module_form_and_the_console_script_agree(tmp_path: Path, trace_config) -> None:
    """Byte-for-byte: the guard must call the same `main`, not a second copy of it."""
    from dataplane.harness import gen_trace

    config = tmp_path / "c.json"
    config.write_text(json.dumps(trace_config()))
    via_module, via_script = tmp_path / "m.jsonl", tmp_path / "s.jsonl"

    _run_module("dataplane.harness.gen_trace", [str(config), "-o", str(via_module)])
    saved = sys.argv
    sys.argv = ["gen-trace", str(config), "-o", str(via_script)]
    try:
        assert gen_trace.main() == 0
    finally:
        sys.argv = saved

    assert via_module.read_bytes() == via_script.read_bytes()


def test_replay_runs_as_a_module_and_reports_its_refusals(tmp_path: Path) -> None:
    """The guard propagates argparse's exit status rather than swallowing it.

    `--nodes` without `--sha256` is the cheapest way in: it is refused before anything
    is bound, so this exercises the entry point without needing a scheduler.
    """
    nodes = tmp_path / "nodes.json"
    nodes.write_text("[]")

    status = _run_module(
        "dataplane.harness.replay",
        [
            "--trace",
            str(tmp_path / "absent.jsonl"),
            "--scheduler",
            "127.0.0.1:1",
            "--run-id",
            "run_0001",
            "--policy",
            "jsq",
            "--out",
            str(tmp_path),
            "--nodes",
            str(nodes),
        ],
    )
    assert status == 2, "argparse.error() exits 2; the guard must pass that through"


@pytest.mark.parametrize("module", MODULES)
def test_the_guard_does_not_fire_on_import(module: str) -> None:
    """Importing must not run `main`. This is what makes the modules usable as libraries."""
    mod = importlib.import_module(module)
    saved = sys.argv
    sys.argv = [module]  # no arguments: `main()` would exit 2 if it ran
    try:
        importlib.reload(mod)
    finally:
        sys.argv = saved


def test_reimporting_proto_does_not_stack_the_generated_dir_on_sys_path() -> None:
    """`dataplane.proto` prepends its output dir to `sys.path` — but only once.

    The module body runs on every reload (an editable install, a `runpy` execution, a
    plugin reimport), and an unguarded `insert` would grow `sys.path` by one entry each
    time. This asserts the guard, and is the only way to reach its false branch.
    """
    proto = pytest.importorskip("dataplane.proto")
    entry = str(proto.OUT_DIR)
    assert sys.path.count(entry) == 1

    importlib.reload(proto)

    assert sys.path.count(entry) == 1, "reload re-inserted an entry that was already there"
    assert proto.sched_pb2 is sys.modules["scheduling_pb2"]
