package com.sched.sim;

import java.util.PriorityQueue;

public class DiscreteEventSimulator {
    private final SimClock clock;
    private final PriorityQueue<SimulationEvent> eventQueue;

    public DiscreteEventSimulator(SimClock clock) {
        this.clock = clock;
        this.eventQueue = new PriorityQueue<>();
    }

    public void scheduleEvent(SimulationEvent event) {
        eventQueue.add(event);
    }

    public void run() {
        System.out.println("Starting Discrete Event Simulator...");

        while (!eventQueue.isEmpty()) {
            // 1. Pop the next chronological event
            SimulationEvent nextEvent = eventQueue.poll();

            // 2. Advance the simulated clock to that exact moment
            clock.advanceTo(nextEvent.getScheduledTimeNs());

            // 3. Process the event
            nextEvent.execute();
        }

        System.out.println("Simulation complete at simulated time: " + clock.nowNs() + " ns");
    }
}