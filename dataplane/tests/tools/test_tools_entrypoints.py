"""The `if __name__ == "__main__"` line of every gated script in tools/.

These are run as `uv run --project dataplane python tools/<script>.py`, which is the only
path that executes that line. `runpy.run_path` runs it in this process, where coverage can
see it; `--help` keeps it from doing any work.
"""

from __future__ import annotations

import runpy
import sys

import pytest
from support import REPO_ROOT

SCRIPTS = [
    "backfill_phase_split.py",
    "campaign_summary.py",
    "cell_intervals.py",
    "compare_sets.py",
    "contrast_check.py",
    "ensure_trace.py",
    "f23_compare.py",
    "hw_runs.py",
    "make_length_mix.py",
    "measure_transport_overhead.py",
    "p4_validate.py",
    "paper_figures.py",
    "parity_check.py",
    "phase_ratio.py",
    "promote_calibration.py",
    "refit_calibration.py",
    "restart_effect.py",
    "results_tables.py",
    "sweep.py",
    "tau_interval.py",
]


@pytest.mark.parametrize("script", SCRIPTS)
def test_the_script_runs_as_a_program(script, monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", [script, "--help"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(REPO_ROOT / "tools" / script), run_name="__main__")
    assert exc.value.code == 0
    assert "usage:" in capsys.readouterr().out
