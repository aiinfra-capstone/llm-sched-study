"""Black-box integration: the commands I actually type, over a real socket, on real files.

Everything else in this suite calls `main()` in-process or drives `replay()` with an
inline servicer. Both are the right tool for their job and both share a blind spot: they
never exercise the console scripts `pyproject.toml` declares, never cross a real process
boundary, and never let the artifacts land on disk and be read back by the contract gate
that judges them in CI.

So this file runs the Week-1 gate the way the freeze checklist states it:

    gen-trace  ->  fake scheduler in its own process  ->  replay  ->  run directory
                                                                          |
                                          validated by contracts/check.py's own schemas

Nothing here imports the harness. If it passes, an installed wheel and a terminal are
enough to produce a valid run — which is the claim the getting-started section makes.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import CONTRACTS, EXAMPLES, REPO_ROOT, assert_conforms, read_jsonl

pytestmark = pytest.mark.integration

BIN = Path(sys.executable).parent
FIXTURE = REPO_ROOT / "fixtures" / "fake_scheduler" / "serve.py"

CONFIG = {
    "gen_seed": 20260421,
    "n_requests": 24,
    "duration_s": 6,
    "arrival": {
        "process": "mmpp",
        "lambda_base": 6.0,
        "burst_lambda": 20.0,
        "burst_mean_s": 1.0,
        "quiet_mean_s": 2.0,
    },
    "length_dist": {"buckets": ["p128_o64", "p512_o128"], "weights": [0.6, 0.4]},
    "priority_mix": {"0": 0.7, "1": 0.3},
    "admissible": {"max_prompt": 2048, "max_output": 256, "timeout_ceiling_ms": 5000},
    "vocab_size": 1000,
}


def _gen_trace(tmp_path: Path) -> tuple[Path, str]:
    """Run the installed `gen-trace` script and take the hash from its own output."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(CONFIG))
    trace_path = tmp_path / "trace.jsonl"

    proc = subprocess.run(
        [str(BIN / "gen-trace"), str(config_path), "-o", str(trace_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    sha = proc.stdout.split("sha256")[1].split()[0]
    return trace_path, sha


@pytest.fixture
def fake_scheduler():
    """`fixtures/fake_scheduler` in loopback, as its own process, on an ephemeral port."""
    if not FIXTURE.exists():
        pytest.skip("fixtures/fake_scheduler removed (expected after Week 3)")

    proc = subprocess.Popen(
        [
            sys.executable,
            str(FIXTURE),
            "--loopback",
            "--bind",
            "127.0.0.1:0",
            "--prefill-ms",
            "2",
            "--per-token-ms",
            "0.05",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        endpoint = None
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if "fake scheduler on :" in line:
                endpoint = f"127.0.0.1:{line.split('on :')[1].split()[0]}"
                break
        assert endpoint, "fake scheduler never announced its port"
        yield endpoint
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _nodes_file(tmp_path: Path) -> Path:
    sample = json.loads((EXAMPLES / "manifest.sample.json").read_text())
    path = tmp_path / "nodes.json"
    path.write_text(json.dumps(sample["nodes"][:1]))
    return path


def _replay(trace: Path, sha: str, endpoint: str, tmp_path: Path, *, nodes: bool = True):
    args = [
        str(BIN / "replay"),
        str(trace),
        "--scheduler",
        endpoint,
        "--run-id",
        "run_e2e",
        "--sha256",
        sha,
        "--bind",
        "127.0.0.1:0",
        "--advertise",
        "127.0.0.1",
        "--out",
        str(tmp_path / "runs"),
        "--policy",
        "round_robin",
    ]
    if nodes:
        args += ["--nodes", str(_nodes_file(tmp_path))]
    return subprocess.run(args, capture_output=True, text=True, check=False)


# --------------------------------------------------------------------------------------
# The Week-1 gate, end to end
# --------------------------------------------------------------------------------------


def test_a_run_completes_from_the_command_line(fake_scheduler, tmp_path, schema) -> None:
    """gen-trace, then replay against a scheduler in another process, then a run directory
    whose manifest passes C-6 and whose log passes C-4. No harness imports anywhere."""
    trace, sha = _gen_trace(tmp_path)
    result = _replay(trace, sha, fake_scheduler, tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "run VALID" in result.stdout

    run_dir = tmp_path / "runs" / "run_e2e"
    records = read_jsonl(run_dir / "client_run_e2e.jsonl")
    assert len(records) == json.loads(trace.read_text().splitlines()[0])["n_requests"]
    assert_conforms(schema("log_client"), records, "client")

    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert_conforms(schema("manifest"), [manifest], "manifest")


def test_the_emitted_manifest_survives_the_real_contract_gate(fake_scheduler, tmp_path) -> None:
    """Not my own validator — `contracts/check.py`, the thing CI runs.

    A manifest that passes an in-process check and fails the gate is the failure mode this
    catches, and it is the one that costs a measurement week.
    """
    trace, sha = _gen_trace(tmp_path)
    assert _replay(trace, sha, fake_scheduler, tmp_path).returncode == 0

    manifest = tmp_path / "runs" / "run_e2e" / "manifest.json"
    checker = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,sys;from jsonschema import Draft202012Validator as V;"
                "s=json.load(open(sys.argv[1]));d=json.load(open(sys.argv[2]));"
                "e=[x.message for x in V(s).iter_errors(d)];"
                "print(chr(10).join(e));sys.exit(1 if e else 0)"
            ),
            str(CONTRACTS / "schemas" / "manifest.schema.json"),
            str(manifest),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert checker.returncode == 0, checker.stdout


def test_the_trace_the_run_used_regenerates_byte_for_byte(tmp_path) -> None:
    """The reproducibility claim, exercised through the CLI rather than the API: the
    manifest carries (config, seed) and the hash, and `gen-trace` reproduces the file."""
    first, sha_a = _gen_trace(tmp_path)
    second_dir = tmp_path / "again"
    second_dir.mkdir()
    second, sha_b = _gen_trace(second_dir)

    assert sha_a == sha_b
    assert first.read_bytes() == second.read_bytes()


def test_without_a_pool_description_only_the_validity_block_is_written(
    fake_scheduler, tmp_path
) -> None:
    """The launcher owns the node block under F-9a. The client hands over the half it
    measured and nothing else."""
    trace, sha = _gen_trace(tmp_path)
    result = _replay(trace, sha, fake_scheduler, tmp_path, nodes=False)

    assert result.returncode == 0
    run_dir = tmp_path / "runs" / "run_e2e"
    assert not (run_dir / "manifest.json").exists()
    assert json.loads((run_dir / "validity.json").read_text())["valid"] is True


def test_a_run_against_a_dead_scheduler_exits_nonzero(tmp_path) -> None:
    """The exit code is what a sweep script branches on."""
    trace, sha = _gen_trace(tmp_path)
    result = _replay(trace, sha, "127.0.0.1:1", tmp_path, nodes=False)

    assert result.returncode == 1
    assert "run INVALID" in result.stdout


def test_the_declared_console_scripts_all_resolve() -> None:
    """`pyproject.toml` declares three. Two exist today; `pipeline` is the Week-4 module,
    and its entry point is already a promise the packaging makes on my behalf."""
    assert (BIN / "gen-trace").exists()
    assert (BIN / "replay").exists()
    proc = subprocess.run(
        [str(BIN / "pipeline"), "--help"], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0 and "ModuleNotFoundError" in proc.stderr:
        pytest.skip("Week 4: dataplane.pipeline.join not implemented yet")
    assert proc.returncode == 0
