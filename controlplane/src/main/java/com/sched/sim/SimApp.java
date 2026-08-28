package com.sched.sim;

import com.sched.core.AdmissionFilter;
import com.sched.core.InMemoryStateStore;
import com.sched.core.StalenessVeil;
import com.sched.core.models.TraceRequest;
import com.sched.core.policies.RoundRobin;
import java.util.HashMap;
import java.util.List;
import java.util.Random;
import java.util.concurrent.atomic.AtomicInteger;

public class SimApp {
    public static void main(String[] args) {
        String p = args.length > 0 ? args[0] : "dummy_trace.jsonl";
        SimClock clk = new SimClock();
        DiscreteEventSimulator des = new DiscreteEventSimulator(clk);
        InMemoryStateStore st = new InMemoryStateStore();
        StalenessVeil vl = new StalenessVeil(100_000_000L, clk);
        AdmissionFilter flt = new AdmissionFilter(new HashMap<>());
        Random r = new Random(42);
        ServiceSampler smp = new ServiceSampler(new HashMap<>(), r);
        RoundRobin rr = new RoundRobin(new AtomicInteger(0));

        try {
            List<TraceRequest> reqs = TraceParser.parse(p);
            for (TraceRequest rq : reqs) {
                long arr = (long) (rq.arrivalOffsetS() * 1_000_000_000L);
                RequestArrivalEvent ev = new RequestArrivalEvent(arr, rq, rr, vl, flt, des, r, smp, st);
                des.scheduleEvent(ev);
            }
            des.run();
        } catch (Exception e) {
            System.err.println("Error running simulation: " + e.getMessage());
        }
    }
}