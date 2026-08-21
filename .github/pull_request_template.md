## What and why

<!-- One or two sentences. If this closes a checklist item, link it. -->

## Requirements touched

<!-- e.g. F-16, F-20. If none, say none. -->

---

## Contract check

- [ ] **This PR does not change `contracts/`** — or, if it does, it is before the
      Week-1 freeze, *or* both people have agreed and the cost against the §6
      timeline is stated below.
- [ ] If a schema changed, its example in `contracts/examples/` changed in the
      same commit.
- [ ] If a version-gated schema changed, the version integer was bumped and the
      loader still rejects unknown versions loudly.

<!-- Contract change? State the re-scoping cost here: -->

## Seam discipline

Tick only what applies; delete the rest.

- [ ] **No cross-host clock subtraction** was introduced. Every duration comes
      from a single machine's monotonic clock.
- [ ] Policies do not call the system clock directly — they go through `Clock`.
- [ ] No policy reads state outside the `StalenessVeil`.
- [ ] `is_warmup` is still computed from the trace's `intended_offset_s`, never
      from run wall-clock.
- [ ] Figures still read `manifest.vehicle` and stamp simulated plots
      automatically (F-24).
