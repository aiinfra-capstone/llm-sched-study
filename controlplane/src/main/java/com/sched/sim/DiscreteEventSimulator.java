package com.sched.sim;

import java.util.PriorityQueue;
import java.util.Map;
import java.util.HashMap;
import com.sched.core.WorkerLogger;
import com.sched.core.ClientLogger;

public class DiscreteEventSimulator {
    private final SimClock clock;
    private final PriorityQueue<SimulationEvent> eventQueue;
    private final Map<String, SimNodeServer> servers = new HashMap<>();
    private WorkerLogger workerLogger;
    private ClientLogger clientLogger;

    public DiscreteEventSimulator(SimClock clock) {
        this.clock = clock;
        this.eventQueue = new PriorityQueue<>();
    }

    public void addServer(SimNodeServer server) {
        servers.put(server.getNodeId(), server);
    }

    public SimNodeServer getServer(String nodeId) {
        return servers.get(nodeId);
    }

    public void setLoggers(WorkerLogger workerLogger, ClientLogger clientLogger) {
        this.workerLogger = workerLogger;
        this.clientLogger = clientLogger;
    }

    private TransportOverhead transportOverhead = TransportOverhead.NONE;

    public void setTransportOverhead(TransportOverhead overhead) {
        this.transportOverhead = overhead == null ? TransportOverhead.NONE : overhead;
    }

    public TransportOverhead getTransportOverhead() { return transportOverhead; }

    public WorkerLogger getWorkerLogger() { return workerLogger; }
    public ClientLogger getClientLogger() { return clientLogger; }

    public void scheduleEvent(SimulationEvent event) {
        eventQueue.add(event);
    }

    public void run() {
        System.out.println("Starting Discrete Event Simulator...");

        while (!eventQueue.isEmpty()) {
            SimulationEvent nextEvent = eventQueue.poll();
            if (nextEvent.isCancelled()) continue;
            clock.advanceTo(nextEvent.getScheduledTimeNs());
            nextEvent.execute();
        }

        System.out.println("Simulation complete at simulated time: " + clock.nowNs() + " ns");
    }
}