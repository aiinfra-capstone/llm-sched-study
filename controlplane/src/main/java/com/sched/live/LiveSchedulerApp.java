package com.sched.live;

import io.grpc.Server;
import io.grpc.ServerBuilder;
import com.sched.core.InMemoryStateStore;
import com.sched.core.StalenessVeil;
import com.sched.core.AdmissionFilter;
import com.sched.core.DecisionLogger;
import com.sched.core.policies.RoundRobin;
import com.sched.core.interfaces.Clock;
import com.sched.core.models.CostModelSnapshot.Admissibility;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

public class LiveSchedulerApp {
    public static void main(String[] args) throws Exception {
        String runId = "live_run_001";

        Clock sysClock = () -> System.currentTimeMillis() * 1_000_000L;

        InMemoryStateStore store = new InMemoryStateStore();
        StalenessVeil veil = new StalenessVeil(0, sysClock);

        // Inject mock hardware constraints so fake-node-A passes the F-14 Admissibility
        // check
        Map<String, Admissibility> boundsMap = new HashMap<>();
        boundsMap.put("fake-node-A", new Admissibility(4096, 2048, 10000));
        AdmissionFilter filter = new AdmissionFilter(boundsMap);

        RoundRobin policy = new RoundRobin(new AtomicInteger(0));
        DecisionLogger logger = new DecisionLogger(runId);

        SchedulerGrpcService service = new SchedulerGrpcService(
                store, veil, filter, policy, logger, runId, "RoundRobin", 0.0);

        Server server = ServerBuilder.forPort(50051)
                .addService(service)
                .build()
                .start();

        System.out.println("Live Control Plane active on port 50051 (Policy: RoundRobin)");
        server.awaitTermination();
    }
}