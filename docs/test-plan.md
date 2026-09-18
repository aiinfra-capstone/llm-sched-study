# Test plan

What the suite has to guarantee, and what "passing" means. The tests themselves are written by
hand, by the owner of the half being tested; this document says what must be covered and why,
and is the place to add a requirement before the test exists.

Two rules that override convenience:

1. **A failing test means the code is wrong until proven otherwise.** Tests are not edited to
   make a run green, and are not read while writing the code they check.
2. **A line of the harness that no test executes is a line whose behaviour we would be
   assuming when we report a measurement taken through it.** That is the reason the gate is
   100% and not a vanity number.

---

## 1. Gates

Everything below must hold before a campaign runs (`experiment-plan.md`, G0) and on every
pull request.

| Gate | Command | Passing |
|---|---|---|
| Data-plane suite and coverage | `cd dataplane && uv run pytest` | Green, with line and branch coverage at 100% over `src/dataplane` and `tools/`, minus the omit list |
| Lint | `uv run ruff check` and `ruff format --check` | Clean, including `tools/` |
| Contracts | `uv run contracts/check.py` | All six artifacts and every committed example validate |
| Cross-seam CI | `.github/workflows/cross-seam.yml` | Both halves agree on the dispatch sequence for the shared fixture |
| Control-plane suite | `cd controlplane && mvn test` | Green |

The coverage omit list is the remaining work, not an exemption. A file leaves it in the same
commit that adds its tests.

## 2. Immediate backlog

| Item | Owner | Note |
|---|---|---|
| An arm key no policy reads is dropped while parsing | D | `Campaign.from_dict` filters an arm's config to `ARM_KEYS`, so a misspelled key never reaches the refusal in `check_campaign` and that arm runs as the baseline under its own name. The refusal is tested on a `Campaign` built in code; the parse has to stop dropping the key before a config file can be refused |
| Policies and both vehicles, `test-plan.md` 3.8 | A | The control-plane suite is the gate for it, and the cross-seam workflow is where the two halves meet |

