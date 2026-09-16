"""tools/campaign_summary.py from the command line: the files it writes and the tables in them.

The markdown is what gets pasted into the writing brief, so every branch of it is rendered
once here: a point with H1 defined and one without, failures, utilisation, operating R, the
load trend, and a value that is not available.
"""

from __future__ import annotations

import json

import campaign_summary as cs
import numpy as np
from support import cell_rows, frame, manifests_for

POLICIES = ("round_robin", "static_weighted", "jsq", "wjsq", "threshold")


def _campaign(tmp_path):
    rng = np.random.default_rng(3)
    runs = []
    for lam, scale in ((1.0, 1.0), (3.0, 1.3)):
        for k, p in enumerate(POLICIES):
            for rep in (1, 2):
                values = rng.normal(900 + 60 * k, 20, 30) * scale
                kw = {"trace_sha256": "a" * 64}  # the first pair's layout: one trace
                if lam == 3.0 and p == "round_robin":
                    values = np.linspace(1500, 3000, 30)  # transient at the heavy point
                if lam == 1.0 and p == "jsq" and rep == 2:
                    kw["warmup"] = [1]
                    kw["status"] = {1: "timeout"}
                runs.append(cell_rows(p, values, lam=lam, repeat=rep, point=f"l{lam:g}", **kw))
    data = frame(*runs)
    path = manifests_for(tmp_path / "set", data)
    data.to_parquet(path)
    return path


def test_main_writes_json_and_markdown_and_prints_the_markdown(tmp_path, capsys) -> None:
    path = _campaign(tmp_path)
    out = tmp_path / "report" / "summary"
    assert cs.main([str(path), "--out", str(out), "--draws", "60", "--seed", "5"]) == 0

    summary = json.loads(out.with_suffix(".json").read_text())
    md = out.with_suffix(".md").read_text()
    assert capsys.readouterr().out.strip() == md.strip()
    assert summary["bootstrap"]["draws"] == 60
    assert [pt["lambda_rps"] for pt in summary["points"]] == [1.0, 3.0]

    assert "Arrivals independent across repeats: **no**" in md
    assert "conditional on one arrival sequence" in md
    assert "Failures over whole runs, warmup included:" in md
    assert "### 1.0 req/s" in md and "### 3.0 req/s" in md
    assert "| H1 on | Gain, queue-blind ms |" in md
    assert "H1 undefined: transient cell(s) round_robin." in md
    assert "| RR - SW | undefined: transient cell(s) round_robin | |" in md
    assert "| JSQ - WJSQ |" in md
    assert "Pool capacity" in md
    assert "Operating R in steady cells" in md
    assert "### Queue-aware calibration gain against load" in md


def test_markdown_says_so_when_arrivals_were_independent_and_staleness_was_injected() -> None:
    summary = {
        "fast_node": "rtx3050",
        "bootstrap": {"draws": 1, "method": "m"},
        "arrivals_independent": True,
        "scheduler_seed_varied": True,
        "primary_statistic": "p",
        "steady_state_rule": "r",
        "failures": {},
        "load_trend_queue_aware": None,
        "points": [
            {
                "lambda_rps": 2.0,
                "staleness_s": 5.0,
                "block_length_requests": 3,
                "measured_arrivals": 10,
                "policies": {
                    "jsq": {
                        "steady_state": {"transient": True, "last_over_first_third": 1.3},
                        "lag1_autocorrelation": 0.5,
                        "share_to_fast_node": 0.5,
                        **{
                            s: {"value": 100.0, "ci95": [90.0, 110.0]}
                            for s in ("mean", "p50", "p95", "p99", "tpot_mean")
                        },
                        "ttft_mean": {"value": None, "ci95": [None, None]},
                        "ttft_p95": {"value": None, "ci95": [None, None]},
                        **{
                            f"slo_{kind}_{k}x": {"value": 0.5, "ci95": [0.4, 0.6]}
                            for kind in ("e2e", "ttft")
                            for k in (2, 5)
                        },
                    }
                },
                "h1": {},
                "h1_status": "undefined: no valid run for jsq",
                "calibration_gain": {
                    "queue_blind": {"status": "undefined: x"},
                    "queue_aware": {"status": "undefined: y"},
                },
                "utilisation": None,
                "operating_R_steady_cells": {"buckets": {}},
            }
        ],
    }
    md = cs.markdown(summary)
    assert "Arrivals independent across repeats: **yes**" in md
    assert "conditional on one arrival sequence" not in md
    assert "### 2.0 req/s, staleness 5 s" in md
    assert "Failures over whole runs" not in md
    assert "Operating R" not in md
    assert "| jsq | **no** | 1.30 |" in md
    assert "| jsq | n/a | n/a | 100.0 | 50% [40%, 60%] |" in md
