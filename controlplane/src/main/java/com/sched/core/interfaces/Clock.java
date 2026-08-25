package com.sched.core.interfaces;

public interface Clock {
    /**
     * Returns the current time in nanoseconds.
     * Live implementation reads System.nanoTime().
     * Sim implementation returns the event-queue time.
     */
    long nowNs();
}