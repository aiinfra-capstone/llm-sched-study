"""F-9a — the synthesizable heterogeneity range R, determined in Week 2 and reported.

R is the ratio of the fastest pool node's throughput to the slowest. It is the study's
main independent variable, and §5.1 makes establishing its *reachable range on real
hardware* a Week-2 deliverable in its own right: the simulator covers R beyond it, but
only once someone has said where "beyond" starts.

Under F-9 the engine, model and quantization are constant across the pool, so R is not a
property of what hardware I own — it is a property of what hardware I own **crossed with
what `-ngl`, `--threads` and `--parallel` can do to it**. A single GPU box yields several
node classes: fully offloaded, partially offloaded, CPU-only. That is what buys the
reduction in threat R2, and it is why this is measured rather than looked up.

**Two different numbers, and conflating them would overstate the result.**

`r_max_configured` is the ratio between the fastest and slowest node class I measured. It
describes what configuration can synthesize *per machine*, and it is the honest answer to
"how far apart can two node classes be made".

`r_max_deployable` is the largest ratio available to a pool that actually obeys F-9a,
which forbids co-locating logical nodes on one host — two logical nodes on one machine
contend for PCIe, memory bandwidth and cache, and would reintroduce as contention the
exact confound per-node throttling exists to remove. So the fastest and slowest members
of a deployed pool must sit on *different physical hosts*, and if every extreme class was
measured on the same box then the deployable ratio is smaller than the configured one.

When those two differ, the configured figure is a statement about configuration and the
deployable figure is the one that bounds MPR-2. Reporting the first as if it were the
second would claim a heterogeneity the pool cannot actually be run at.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Any

__all__ = ["NodeClassThroughput", "RRange", "synthesizable_range"]


@dataclass(frozen=True)
class NodeClassThroughput:
    """One (hardware x engine_config) node class and the tok/s it was measured at.

    `tokens_per_s` is decode tok/s at a stated operating point — the same definition used
    everywhere else in my half. A ratio of two numbers is only meaningful if both were
    measured the same way, and R is the ratio this whole study turns on.
    """

    node_class: str
    host: str
    tokens_per_s: float
    engine_config: dict[str, Any]
    model: str = "unspecified"

    def __post_init__(self) -> None:
        if self.tokens_per_s <= 0:
            raise ValueError(
                f"{self.node_class}: throughput must be > 0 to enter an R ratio, got "
                f"{self.tokens_per_s}"
            )


@dataclass(frozen=True)
class RRange:
    """The Week-2 answer, reported as a range rather than a single figure (§7, MPR-2)."""

    r_min: float
    r_max_configured: float
    r_max_deployable: float
    fastest: NodeClassThroughput
    slowest: NodeClassThroughput
    deployable_pair: tuple[NodeClassThroughput, NodeClassThroughput] | None
    n_classes: int
    n_hosts: int

    @property
    def colocation_limited(self) -> bool:
        """True when F-9a's no-co-location rule is what caps the deployable ratio.

        Worth surfacing explicitly, because the fix is not code: it is another machine.
        """
        return self.r_max_deployable < self.r_max_configured

    def to_dict(self) -> dict[str, Any]:
        return {
            "r_min": round(self.r_min, 4),
            "r_max_configured": round(self.r_max_configured, 4),
            "r_max_deployable": round(self.r_max_deployable, 4),
            "colocation_limited": self.colocation_limited,
            "model": self.fastest.model,
            "n_classes": self.n_classes,
            "n_hosts": self.n_hosts,
            "fastest": {
                "node_class": self.fastest.node_class,
                "host": self.fastest.host,
                "tokens_per_s": round(self.fastest.tokens_per_s, 4),
                "engine_config": self.fastest.engine_config,
            },
            "slowest": {
                "node_class": self.slowest.node_class,
                "host": self.slowest.host,
                "tokens_per_s": round(self.slowest.tokens_per_s, 4),
                "engine_config": self.slowest.engine_config,
            },
            "deployable_pair": (
                [self.deployable_pair[0].node_class, self.deployable_pair[1].node_class]
                if self.deployable_pair
                else None
            ),
        }

    def summary(self) -> str:
        """One line for the console and for a figure caption."""
        line = (
            f"R in [1.00, {self.r_max_deployable:.2f}] deployable across {self.n_hosts} host(s) "
            f"from {self.n_classes} node class(es)"
        )
        if self.colocation_limited:
            line += (
                f"; configuration alone reaches {self.r_max_configured:.2f} but F-9a forbids "
                "co-locating the extremes on one host"
            )
        return line


def synthesizable_range(classes: list[NodeClassThroughput]) -> RRange:
    """Compute both R ceilings from the measured node classes.

    `r_min` is 1.0 by construction — any pool can be made homogeneous by running one
    class on every host — and it is stated rather than assumed so the reported interval
    has both ends.
    """
    if len(classes) < 2:
        raise ValueError(
            f"R is a ratio between two node classes; got {len(classes)}. Sweep at least "
            "two (-ngl, threads, parallel) points before asking for a range"
        )
    models = {c.model for c in classes}
    if len(models) > 1:
        # F-9 holds engine, quantization AND model constant across a pool precisely so
        # that R is a property of hardware and configuration alone. An R computed across
        # two models is a ratio with a model effect baked into it, and neither the H1 2x2
        # decomposition nor the R-sweep can pull those apart afterwards. Model variety is
        # a replication axis *across run sets*, not a way to widen R inside one.
        raise ValueError(
            f"R would be computed across {len(models)} models ({sorted(models)}), which "
            "confounds the heterogeneity ratio with a model effect (F-9). Compute one "
            "range per model and report them separately"
        )
    fastest = max(classes, key=lambda c: c.tokens_per_s)
    slowest = min(classes, key=lambda c: c.tokens_per_s)

    cross_host = [(a, b) for a, b in combinations(classes, 2) if a.host != b.host]
    if cross_host:
        pair = max(
            cross_host,
            key=lambda ab: (
                max(ab[0].tokens_per_s, ab[1].tokens_per_s)
                / min(ab[0].tokens_per_s, ab[1].tokens_per_s)
            ),
        )
        hi, lo = max(pair, key=lambda c: c.tokens_per_s), min(pair, key=lambda c: c.tokens_per_s)
        deployable = hi.tokens_per_s / lo.tokens_per_s
        deployable_pair: tuple[NodeClassThroughput, NodeClassThroughput] | None = (hi, lo)
    else:
        # Every class measured on one machine. Configuration says one thing; a pool that
        # obeys F-9a cannot yet say anything, and 1.0 is the truthful floor rather than a
        # ratio borrowed from a pool that does not exist.
        deployable, deployable_pair = 1.0, None

    return RRange(
        r_min=1.0,
        r_max_configured=fastest.tokens_per_s / slowest.tokens_per_s,
        r_max_deployable=deployable,
        fastest=fastest,
        slowest=slowest,
        deployable_pair=deployable_pair,
        n_classes=len(classes),
        n_hosts=len({c.host for c in classes}),
    )


def from_reports(reports: list[dict[str, Any]]) -> list[NodeClassThroughput]:
    """Read node classes out of the `campaign.json` files each calibration run writes.

    Deliberately driven off the campaign's own output rather than a hand-maintained
    table: R is the study's main independent variable, and a hand-copied throughput
    figure is the easiest possible place to introduce an error nobody can find later.
    """
    out: list[NodeClassThroughput] = []
    for r in reports:
        if "model" not in r:
            raise ValueError(
                f"campaign report for {r.get('node_class', '?')!r} has no `model` field, so "
                "it cannot be checked against F-9's constant-model rule — re-run the "
                "campaign with a config that names its model"
            )
        out.append(
            NodeClassThroughput(
                node_class=r["node_class"],
                host=r["host"],
                tokens_per_s=r["headline_tokens_per_s"],
                engine_config=r["engine_config"],
                model=r["model"],
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    from pathlib import Path

    ap = argparse.ArgumentParser(
        description="F-9a — the synthesizable R range across calibrated node classes"
    )
    ap.add_argument(
        "root",
        type=Path,
        help="directory of calibration runs; every campaign.json under it is read",
    )
    ap.add_argument("--out", type=Path, help="write the range as JSON here as well as printing it")
    args = ap.parse_args(argv)

    reports = [json.loads(p.read_text()) for p in sorted(args.root.rglob("campaign.json"))]
    if not reports:
        raise SystemExit(f"no campaign.json under {args.root} — run the calibration campaign first")

    classes = from_reports(reports)
    rng = synthesizable_range(classes)
    print(f"model: {classes[0].model}")
    print(rng.summary())
    for c in sorted(classes, key=lambda c: -c.tokens_per_s):
        print(f"  {c.tokens_per_s:9.2f} tok/s  {c.node_class}  ({c.host}, {c.engine_config})")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(rng.to_dict(), indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
