**FAIL** on analysis-plan 6.6: ranking, WJSQ/JSQ and the H1 interaction at every point.

### 2.385 req/s, staleness 0 s
- Ranking, hardware: wjsq < threshold < jsq < static_weighted < round_robin
- Ranking, simulator: wjsq < threshold < jsq < static_weighted < round_robin
- WJSQ/JSQ: hardware 0.8661 [0.8338, 0.8961], simulator 0.8759
- H1 interaction, log: hardware 0.2213 [0.0891, 0.3458], simulator None
- Misses: the simulator cannot define the H1 interaction (undefined: transient cell(s) round_robin)

### 3.195 req/s, staleness 0 s
- Ranking, hardware: wjsq < jsq
- Ranking, simulator: wjsq < jsq
- WJSQ/JSQ: hardware 0.8444 [0.82, 0.8704], simulator 0.8818
- Misses: WJSQ over JSQ outside the hardware interval

