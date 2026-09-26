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

            List<TraceRequest> rawReqs = TraceParser.parseVerified(trc, manifest.traceSha256());

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
            
            // Seed config and RNG (issue #21 item 1: separate streams per purpose). The seed
            // is required: a default made a replay of a seedless run use a stream the hardware
            // manifest never recorded (3.2).
            Object seedValue = manifest.config() != null ? manifest.config().get("seed") : null;
            if (!(seedValue instanceof Number seedNumber)) {
                throw new IllegalArgumentException("manifest " + manifestPath + " has no config.seed");
            }
            int rngSeed = seedNumber.intValue();
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

            // The sim manifest (3.2). Validity is what this run did: a request no node could
            // admit is a drop, counted over the measurement window as the replay counts it.
            // Send lag, engine restarts and co-location are 0 by construction in the DES, and
            // an uncalibrated shape never reaches here because SimNodeServer throws on it.
            // There are no heartbeats, so heartbeat_gaps is null and named as unmeasured.
            double warmupS = manifest.warmupS() != null ? manifest.warmupS() : 0.0;
            int dropped = des.droppedFrom(warmupS);
            Map<String, Object> validity = new java.util.LinkedHashMap<>();
            validity.put("max_send_lag_ms", 0.0);
            validity.put("send_lag_violations", 0);
            validity.put("dropped_requests", dropped);
            validity.put("heartbeat_gaps", null);
            validity.put("engine_restarts", 0);
            validity.put("valid", dropped == 0);
            validity.put("colocated_nodes", 0);
            validity.put("unmeasured", List.of("heartbeat_gaps"));

            // Every component that produced this run is this checkout: the scheduler core and
            // the simulator ran, and no worker or harness did. The hardware run's own shas stay
            // in its manifest, which this run_id names.
            String sha = gitOutput("rev-parse", "HEAD");
            if (sha == null || !sha.matches("[0-9a-f]{40}")) {
                System.err.println("Warning: git rev-parse HEAD failed, recording the simulator sha as sim-unknown");
                sha = "sim-unknown";
            }
            Map<String, String> gitShas = new java.util.LinkedHashMap<>();
            for (String c : List.of("worker", "scheduler", "harness", "sim")) gitShas.put(c, sha);

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
                gitShas,
                validity,
                null,
                manifest.transportOverhead()
            );
            ObjectMapper mapper = new ObjectMapper();
            mapper.setSerializationInclusion(com.fasterxml.jackson.annotation.JsonInclude.Include.NON_NULL);
            @SuppressWarnings("unchecked")
            Map<String, Object> out = mapper.convertValue(simManifest, java.util.LinkedHashMap.class);
            // convertValue drops the null heartbeat_gaps with the other nulls; it is a value here.
            out.put("validity", validity);
            String porcelain = gitOutput("status", "--porcelain");
            if (porcelain != null) {
                Map<String, Boolean> dirty = new java.util.LinkedHashMap<>();
                for (String c : gitShas.keySet()) dirty.put(c, !porcelain.isEmpty());
                out.put("git_dirty", dirty);
            }
            // Written with a mapper that keeps nulls: NON_NULL drops null map values too, and
            // C-6 requires the heartbeat_gaps key. convertValue has already dropped every other
            // null, so this one is the only null the file carries.
            writeAtomically(new ObjectMapper(), new File(dir, "manifest.json"), out);

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

    /**
     * Write through a temporary file and a rename, so a failure leaves no manifest at all
     * rather than a partial one. The caller's catch then exits non-zero. Copying the hardware
     * manifest here, as a fallback once did, made a failed replay look like a hardware run.
     */
    private static void writeAtomically(ObjectMapper mapper, File target, Object value)
            throws java.io.IOException {
        File tmp = new File(target.getParentFile(), target.getName() + ".tmp");
        try {
            mapper.writerWithDefaultPrettyPrinter().writeValue(tmp, value);
            java.nio.file.Files.move(tmp.toPath(), target.toPath(),
                    java.nio.file.StandardCopyOption.ATOMIC_MOVE,
                    java.nio.file.StandardCopyOption.REPLACE_EXISTING);
        } finally {
            java.nio.file.Files.deleteIfExists(tmp.toPath());
        }
    }

    /** Trimmed stdout of a git command, or null when git is missing or fails. */
    private static String gitOutput(String... args) {
        try {
            List<String> cmd = new java.util.ArrayList<>(List.of("git"));
            cmd.addAll(List.of(args));
            Process p = new ProcessBuilder(cmd).start();
            String out = new String(p.getInputStream().readAllBytes()).trim();
            return p.waitFor() == 0 ? out : null;
        } catch (Exception e) {
            return null;
        }
    }
}
