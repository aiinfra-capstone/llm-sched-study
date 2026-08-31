"""Compatibility alias — the module now lives at `r_range`.

The Week-1 forward test named this deliverable `dataplane.calibration.r_range`, and I
built it as `rrange`. The implementation has moved to the contract's name; this module
stays so that importers of the old path keep working, and it re-exports rather than
wrapping so there is exactly one implementation to reason about.

It is a redirect, not a second API. New code should import `r_range` directly, and this
file should be deleted once nothing imports the old path.
"""

from __future__ import annotations

from dataplane.calibration.r_range import (
    NodeClassThroughput,
    RRange,
    from_reports,
    main,
    synthesizable,
    synthesizable_range,
)

__all__ = [
    "NodeClassThroughput",
    "RRange",
    "from_reports",
    "main",
    "synthesizable",
    "synthesizable_range",
]


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
