package com.sched.sim;

import com.sched.core.AdmissionFilter;
import com.sched.core.Capability;
import com.sched.core.DecisionLogger;
import com.sched.core.WorkerLogger;
import com.sched.core.ClientLogger;
import com.sched.core.InMemoryStateStore;
import com.sched.core.StalenessVeil;
import com.sched.core.models.TraceRequest;
import com.sched.core.models.CostModelSnapshot;
import com.sched.core.models.CostModelSnapshots;
import com.sched.core.models.Manifest;
import com.sched.core.models.ManifestParser;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.core.policies.*;
import com.sched.core.interfaces.Policy;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.File;
import java.nio.file.Path;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

public class SimApp {
    public static void main(String[] args) {
        if (args.length < 3) {
            System.err.println("Usage: SimApp <trace_file.jsonl> <manifest_file.json> <output_dir> [--cost-models dir] [--deterministic]");
            return;
        }
        String trc = args[0];
        String manifestPath = args[1];
        String outputDir = args[2];
        
        String costModelDir = "../contracts/cost_models";
        boolean deterministic = false;
        
        for (int i = 3; i < args.length; i++) {
            if (args[i].equals("--cost-models") && i + 1 < args.length) {
                costModelDir = args[i + 1];
                i++;
            } else if (args[i].equals("--deterministic")) {
                deterministic = true;
            }
        }

        try {
            Manifest manifest = ManifestParser.parse(manifestPath);

            // The same loader the live scheduler uses, so a replay serves the snapshot the
            // hardware run named (F-21).
            Map<String, CostModelSnapshot> loadedSnaps =
                    CostModelSnapshots.loadNamed(Path.of(costModelDir), manifest.costModelSnapshots());
            Map<String, CostModelSnapshot.Admissibility> admBounds = new HashMap<>();
            for (Map.Entry<String, CostModelSnapshot> e : loadedSnaps.entrySet()) {
                admBounds.put(e.getKey(), e.getValue().admissibility());
            }
            Map<String, String> resolvedSnapshots = new HashMap<>(manifest.costModelSnapshots());

            List<TraceRequest> rawReqs = TraceParser.parse(trc);

            // §5: anchors reach operating points by dividing offsets by rate_scale (1.15 for light)
            double rsTmp = 1.0;
            if (manifest.config() != null && manifest.config().containsKey("rate_scale")) {
                Object v = manifest.config().get("rate_scale");
                if (v instanceof Number n) rsTmp = n.doubleValue();
            }
            final double rateScale = rsTmp;
            List<TraceRequest> reqs = rawReqs.stream()
                .map(rq -> new TraceRequest(rq.record(), rq.reqId(), rq.arrivalOffsetS() / rateScale, rq.promptLen(), rq.outputLen(), rq.bucketId(), rq.priority()))
                .toList();
            
            String origRunId = manifest.runId();
            String rId = origRunId + "_sim";
            File dir = new File(outputDir);
            if (!dir.exists()) dir.mkdirs();

            SimClock clk = new SimClock();
            DiscreteEventSimulator des = new DiscreteEventSimulator(clk);
            InMemoryStateStore st = new InMemoryStateStore();
            
            double stalenessS = manifest.stalenessS() != null ? manifest.stalenessS() : 0.0;
            long stalenessNs = (long)(stalenessS * 1_000_000_000L);
            StalenessVeil vl = new StalenessVeil(stalenessNs, clk);
            AdmissionFilter flt = new AdmissionFilter(admBounds);
            
            // Seed config and RNG (issue #21 item 1: separate streams per purpose)
            int rngSeed = 42;
            if (manifest.config() != null && manifest.config().containsKey("seed")) {
                rngSeed = ((Number) manifest.config().get("seed")).intValue();
            }
            Random policyRng = new Random(rngSeed);
            // Per-node service noise streams so policy contrasts use common
            // random numbers (two SimApp runs with same seed and different
            // policies draw identical service times for the same request).
            // The fallback stream is also service-side so policyRng is never
            // consumed by sampling, even for a node missing from the map.
            Random serviceFallbackRng = new Random(rngSeed + 1000);
            Map<String, Random> serviceRngByNode = new HashMap<>();
            List<Manifest.SimNode> poolNodes = manifest.nodes().stream()
                    .filter(n -> "pool".equals(n.role()))
                    .sorted(Comparator.comparing(Manifest.SimNode::nodeId))
                    .toList();
            int nodeIdx = 0;
            for (Manifest.SimNode n : poolNodes) {
                serviceRngByNode.put(n.nodeId(),
                        new Random(rngSeed + 100 + nodeIdx++));
            }

            ServiceSampler smp = new ServiceSampler(loadedSnaps, serviceFallbackRng,
                    serviceRngByNode);
            if (deterministic) {
                smp.setDeterministic(true);
            }

            // Transport gets its own stream so that adding or removing it cannot shift the
            // service draws and silently change a dispatch sequence F-20 is checking.
            TransportOverhead overhead = TransportOverhead.NONE;
            Map<String, TransportOverhead> perNodeTransport = new HashMap<>();
            if (manifest.transportOverhead() != null) {
                if (manifest.transportOverhead().containsKey("mean_ms")) {
                    overhead = new TransportOverhead(
                        manifest.transportOverheadMeanMs(),
                        manifest.transportOverheadSdMs(),
                        new Random(rngSeed + 1),
                        deterministic);
                    System.out.printf("Transport overhead from manifest: %.2f +/- %.2f ms%n",
                        overhead.meanMs(), overhead.sdMs());
                } else {
                    int tIdx = 0;
                    for (Map.Entry<String, Object> entry : manifest.transportOverhead().entrySet()) {
                        String nId = entry.getKey();
                        if (entry.getValue() instanceof Map<?, ?> nodeMap) {
                            double meanMs = nodeMap.get("mean_ms") instanceof Number n ? n.doubleValue() : 0.0;
                            double sdMs = nodeMap.get("sd_ms") instanceof Number n ? n.doubleValue() : 0.0;
                            if (meanMs > 0.0) {
                                TransportOverhead to = new TransportOverhead(meanMs, sdMs,
                                    new Random(rngSeed + 500 + tIdx++), deterministic);
                                perNodeTransport.put(nId, to);
                                System.out.printf("Transport overhead for node %s: %.2f +/- %.2f ms%n",
                                    nId, meanMs, sdMs);
                            }
                        }
                    }
                }
            } else {
                System.out.println("Transport overhead: none recorded in manifest, applying 0 ms");
            }
            des.setTransportOverhead(overhead);
            des.setPerNodeTransportOverhead(perNodeTransport);

            DecisionLogger log = new DecisionLogger(outputDir, rId);
            WorkerLogger wLog = new WorkerLogger(outputDir, rId);
            ClientLogger cLog = new ClientLogger(outputDir, rId);
            des.setLoggers(wLog, cLog);

            for (Manifest.SimNode n : manifest.nodes()) {
                if (!"pool".equals(n.role())) continue;
                double cap = Capability.forPoolNode(n.nodeId(), loadedSnaps.get(n.nodeId()), manifest.config());
                System.out.println("Capability for " + n.nodeId() + ": " + cap + " tok/s");
                NodeView seed = new NodeView(n.nodeId(), 0, 0, cap, 0L, true);
                st.updateNode(seed);
                vl.seed(seed, -stalenessNs);

                SimNodeServer srv = new SimNodeServer(n.nodeId(), n.batchCapacity());
                des.addServer(srv);
            }

            AtomicLong seq = new AtomicLong(0);
            double thresholdT = manifest.config() != null && manifest.config().containsKey("threshold_t") ? ((Number) manifest.config().get("threshold_t")).doubleValue() : 0.0;
            Map<String, Integer> capacities = new HashMap<>();
            for (Manifest.SimNode n : manifest.nodes()) {
                if (!"pool".equals(n.role())) continue;
                capacities.put(n.nodeId(), n.batchCapacity());
            }
            Policy pol = Policies.fromName(manifest.policy(), new AtomicInteger(0), thresholdT,
                    loadedSnaps, capacities, manifest.config());

            for (TraceRequest rq : reqs) {
                long arr = (long) (rq.arrivalOffsetS() * 1_000_000_000L);
                RequestArrivalEvent ev = new RequestArrivalEvent(
                        arr, rq, pol, vl, flt, des, policyRng, smp, st, log, rId, manifest.policy(), stalenessS, seq);
                des.scheduleEvent(ev);
            }

            des.run();
            log.close();
            wLog.close();
            cLog.close();

            // Emit sim manifest (vehicle: simulator, own run_id/git_shas/validity, no inherited hardware ids)
            try {
                String simSha = getGitSha();
                Map<String, String> newGitShas = new HashMap<>();
                if (manifest.gitShas() != null) newGitShas.putAll(manifest.gitShas());
                // sim describe the sim vehicle; fallback to current sha
                newGitShas.put("sim", simSha);
                newGitShas.putIfAbsent("worker", simSha);
                newGitShas.putIfAbsent("scheduler", simSha);
                newGitShas.putIfAbsent("harness", simSha);

                Map<String, Object> newValidity = new HashMap<>();
                newValidity.put("max_send_lag_ms", 0.0);
                newValidity.put("send_lag_violations", 0);
                newValidity.put("dropped_requests", 0);
                newValidity.put("heartbeat_gaps", 0);
                newValidity.put("engine_restarts", 0);
                newValidity.put("valid", true);
                newValidity.put("colocated_nodes", 0);

                Manifest simManifest = new Manifest(
                    rId,
                    System.currentTimeMillis() / 1000L,
                    "simulator",
                    manifest.configHash(),
                    manifest.config(),
                    manifest.tracePath(),
                    manifest.traceSha256(),
                    manifest.policy(),
                    manifest.lambdaValue(),
                    manifest.stalenessS(),
                    manifest.warmupS(),
                    manifest.durationS(),
                    resolvedSnapshots,
                    manifest.nodes(),
                    newGitShas,
                    newValidity,
                    null,
                    manifest.transportOverhead()
                );
                ObjectMapper mapper = new ObjectMapper();
                mapper.setSerializationInclusion(com.fasterxml.jackson.annotation.JsonInclude.Include.NON_NULL);
                mapper.writerWithDefaultPrettyPrinter().writeValue(new File(dir, "manifest.json"), simManifest);
            } catch (Exception me) {
                System.err.println("Failed to write sim manifest: " + me.getMessage());
                me.printStackTrace();
                // fallback: write original without extra keys via NON_NULL mapper
                ObjectMapper mapper = new ObjectMapper();
                mapper.setSerializationInclusion(com.fasterxml.jackson.annotation.JsonInclude.Include.NON_NULL);
                mapper.writeValue(new File(dir, "manifest.json"), manifest);
            }

        } catch (Exception e) {
            System.err.println("Error during simulation: " + e.getMessage());
            e.printStackTrace();
            // Non-zero, so a caller can tell. Returning normally here made the JVM exit 0
            // for a simulation that threw, which is why validate_f23.sh has to grep stdout
            // for "Error during simulation" instead of reading $?, and why the sweep
            // runner's stop-on-first-failure could not see a failed point at all.
            System.exit(1);
        }
    }

    private static String getGitSha() {
        try {
            Process p = new ProcessBuilder("git", "rev-parse", "HEAD").redirectErrorStream(true).start();
            String out = new String(p.getInputStream().readAllBytes()).trim();
            p.waitFor();
            if (out.matches("[0-9a-f]{7,40}")) return out.substring(0, 7);
        } catch (Exception ignored) {}
        // The sha stamps which simulator produced the run, so a run carrying the fallback
        // cannot be traced back to code. Say so where the operator will see it.
        System.err.println("Warning: git rev-parse HEAD failed, recording the simulator sha as sim-unknown");
        return "sim-unknown";
    }
}
