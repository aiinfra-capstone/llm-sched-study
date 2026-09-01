"""Before the pool spans two machines: prove the network can carry the experiment.

Everything measured so far ran on one host, where `127.0.0.1` hides every question this
module asks. The moment a second machine joins, three things can be wrong in ways that look
like *results* rather than like faults — and that is what makes them worth a separate check
rather than a careful first run.

**The response path is the one people forget.** Under F-11 the worker returns the response
**directly to the client**, so the client's `Deliver` port has to be reachable *inbound*
from every worker host. Nothing about setting up the scheduler exercises that. When it is
blocked, every request looks like a clean timeout: the dispatch succeeds, the worker serves
it, the record says `status: timeout`, and the run looks like a saturated pool rather than
a firewall. `--serve` and `--probe` exist to test that direction on purpose.

**Bandwidth is not the risk, and it is worth saying so numerically** so that nobody buys
hardware to fix it. A `Dispatch` for this study's largest bucket is about 1.6 KB, a
`Deliver` is 33 bytes, a heartbeat 47. A five-node pool at the measured load band runs at
roughly 0.08 Mbit/s. Service times are seconds and the wire is microseconds; even a 30 ms
hop is about 2% of the fastest end-to-end latency measured. What actually bites is
addressing, firewalls, and a node running a different engine build.

**Clock synchronisation is checked, but not for the reason people expect.** No duration in
this study is computed by subtracting timestamps taken on different hosts, and heartbeat
gaps are found through `Heartbeat.seq` rather than through time, so the *offset* between
two machines corrupts nothing and this preflight does not demand that it be small. What it
does check is that a time daemon is disciplining the clock at all, because Linux slews
`CLOCK_MONOTONIC` along with the system clock: a disciplined host's monotonic clock ticks
at the reference's *rate*, and that rate is what makes one host's `service_ms` comparable
with another host's `e2e_ms`. An undisciplined crystal sits tens of ppm off, which is
microseconds on a request and still nothing, but it is nothing that has been measured
rather than nothing that has been assumed. `clocksync` does the reading; this reports it
here so a two-machine pool cannot be brought up without the question being asked once.

What this refuses to do is guess. It reports what it measured and names what it could not
reach; deciding whether a 40 ms link is acceptable is a judgement about the experiment, not
a property of the network.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import grpc

from dataplane.harness.clocksync import HostClock, measure
from dataplane.harness.launch import build_nodes, colocated_count
from dataplane.proto import sched_grpc

__all__ = [
    "DEFAULT_SAMPLES",
    "PeerCheck",
    "PreflightReport",
    "check_peer",
    "main",
    "run_preflight",
    "serve_probe",
]

# Enough connects to see a p95 without making the check itself a load test. The number that
# matters is jitter, not the mean: a link whose p50 is 1 ms and p95 is 400 ms will produce
# send-lag violations that look like a slow pool.
DEFAULT_SAMPLES = 20

# A peer that cannot be reached in this long is not "slow", it is misconfigured.
DEFAULT_TIMEOUT_S = 3.0


@dataclass
class PeerCheck:
    """One endpoint, seen from wherever this ran. `role` is what it is expected to be."""

    name: str
    role: str
    endpoint: str
    reachable: bool = False
    grpc_ready: bool = False
    latencies_ms: list[float] = field(default_factory=list)
    error: str = ""

    @property
    def p50_ms(self) -> float:
        return statistics.median(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def p95_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        return ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]

    @property
    def ok(self) -> bool:
        # gRPC readiness rather than a bare TCP connect: a port that accepts and then does
        # not speak HTTP/2 is a proxy or a wrong service, and it fails at the first RPC
        # instead of here, which is much later and much harder to read.
        return self.reachable and self.grpc_ready

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "endpoint": self.endpoint,
            "reachable": self.reachable,
            "grpc_ready": self.grpc_ready,
            "connect_p50_ms": round(self.p50_ms, 3),
            "connect_p95_ms": round(self.p95_ms, 3),
            "samples": len(self.latencies_ms),
            "ok": self.ok,
            "error": self.error,
        }

    def line(self) -> str:
        if not self.ok:
            return f"  FAIL {self.role:<10} {self.name:<14} {self.endpoint:<24} {self.error}"
        return (
            f"  ok   {self.role:<10} {self.name:<14} {self.endpoint:<24} "
            f"connect p50 {self.p50_ms:6.2f} ms  p95 {self.p95_ms:6.2f} ms"
        )


@dataclass
class PreflightReport:
    peers: list[PeerCheck]
    pool_problems: list[str]
    hosts: list[str]
    colocated: int
    clock: HostClock | None = None

    @property
    def clock_warning(self) -> str:
        """Said out loud, and deliberately kept out of `ok`.

        A preflight sees one machine. An undisciplined clock here is a reason to fix this
        host before a two-machine run, but it is not evidence about the pool, and failing
        the check would conflate "the LAN is misconfigured" with "chronyd is not installed
        on the box I happened to run this from". The teeth live where a measurement of
        *every* host exists: `clocksync --combine` exits non-zero on the same condition,
        and `join` says so again over the rows.
        """
        if self.clock is None or self.clock.synchronised or len(self.hosts) < 2:
            return ""
        return (
            f"this host's clock is not disciplined ({self.clock.note}), so its monotonic "
            "clock ticks at its crystal's rate rather than a shared one, and its durations "
            "are not comparable with the other hosts'"
        )

    @property
    def ok(self) -> bool:
        return all(p.ok for p in self.peers) and not self.pool_problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_at_unix": int(time.time()),
            "hosts": self.hosts,
            "colocated_nodes": self.colocated,
            "pool_problems": self.pool_problems,
            "peers": [p.to_dict() for p in self.peers],
            **({"clock": {"host": self.clock.host} | self.clock.to_dict()} if self.clock else {}),
        }

    def summary(self) -> str:
        n_ok = sum(1 for p in self.peers if p.ok)
        head = (
            f"preflight: {n_ok}/{len(self.peers)} endpoint(s) reachable across "
            f"{len(self.hosts)} host(s)"
        )
        if self.colocated:
            head += f"; {self.colocated} co-located pool node(s) — F-9a says this run is invalid"
        elif len(self.hosts) < 2:
            head += "; single host, so nothing crossed a wire — this proves only that the "
            head += "services are up"
        return head

    def clock_line(self) -> str:
        return self.clock.line() if self.clock else ""


def _tcp_connect_ms(host: str, port: int, timeout_s: float) -> float:
    """One TCP connect, timed. Raises OSError if it cannot be made."""
    started = time.perf_counter()
    with socket.create_connection((host, port), timeout=timeout_s):
        return (time.perf_counter() - started) * 1000.0


async def _grpc_ready(endpoint: str, timeout_s: float) -> None:
    """Wait for a channel to reach READY, or raise. Invokes no method.

    Deliberately no RPC: `Worker.Begin` would open a log for a run that is not happening,
    and a preflight that leaves artifacts behind is one people stop running.
    """
    async with grpc.aio.insecure_channel(endpoint) as channel:
        await asyncio.wait_for(channel.channel_ready(), timeout=timeout_s)


def check_peer(
    name: str,
    role: str,
    endpoint: str,
    *,
    samples: int = DEFAULT_SAMPLES,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> PeerCheck:
    """Reachability, protocol, and connect-latency distribution for one endpoint."""
    check = PeerCheck(name=name, role=role, endpoint=endpoint)
    try:
        host, port_s = endpoint.rsplit(":", 1)
        port = int(port_s)
    except ValueError:
        check.error = f"{endpoint!r} is not host:port"
        return check

    for _ in range(max(1, samples)):
        try:
            check.latencies_ms.append(_tcp_connect_ms(host, port, timeout_s))
        except OSError as exc:
            check.error = f"{type(exc).__name__}: {exc}"
            return check
    check.reachable = True

    try:
        asyncio.run(_grpc_ready(endpoint, timeout_s))
    except (TimeoutError, grpc.aio.AioRpcError, OSError) as exc:
        check.error = f"TCP open but no gRPC: {type(exc).__name__}: {exc}"
        return check
    check.grpc_ready = True
    return check


def run_preflight(
    config: dict[str, Any],
    *,
    samples: int = DEFAULT_SAMPLES,
    clock: bool = True,
) -> PreflightReport:
    """Check every endpoint the config declares, the pool behind them, and this clock.

    The clock reading is local by construction: a preflight run on the client host can say
    whether *this* machine is disciplined and nothing about the workers'. That is why it is
    run on each host during setup, and why `clocksync --combine` is a separate step.
    """
    peers: list[PeerCheck] = []
    for role, key in (("scheduler", "scheduler"), ("client", "client_endpoint")):
        if config.get(key):
            peers.append(check_peer(role, role, config[key], samples=samples))
    for name, endpoint in sorted(config.get("workers", {}).items()):
        peers.append(check_peer(name, "worker", endpoint, samples=samples))

    problems: list[str] = []
    hosts: list[str] = []
    colocated = 0
    pool = config.get("pool")
    if pool:
        # Reuse the launcher rather than re-deriving F-9's homogeneity rules here: two
        # implementations of "is this a legal pool" is one more than can stay in agreement.
        try:
            nodes = build_nodes(pool, allow_colocation=True)
            colocated = colocated_count(nodes)
            hosts = sorted({n["host"] for n in nodes})
            if colocated:
                problems.append(
                    f"{colocated} co-located pool node(s) — F-9a requires one logical node "
                    "per physical host, and a run with any is marked invalid"
                )
            declared = set(config.get("workers", {}))
            missing = sorted(
                {n["node_id"] for n in nodes if n.get("role", "pool") == "pool"} - declared
            )
            if missing:
                problems.append(f"pool node(s) with no endpoint to check: {missing}")
        except ValueError as exc:
            problems.append(str(exc))
    return PreflightReport(
        peers=peers,
        pool_problems=problems,
        hosts=hosts,
        colocated=colocated,
        clock=measure() if clock else None,
    )


async def serve_probe(bind: str, *, stop: asyncio.Event) -> int:
    """Stand up a bare gRPC server so the far side can prove it can reach this port.

    This is how the F-11 response path gets tested before a run rather than after one. The
    client's `Deliver` port is inbound from every worker host, and no amount of checking
    from the client's side can tell you whether a worker can open it.
    """
    server = grpc.aio.server()
    sched_grpc.add_ClientServicer_to_server(sched_grpc.ClientServicer(), server)
    port = server.add_insecure_port(bind)
    await server.start()
    print(
        f"preflight probe listening on :{port} — run `preflight --probe <this host>:{port}` "
        "from each worker host",
        flush=True,
    )
    try:
        await stop.wait()
    finally:
        await server.stop(grace=1.0)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Prove the LAN can carry the experiment before a multi-node run"
    )
    ap.add_argument("config", type=Path, nargs="?", help="preflight config (JSON)")
    ap.add_argument(
        "--serve",
        metavar="BIND",
        help="stand up a probe server on this host so the far side can test inbound "
        "reachability of the F-11 response port",
    )
    ap.add_argument("--probe", metavar="HOST:PORT", help="check one endpoint and exit")
    ap.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    ap.add_argument(
        "--no-clock",
        action="store_true",
        help="skip the local clock-discipline reading (it shells out to chronyc)",
    )
    ap.add_argument("--out", type=Path, help="write the report as JSON here as well")
    args = ap.parse_args(argv)

    if args.serve:
        return asyncio.run(serve_probe(args.serve, stop=asyncio.Event()))

    if args.probe:
        check = check_peer("probe", "probe", args.probe, samples=args.samples)
        print(check.line())
        return 0 if check.ok else 1

    if args.config is None:
        ap.error("give a config, or --probe HOST:PORT, or --serve BIND")

    report = run_preflight(
        json.loads(args.config.read_text()), samples=args.samples, clock=not args.no_clock
    )
    print(report.summary())
    for peer in report.peers:
        print(peer.line())
    for problem in report.pool_problems:
        print(f"  POOL {problem}")
    if report.clock_line():
        print(report.clock_line())
    if report.clock_warning:
        print(f"  CLOCK {report.clock_warning}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report.to_dict(), indent=2) + "\n")
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