Closed on 2026-09-16: `tools/p4_validate.py` is in the omit list beside `sweep.py` and
`f23_compare.py` until ownership is agreed, and `hw_runs.py` and `campaign_summary.py` are
back at 100% (the four capability-arm refusals, the placeholder-snapshot refusal, the
campaign-level capability keys in the manifest, the Sokal sum running to exhaustion, and a
repeat with no rows at the point's positions).

Closed on 2026-09-18: `promote_calibration.py`, `compare_sets.py` and `contrast_check.py` have
tests and have left the omit list, and a two-workload fixture covers the workload as part of a
point. That last one is the case the gate could not have caught: the workload enters through
conditional expressions, and branch coverage does not measure the two sides of one, so the file
read as fully covered while no test had ever put two workloads in a frame.

## 3. What must be covered, by area

Each row is a behaviour a result depends on. The acceptance criterion is what a test has to
assert, not how.

### 3.1 Contracts

| Requirement | Acceptance |
|---|---|
| Every schema accepts its example and rejects a plausible near-miss | A missing required field, a wrong enum value and an extra property each fail |
| A C-6 manifest with a new field stays consistent with the Java bindings | The checker fails when one side gains a field the other lacks |
| `check.py --validate` routes a file to the contract its name implies | A scheduler log validated as a worker log is refused |

### 3.2 Trace generation and identity

| Requirement | Acceptance |
|---|---|
| A trace is byte-identical from (config, seed) | Two generations of the same config produce the same bytes and the same SHA-256 |
| Arrival, length, priority and content streams are separate | Changing the length distribution leaves arrival offsets unchanged for the same seed |
| Two configs sharing a seed share arrivals and priorities | Verified across the shape traces over every request |
| Bucket parsing refuses a shape outside the admissible envelope | A bucket above the envelope raises rather than writing a trace |
| The replay refuses a trace whose hash moved | Mismatch stops before the first request |

### 3.3 Open-loop replay and validity

| Requirement | Acceptance |
|---|---|
| Send lag is asserted per request, in the measured window only | A warmup breach does not invalidate; a measured breach does |
| A dropped or timed-out measured request invalidates the run | Validity reports the reason |
| Failures are counted over the whole run, warmup included | A response lost in warmup appears in the failure count while the run stays valid |
| Prompts are materialised before `t0` | No allocation inside the timing loop |
| The client refuses a run whose id, hash or policy disagrees with the manifest | Refusal before the first request |

### 3.4 Worker and engine adapter

| Requirement | Acceptance |
|---|---|
| The wrapper admits exactly `--parallel` requests | Queue wait is measured in the wrapper, service time is the engine's own span |
| The phase split is reported, never reconstructed | A `timings` block missing `predicted_ms` degrades to `partial` with both halves absent |
| Engine failures are classified, not raised | OOM is distinguished from a generic engine error and from a timeout |
| The request carries forced length, token ids, no prompt cache and temperature 0 | Asserted on the body |
| Delivery waits for the client channel and counts undelivered responses | A channel in reconnect backoff does not produce a silent loss |

### 3.5 Pipeline and join

| Requirement | Acceptance |
|---|---|
| The join is a pure function of the manifest and the three logs | No network, no engine, deterministic output |
| A missing client log refuses rather than writing an empty frame | Explicit failure |
| Warmup marking follows intended arrival, not send time | Boundary case asserted |
| Rate correction from `clock_sync` divides the right quantity | Rate, never offset |
| `costcheck` compares a run against the model that served it | A superseded snapshot is detected |

### 3.6 Analysis rules

These carry the published numbers, so each rule in `analysis-plan.md` has a test.

| Requirement | Acceptance |
|---|---|
| Steady-state gate | A synthetic climbing cell is marked transient; a flat one is not; the borderline case reports its interval |
| Contrasts refuse transient cells | No interaction is produced at a point where any of the four cells is transient |
| Pairing | Repeats are resampled jointly across policies; a contrast uses only the repeats both cells have, and reports `repeats_used`, `repeats_dropped` and `single_repeat` |
| Block bootstrap | Block length follows the rule, including the n/5 cap; a constant series does not produce a spurious autocorrelation time |
| Independent streams per contrast | A failure under one policy does not move another contrast's interval |
| τ interval | Block bootstrap covers a known τ on synthetic series at the stated rate, and blocks shorter than the correlation being measured are rejected |
| Failures count as SLO misses, and a short run's missing positions do not | Both asserted |
| `arrivals_independent` reflects the seeds actually used | False for the first pair's campaigns, true for a seeded one |
| The workload is part of a point | Two workloads at one rate are two points, each with its own block length, its own load trend and its own random stream. A campaign with one workload is unchanged, and its committed summary stays reproducible |

### 3.7 Campaign driver

| Requirement | Acceptance |
|---|---|
| Per-repeat seeds | Missing `repeat_seeds` with a seeded workload refuses; duplicate seeds refuse; each repeat's manifest carries its own seed |
| Load targets | A utilisation target resolves to a rate through `pool_load`, and the manifest records both |
| Capability arms | Unknown key, unknown policy, duplicate arm name and an unnamed arm in a multi-arm campaign all refuse; the arm reaches the manifest and the run id |
| Arm precedence | An arm's setting overrides the campaign's for every key an arm may set, `threshold_t` included, and a cutoff is written for Threshold only |
| Engine identity | Same, changed, died and unknown are distinguished; a failed read retries; an unreadable engine before a run stops the campaign before it writes anything |
| Restartability | A run directory holding a manifest and a client log is skipped |
| Refusals | A snapshot that is not the newest in its class, a pool node with no endpoint or log location, a co-located pool, and a placeholder snapshot outside `--dry-run` |
| Tests survive recalibration | Refusal tests run against a synthetic snapshot index, and only the tests that read committed configs on purpose name a real snapshot id. The first pair's snapshots stay pinned where the history is what is checked |

### 3.8 Policies and both vehicles

Owned by the control plane, and the cross-seam CI is where the two meet.

| Requirement | Acceptance |
|---|---|
| Each policy's rule | The score it computes, on a constructed node view |
| Tie-breaking | Uniform over tied nodes, driven by the recorded draw; `jsq_fastfirst` resolves to the highest capability whatever order the state store lists the nodes in |
| Capability | Service rate from the reference cell, with the documented fallback when a snapshot has no phase split. Pinned to the first pair's two snapshots at 103.9472 and 163.6070 tok/s, the numbers `tools/pool_load.py` is also pinned to, so the Java and Python copies cannot drift apart |
| Capability overrides | An override reaches the policy and is recorded on the decision |
| `ECT` | On two nodes where scalar capability and the cost model disagree, ECT picks the lower predicted completion. Known and unknown mode give different scores for the same request, and a changed output length changes a known-mode score. The fallback is compared against a millisecond score, not only checked for being finite. A one-node pool asserts nothing about pricing |
| Admission | A request outside a node's bounds is refused there, and a node with no snapshot is not admissible |
| Staleness veil | The view served is the one at `now - staleness`; concurrent writes do not corrupt it |
| Concurrent dispatch | N simultaneous dispatches at `SchedulerGrpcService` in fixture mode: decision k sees exactly k earlier admissions. The veil's concurrent-write test covers the map, not read, decide and admit as one step |
| Determinism inside `mvn test` | Two simulator runs of one manifest produce an identical dispatch sequence, asserted in the Java suite; `determinism_test.sh` alone is not part of that gate |
| Determinism | Two simulator runs of one manifest produce an identical dispatch sequence |
| Live and simulated parity | The same trace and manifest produce the same decisions where the state is the same |
| A missing cost-model cell | Refuses rather than fabricating a service time |

### 3.9 Comparing run sets, and promoting a calibration

The numbers three of the frozen decision rules turn on are computed across run sets rather than
inside one, and a recalibration has to reach the contracts and every config before a campaign
can run.

| Requirement | Acceptance |
|---|---|
| A cross-set ratio is the set's own | The ratio `compare_sets.py` prints for a set equals that set's `summary.json` ratio, on the same cells and the same steady-state gate |
| One point is one rate | A point holding two rates is refused rather than averaged, and a set that cannot be measured leaves its pair undefined rather than dropping out |
| Workloads are sets | `path#workload` selects one workload's runs from a campaign that ran several |
| Ordering | `--ordered` reports monotone and extremes-separated, and both are false when a set has no ratio |
| The tie rate | A shared best score counts once per decision, a lone admissible candidate is not a choice, and a run with no scheduler log has no rate rather than a zero |
| The simulator's verdict | Ranking, WJSQ/JSQ and the H1 interaction each miss where analysis-plan 6.6 says they do; a ranking swap is a miss even where the hardware intervals overlap, and is listed as such; the exit code is 2 on any miss |
| Promotion order | The phase split is backfilled before C-3 is checked, and a backfill that produces no split is refused |
| Promotion refusals | A snapshot older than the one in the contracts, one that fails C-3, and a file name already there with different content |
| Repointing | Only the configs that name the previous newest id are rewritten, by parsing rather than grep, and a list-shaped config is left alone |
| `--dry-run` | Reports and writes nothing, and runs no campaign checks |

### 3.10 Figures

| Requirement | Acceptance |
|---|---|
| A figure refuses what it cannot support | H1 with a missing cell of the 2x2, H2 with a single R, H3 without τ, phase advantage with one shape |
| The vehicle stamp appears on every figure | Simulated sets are labelled |
| H3 uses regret when present and says so when it falls back | Asserted on the rendered figure |

## 4. Property-based tests

`hypothesis` covers the invariants that can be stated but not enumerated: trace determinism
across seeds and configs, the join's purity, arrival-stream separation, and the bootstrap's
behaviour on constructed series. New invariants belong here rather than as another example
test.

## 5. What tests are not for

- Not for asserting a performance number on a shared runner. `perf` is deselected by default;
  send-lag under load is a property of the machine and is run on the load host.
- Not for pinning a measured result. A number that changes when the hardware changes belongs
  in `results.md`, not in an assertion.
- Not for raising coverage by exercising code without checking behaviour.
