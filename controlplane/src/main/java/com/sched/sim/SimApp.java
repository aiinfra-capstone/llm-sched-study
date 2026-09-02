package com.sched.sim;

import com.sched.core.AdmissionFilter;
import com.sched.core.DecisionLogger;
import com.sched.core.WorkerLogger;
import com.sched.core.ClientLogger;
import com.sched.core.InMemoryStateStore;
import com.sched.core.StalenessVeil;
import com.sched.core.models.TraceRequest;
import com.sched.core.models.CostModelSnapshot;
import com.sched.core.models.CostModelParser;
import com.sched.core.models.Manifest;
import com.sched.core.models.ManifestParser;
import com.sched.core.interfaces.StateStore.NodeView;
import com.sched.core.policies.*;
import com.sched.core.interfaces.Policy;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.File;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.stream.Stream;

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

            Map<String, CostModelSnapshot> byId = new HashMap<>();
            File root = new File(costModelDir);
            if (root.exists()) {
                try (Stream<Path> paths = Files.walk(root.toPath())) {
                    for (Path p : (Iterable<Path>) paths.filter(f -> f.toString().endsWith(".json"))::iterator) {
                        CostModelSnapshot s = CostModelParser.parse(p.toFile());
                        byId.put(s.snapshotId(), s);
                    }
                }
            } else {
                System.err.println("Cost models dir not found: " + costModelDir);
            }

            Map<String, CostModelSnapshot> loadedSnaps = new HashMap<>();
            Map<String, CostModelSnapshot.Admissibility> admBounds = new HashMap<>();
            for (Map.Entry<String, String> e : manifest.costModelSnapshots().entrySet()) {
                CostModelSnapshot snap = byId.get(e.getValue());
                if (snap == null)
                    throw new IllegalStateException("node " + e.getKey() + " names snapshot "
                        + e.getValue() + ", which is not in " + costModelDir + "/");
                loadedSnaps.put(e.getKey(), snap);
                admBounds.put(e.getKey(), snap.admissibility());
            }

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
            
            // Seed config and RNG
            int rngSeed = 42;
            if (manifest.config() != null && manifest.config().containsKey("seed")) {
                rngSeed = ((Number) manifest.config().get("seed")).intValue();
            }
            Random rng = new Random(rngSeed);
            
            ServiceSampler smp = new ServiceSampler(loadedSnaps, rng);
            if (deterministic) {
                smp.setDeterministic(true);
            }

            DecisionLogger log = new DecisionLogger(outputDir, rId);
            WorkerLogger wLog = new WorkerLogger(outputDir, rId);
            ClientLogger cLog = new ClientLogger(outputDir, rId);
            des.setLoggers(wLog, cLog);

            for (Manifest.SimNode n : manifest.nodes()) {
                if (!"pool".equals(n.role())) continue;
                CostModelSnapshot snap = loadedSnaps.get(n.nodeId());
                if (snap == null)
                    throw new IllegalStateException("node " + n.nodeId() + " is a pool member but has no snapshot");
                
                NodeView seed = new NodeView(n.nodeId(), 0, 0, referenceTokensPerS(snap), 0L, true);
                st.updateNode(seed);
                vl.seed(seed, -stalenessNs);

                SimNodeServer srv = new SimNodeServer(n.nodeId(), n.batchCapacity());
                des.addServer(srv);
            }

            AtomicLong seq = new AtomicLong(0);
            double thresholdT = manifest.config() != null && manifest.config().containsKey("threshold_t") ? ((Number) manifest.config().get("threshold_t")).doubleValue() : 0.0;
            Policy pol = Policies.fromName(manifest.policy(), new AtomicInteger(0), thresholdT);

            for (TraceRequest rq : reqs) {
                long arr = (long) (rq.arrivalOffsetS() * 1_000_000_000L);
                RequestArrivalEvent ev = new RequestArrivalEvent(
                        arr, rq, pol, vl, flt, des, rng, smp, st, log, rId, manifest.policy(), stalenessS, seq);
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
                    manifest.costModelSnapshots(),
                    manifest.nodes(),
                    newGitShas,
                    newValidity,
                    null
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
        }
    }

    private static double referenceTokensPerS(CostModelSnapshot snap) {
        int minPrompt = Integer.MAX_VALUE;
        int minOutput = Integer.MAX_VALUE;
        for (CostModelSnapshot.CostEntry e : snap.entries()) {
            if (e.promptBucket().get(0) < minPrompt) minPrompt = e.promptBucket().get(0);
            if (e.outputBucket().get(0) < minOutput) minOutput = e.outputBucket().get(0);
        }
        for (CostModelSnapshot.CostEntry e : snap.entries()) {
            if (e.promptBucket().get(0) == minPrompt && e.outputBucket().get(0) == minOutput && e.concurrency() == 1) {
                return e.tokensPerS();
            }
        }
        throw new IllegalArgumentException("Cost model snapshot " + snap.snapshotId() + " has no cell for lowest bucket at concurrency 1.");
    }

    private static String getGitSha() {
        try {
            Process p = new ProcessBuilder("git", "rev-parse", "HEAD").redirectErrorStream(true).start();
            String out = new String(p.getInputStream().readAllBytes()).trim();
            p.waitFor();
            if (out.matches("[0-9a-f]{7,40}")) return out.substring(0, 7);
        } catch (Exception ignored) {}
        return "sim-unknown";
    }
}
