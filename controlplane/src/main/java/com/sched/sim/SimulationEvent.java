package com.sched.sim;

public abstract class SimulationEvent implements Comparable<SimulationEvent> {
    protected final long scheduledTimeNs;
    private boolean cancelled = false;

    public SimulationEvent(long scheduledTimeNs) {
        this.scheduledTimeNs = scheduledTimeNs;
    }

    public long getScheduledTimeNs() {
        return scheduledTimeNs;
    }

    public void cancel() { this.cancelled = true; }
    public boolean isCancelled() { return cancelled; }

    // Every specific event (like RequestArrivalEvent) will define its own logic here
    public abstract void execute();

    @Override
    public int compareTo(SimulationEvent other) {
        // Ensures events are sorted chronologically in the PriorityQueue
        return Long.compare(this.scheduledTimeNs, other.scheduledTimeNs);
    }
}