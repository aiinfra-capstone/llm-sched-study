package com.sched.sim;

import com.sched.core.interfaces.Clock;

public class SimClock implements Clock {
    private long currentTimeNs = 0;

    @Override
    public long nowNs() {
        return currentTimeNs;
    }

    /**
     * The DES engine calls this to warp time forward to the next scheduled event.
     */
    public void advanceTo(long targetTimeNs) {
        if (targetTimeNs < currentTimeNs) {
            throw new IllegalArgumentException("Time cannot move backward in the simulator!");
        }
        this.currentTimeNs = targetTimeNs;
    }
}