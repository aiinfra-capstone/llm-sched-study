**FAIL** on analysis-plan 6.6: ranking, WJSQ/JSQ and the H1 interaction at every point.

### 2.385 req/s, staleness 0 s
- Ranking, hardware: wjsq < static_weighted < jsq < threshold < round_robin
- Ranking, simulator: wjsq < jsq < threshold < static_weighted < round_robin
- Swapped pairs whose hardware intervals overlap: static_weighted/jsq, static_weighted/threshold
- WJSQ/JSQ: hardware 0.8891 [0.8495, 0.9259], simulator 0.9189
- H1 interaction, log: hardware -0.0081 [-0.093, 0.0662], simulator 0.0736
- Misses: ranking on mean latency differs; H1 interaction outside the hardware interval

### 3.195 req/s, staleness 0 s
- Ranking, hardware: wjsq < jsq < static_weighted
- Ranking, simulator: wjsq < jsq < static_weighted
- WJSQ/JSQ: hardware 0.9004 [0.8698, 0.933], simulator 0.9189
- Misses: none

