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
    /** Intended send offset, in seconds, of every request no node could admit. */
    private final java.util.List<Double> droppedOffsetsS = new java.util.ArrayList<>();

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
    private final Map<String, TransportOverhead> perNodeTransport = new HashMap<>();

    public void setTransportOverhead(TransportOverhead overhead) {
        this.transportOverhead = overhead == null ? TransportOverhead.NONE : overhead;
    }

    public void setPerNodeTransportOverhead(Map<String, TransportOverhead> perNode) {
        this.perNodeTransport.clear();
        if (perNode != null) {
            this.perNodeTransport.putAll(perNode);
        }
    }

    public TransportOverhead getTransportOverhead() { return transportOverhead; }

    public TransportOverhead getTransportOverhead(String nodeId) {
        if (nodeId != null && perNodeTransport.containsKey(nodeId)) {
            return perNodeTransport.get(nodeId);
        }
        return transportOverhead;
    }

    /** Called for a request the admission filter left no node for. */
    public void recordDrop(double intendedOffsetS) {
        droppedOffsetsS.add(intendedOffsetS);
    }

    /**
     * Drops inside the measurement window, the way the replay counts them for hardware: a
     * request intended at or after {@code warmupS}.
     */
    public int droppedFrom(double warmupS) {
        int n = 0;
        for (double t : droppedOffsetsS) if (t >= warmupS) n++;
        return n;
    }

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