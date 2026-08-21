# Fixtures — the fake scheduler and the fake worker

Throwaway, and load-bearing.

| | Built by | Talks to | Behaviour |
|---|---|---|---|
| `fake_scheduler/` | **A** | A's replay client | Round-robins blindly. Accepts `Dispatch`, returns a `DispatchAck`, forwards `Execute`. No policy, no state store. |
| `fake_worker/` | **B** | B's scheduler | Heartbeats scripted state. Accepts `Execute`, sleeps a scripted duration, delivers to `client_endpoint`. No engine. |

Each costs about half a day. They are the difference between two people working in
parallel and two people working in sequence on a timeline with no slack.

**Both must be running by end of Week 1.** The Week-1 joint gate is an end-to-end single
request through the *real* worker and *real* scheduler — but neither person should be
blocked waiting for that to get their own half working.

Emit C-4-conformant records from both. `uv run contracts/check.py` validates the
examples; point it at your fixture output too. If A's fake scheduler and B's fake worker
both emit records that pass, the two halves will join when they meet for real.

Delete these after Week 3. They are not part of the instrument, and a fake worker that
survives into the measurement weeks is a fake worker somebody will eventually run a
calibration against by accident.
