# Elevation 1

September 2026. The Week-4 system was finished, contract-clean and validated, and we put it
through a hostile scrutiny pass to find out what it would not survive. It found five real
defects and two real gaps, and we found a third gap while fixing them. This folder is what
we decided to do about it.

Everything here is a **delta on the base scope**. The
[specification](../base_scope/scheduling-requirements-spec.pdf) is still the authority for
every F-requirement, contract and hypothesis, and the
[split](../base_scope/two-person-split-and-interface-contract.md) still says who owns what.
Nothing in this folder restates them.

| File | What it is |
|---|---|
| [`scope.md`](scope.md) | What the elevation adds, what it refuses and why, and how the research question changes. |
| [`evidence.md`](evidence.md) | Every measurement that justifies a decision here, with the numbers. This is the durable one; the writeup draws from it. |
| [`workplan.md`](workplan.md) | The seven workstreams, who owns each, the order they unblock in, and how each is verified. |

## The one-paragraph version

The scrutiny pass was right that the simulator had been fitted to its own validation gate,
that three of the five policies had bugs that only appear once a pool has more than one
node, and that the study had no sweep data and one physical machine. It was wrong that our
service-time residuals are bimodal, though our own first answer to that was wrong too, and
it graded us against IEEE acceptance when October is a checkpoint. The policy and simulator
defects are fixed and pushed. What remains is
hardware, sweeps, and one elevation of the science: **the heterogeneity ratio R is not a
scalar, because prefill and decode do not degrade at the same rate across machines.** That
last point is what turns a queueing study into an LLM one, and our own cost models already
show it.

## What was already fixed before this folder existed

Commits `81fd207`, `e256bfe`, `3b43e24`.

- The hardcoded `* 1.05` in `ServiceSampler` is gone, replaced by a measured additive
  transport term carried on the C-6 manifest.
- `JSQ` and `WJSQ` tie-break uniformly among tied nodes instead of always taking the first
  one in list order.
- `WJSQ` scores `(pending + 1) / capability`, so idle nodes are no longer indistinguishable.
- `Threshold` falls back to the fastest admissible node instead of dropping the request.
- `SimNodeServer` holds prefill invariant when batch composition changes, using a real
  prefill/decode split now carried in C-3.
- A silent 100 ms fabrication for uncalibrated cost-model cells now throws instead.

F-23 still passes at all four anchors with nothing fitted to the tolerance, and F-20
determinism holds.
