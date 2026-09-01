"""Measure how far apart the machines' clocks are, and record it, at LAN setup.

Every duration in this study is measured on one host and never crossed with another
host's clock. That was a design decision, and it is the reason the pipeline has needed no
clock synchronisation so far. It also left an assumption unmeasured, and once the pool
spans two machines the assumption is doing real work, so this module turns it into a
number that lands in the manifest.

**Three separate facts about clocks, which get confused constantly.**

*Offset* is how far ahead host B's wall clock reads compared to host A's. It biases any
quantity built by subtracting one host's timestamp from another's. This study builds no
such quantity: `e2e_ms` is client-local, `queue_wait_ms` and `service_ms` are worker-local,
`decide_us` is scheduler-local, and `transport_residual_ms` is a difference of those
durations, in which a constant offset cancels exactly. So the offset corrects nothing. It
is recorded anyway, because it is the evidence that the clocks were being disciplined at
all, and because a reader is entitled to check the claim rather than take it.

*Epoch* is why the offset could not help even if we wanted it to. The two timestamps that
do cross the wire under C-1, `client_send_mono_ns` and `worker_mono_ns`, are
`CLOCK_MONOTONIC` reads, whose zero is that machine's boot. An NTP offset is a statement
about wall clocks and says nothing about the gap between two boot times. This is the
technical reason F-18's transport decomposition stays out of reach no matter how well the
clocks are synchronised, and the reason the split doc's "do not install PTP" advice
survives this module rather than being overturned by it.

*Rate* is the one that actually touches a measured number. If the worker's clock ticks
faster than the client's by r parts per million, then a worker-local `service_ms` is
inflated by r ppm relative to the client's `e2e_ms`, and the residual absorbs the
difference. That error is multiplicative in the duration, so it does not cancel. It is
also small: at the 1.1 ppm skew this hardware reports, a 2 s service time is off by 2 us,
roughly 500 times under the millisecond the residual is quoted in. `join`
applies the correction anyway and reports its size, so that "the clocks did not matter"
becomes a measured statement with a number attached instead of an assertion.

The practical consequence for the LAN setup is that **chrony's value here is rate
discipline, not offset**. Linux slews `CLOCK_MONOTONIC` along with the system clock, so a
disciplined host's monotonic clock ticks at the reference's rate; that is what makes two
hosts' durations comparable. Point every machine at one reference (the simplest choice is
the client host, running chronyd as a local server, with the workers peering to it: the
root dispersion then describes the LAN hop rather than a WAN pool server), then run
`clocksync --measure` on each machine and `clocksync --combine` on one of them.

The tool refuses to invent numbers. A host whose chronyd is not synchronised is reported
as unsynchronised, not as offset zero.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "MAX_OFFSET_MS",
    "MAX_RATE_ERROR_PPM",
    "ClockSync",
    "HostClock",
    "combine",
    "main",
    "measure",
    "parse_tracking",
    "rate_factors",
]

# A run whose hosts disagree by more than this is not a run with a small clock error, it
# is a run where the discipline was not working. The number is a proxy: the quantity that
# could actually corrupt a duration is the rate error below, and a host that has drifted
# a tenth of a second from its reference is not being rate-disciplined either. Chosen far
# above anything a working chronyd produces on a LAN (sub-millisecond) and far below the
# smallest service time this study measures (~600 ms), so it fires on breakage and never
# on noise.
MAX_OFFSET_MS = 100.0

# The rate error that would matter. At r ppm a service time of S ms carries an error of
# S * r * 1e-6 ms, so reaching 0.1 ms of error on the longest service time measured here
# (~2.3 s) needs about 43 ppm. 100 ppm is a clock nobody is disciplining: an untouched
# crystal sits in the tens of ppm and chrony pulls it to about 1.
MAX_RATE_ERROR_PPM = 100.0

_SECONDS = re.compile(r"^([-+]?[0-9.]+)\s+seconds\b")
_PPM = re.compile(r"^([-+]?[0-9.]+)\s+ppm\b")


@dataclass
class HostClock:
    """One machine's clock, as its own time daemon describes it.

    `offset_ms` is positive when this host's clock reads ahead of its reference.
    `rate_error_ppm` is signed and is the residual frequency error left after discipline,
    which is the term that scales a duration. `skew_ppm` is the daemon's own error bound
    on that estimate, and `dispersion_ms` is the bound on the offset. Both bounds are
    carried because a correction without its uncertainty is a number nobody can argue
    with, which is worse than no number.
    """

    host: str
    method: str = "none"
    synchronised: bool = False
    offset_ms: float = 0.0
    dispersion_ms: float = 0.0
    rate_error_ppm: float = 0.0
    skew_ppm: float = 0.0
    source: str = ""
    stratum: int = 0
    measured_unix: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "method": self.method,
            "synchronised": self.synchronised,
            "offset_ms": round(self.offset_ms, 4),
            "dispersion_ms": round(self.dispersion_ms, 4),
            "rate_error_ppm": round(self.rate_error_ppm, 4),
            "skew_ppm": round(self.skew_ppm, 4),
            "source": self.source,
            "stratum": self.stratum,
            "measured_unix": self.measured_unix,
        }
        if self.note:
            out["note"] = self.note
        return out

    def line(self) -> str:
        if not self.synchronised:
            return f"  UNSYNC {self.host:<16} {self.method:<9} {self.note}"
        return (
            f"  ok     {self.host:<16} {self.method:<9} "
            f"offset {self.offset_ms:+8.3f} ms  +/- {self.dispersion_ms:6.3f}  "
            f"rate {self.rate_error_ppm:+7.3f} ppm  +/- {self.skew_ppm:.3f}  "
            f"via {self.source}"
        )


@dataclass
class ClockSync:
    """The C-6 `clock_sync` block: every host's clock, on one stated reference.

    The reference host is the one whose timebase the corrected durations are expressed
    in. It is the client host by default, because `e2e_ms` is the measurement everything
    else is being reconciled against.
    """

    reference: str
    hosts: dict[str, HostClock] = field(default_factory=dict)
    measured_unix: int = 0

    @property
    def max_abs_offset_ms(self) -> float:
        """Largest gap between any two hosts, not between a host and its NTP source.

        Pairwise rather than absolute because a common-mode error against a distant
        upstream is not an error between the machines, and it is the machines that have
        to agree.
        """
        offsets = [h.offset_ms for h in self.hosts.values() if h.synchronised]
        return max(offsets) - min(offsets) if len(offsets) > 1 else 0.0

    @property
    def max_rate_error_ppm(self) -> float:
        """Largest rate difference between any two hosts, in ppm. The term that scales."""
        rates = [h.rate_error_ppm for h in self.hosts.values() if h.synchronised]
        return max(rates) - min(rates) if len(rates) > 1 else 0.0

    @property
    def unsynchronised(self) -> list[str]:
        return sorted(h for h, c in self.hosts.items() if not c.synchronised)

    @property
    def ok(self) -> bool:
        return (
            not self.unsynchronised
            and self.reference in self.hosts
            and self.max_abs_offset_ms <= MAX_OFFSET_MS
            and self.max_rate_error_ppm <= MAX_RATE_ERROR_PPM
        )

    def problems(self) -> list[str]:
        """Why this measurement should stop a run, in words. Empty when it should not."""
        out: list[str] = []
        if self.reference not in self.hosts:
            out.append(
                f"reference host {self.reference!r} was not measured, so there is no "
                "timebase to express the other hosts against"
            )
        if self.unsynchronised:
            out.append(
                f"unsynchronised host(s) {self.unsynchronised}: their clocks are not being "
                "rate-disciplined, so their durations are not comparable with the client's"
            )
        if self.max_abs_offset_ms > MAX_OFFSET_MS:
            out.append(
                f"hosts disagree by {self.max_abs_offset_ms:.1f} ms, over the "
                f"{MAX_OFFSET_MS:g} ms bound: the offset itself corrupts nothing here, but "
                "a gap this size means the discipline is not running"
            )
        if self.max_rate_error_ppm > MAX_RATE_ERROR_PPM:
            out.append(
                f"host clocks differ in rate by {self.max_rate_error_ppm:.1f} ppm, over the "
                f"{MAX_RATE_ERROR_PPM:g} ppm bound: worker-local durations are scaled "
                "relative to client-local ones by that fraction"
            )
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "measured_unix": self.measured_unix,
            "hosts": {h: c.to_dict() for h, c in sorted(self.hosts.items())},
            "max_abs_offset_ms": round(self.max_abs_offset_ms, 4),
            "max_rate_error_ppm": round(self.max_rate_error_ppm, 4),
            "ok": self.ok,
        }

    def summary(self) -> str:
        n = len(self.hosts)
        if n < 2:
            return (
                f"clocksync: 1 host ({self.reference}), so nothing was compared. This "
                "records that the clock was disciplined, not that two agree."
            )
        return (
            f"clocksync: {n} hosts on reference {self.reference}; "
            f"worst pair offset {self.max_abs_offset_ms:.3f} ms, "
            f"worst pair rate difference {self.max_rate_error_ppm:.3f} ppm"
        )


def parse_tracking(text: str, *, host: str, now_unix: int | None = None) -> HostClock:
    """Turn `chronyc tracking` output into a HostClock.

    Chrony reports the system clock as "N seconds slow/fast of NTP time", where slow means
    behind the reference. `offset_ms` inverts that into a signed number so the arithmetic
    downstream never has to read an English word.

    An unsynchronised daemon prints a zero Reference ID and a "Not synchronised" leap
    status while still printing plausible-looking numbers for everything else. Those
    numbers are stale, so they are dropped rather than recorded, and the host comes back
    marked unsynchronised.
    """
    clock = HostClock(host=host, method="chrony", measured_unix=now_unix or int(time.time()))
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()

    ref_id = fields.get("Reference ID", "")
    leap = fields.get("Leap status", "")
    if ref_id.startswith("00000000") or leap.startswith("Not synchronised"):
        clock.note = "chronyd is running but not synchronised to any source"
        return clock

    system = fields.get("System time", "")
    match = _SECONDS.match(system)
    if not match:
        clock.note = f"could not read a System time from chronyc: {system!r}"
        return clock
    seconds = float(match.group(1))
    # "slow of NTP time" means behind the reference, so the signed offset is negative.
    clock.offset_ms = -seconds * 1000.0 if "slow" in system else seconds * 1000.0

    for key, attr, pattern, scale in (
        ("Root dispersion", "dispersion_ms", _SECONDS, 1000.0),
        ("Residual freq", "rate_error_ppm", _PPM, 1.0),
        ("Skew", "skew_ppm", _PPM, 1.0),
    ):
        found = pattern.match(fields.get(key, ""))
        if found:
            setattr(clock, attr, float(found.group(1)) * scale)

    clock.source = ref_id.partition("(")[2].rstrip(")") or ref_id
    stratum = fields.get("Stratum", "")
    clock.stratum = int(stratum) if stratum.isdigit() else 0
    clock.synchronised = True
    return clock


def measure(host: str | None = None, *, timeout_s: float = 5.0) -> HostClock:
    """Read this machine's clock discipline from the local time daemon.

    Runs on each host during LAN setup. Only chrony is read, because it is what these
    machines run and because a second parser for a daemon nobody here uses is a second
    thing to keep correct. Anything else goes in through `--declare`, which records the
    number with `method: "declared"` so the audit trail shows it was not machine-read.
    """
    host = host or socket.gethostname()
    try:
        out = subprocess.run(
            ["chronyc", "tracking"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=True,
        )
    except FileNotFoundError:
        return HostClock(host=host, note="chronyc is not installed on this host")
    except (subprocess.SubprocessError, OSError) as exc:
        return HostClock(host=host, note=f"chronyc failed: {type(exc).__name__}: {exc}")
    return parse_tracking(out.stdout, host=host)


def combine(clocks: list[HostClock], *, reference: str | None = None) -> ClockSync:
    """One `clock_sync` block from the per-host measurements.

    `reference` defaults to the first host given, which the CLI orders so that the client
    host comes first. Passing it explicitly is better; defaulting silently to whichever
    file was listed first would make the timebase an accident of shell globbing.
    """
    if not clocks:
        raise ValueError("combine() needs at least one measured host")
    by_host = {c.host: c for c in clocks}
    if len(by_host) != len(clocks):
        raise ValueError("two measurements for the same host; each host is measured once")
    return ClockSync(
        reference=reference or clocks[0].host,
        hosts=by_host,
        measured_unix=max(c.measured_unix for c in clocks),
    )


def rate_factors(clock_sync: dict[str, Any] | None) -> dict[str, float]:
    """host -> the factor that puts that host's durations on the reference's timebase.

    A host whose clock runs r ppm fast reports a duration inflated by (1 + r*1e-6)
    relative to the reference, so dividing by that factor removes it. Only the difference
    between the two hosts' rate errors matters, since a common error is a rescaling of
    everything and cancels in the residual exactly as the offset does.

    Returns an empty mapping when there is nothing to correct, so the caller's default
    path is byte-identical to having no clock measurement at all rather than being a
    multiplication by a float that happens to be 1.0.
    """
    if not clock_sync:
        return {}
    hosts = clock_sync.get("hosts") or {}
    reference = clock_sync.get("reference")
    ref = hosts.get(reference)
    if not ref or not ref.get("synchronised"):
        return {}
    ref_ppm = float(ref.get("rate_error_ppm", 0.0))
    factors: dict[str, float] = {}
    for host, clock in hosts.items():
        if host == reference or not clock.get("synchronised"):
            continue
        delta_ppm = float(clock.get("rate_error_ppm", 0.0)) - ref_ppm
        if delta_ppm:
            factors[host] = 1.0 + delta_ppm * 1e-6
    return factors


def _load(path: Path) -> HostClock:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return HostClock(
        host=raw["host"],
        method=raw.get("method", "none"),
        synchronised=bool(raw.get("synchronised", False)),
        offset_ms=float(raw.get("offset_ms", 0.0)),
        dispersion_ms=float(raw.get("dispersion_ms", 0.0)),
        rate_error_ppm=float(raw.get("rate_error_ppm", 0.0)),
        skew_ppm=float(raw.get("skew_ppm", 0.0)),
        source=raw.get("source", ""),
        stratum=int(raw.get("stratum", 0)),
        measured_unix=int(raw.get("measured_unix", 0)),
        note=raw.get("note", ""),
    )


def _write(clock: HostClock, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"host": clock.host} | clock.to_dict(), indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Measure and record how far apart this pool's clocks are (C-6 clock_sync)"
    )
    ap.add_argument("--measure", action="store_true", help="read this host's time daemon")
    ap.add_argument("--host", help="name to record for this host (default: hostname)")
    ap.add_argument(
        "--declare",
        nargs=3,
        metavar=("HOST", "OFFSET_MS", "RATE_PPM"),
        help="record a host measured by some other means; marked method='declared'",
    )
    ap.add_argument(
        "--combine",
        nargs="+",
        type=Path,
        metavar="FILE",
        help="per-host files to fold into one clock_sync block",
    )
    ap.add_argument("--reference", help="host whose timebase the block is expressed in")
    ap.add_argument("--out", type=Path, help="write the result as JSON here")
    args = ap.parse_args(argv)

    if args.declare:
        host, offset, ppm = args.declare
        clock = HostClock(
            host=host,
            method="declared",
            synchronised=True,
            offset_ms=float(offset),
            rate_error_ppm=float(ppm),
            measured_unix=int(time.time()),
            note="declared by hand, not read from a time daemon",
        )
        print(clock.line())
        if args.out:
            _write(clock, args.out)
        return 0

    if args.measure:
        clock = measure(args.host)
        print(clock.line())
        if args.out:
            _write(clock, args.out)
        return 0 if clock.synchronised else 1

    if args.combine:
        sync = combine([_load(p) for p in args.combine], reference=args.reference)
        print(sync.summary())
        for clock in sync.hosts.values():
            print(clock.line())
        for problem in sync.problems():
            print(f"  PROBLEM {problem}")
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(sync.to_dict(), indent=2) + "\n")
        return 0 if sync.ok else 1

    ap.error("give --measure, --declare, or --combine")
    return 2  # pragma: no cover - argparse exits


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
