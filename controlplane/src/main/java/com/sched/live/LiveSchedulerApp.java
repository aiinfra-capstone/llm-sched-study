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
            System.err.println("Usage: LiveSchedulerApp <manifest_file.json>");
            return;
        }

        Manifest manifest = ManifestParser.parse(args[0]);
        String runId = manifest.runId();
        double stalenessS = manifest.stalenessS() != null ? manifest.stalenessS() : 0.0;
        long stalenessNs = (long)(stalenessS * 1_000_000_000L);
        
        Clock sysClock = () -> System.currentTimeMillis() * 1_000_000L;
        InMemoryStateStore store = new InMemoryStateStore();
        StalenessVeil veil = new StalenessVeil(stalenessNs, sysClock);

        Map<String, Admissibility> boundsMap = new HashMap<>();
        boundsMap.put("fake-node-A", new Admissibility(4096, 2048, 10000));
        AdmissionFilter filter = new AdmissionFilter(boundsMap);

        double thresholdT = manifest.config() != null && manifest.config().containsKey("threshold_t") ? ((Number) manifest.config().get("threshold_t")).doubleValue() : 0.0;
        Policy policy = Policies.fromName(manifest.policy(), new AtomicInteger(0), thresholdT);
        
        DecisionLogger logger = new DecisionLogger(".", runId);

        int rngSeed = 42;
        if (manifest.config() != null && manifest.config().containsKey("seed")) {
            rngSeed = ((Number) manifest.config().get("seed")).intValue();
        }

        SchedulerGrpcService service = new SchedulerGrpcService(
                store, veil, filter, policy, logger, runId, manifest.policy(), stalenessS, rngSeed);

        Server server = ServerBuilder.forPort(50051)
                .addService(service)
                .build()
                .start();

        System.out.println("Live Control Plane active on port 50051 (Policy: " + manifest.policy() + ")");
        server.awaitTermination();
    }
}