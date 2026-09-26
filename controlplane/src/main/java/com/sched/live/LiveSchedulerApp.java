package com.sched.live;

import io.grpc.Server;
import io.grpc.ServerBuilder;
import com.sched.core.InMemoryStateStore;
import com.sched.core.StalenessVeil;
import com.sched.core.AdmissionFilter;
import com.sched.core.DecisionLogger;
import com.sched.core.Capability;
import com.sched.core.policies.Policies;
import com.sched.core.interfaces.Policy;
import com.sched.core.interfaces.Clock;
import com.sched.core.models.CostModelSnapshot.Admissibility;
import com.sched.core.models.CostModelSnapshots;
import com.sched.core.models.Manifest;
import com.sched.core.models.ManifestParser;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

public class LiveSchedulerApp {
    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            // Non-zero, so a launcher script can tell a usage error from a clean start.
            System.err.println("Usage: LiveSchedulerApp <manifest_file.json> [--worker <node_id>=<host:port> ...] [--port <port>] [--cost-models <dir>] [--log-dir <dir>]");
            System.exit(2);
        }

        String manifestPath = null;
        int port = 50051;
        String costModelDir = "../contracts/cost_models";
        String logDir = ".";
        java.util.List<String> workerArgs = new java.util.ArrayList<>();

        for (int i = 0; i < args.length; i++) {
            if (args[i].equals("--worker") && i + 1 < args.length) {
                workerArgs.add(args[++i]);
            } else if (args[i].equals("--port") && i + 1 < args.length) {
                port = Integer.parseInt(args[++i]);
            } else if (args[i].equals("--cost-models") && i + 1 < args.length) {
                costModelDir = args[++i];
            } else if (args[i].equals("--log-dir") && i + 1 < args.length) {
                logDir = args[++i];
            } else if (!args[i].startsWith("--") && manifestPath == null) {
                manifestPath = args[i];
            } else if (args[i].equals("--help") || args[i].equals("-h")) {
                System.out.println("Usage: LiveSchedulerApp <manifest_file.json> [--worker <node_id>=<host:port> ...] [--port <port>] [--cost-models <dir>] [--log-dir <dir>]");
                return;
            }
        }

        if (manifestPath == null) {
            System.err.println("Manifest file required");
            System.exit(2);
        }

        Manifest manifest = ManifestParser.parse(manifestPath);
        String runId = manifest.runId();
        double stalenessS = manifest.stalenessS() != null ? manifest.stalenessS() : 0.0;
        long stalenessNs = (long)(stalenessS * 1_000_000_000L);
        
        // The seed is part of what the run was, so it comes from the manifest or the run does
        // not start. A default here made every seedless campaign share one tie-break stream
        // without the manifest saying so (3.2).
        Object seedValue = manifest.config() != null ? manifest.config().get("seed") : null;
        if (!(seedValue instanceof Number seedNumber)) {
            System.err.println("Manifest " + manifestPath + " has no config.seed; refusing to start");
            System.exit(2);
            return;
        }
        int rngSeed = seedNumber.intValue();

        Clock sysClock = () -> System.nanoTime();
        InMemoryStateStore store = new InMemoryStateStore();
        StalenessVeil veil = new StalenessVeil(stalenessNs, sysClock);

        // Load admissibility bounds from the C-3 snapshots the manifest names, through the
        // loader SimApp uses too. Each node gets the snapshot it names: a named snapshot
        // that is not on disk refuses instead of widening the envelope, and a newer one of
        // the same class is never swapped in, since the manifest records what the run used.
        Map<String, com.sched.core.models.CostModelSnapshot> loadedSnaps =
                CostModelSnapshots.loadNamed(Path.of(costModelDir), manifest.costModelSnapshots());
        Map<String, Admissibility> boundsMap = new HashMap<>();
        for (Map.Entry<String, com.sched.core.models.CostModelSnapshot> e : loadedSnaps.entrySet()) {
            boundsMap.put(e.getKey(), e.getValue().admissibility());
        }
        AdmissionFilter filter = new AdmissionFilter(boundsMap);

        // Seed store with pool nodes from manifest so dispatch works before first heartbeat
        // (capability from C-3, like SimApp; heartbeat will update live state afterwards)
        // Note: seed at now-staleness, not -staleness like SimApp, because live clock is nanoTime (large), not 0
        long seedAt = sysClock.nowNs() - stalenessNs;
        for (Manifest.SimNode n : manifest.nodes()) {
            if (!"pool".equals(n.role())) continue;
            // A pool node with no snapshot refuses here, as in SimApp. Seeding it at 0 left
            // capability-weighted policies starving it for the whole run without a word.
            double cap = Capability.forPoolNode(n.nodeId(), loadedSnaps.get(n.nodeId()), manifest.config());
            System.out.println("Capability for " + n.nodeId() + ": " + cap + " tok/s");
            com.sched.core.interfaces.StateStore.NodeView seed =
                new com.sched.core.interfaces.StateStore.NodeView(n.nodeId(), 0, 0, cap, 0L, true);
            store.updateNode(seed);
            veil.seed(seed, seedAt);
        }

        double thresholdT = manifest.config() != null && manifest.config().containsKey("threshold_t") ? ((Number) manifest.config().get("threshold_t")).doubleValue() : 0.0;
        // workerCapacity is filled below from --worker args + manifest; build the policy
        // after channels so ECT sees capacities. For now pass what we have (manifest
        // parallel); the channel loop below refines workerCapacity but the values agree
        // because both come from the same manifest node block.
        java.util.Map<String, Integer> ectCaps = new java.util.HashMap<>();
        for (Manifest.SimNode n : manifest.nodes()) {
            if (!"pool".equals(n.role())) continue;
            ectCaps.put(n.nodeId(), n.batchCapacity());
        }
        Policy policy = Policies.fromName(manifest.policy(), new AtomicInteger(0), thresholdT,
                loadedSnaps, ectCaps, manifest.config());
        
        // The scheduler log is a C-4 artifact of the run, so it belongs in the run
        // directory rather than in whatever directory the process happened to start in.
        DecisionLogger logger = new DecisionLogger(logDir, runId);


        // Build worker channels: --worker node_id=host:port
        Map<String, io.grpc.ManagedChannel> workerChannels = new HashMap<>();
        Map<String, Integer> workerCapacity = new HashMap<>();
        for (String w : workerArgs) {
            // A malformed endpoint refuses the run (3.7). Skipping it left a pool node the
            // policy could choose but no channel could reach, and every dispatch to it failed.
            String[] parts = w.split("=", 2);
            int colon = parts.length == 2 ? parts[1].lastIndexOf(':') : -1;
            if (parts.length != 2 || parts[0].isEmpty() || colon <= 0) {
                System.err.println("Invalid --worker arg, expected node_id=host:port: " + w);
                System.exit(2);
                return;
            }
            String nodeId = parts[0];
            String host = parts[1].substring(0, colon);
            int wport;
            try {
                wport = Integer.parseInt(parts[1].substring(colon + 1));
            } catch (NumberFormatException e) {
                wport = -1;
            }
            if (wport <= 0 || wport > 65535) {
                System.err.println("Invalid port in --worker arg: " + w);
                System.exit(2);
                return;
            }
            boolean inPool = manifest.nodes().stream()
                    .anyMatch(n -> n.nodeId().equals(nodeId) && "pool".equals(n.role()));
            if (!inPool || workerChannels.containsKey(nodeId)) {
                System.err.println("--worker " + nodeId + " is "
                    + (inPool ? "given twice" : "not a pool node in the manifest"));
                System.exit(2);
                return;
            }
            io.grpc.ManagedChannel ch = io.grpc.ManagedChannelBuilder.forAddress(host, wport).usePlaintext().build();
            workerChannels.put(nodeId, ch);
            // Pick up the per node parallel slot count from the manifest so the
            // scheduler can keep state on admit (F-9a: --parallel is the per node
            // batch capacity the DES reads in SimNode.batchCapacity).
            for (Manifest.SimNode n : manifest.nodes()) {
                if (n.nodeId().equals(nodeId)) {
                    workerCapacity.put(nodeId, n.batchCapacity());
                    break;
                }
            }
            System.out.println("Worker channel: " + nodeId + " -> " + host + ":" + wport
                + (workerCapacity.containsKey(nodeId) ? " (parallel=" + workerCapacity.get(nodeId) + ")" : " (parallel=unknown)"));
        }
        if (workerArgs.isEmpty()) {
            System.out.println("No --worker endpoints given; scheduler will log decisions but not forward Execute (fixture mode)");
        }

        // Announce the run to every worker before the first dispatch (3.7), so each opens
        // this run's log and drops whatever the previous run left running, rather than doing
        // it on the first Execute. A worker that cannot be told refuses the run.
        com.sched.v1.BeginRun begin = com.sched.v1.BeginRun.newBuilder()
                .setRunId(runId)
                .setConfigHash(manifest.configHash() != null ? manifest.configHash() : "")
                .setTraceSha256(manifest.traceSha256() != null ? manifest.traceSha256() : "")
                .build();
        for (Map.Entry<String, io.grpc.ManagedChannel> e : workerChannels.entrySet()) {
            try {
                com.sched.v1.WorkerGrpc.newBlockingStub(e.getValue())
                        .withDeadlineAfter(10, java.util.concurrent.TimeUnit.SECONDS)
                        .begin(begin);
            } catch (io.grpc.StatusRuntimeException ex) {
                System.err.println("BeginRun to " + e.getKey() + " failed: " + ex.getStatus());
                for (io.grpc.ManagedChannel ch : workerChannels.values()) ch.shutdownNow();
                System.exit(1);
                return;
            }
        }

        SchedulerGrpcService service = new SchedulerGrpcService(
                store, veil, filter, policy, logger, runId, manifest.policy(), stalenessS, rngSeed, workerChannels, workerCapacity);

        Server server = ServerBuilder.forPort(port)
                .addService(service)
                .build()
                .start();

        System.out.println("Live Control Plane active on port " + port + " (Policy: " + manifest.policy() + ") with " + workerChannels.size() + " workers");
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            System.out.println("Shutting down scheduler");
            server.shutdown();
            for (io.grpc.ManagedChannel ch : workerChannels.values()) ch.shutdown();
            service.writeHeartbeatSummaries("shutdown");
            logger.close();
        }));
        server.awaitTermination();
    }
}