# Fixtures: the fake scheduler

**Superseded for anything measured, as of 2026-09-04.** The real control plane dispatches:
`LiveSchedulerApp` runs the veil, the filter and the policy, writes the C-4 decision record,
and forwards `Execute` to the chosen worker. Every committed run set from the MPR-2 campaign
on went through it. Verified end to end on a two-node local pool, where WJSQ split 200
requests 130/70 between an `ngl 99` node and an `ngl 0` one and every row came out with
`chosen_node` and `routing_error_ms` populated.

`fake_scheduler/` round-robins blindly and writes no decision record. We keep it for two
things: developing the data plane without the JVM in the loop, and
`dataplane/tests/test_end_to_end.py`, which drives a replay through it as its own process.
It is never pointed at a measurement.

| | Built by | Talks to | Behaviour |
|---|---|---|---|
| `fake_scheduler/` | **A** | A's replay client | Round-robins blindly. Accepts `Dispatch`, returns a `DispatchAck`, forwards `Execute`. No policy, no state store. |

The fake worker planned for the control-plane side was never needed: the control plane was
developed against the real worker, and its placeholder directory is gone.

**What a run driven by the fake scheduler cannot tell us.** It writes no scheduler log, on
purpose, so `chosen_node`, `decide_us`, `chosen_queue_depth`, `best_alt_node` and
`routing_error_ms` are null in every joined record it produces, and the `chosen_node` in its
ack is the worker's endpoint string rather than a node id. It also selects by blind rotation,
so it cannot carry a policy comparison: MPR-2 is the four-policy decomposition, and a
rotation has no policy to decompose.

Its records are C-4-conformant, and `uv run contracts/check.py` validates the examples.
