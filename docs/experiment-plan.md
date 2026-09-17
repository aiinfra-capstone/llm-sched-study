# Experiment plan

What we run, in what order, and what has to be true before each one starts. Status as of
**2026-09-16**. Owners: **D** Divyansh (data plane, measurement), **A** Aditya (control plane,
simulation), **J** joint.

This document is edited in place as campaigns finish. It does not grow new planning documents
beside it.

---

## 1. Gates

A gate is a condition that stops work rather than a task to schedule around. Each one exists
because running past it would spend machine time that has to be spent again.

| Gate | Condition | Why |
|---|---|---|
| **G0** | The working tree is committed before a campaign starts, and the campaign's config is in that commit | A run whose code state is not recoverable is not a data point |
| **G1** | `tools/engine_bench.sh` has run on both nodes, the 1650 Ti has been rebuilt with `pool-install.sh`, and it has been benched again | The 1650 Ti binary reports build 1 with unknown CMake flags. If the rebuild moves its prefill speed, the 10x prefill ratio was partly an engine effect and K1 needs re-measuring. One hour now protects fourteen |
| **G2** | The 1650 Ti has been recalibrated under driver 580.178.04, `-c 55296` and `--cache-ram 0` | Four campaigns set their load from its cost model, and the committed snapshot predates the context pin and the driver |
| **G3** | `analysis-plan.md` is frozen and committed | The decision rules have to exist before the results do |
| **G4** | The simulator passes the contrast criterion (analysis plan 6.6) | Nothing from the simulator is citable before it, including H2 |

G0, G1, G2 and G3 gate the hardware campaigns. G4 gates only the simulator sweeps.

## 2. Standing constraints

- **The 3050 laptop is not here.** It went back to its owner on 2026-09-15. Every hardware
  campaign below needs it back, and the whole set fits in two nights once it is.
- **The rebuild needs a person.** `pool-install.sh` installs packages, writes a chrony
  drop-in and restarts a service, all with sudo, and it stops for confirmation. It is not
  run unattended, so G1's rebuild is a supervised step and everything gated behind it waits
  for one.
- **The harness shares a host with the 1650 Ti.** The scheduler and the replay client sit on
  the same 6-core laptop as that node's engine. Acceptable for a fully offloaded GPU node,
  and not acceptable for a CPU node, which is why a third machine gates any CPU pair.
- **One engine command, recorded.** `--cache-ram 0` from now on, and the driver records each
  engine's command line, start time and library hashes into every manifest.
- **Machine time is the scarce resource**, not code. About 15 hours of replay stands between
  today and the full claims ladder.

## 3. Order

Blocks run top to bottom. Inside a block, order is fixed.

### Block 0: no machine, do first

| # | Work | Owner | Closes |
|---|---|---|---|
| E0.1 | Commit the current tree and this doc set | D | G0, G3 |
| E0.2 | Settle the coverage gate: the omit list for `tools/p4_validate.py`, and the uncovered refusals in `hw_runs.py` and `campaign_summary.py` | D | `test-plan.md` section 6 |
| E0.3 | Simulator sweep for the value-of-calibration curve, so the hardware arm lands on a curve we already understand | A | K2, first half |
| E0.4 | P4 on contrasts against the three held-out shape run sets | A | G4 |

E0.1 and E0.2 are done (2026-09-16). The tree and this doc set are committed, `p4_validate.py`
is in the coverage omit list, and `hw_runs.py` and `campaign_summary.py` are back at 100%, so
G0 and G3 hold. One item found while closing E0.2 moved to `test-plan.md` section 2.

### Block 1: this laptop only, about 3 hours

| # | Work | Cost | Owner | Closes |
|---|---|---|---|---|
| E1.1a | `engine_bench.sh` on the 1650 Ti as it stands, which also records the CMake flags the installed engine was built with | 15 min | D | G1, first half |
| E1.1b | Rebuild with `pool-install.sh`, bench again, compare pp512 and tg128 with E1.1a and with published figures for the card | 45 min, supervised | D | G1 |
| E1.2 | Recalibrate the 1650 Ti on the 24-cell grid under the campaign engine settings | 25 min | D | G2 |
| E1.3 | The 80-minute sustained segment on the CPU 1B class, then `tools/tau_interval.py` | 1.5 h | D | K6 |

If E1.1 moves the 1650 Ti's prefill by more than the calibration's own interval, stop. The
first pair's K1 numbers are then about a binary we cannot characterise, and the campaigns wait
until the pair has been re-measured on the rebuilt engine.

### Block 2: the pool, night one, about 7 hours

Run in this order. The ablation goes first because it decides which headline sentence the
paper carries, and that is worth knowing before the rest of the machine time is spent.

| # | Campaign | Runs | Replay | Closes |
|---|---|---:|---:|---|
| E2.1 | `hw_calibration_ablation_3050.json` | 48 | 1.5 h | K2 |
| E2.2 | `hw_seeded_anchor_3050.json` | 75 | 2.6 h | K3, and the first intervals that include arrivals |
| E2.3 | `hw_staleness_h1_3050.json` | 36 | 1.2 h | K5, staleness half |

### Block 3: the pool, night two, about 7 hours

| # | Campaign | Runs | Replay | Closes |
|---|---|---:|---:|---|
| E3.1 | `hw_heavytail_3050.json` | 72 | 3.7 h | K5, size-variability half |
| E3.2 | `hw_shapes_matched_3050.json` | 45 | 4.0 h | K4 |

### Block 4: after the hardware, if time allows

