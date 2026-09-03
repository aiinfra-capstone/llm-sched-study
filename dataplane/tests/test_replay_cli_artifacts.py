"""What a run leaves on disk, and what the launcher gets to assume about it.

`replay.main()` is the thing I actually type on the load host. Its output is the run
directory: one C-4 client log, and either the finished C-6 manifest (when the launcher
hands over the pool description) or the validity block alone (when the launcher assembles
the manifest itself). Both branches are contract surfaces, so both are checked here rather
than discovered at the end of a measurement week.

The exit code matters too. It is what a sweep script reads to decide whether to keep a
run, so a run that failed its own open-loop guard must not exit 0.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import grpc
import pytest
from conftest import assert_conforms, read_jsonl

from dataplane.harness import gen_trace, replay
from dataplane.proto import sched_grpc, sched_pb2

CONFIG = {
    "gen_seed": 77,
    "n_requests": 5,
    "duration_s": 10,
    "arrival": {"process": "poisson", "lambda_base": 40.0},
    "length_dist": {"buckets": ["p128_o64"], "weights": [1.0]},
    "priority_mix": {"0": 1.0},
    "admissible": {"max_prompt": 2048, "max_output": 256, "timeout_ceiling_ms": 400},
    "vocab_size": 1000,
}


class _Scheduler(sched_grpc.SchedulerServicer):
    def __init__(self, deliver: bool = True) -> None:
        self.deliver = deliver

    async def Dispatch(self, request, context):
        if self.deliver:
            asyncio.create_task(self._deliver(request))
        return sched_pb2.DispatchAck(req_id=request.req_id, chosen_node="n1", accepted=True)

    async def _deliver(self, request) -> None:
        async with grpc.aio.insecure_channel(request.client_endpoint) as ch:
            await sched_grpc.ClientStub(ch).Deliver(
                sched_pb2.ResponseDelivery(
                    run_id=request.run_id,
                    req_id=request.req_id,
                    node_id="n1",
                    output_tokens=request.output_len,
                    status="ok",
                    worker_service_ns=1_000_000,
                    worker_queue_wait_ns=0,
                )
            )


@pytest.fixture
def scheduler():
    """A fake scheduler on its own loop in its own thread.

    `main()` calls `asyncio.run` itself, so the scheduler cannot share the client's loop.
    Two threads is also closer to the real thing, where they are two hosts.
    """
    ready = threading.Event()
    box: dict = {}

    def run(deliver: bool = True) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def serve() -> None:
            server = grpc.aio.server()
            sched_grpc.add_SchedulerServicer_to_server(_Scheduler(box["deliver"]), server)
            box["port"] = server.add_insecure_port("127.0.0.1:0")
            await server.start()
            ready.set()
            await box["stop"].wait()
            await server.stop(grace=0.5)

        box["stop"] = asyncio.Event()
        loop.run_until_complete(serve())
        loop.close()

    def start(deliver: bool = True) -> str:
        box["deliver"] = deliver
        thread = threading.Thread(target=run, daemon=True)
        box["thread"] = thread
        thread.start()
        ready.wait(timeout=10)
        return f"127.0.0.1:{box['port']}"

    yield start

    if "stop" in box:
        box["stop"]._loop.call_soon_threadsafe(box["stop"].set)
        box["thread"].join(timeout=5)


@pytest.fixture
def trace(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "t.jsonl"
    return path, gen_trace.generate(CONFIG, path)


def _nodes_file(tmp_path: Path) -> Path:
    """The pool description the launcher hands over. Taken from the committed C-6 example
    rather than invented, so the test cannot drift from the contract."""
    sample = json.loads(
        (
            Path(__file__).resolve().parents[2] / "contracts/examples/manifest.sample.json"
        ).read_text()
    )
    path = tmp_path / "nodes.json"
    path.write_text(json.dumps(sample["nodes"]))
    return path


def _main(monkeypatch, args: list[str]) -> int:
    monkeypatch.setattr("sys.argv", ["replay", *args])
    return replay.main()


def test_a_clean_run_writes_a_conformant_client_log(
    trace, scheduler, tmp_path, monkeypatch, schema
) -> None:
    path, sha = trace
    endpoint = scheduler()
    out = tmp_path / "runs"

    rc = _main(
        monkeypatch,
        [
            str(path),
            "--scheduler",
            endpoint,
            "--run-id",
            "run_0001",
            "--sha256",
            sha,
            "--bind",
            "127.0.0.1:0",
            "--advertise",
            "127.0.0.1",
            "--out",
            str(out),
        ],
    )

    assert rc == 0
    log = out / "run_0001" / "client_run_0001.jsonl"
    records = read_jsonl(log)
    assert len(records) == CONFIG["n_requests"]
    assert_conforms(schema("log_client"), records, "client")


def test_without_a_pool_description_the_client_writes_only_what_it_measured(
    trace, scheduler, tmp_path, monkeypatch
) -> None:
    """Under F-9a the node block IS the experimental condition and the launcher owns it.
    The client contributes the half it measured and refuses to invent the rest."""
    path, sha = trace
    endpoint = scheduler()
    out = tmp_path / "runs"

    _main(
        monkeypatch,
        [
            str(path),
            "--scheduler",
            endpoint,
            "--run-id",
            "run_0002",
            "--sha256",
            sha,
            "--bind",
            "127.0.0.1:0",
            "--advertise",
            "127.0.0.1",
            "--out",
            str(out),
        ],
    )

    run_dir = out / "run_0002"
    assert not (run_dir / "manifest.json").exists()
    validity = json.loads((run_dir / "validity.json").read_text())
    assert validity["valid"] is True
    assert validity["dropped_requests"] == 0


def test_with_a_pool_description_the_manifest_conforms_to_c6(
    trace, scheduler, tmp_path, monkeypatch, schema
) -> None:
    path, sha = trace
    endpoint = scheduler()
    out = tmp_path / "runs"

    rc = _main(
        monkeypatch,
        [
            str(path),
            "--scheduler",
            endpoint,
            "--run-id",
            "run_0003",
            "--sha256",
            sha,
            "--bind",
            "127.0.0.1:0",
            "--advertise",
            "127.0.0.1",
            "--out",
            str(out),
            "--nodes",
            str(_nodes_file(tmp_path)),
            "--policy",
            "wjsq",
        ],
    )

    assert rc == 0
    man = json.loads((out / "run_0003" / "manifest.json").read_text())
    assert_conforms(schema("manifest"), [man], "manifest")
    assert man["policy"] == "wjsq"
    assert man["trace_sha256"] == sha


def test_a_manifest_without_a_trace_hash_is_refused_before_the_run(
    trace, tmp_path, monkeypatch
) -> None:
    """C-6 pins `trace_sha256` to 64 hex characters, so a manifest written without one
    fails `contracts/check.py` — after the run, when the pool has already moved on. The
    check belongs before t0."""
    path, _ = trace
    with pytest.raises(SystemExit):
        _main(
            monkeypatch,
            [
                str(path),
                "--scheduler",
                "127.0.0.1:1",
                "--run-id",
                "run_0004",
                "--out",
                str(tmp_path),
                "--nodes",
                str(_nodes_file(tmp_path)),
            ],
        )


def test_an_invalid_run_exits_nonzero(trace, scheduler, tmp_path, monkeypatch, capsys) -> None:
    """A sweep script reads this. A run that lost every request must not look like a
    success, and the console has to say why in words."""
    path, sha = trace
    endpoint = scheduler(deliver=False)
    out = tmp_path / "runs"

    rc = _main(
        monkeypatch,
        [
            str(path),
            "--scheduler",
            endpoint,
            "--run-id",
            "run_0005",
            "--sha256",
            sha,
            "--bind",
            "127.0.0.1:0",
            "--advertise",
            "127.0.0.1",
            "--out",
            str(out),
        ],
    )

    assert rc == 1
    printed = capsys.readouterr().out
    assert "INVALID" in printed
    assert "never returned a response" in printed


def test_the_log_is_one_json_object_per_line(trace, scheduler, tmp_path, monkeypatch) -> None:
    """JSONL, compact, newline-terminated — the pipeline streams it and Aditya's DES
    writes the same shape."""
    path, sha = trace
    endpoint = scheduler()
    out = tmp_path / "runs"

    _main(
        monkeypatch,
        [
            str(path),
            "--scheduler",
            endpoint,
            "--run-id",
            "run_0006",
            "--sha256",
            sha,
            "--bind",
            "127.0.0.1:0",
            "--advertise",
            "127.0.0.1",
            "--out",
            str(out),
        ],
    )

    blob = (out / "run_0006" / "client_run_0006.jsonl").read_bytes()
    assert blob.endswith(b"\n")
    for line in blob.decode().splitlines():
        assert json.loads(line)["run_id"] == "run_0006"


def test_a_wrong_trace_hash_stops_the_run_before_t0(trace, tmp_path, monkeypatch) -> None:
    """The trace's SHA-256 is its identity. Replaying a file that is not the one the
    manifest names would attribute the results to the wrong workload."""
    path, _ = trace
    with pytest.raises(ValueError, match="sha256 mismatch"):
        _main(
            monkeypatch,
            [
                str(path),
                "--scheduler",
                "127.0.0.1:1",
                "--run-id",
                "run_0007",
                "--sha256",
                "0" * 64,
                "--bind",
                "127.0.0.1:0",
                "--advertise",
                "127.0.0.1",
                "--out",
                str(tmp_path),
            ],
        )


def _colocated_nodes_file(tmp_path: Path) -> Path:
    """The same pool, but with two of its members on one physical host.

    F-9a exists because two logical nodes sharing a host contend for the same memory
    bandwidth and the same GPU, which reintroduces exactly the confound the study is trying
    to isolate. The launcher refuses to *start* such a pool; this is the case where someone
    drove `replay --nodes` directly and the manifest has to say what actually ran.
    """
    sample = json.loads(
        (
            Path(__file__).resolve().parents[2] / "contracts/examples/manifest.sample.json"
        ).read_text()
    )
    nodes = [n for n in sample["nodes"] if n.get("role", "pool") == "pool"][:2]
    assert len(nodes) == 2, "the C-6 example needs two pool nodes for this test to mean anything"
    for n in nodes:
        n["host"] = "one-box"
    path = tmp_path / "colocated.json"
    path.write_text(json.dumps(nodes))
    return path


def test_a_colocated_pool_is_counted_and_fails_the_run(
    trace, scheduler, tmp_path, monkeypatch, schema, capsys
) -> None:
    """`replay --nodes` used to write colocated_nodes: 0 for any pool at all.

    Only `main()` ever sees the node block, so `replay()` cannot count this and the manifest
    came out `valid: true` for a pool that was not admissible. The exit code has to agree
    with the manifest it sits beside, because that is what a sweep script reads.
    """
    path, sha = trace
    out = tmp_path / "runs"

    rc = _main(
        monkeypatch,
        [
            str(path),
            "--scheduler",
            scheduler(),
            "--run-id",
            "run_colo",
            "--sha256",
            sha,
            "--bind",
            "127.0.0.1:0",
            "--advertise",
            "127.0.0.1",
            "--out",
            str(out),
            "--nodes",
            str(_colocated_nodes_file(tmp_path)),
        ],
    )

    man = json.loads((out / "run_colo" / "manifest.json").read_text())
    assert_conforms(schema("manifest"), [man], "manifest")
    assert man["validity"]["colocated_nodes"] == 2
    assert man["validity"]["valid"] is False
    assert rc == 1
    assert "colocated node(s)" in capsys.readouterr().out
