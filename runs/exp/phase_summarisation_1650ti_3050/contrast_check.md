**FAIL** on analysis-plan 6.6: ranking, WJSQ/JSQ and the H1 interaction at every point.

### 2.385 req/s, staleness 0 s
- Ranking, hardware: threshold < wjsq < jsq < static_weighted
- Ranking, simulator: threshold < wjsq < jsq < static_weighted
- WJSQ/JSQ: hardware 0.7687 [0.7301, 0.8103], simulator 0.8076
- Misses: none

### 3.195 req/s, staleness 0 s
- Ranking, hardware: threshold < wjsq < jsq
- Ranking, simulator: threshold < wjsq < jsq
- WJSQ/JSQ: hardware 0.7375 [0.6915, 0.7879], simulator 0.8039
- Misses: WJSQ over JSQ outside the hardware interval

