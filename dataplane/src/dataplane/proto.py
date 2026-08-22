"""C-1 stubs, generated on demand from `contracts/scheduling.proto`.

The stubs are gitignored (`*_pb2.py`) because the proto is the contract and the stubs
are a build artifact — committing them creates a second copy that can disagree with the
thing it was generated from, which is exactly the drift §12.2 is written against.

Import cost is one protoc invocation, once, and only when the proto is newer than what
was last generated:

    from dataplane.proto import sched_pb2, sched_grpc

Nothing else in the data plane imports this. `gen_trace` and `pipeline` stay importable
on a laptop with no gRPC toolchain and no contracts directory — the pipeline is a pure
function of (manifest, logs) and must not acquire a transport dependency by accident.
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["OUT_DIR", "PROTO", "generate", "sched_grpc", "sched_pb2"]


def _find_proto() -> Path:
    """Walk up from this file for contracts/scheduling.proto (works from a source checkout)."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "contracts" / "scheduling.proto"
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "contracts/scheduling.proto not found. The stub generator needs a source checkout; "
        "an installed wheel has no contracts/ directory."
    )


PROTO = _find_proto()
OUT_DIR = Path(__file__).resolve().parent / "_generated"


def generate(force: bool = False) -> Path:
    """Run protoc if the stubs are missing or older than the proto. Returns the output dir."""
    pb2 = OUT_DIR / "scheduling_pb2.py"
    grpc_py = OUT_DIR / "scheduling_pb2_grpc.py"
    fresh = pb2.exists() and grpc_py.exists() and pb2.stat().st_mtime >= PROTO.stat().st_mtime
    if fresh and not force:
        return OUT_DIR

    from grpc_tools import protoc

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rc = protoc.main(
        [
            "protoc",
            f"-I{PROTO.parent}",
            f"--python_out={OUT_DIR}",
            f"--pyi_out={OUT_DIR}",
            f"--grpc_python_out={OUT_DIR}",
            str(PROTO),
        ]
    )
    if rc != 0:
        raise RuntimeError(f"protoc failed on {PROTO} (exit {rc})")
    return OUT_DIR


generate()

# grpc_python_out emits `import scheduling_pb2` at top level, so the output directory has
# to be importable in its own right. Prepending is deliberate: this name is ours.
if str(OUT_DIR) not in sys.path:
    sys.path.insert(0, str(OUT_DIR))

import scheduling_pb2 as sched_pb2
import scheduling_pb2_grpc as sched_grpc
