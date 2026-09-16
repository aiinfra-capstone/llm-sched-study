"""Builders shared by the tests of the scripts in tools/.

Not a conftest.py: tests/conftest.py is imported by name (`from conftest import ...`), and a
second conftest.py here would shadow it for every test collected after this directory.

The scripts are imported as top-level modules (`pythonpath = ["../tools"]`), the way they
import each other. Two builders live here because several files need them:

  * `runset_frame` writes rows shaped like a `runset.parquet`, with one column per field
    `campaign_summary.py` reads, so a test states only the latencies and statuses it is
    about.
  * `campaign_dict` returns a campaign config that passes `check_campaign` against the
    committed cost models, so a refusal test changes exactly one thing.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIGS = REPO_ROOT / "dataplane" / "configs"
SNAPSHOTS = {
    "gtx1650ti": "cm_gtx1650ti_ngl99_p4_q4km_llama32_1b_20260831T153652Z_008",
    "rtx3050": "cm_rtx3050_ngl99_p4_q4km_llama32_1b_20260914T200053Z_008",
}
TAG = "t"


def pool_nodes() -> list[dict[str, Any]]:
    """The committed 1650 Ti / 3050 pool, copied from the seeded anchor campaign."""
    return json.loads((CONFIGS / "hw_seeded_anchor_3050.json").read_text())["nodes"]


def run_id(policy: str, point: str = "p1", repeat: int = 1, staleness: float = 0.0) -> str:
    return f"{TAG}_{policy}_s{staleness:g}_{point}_r{repeat}"


def cell_rows(
    policy: str,
    e2e: Sequence[float],
    *,
    repeat: int = 1,
    lam: float = 2.0,
    staleness: float = 0.0,
    point: str = "p1",
    status: dict[int, str] | None = None,
    warmup: Sequence[int] = (),
    drop: Sequence[int] = (),
    **columns: Any,
) -> list[dict[str, Any]]:
    """Rows for one run. Positions are 1-based req_ids `r000001`...

    `status` maps a position to a non-ok status, `warmup` lists positions inside warmup,
    and `drop` lists positions whose row is missing from the log. Any other keyword is a
    column: a scalar applies to every row, a sequence gives one value per position.
    """
    rid = run_id(policy, point, repeat, staleness)
    status = status or {}
    out = []
    for i, value in enumerate(e2e, start=1):
        if i in drop:
            continue
        row = {
            "run_id": rid,
            "policy": policy,
            "lambda": lam,
            "staleness_s": staleness,
            "R": 2.0,
            "req_id": f"r{i:06d}",
            "bucket_id": "p128_o64",
            "output_len": 64,
            "intended_offset_s": float(i),
            "e2e_ms": float(value),
            "status": status.get(i, "ok"),
            "chosen_node": "rtx3050" if i % 2 else "gtx1650ti",
            "queue_wait_ms": 1.0,
            "prefill_ms": 20.0,
            "decode_ms": 630.0,
            "service_ms": 651.0,
            "transport_residual_ms": 5.0,
            "is_warmup": i in warmup,
            "vehicle": "hardware",
            "trace_sha256": f"{repeat:064x}",
        }
        for k, v in columns.items():
            row[k] = v[i - 1] if isinstance(v, (list, tuple)) else v
        out.append(row)
    return out


def frame(*runs: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame([r for rows in runs for r in rows])


def write_manifest(
    root: Path,
    rid: str,
    *,
    policy: str,
    lam: float = 2.0,
    staleness: float = 0.0,
    gen_seed: int | None = 1,
    seed: int | None = None,
    validity: dict[str, Any] | None = None,
    buckets: Sequence[str] = ("p128_o64",),
) -> Path:
    """A post-run manifest beside a run set, with only the fields the summary reads."""
    v = {
        "max_send_lag_ms": 1.0,
        "send_lag_violations": 0,
        "dropped_requests": 0,
        "heartbeat_gaps": 0,
        "engine_restarts": 0,
        "colocated_nodes": 0,
        "valid": True,
    } | (validity or {})
    config: dict[str, Any] = {
        "length_dist": {"buckets": list(buckets), "weights": [1.0] * len(buckets)},
    }
    if gen_seed is not None:
        config["gen_seed"] = gen_seed
    if seed is not None:
        config["seed"] = seed
    man = {
        "run_id": rid,
        "policy": policy,
        "lambda": lam,
        "staleness_s": staleness,
        "config": config,
        "cost_model_snapshots": dict(SNAPSHOTS),
        "nodes": pool_nodes(),
        "validity": v,
    }
    path = root / rid / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(man))
    return path


def manifests_for(root: Path, data: pd.DataFrame, **kw: Any) -> Path:
    """One manifest per run in `data`, and the runset path `summarise` expects beside them."""
    for rid, g in data.groupby("run_id"):
        write_manifest(
            root,
            rid,
            policy=g["policy"].iloc[0],
            lam=float(g["lambda"].iloc[0]),
            staleness=float(g["staleness_s"].iloc[0]),
            **kw,
        )
    return root / "runset.parquet"


def campaign_dict(tmp_path: Path, **over: Any) -> dict[str, Any]:
    """The seeded anchor campaign, writing under tmp_path, with a trace config by absolute path."""
    d = json.loads((CONFIGS / "hw_seeded_anchor_3050.json").read_text())
    d["trace_config"] = str(CONFIGS / "trace_anchor_1b.json")
    d["out_root"] = str(tmp_path / "out")
    d.update(over)
    return d


def ceil_cbrt(n: int) -> int:
    return math.ceil(n ** (1 / 3))
