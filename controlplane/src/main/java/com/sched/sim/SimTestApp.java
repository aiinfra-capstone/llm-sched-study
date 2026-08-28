package com.sched.sim;

public class SimTestApp {
    public static void main(String[] args) {
        SimClock clock = new SimClock();
        DiscreteEventSimulator des = new DiscreteEventSimulator(clock);

        // Schedule an event at 500ns
        des.scheduleEvent(new SimulationEvent(500) {
            @Override
            public void execute() {
                System.out.println("[" + clock.nowNs() + " ns] Node A finished processing.");
            }
        });

        // Schedule an event at 100ns (added later, but should execute first)
        des.scheduleEvent(new SimulationEvent(100) {
            @Override
            public void execute() {
                System.out.println("[" + clock.nowNs() + " ns] Request r001 arrived.");
            }
        });

        des.run();
    }
}