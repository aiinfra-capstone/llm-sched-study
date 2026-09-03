package com.sched.sim;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/**
 * The event loop itself.
 *
 * F-20 says a dispatch sequence is a property of (trace, seed, policy) and of nothing else.
 * That only holds if the loop drains in time order and never runs a cancelled event, because
 * rescheduling under contention leaves cancelled events in the queue on every batch change.
 */
class DiscreteEventSimulatorTest {

    /** An event that writes its own name down when it fires. */
    private static final class Recorder extends SimulationEvent {
        private final String name;
        private final List<String> log;

        Recorder(long atNs, String name, List<String> log) {
            super(atNs);
            this.name = name;
            this.log = log;
        }

        @Override
        public void execute() {
            log.add(name);
        }
    }

    @Test
    @DisplayName("events fire in time order regardless of the order they were queued")
    void eventsDrainInTimeOrder() {
        List<String> fired = new ArrayList<>();
        DiscreteEventSimulator des = new DiscreteEventSimulator(new SimClock());

        des.scheduleEvent(new Recorder(300, "third", fired));
        des.scheduleEvent(new Recorder(100, "first", fired));
        des.scheduleEvent(new Recorder(200, "second", fired));
        des.run();

        assertEquals(List.of("first", "second", "third"), fired);
    }

    @Test
    @DisplayName("a cancelled event never executes")
    void cancelledEventsAreSkipped() {
        // Every concurrency change cancels and reschedules the requests already running, so
        // the queue routinely holds events that must not fire. If one did, a request would
        // complete twice and the node's busy count would go negative.
        List<String> fired = new ArrayList<>();
        DiscreteEventSimulator des = new DiscreteEventSimulator(new SimClock());

        Recorder stale = new Recorder(100, "stale", fired);
        des.scheduleEvent(stale);
        des.scheduleEvent(new Recorder(200, "live", fired));
        stale.cancel();
        des.run();

        assertEquals(List.of("live"), fired);
    }

    @Test
    @DisplayName("an event scheduled during the run is picked up")
    void eventsScheduledMidRunAreHonoured() {
        // A completion enqueues the next queued request, so the loop has to keep draining
        // rather than take a snapshot of the queue when it starts.
        List<String> fired = new ArrayList<>();
        DiscreteEventSimulator des = new DiscreteEventSimulator(new SimClock());

        des.scheduleEvent(new SimulationEvent(100) {
            @Override
            public void execute() {
                fired.add("first");
                des.scheduleEvent(new Recorder(150, "spawned", fired));
            }
        });
        des.scheduleEvent(new Recorder(200, "last", fired));
        des.run();

        assertEquals(List.of("first", "spawned", "last"), fired);
    }

    @Test
    @DisplayName("the clock ends on the last event it ran")
    void clockAdvancesToTheFinalEvent() {
        SimClock clock = new SimClock();
        DiscreteEventSimulator des = new DiscreteEventSimulator(clock);

        des.scheduleEvent(new Recorder(500, "a", new ArrayList<>()));
        des.run();

        assertEquals(500L, clock.nowNs());
    }

    @Test
    @DisplayName("simulated time never runs backwards")
    void clockRefusesToGoBackwards() {
        // Simulated time going backwards would produce negative durations in the logs, which
        // is a corruption that reads as a fast request rather than as an error.
        SimClock clock = new SimClock();
        clock.advanceTo(1000);

        assertThrows(IllegalArgumentException.class, () -> clock.advanceTo(999));
        assertEquals(1000L, clock.nowNs());
    }

    @Test
    @DisplayName("an empty queue is a complete run, not a failure")
    void emptyRunIsFine() {
        SimClock clock = new SimClock();
        new DiscreteEventSimulator(clock).run();
        assertEquals(0L, clock.nowNs());
    }

    @Test
    @DisplayName("servers are addressable by node id")
    void serversAreRegisteredByNodeId() {
        DiscreteEventSimulator des = new DiscreteEventSimulator(new SimClock());
        SimNodeServer server = new SimNodeServer("n1", 4);
        des.addServer(server);

        assertEquals(server, des.getServer("n1"));
        assertEquals(null, des.getServer("nope"));
    }

    @Test
    @DisplayName("transport overhead defaults to none and a null clears it back")
    void transportOverheadDefaultsToNone() {
        DiscreteEventSimulator des = new DiscreteEventSimulator(new SimClock());
        assertEquals(0L, des.getTransportOverhead().sampleNs());

        des.setTransportOverhead(new TransportOverhead(5.0, 0.0, new java.util.Random(1), true));
        assertEquals(5_000_000L, des.getTransportOverhead().sampleNs());

        des.setTransportOverhead(null);
        assertTrue(des.getTransportOverhead() == TransportOverhead.NONE,
                "clearing it must give nothing, not a null that throws at the client boundary");
    }
}
