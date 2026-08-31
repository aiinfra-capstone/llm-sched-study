"""The LAN preflight, and the failure it exists to catch before a run rather than after one.

Under F-11 the worker returns responses **directly to the client**, so the client's port is
inbound from every worker host — and nothing about bringing the scheduler up exercises that
direction. When it is blocked every request looks like a clean timeout: dispatch succeeds,
the worker serves it, the record says `timeout`, and a firewall is indistinguishable from a
saturated pool. That is the shape of bug this module exists for, so most of these tests are
about reporting an unreachable peer precisely rather than plausibly.
"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent import futures

import grpc
import pytest

from dataplane.harness import preflight
from dataplane.proto import sched_grpc

NODE = {
    "node_id": "gtx1650ti",
    "host": "fedora",
    "role": "pool",
    "engine": "llamacpp",
    "engine_version": "b10569+p1+cuda13.2",
    "model": "Meta-Llama-3-8B-Instruct",
    "quant": "Q4_K_M",
    "gpu": "NVIDIA GeForce GTX 1650 Ti",
    "prefix_caching": False,
    "max_batch": 4,
    "engine_config": {"ngl": 20, "threads": 6, "parallel": 4},
}


def _with_server(fn):
    """Run `fn(endpoint)` against a real gRPC server.

    A **synchronous** server on purpose: `check_peer` is a CLI helper and calls
    `asyncio.run` itself, so a fixture built on `grpc.aio` would be asking it to nest event
    loops. The thing under test is reachability, and a threaded server is as real a peer as
    an async one.
    """
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    sched_grpc.add_ClientServicer_to_server(sched_grpc.ClientServicer(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        return fn(f"127.0.0.1:{port}")
    finally:
        server.stop(None).wait(timeout=5)


def test_a_reachable_endpoint_reports_its_connect_distribution() -> None:
    """Jitter is the number that matters, not the mean: a link with a 1 ms p50 and a 400 ms
    p95 produces send-lag violations that read as a slow pool."""
    check = _with_server(lambda ep: preflight.check_peer("client", "client", ep, samples=5))
    assert check.ok and check.reachable and check.grpc_ready
    assert len(check.latencies_ms) == 5
    assert check.p95_ms >= check.p50_ms >= 0.0
    assert "ok " in check.line()
    assert check.to_dict()["samples"] == 5


def test_an_unreachable_port_is_named_not_guessed_at() -> None:
    """This is the firewall case, and the whole point is that it says so here instead of
    turning into 200 timeouts three minutes into a run."""
    check = preflight.check_peer("client", "client", "127.0.0.1:9", samples=2)
    assert not check.ok and not check.reachable
    assert check.error
    assert "FAIL" in check.line()
    assert check.p50_ms == 0.0 and check.p95_ms == 0.0


def test_a_port_that_accepts_but_does_not_speak_grpc_is_not_ok() -> None:
    """A proxy, or the wrong service on the right port. Without this it fails at the first
    RPC instead, which is much later and much harder to read."""
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    endpoint = f"127.0.0.1:{sock.getsockname()[1]}"
    try:
        check = preflight.check_peer("sched", "scheduler", endpoint, samples=2, timeout_s=0.5)
    finally:
        sock.close()
    assert check.reachable and not check.grpc_ready and not check.ok
    assert "no gRPC" in check.error


def test_an_endpoint_that_is_not_host_port_is_refused_before_any_socket() -> None:
    check = preflight.check_peer("x", "worker", "not-an-endpoint", samples=1)
    assert not check.reachable and "is not host:port" in check.error


def test_a_single_host_pool_says_that_nothing_crossed_a_wire() -> None:
    """The check passes and still has to be honest: on one box it proves the services are
    up, and nothing at all about the network."""
    report = _with_server(
        lambda ep: preflight.run_preflight(
            {"scheduler": ep, "client_endpoint": ep, "workers": {"gtx1650ti": ep}, "pool": [NODE]},
            samples=2,
        )
    )
    assert report.ok
    assert report.hosts == ["fedora"]
    assert "nothing crossed a wire" in report.summary()
    assert [p.role for p in report.peers] == ["scheduler", "client", "worker"]


def test_two_logical_nodes_on_one_host_fail_the_pool_check(tmp_path) -> None:
    """F-9a. The launcher refuses to start such a pool; preflight says so before anyone tries."""
    report = _with_server(
        lambda ep: preflight.run_preflight(
            {
                "workers": {"gtx1650ti": ep, "second": ep},
                "pool": [NODE, {**NODE, "node_id": "second"}],
            },
            samples=1,
        )
    )
    assert not report.ok
    assert report.colocated == 2
    assert any("co-located" in p for p in report.pool_problems)
    assert "F-9a says this run is invalid" in report.summary()


def test_a_pool_node_with_no_endpoint_is_reported_rather_than_skipped() -> None:
    """A node nobody checked is a node that fails during the run instead."""
    report = _with_server(
        lambda ep: preflight.run_preflight(
            {
                "workers": {"gtx1650ti": ep},
                "pool": [NODE, {**NODE, "node_id": "node2", "host": "laptop"}],
            },
            samples=1,
        )
    )
    assert not report.ok
    assert any("no endpoint to check" in p for p in report.pool_problems)


def test_a_pool_that_breaks_f9_is_reported_by_the_launcher_not_re_derived_here() -> None:
    """Two implementations of 'is this a legal pool' is one more than can stay in agreement,
    so the homogeneity rules come from `launch` and the message comes back verbatim."""
    report = preflight.run_preflight(
        {
            "pool": [
                NODE,
                {**NODE, "node_id": "n2", "host": "laptop", "model": "Llama-3.2-1B-Instruct"},
            ]
        },
        samples=1,
    )
    assert not report.ok
    assert any("not homogeneous" in p for p in report.pool_problems)


def test_cli_probe_mode_checks_one_endpoint(capsys) -> None:
    rc = _with_server(lambda ep: preflight.main(["--probe", ep, "--samples", "2"]))
    assert rc == 0
    assert "ok   probe" in capsys.readouterr().out
    assert preflight.main(["--probe", "127.0.0.1:9", "--samples", "1"]) == 1


def test_cli_writes_a_record_and_exits_nonzero_on_a_bad_pool(tmp_path, capsys) -> None:
    out = tmp_path / "preflight.json"

    def _run(ep):
        cfg = tmp_path / "pre.json"
        cfg.write_text(
            json.dumps(
                {
                    "scheduler": ep,
                    "client_endpoint": ep,
                    "workers": {"gtx1650ti": ep},
                    "pool": [NODE],
                }
            )
        )
        return preflight.main([str(cfg), "--samples", "2", "--out", str(out)])

    assert _with_server(_run) == 0
    written = json.loads(out.read_text())
    assert written["ok"] is True
    assert written["hosts"] == ["fedora"]
    assert len(written["peers"]) == 3

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"workers": {"gone": "127.0.0.1:9"}, "pool": [NODE]}))
    assert preflight.main([str(bad), "--samples", "1"]) == 1
    assert "no endpoint to check" in capsys.readouterr().out


def test_cli_needs_something_to_do(capsys) -> None:
    with pytest.raises(SystemExit):
        preflight.main([])


def test_serve_mode_stands_up_a_port_for_the_far_side_to_prove_it_can_reach(capsys) -> None:
    """The F-11 response path is inbound to the client, and no check run *on* the client can
    tell you whether a worker can open it."""

    # The probe server owns its own loop on its own thread, so the check can run as a plain
    # synchronous call from here — which is how an operator runs it, from the other machine.
    result: dict[str, int] = {}
    stop_flag = threading.Event()

    def serve():
        async def go():
            stop = asyncio.Event()
            waiter = asyncio.get_running_loop().run_in_executor(None, stop_flag.wait)
            task = asyncio.create_task(preflight.serve_probe("127.0.0.1:50079", stop=stop))
            await waiter
            stop.set()
            result["rc"] = await task

        asyncio.run(go())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        for _ in range(50):
            check = preflight.check_peer("probe", "probe", "127.0.0.1:50079", samples=2)
            if check.ok:
                break
    finally:
        stop_flag.set()
        thread.join(timeout=10)

    assert check.ok
    assert result["rc"] == 0
    assert "preflight probe listening" in capsys.readouterr().out


def test_a_two_host_pool_stops_apologising_for_itself() -> None:
    """The single-host caveat has to disappear once there really is a wire, or nobody will
    believe the line the day it matters."""
    report = _with_server(
        lambda ep: preflight.run_preflight(
            {
                "workers": {"gtx1650ti": ep, "node2": ep},
                "pool": [NODE, {**NODE, "node_id": "node2", "host": "laptop"}],
            },
            samples=1,
        )
    )
    assert report.ok and report.colocated == 0
    assert report.hosts == ["fedora", "laptop"]
    assert "2 host(s)" in report.summary()
    assert "nothing crossed a wire" not in report.summary()


def test_endpoints_can_be_checked_without_a_pool_description() -> None:
    """Bringing one machine up before the pool is written down is a normal thing to do."""
    report = _with_server(lambda ep: preflight.run_preflight({"scheduler": ep}, samples=1))
    assert report.ok and report.hosts == [] and report.pool_problems == []


def test_cli_serve_mode_hands_off_to_the_probe_server(monkeypatch) -> None:
    seen = {}

    async def _fake(bind, *, stop):
        seen["bind"] = bind
        return 0

    monkeypatch.setattr(preflight, "serve_probe", _fake)
    assert preflight.main(["--serve", "0.0.0.0:50071"]) == 0
    assert seen["bind"] == "0.0.0.0:50071"
