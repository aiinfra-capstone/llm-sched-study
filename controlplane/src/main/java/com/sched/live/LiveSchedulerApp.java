package com.sched.live;

import io.grpc.Server;
import io.grpc.ServerBuilder;
import com.sched.core.InMemoryStateStore;
import com.sched.core.StalenessVeil;
import com.sched.core.AdmissionFilter;
import com.sched.core.DecisionLogger;
import com.sched.core.policies.Policies;
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.Clock;
import com.sched.core.models.CostModelSnapshot.Admissibility;
import com.sched.core.models.Manifest;
import com.sched.core.models.ManifestParser;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

public class LiveSchedulerApp {
    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            System.err.println("Usage: LiveSchedulerApp <manifest_file.json> [--worker <node_id>=<host:port> ...] [--port <port>] [--cost-models <dir>]");
            return;
        }

        String manifestPath = null;
        int port = 50051;
        String costModelDir = "../contracts/cost_models";
        java.util.List<String> workerArgs = new java.util.ArrayList<>();

        for (int i = 0; i < args.length; i++) {
            if (args[i].equals("--worker") && i + 1 < args.length) {
                workerArgs.add(args[++i]);
            } else if (args[i].equals("--port") && i + 1 < args.length) {
                port = Integer.parseInt(args[++i]);
            } else if (args[i].equals("--cost-models") && i + 1 < args.length) {
                costModelDir = args[++i];
            } else if (!args[i].startsWith("--") && manifestPath == null) {
                manifestPath = args[i];
            } else if (args[i].equals("--help") || args[i].equals("-h")) {
                System.out.println("Usage: LiveSchedulerApp <manifest> [--worker <node_id>=<host:port> ...] [--port <port>] [--cost-models <dir>]");
                return;
            }
        }

        if (manifestPath == null) {
            System.err.println("Manifest file required");
            return;
        }

        Manifest manifest = ManifestParser.parse(manifestPath);
        String runId = manifest.runId();
        double stalenessS = manifest.stalenessS() != null ? manifest.stalenessS() : 0.0;
        long stalenessNs = (long)(stalenessS * 1_000_000_000L);
        
        Clock sysClock = () -> System.nanoTime();
        InMemoryStateStore store = new InMemoryStateStore();
        StalenessVeil veil = new StalenessVeil(stalenessNs, sysClock);

        // Load admissibility bounds from the C-3 snapshots the manifest names.
        // Same rule as SimApp: a named snapshot that is not on disk refuses
        // instead of widening the envelope, and a stale series resolves to the
        // newest snapshot per node class so both vehicles serve the same model.
        Map<String, Admissibility> boundsMap = new HashMap<>();
        Map<String, com.sched.core.models.CostModelSnapshot> loadedSnaps = new HashMap<>();
        try {
            java.io.File root = new java.io.File(costModelDir);
            Map<String, com.sched.core.models.CostModelSnapshot> byId = new HashMap<>();
            if (root.exists()) {
                try (java.util.stream.Stream<java.nio.file.Path> paths = java.nio.file.Files.walk(root.toPath())) {
                    for (java.nio.file.Path p : (Iterable<java.nio.file.Path>) paths.filter(f -> f.toString().endsWith(".json"))::iterator) {
                        com.sched.core.models.CostModelSnapshot s = com.sched.core.models.CostModelParser.parse(p.toFile());
                        byId.put(s.snapshotId(), s);
                    }
                }
            } else {
                throw new IllegalStateException("cost models dir not found: " + costModelDir);
            }
            Map<String, com.sched.core.models.CostModelSnapshot> newestByClass = new HashMap<>();
            for (com.sched.core.models.CostModelSnapshot s : byId.values()) {
                com.sched.core.models.CostModelSnapshot cur = newestByClass.get(s.nodeClass());
                if (cur == null || s.measuredAtUnix() > cur.measuredAtUnix()) {
                    newestByClass.put(s.nodeClass(), s);
                }
            }
            for (Map.Entry<String, String> e : manifest.costModelSnapshots().entrySet()) {
                com.sched.core.models.CostModelSnapshot snap = byId.get(e.getValue());
                if (snap == null)
                    throw new IllegalStateException("node " + e.getKey() + " names snapshot "
                        + e.getValue() + ", which is not in " + costModelDir + "/");
                com.sched.core.models.CostModelSnapshot newest = newestByClass.get(snap.nodeClass());
                if (newest != null && newest.measuredAtUnix() > snap.measuredAtUnix()) {
                    System.out.printf("Resolving snapshot for node %s: %s -> %s [%s]%n",
                        e.getKey(), snap.snapshotId(), newest.snapshotId(), snap.nodeClass());
                    snap = newest;
                }
                boundsMap.put(e.getKey(), snap.admissibility());
                loadedSnaps.put(e.getKey(), snap);
            }
        } catch (RuntimeException e) {
            throw e;
        } catch (Exception e) {
            throw new IllegalStateException("failed to load C-3 snapshots from " + costModelDir + ": " + e.getMessage(), e);
        }
        AdmissionFilter filter = new AdmissionFilter(boundsMap);

        // Seed store with pool nodes from manifest so dispatch works before first heartbeat
        // (capability from C-3, like SimApp; heartbeat will update live state afterwards)
        // Note: seed at now-staleness, not -staleness like SimApp, because live clock is nanoTime (large), not 0
        long seedAt = sysClock.nowNs() - stalenessNs;
        for (Manifest.SimNode n : manifest.nodes()) {
            if (!"pool".equals(n.role())) continue;
            com.sched.core.models.CostModelSnapshot snap = loadedSnaps.get(n.nodeId());
            double cap = 0.0;
            if (snap != null) {
                try {
                    cap = referenceTokensPerS(snap);
                } catch (Exception ignored) {
                    cap = 0.0;
                }
            }
            com.sched.core.interfaces.StateStore.NodeView seed =
                new com.sched.core.interfaces.StateStore.NodeView(n.nodeId(), 0, 0, cap, 0L, true);
            store.updateNode(seed);
            veil.seed(seed, seedAt);
        }

        double thresholdT = manifest.config() != null && manifest.config().containsKey("threshold_t") ? ((Number) manifest.config().get("threshold_t")).doubleValue() : 0.0;
        Policy policy = Policies.fromName(manifest.policy(), new AtomicInteger(0), thresholdT);
        
        DecisionLogger logger = new DecisionLogger(".", runId);

        int rngSeed = 42;
        if (manifest.config() != null && manifest.config().containsKey("seed")) {
            rngSeed = ((Number) manifest.config().get("seed")).intValue();
        }

        // Build worker channels: --worker node_id=host:port
        Map<String, io.grpc.ManagedChannel> workerChannels = new HashMap<>();
        for (String w : workerArgs) {
            String[] parts = w.split("=", 2);
            if (parts.length != 2) {
                System.err.println("Invalid --worker arg, expected node_id=host:port: " + w);
                continue;
            }
            String nodeId = parts[0];
            String target = parts[1];
            String host;
            int wport;
            int colon = target.lastIndexOf(':');
            if (colon < 0) {
                host = target;
                wport = 50061;
            } else {
                host = target.substring(0, colon);
                wport = Integer.parseInt(target.substring(colon + 1));
            }
            io.grpc.ManagedChannel ch = io.grpc.ManagedChannelBuilder.forAddress(host, wport).usePlaintext().build();
            workerChannels.put(nodeId, ch);
            System.out.println("Worker channel: " + nodeId + " -> " + host + ":" + wport);
        }
        if (workerArgs.isEmpty()) {
            System.out.println("No --worker endpoints given; scheduler will log decisions but not forward Execute (fixture mode)");
        }

        SchedulerGrpcService service = new SchedulerGrpcService(
                store, veil, filter, policy, logger, runId, manifest.policy(), stalenessS, rngSeed, workerChannels);

        Server server = ServerBuilder.forPort(port)
                .addService(service)
                .build()
                .start();

        System.out.println("Live Control Plane active on port " + port + " (Policy: " + manifest.policy() + ") with " + workerChannels.size() + " workers");
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            System.out.println("Shutting down scheduler");
            server.shutdown();
            for (io.grpc.ManagedChannel ch : workerChannels.values()) ch.shutdown();
        }));
        server.awaitTermination();
    }

    private static double referenceTokensPerS(com.sched.core.models.CostModelSnapshot snap) {
        int minPrompt = Integer.MAX_VALUE;
        int minOutput = Integer.MAX_VALUE;
        for (com.sched.core.models.CostModelSnapshot.CostEntry e : snap.entries()) {
            if (e.promptBucket().get(0) < minPrompt) minPrompt = e.promptBucket().get(0);
            if (e.outputBucket().get(0) < minOutput) minOutput = e.outputBucket().get(0);
        }
        for (com.sched.core.models.CostModelSnapshot.CostEntry e : snap.entries()) {
            if (e.promptBucket().get(0) == minPrompt && e.outputBucket().get(0) == minOutput && e.concurrency() == 1) {
                return e.tokensPerS();
            }
        }
        return 0.0;
    }
}