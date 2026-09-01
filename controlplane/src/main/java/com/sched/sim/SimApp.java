package com.sched.sim;

import com.sched.core.AdmissionFilter;
import com.sched.core.DecisionLogger;
import com.sched.core.InMemoryStateStore;
import com.sched.core.StalenessVeil;
import com.sched.core.models.TraceRequest;
import com.sched.core.models.CostModelSnapshot;
import com.sched.core.models.CostModelParser;
import com.sched.core.models.Manifest;
import com.sched.core.models.ManifestParser;
import com.sched.core.policies.*;

import java.io.File;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

public class SimApp {
    public static void main(String[] args) {
        if (args.length < 3) {
            System.err.println("Usage: SimApp <trace_file.jsonl> <manifest_file.json> <output_dir>");
            return;
        }
        String trc = args[0];
        String manifestPath = args[1];
        String outputDir = args[2];

        try {
            // 1. Parse the specific C-6 Manifest for this anchor run
            Manifest manifest = ManifestParser.parse(manifestPath);
            if (manifest.manifestSchema() != null && manifest.manifestSchema() != 1) {
                System.err.println("Warning: Manifest schema is not 1");
            }

            // 2. Load the exact C-3 Cost Models defined by the manifest
            Map<String, CostModelSnapshot> loadedSnaps = new HashMap<>();
            Map<String, CostModelSnapshot.Admissibility> admBounds = new HashMap<>();

            for (Map.Entry<String, String> entry : manifest.costModelSnapshots().entrySet()) {
                String nodeId = entry.getKey();
                String snapshotFileName = entry.getValue() + ".json";

                File snapFile = findSnapshotFile(snapshotFileName);
                if (snapFile == null || !snapFile.exists()) {
                    throw new RuntimeException("Could not find snapshot file: " + snapshotFileName);
                }
                CostModelSnapshot snap = CostModelParser.parse(snapFile);
                loadedSnaps.put(nodeId, snap);
                admBounds.put(nodeId, snap.admissibility());
            }

            List<TraceRequest> reqs = TraceParser.parse(trc);
            System.out.println("Parsed " + reqs.size() + " requests from " + trc);
            System.out.println("Loaded " + loadedSnaps.size() + " cost models based on manifest.");

            // 3. Configure the Simulator for the F-23 Anchor Validation
            String rId = manifest.runId();
            File dir = new File(outputDir);
            if (!dir.exists())
                dir.mkdirs();

            SimClock clk = new SimClock();
            DiscreteEventSimulator des = new DiscreteEventSimulator(clk);
            InMemoryStateStore st = new InMemoryStateStore();
            StalenessVeil vl = new StalenessVeil(0L, clk);
            AdmissionFilter flt = new AdmissionFilter(admBounds);
            Random rng = new Random(42);
            ServiceSampler smp = new ServiceSampler(loadedSnaps, rng);

            // Use the standard 1-argument constructor
            DecisionLogger log = new DecisionLogger(rId);
            AtomicLong seq = new AtomicLong(0);
            com.sched.core.interfaces.Policy pol = new RoundRobin(new AtomicInteger(0));

            for (TraceRequest rq : reqs) {
                long arr = (long) (rq.arrivalOffsetS() * 1_000_000_000L);

                // RequestArrivalEvent is in this same package, so no import needed.
                RequestArrivalEvent ev = new RequestArrivalEvent(
                        arr, rq, pol, vl, flt, des, rng, smp, st, log, rId, "RoundRobin", 0.0, seq);
                des.scheduleEvent(ev);
            }

            des.run();
            log.close();

            // Move the generated log to the required F-23 output directory
            File defaultLog = new File("scheduler_" + rId + ".jsonl");
            if (!defaultLog.exists()) {
                defaultLog = new File(rId + ".jsonl");
            }
            if (defaultLog.exists()) {
                File dest = new File(dir, "scheduler_" + rId + ".jsonl");
                Files.move(defaultLog.toPath(), dest.toPath(), StandardCopyOption.REPLACE_EXISTING);
            }
            System.out.println("Validation Run Completed: " + rId + " | Decisions: " + seq.get());

        } catch (Exception e) {
            System.err.println("Error during simulation: " + e.getMessage());
            e.printStackTrace();
        }
    }

    private static File findSnapshotFile(String fileName) {
        File root = new File("../contracts/cost_models");
        if (!root.exists())
            return new File(fileName);
        return searchFile(root, fileName);
    }

    private static File searchFile(File dir, String fileName) {
        File[] files = dir.listFiles();
        if (files != null) {
            for (File f : files) {
                if (f.isDirectory()) {
                    File found = searchFile(f, fileName);
                    if (found != null)
                        return found;
                } else if (f.getName().equals(fileName)) {
                    return f;
                }
            }
        }
        return null;
    }
}
