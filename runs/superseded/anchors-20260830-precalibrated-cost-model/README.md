# The first anchor set, retired 2026-09-03

These are the four F-23 validation anchors collected on 2026-08-30. They are kept because
several numbers reported earlier in the study were read off them, and because they are the
evidence for why the anchors were collected a second time.

Their manifests name `cm_gtx1650ti_ngl99_p4_q4km_llama32_1b_20260830T134342Z_008`. That
snapshot series was measured with bucket representatives at prompt 64 and output 32, and a
concurrency grid holding only 1 and 4, while these runs used prompts of 128 to 512, outputs
of 64 and 128, and spent about a quarter of their time at two and three slots. `costcheck`
priced them against it at 127% request-weighted. A simulator parameterised from that table
ran 58.7% to 93.8% fast against these same anchors, which was a statement about the
calibration grid rather than about the simulator.

Recalibrating produced the `20260831T153652Z` series, and rather than have the hardware
runs name one snapshot while the simulator was parameterised from another, we replayed the
same trace against the same pool on 2026-09-03 and let the new manifests name the model we
actually believe. The trace is unchanged, `bea054628d19...`, so the two sets are directly
comparable:

| point | old p50 | new p50 | old p95 | new p95 |
|---|---:|---:|---:|---:|
| quiet | 1366.6 ms | 1296.0 ms | 3739.1 ms | 3709.3 ms |
| light | 1981.2 ms | 1820.1 ms | 4534.4 ms | 4343.9 ms |
| mid | 2980.2 ms | 2931.0 ms | 5974.2 ms | 5794.4 ms |
| heavy | 14137.1 ms | 13421.7 ms | 23985.7 ms | 22670.4 ms |

The hardware moved by 5 to 8%, and the load band came back identical at 1.03 to 1.30 req/s,
so the engine we rebuilt for the second campaign reproduces the one that ran the first. The
`llama-server` binary had been removed from the host in between, and was rebuilt from the
same pin: tag b10569, commit 5a32f7b, the `/completion` patch in `patches/`, CUDA 13.2,
driver 580.173.02.

`load_band.json` here is the band as it was read off this set.
