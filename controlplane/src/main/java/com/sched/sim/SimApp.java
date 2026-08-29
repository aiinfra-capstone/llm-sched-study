package com.sched.sim;

import com.sched.core.AdmissionFilter;
import com.sched.core.DecisionLogger;
import com.sched.core.InMemoryStateStore;
import com.sched.core.StalenessVeil;
import com.sched.core.models.TraceRequest;
import com.sched.core.models.CostModelSnapshot.Admissibility;
import com.sched.core.policies.*;

import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

public class SimApp {
    public static void main(String[] args) {
        String trc = args.length > 0 ? args[0] : "dummy_trace.jsonl";
        String[] pols = { "RoundRobin", "JSQ", "WJSQ", "StaticWeighted", "Threshold" };
        double[] stales = { 0.0, 0.1, 0.5, 1.0 };
        Map<String, Admissibility> adm = new HashMap<>();

        try {
            List<TraceRequest> reqs = TraceParser.parse(trc);
            System.out.println("Parsed " + reqs.size() + " requests from " + trc);

            for (String pName : pols) {
                for (double stS : stales) {
                    String rId = String.format("%s_s%.1f", pName, stS);
                    System.out.println("\n=== Starting Run: " + rId + " ===");

                    SimClock clk = new SimClock();
                    DiscreteEventSimulator des = new DiscreteEventSimulator(clk);
                    InMemoryStateStore st = new InMemoryStateStore();
                    StalenessVeil vl = new StalenessVeil((long) (stS * 1_000_000_000L), clk);
                    AdmissionFilter flt = new AdmissionFilter(adm);
                    Random rng = new Random(42);
                    ServiceSampler smp = new ServiceSampler(new HashMap<>(), rng);
                    DecisionLogger log = new DecisionLogger(rId);
                    AtomicLong seq = new AtomicLong(0);

                    com.sched.core.interfaces.Policy pol;
                    switch (pName) {
                        case "RoundRobin":
                            pol = new RoundRobin(new AtomicInteger(0));
                            break;
                        case "JSQ":
                            pol = new JSQ();
                            break;
                        case "WJSQ":
                            pol = new WJSQ();
                            break;
                        case "StaticWeighted":
                            pol = new StaticWeighted();
                            break;
                        case "Threshold":
                            pol = new Threshold(40.0, new AtomicInteger(0));
                            break;
                        default:
                            throw new IllegalArgumentException("Unknown policy");
                    }

                    for (TraceRequest rq : reqs) {
                        long arr = (long) (rq.arrivalOffsetS() * 1_000_000_000L);
                        RequestArrivalEvent ev = new RequestArrivalEvent(
                                arr, rq, pol, vl, flt, des, rng,
                                smp, st, log, rId, pName, stS, seq);
                        des.scheduleEvent(ev);
                    }

                    des.run();
                    log.close();
                    System.out.println("Completed Run: " + rId + " | Decisions: " + seq.get());
                }
            }
            System.out.println("\nAll sweeps finished.");
        } catch (Exception e) {
            System.err.println("Error: " + e.getMessage());
            e.printStackTrace();
        }
    }
}