| # | Work | Cost | Owner | Notes |
|---|---|---|---|---|
| E4.1 | Simulator sweeps for H2, at fixed utilisation and at fixed λ, with 1 fast plus k slow | hours of simulator time | A | Needs G4 and the load-normalisation change in `sweep.py` |
| E4.2 | Harness on a third host, one anchor rerun | 2.6 h | J | Removes the co-location threat from K1 |
| E4.3 | A second real pair, CPU class on one laptop | 8 to 12 h | D, J | Only after Block 3. Running it earlier copies the design into three pairs |
| E4.4 | The H3 instrument | days | A, D | Out of paper one by `research-plan.md` section 5 |

## 4. The campaigns

Each config carries its own reasoning in its `_comment`. What follows is what each one is for
and what would make it a wasted night.

| Campaign | Purpose | Controls it adds | Fails if |
|---|---|---|---|
| `hw_calibration_ablation_3050` | The value-of-calibration curve (K2) | Believed capability ratio as the independent variable; `jsq_fastfirst` as the ordinal-only control; `ect` in both modes; `static_weighted_wrr` in place of the random draw | The arms are not all in one run set with the arm recorded, or `ect`'s unknown-mode prior is left at a value nobody chose |
| `hw_seeded_anchor_3050` | K3 with honest intervals | Five independent arrival seeds and scheduler seeds; load set as 0.2, 0.3 and 0.4 of pool capacity | `arrivals_independent` comes out false, or a point lands transient because the capacity estimate was off |
| `hw_staleness_h1_3050` | K5, queue staleness | The veil at 0, 1 and 5 s with staleness 0 in the same set so contrasts stay paired | Anyone reads it as an H3 measurement |
| `hw_heavytail_3050` | K5, size variability | 66 length buckets from clamped lognormals, output coefficient of variation about 0.9; MMPP bursts beside Poisson at the same utilisation; `ect` in unknown mode with a prior of 44 tokens, this mix's mean output | Lengths fall outside calibrated cost-model cells, or the comparison trace is not at matched utilisation |
| `hw_shapes_matched_3050` | K4 | Slow-node utilisation held at 0.7 across all three shapes; shapes interleaved within each point; shared repeat seeds | The realised slow-node load still differs across shapes, which the summary will show |

**One thing to set before E2.1.** `ECT` in unknown mode uses a fixed prior output length,
currently 16 tokens, while the heavy-tailed trace has a mean output of 43. The prior is a free
parameter that decides how good the strongest calibrated baseline looks, so it is set to the
workload's mean output length and recorded in the config before the arm runs.

## 5. After every campaign

Done in order, before the next campaign starts. A campaign that skips this is not finished.

1. Check the manifest fields that decide whether a run is a data point: `send_lag_violations`,
   `dropped_requests`, `colocated_nodes`, `clock_sync.ok`, the engine verdict, and the
   whole-run failure count.
2. Regenerate the run set and the summary, and read the transient marks and
   `arrivals_independent` before reading any number.
3. Run `costcheck` before blaming the simulator for anything.
4. Compare the realised utilisation against the target. A miss larger than 10% is reported and
   may need one point re-run.
5. Apply the decision rule from `analysis-plan.md` section 6, and write the outcome into
   `results.md` with its provenance.
6. Commit the manifests and the summary.

## 6. What we are not running, and why

| Not running | Reason |
|---|---|
| Five repeats of the first pair's campaigns on the same trace and seed | Identical traces add almost no information, and choosing the points by how close their intervals sit to zero is optional stopping. The seeded anchor replaces it |
| A second and third pair before Block 3 | One pair is an anecdote, and three pairs built on the old design would be three anecdotes with the same confounds |
| Wired LAN | The transport difference is 5 to 16 ms against effects of 100 ms and more. Moving the harness off the slow node is the better use of the same night |
| An 8B point | External validity, at 2 to 4 hours per calibration, is worth less than the size-variability campaign |
| A wider token envelope | Declined in scope; costs a recalibration and lengthens every run |
| H3's instrument | Out of paper one, by `research-plan.md` section 5 |

## 7. Machine time

| Block | Hours |
|---|---:|
| Block 1, this laptop | 3 |
| Block 2, night one | 7 |
| Block 3, night two | 8 |
| Block 4, optional | 11 to 15 |
| **To the full claims ladder** | **18** |

## 8. Log

Append one line per campaign, newest last. Numbers go in `results.md`, not here.

- **2026-09-15** First pair finished: anchor, generation, balanced, summarisation. 132 valid
  runs, one trace and one scheduler seed per campaign. Pool shut down and the 3050 returned.
- **2026-09-15, evening** Audit of the first pair's design and analysis. All 132 runs
  re-derived under the rules now in `analysis-plan.md`. Five campaigns configured, unrun.
- **2026-09-16** Doc set replaced. Plan locked.
- **2026-09-16, night** E1.1a done: the 1650 Ti benched as installed, and again from a build
  with forced MMQ, which changes nothing. Its build flags turn out to be recorded in the build
  tree and are ordinary; only the build number was missing. The card runs at its 50 W limit
  while prefilling. Published figures put a tensor-core-less Turing card about 7.7x below an
  RTX 3050 on prompt processing, against our 9 to 10x, so the prefill ratio is the hardware.
  G1's rebuild is now provenance rather than a risk to K1.
- **2026-09-17, 00:33** E1.3 done. The CPU 1B class shows no resolvable drift over 80 minutes
  (lag-1 0.093, censored in every bootstrap draw), which is K6 at its strongest. Its snapshots
  are not promoted: the config claimed an engine build that does not exist here and carried no
  driver field, both now fixed, and the class is recalibrated when a CPU pair is scheduled.
