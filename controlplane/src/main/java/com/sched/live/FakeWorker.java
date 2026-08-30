package com.sched.live;

import io.grpc.ManagedChannel;
import io.grpc.ManagedChannelBuilder;
import io.grpc.stub.StreamObserver;
import com.sched.v1.SchedulerGrpc;
import com.sched.v1.Heartbeat;
import com.sched.v1.BeginRun;

public class FakeWorker {
    public static void main(String[] args) throws InterruptedException {
        // 1. Open a connection to the Live Scheduler
        ManagedChannel channel = ManagedChannelBuilder.forAddress("localhost", 50051)
                .usePlaintext() // No TLS needed for local testing
                .build();

        // 2. Create an asynchronous stub for streaming
        SchedulerGrpc.SchedulerStub stub = SchedulerGrpc.newStub(channel);

        // 3. Define how the Fake Worker reacts to messages from the Scheduler
        StreamObserver<BeginRun> responseObserver = new StreamObserver<BeginRun>() {
            @Override
            public void onNext(BeginRun value) {
                System.out.println("Scheduler sent BeginRun command for run: " + value.getRunId());
            }

            @Override
            public void onError(Throwable t) {
                System.err.println("Stream error: " + t.getMessage());
            }

            @Override
            public void onCompleted() {
                System.out.println("Scheduler closed the stream.");
            }
        };

        // 4. Initialize the bidirectional stream
        StreamObserver<Heartbeat> requestObserver = stub.streamHeartbeat(responseObserver);

        // 5. Send 5 scripted heartbeats (fulfills F-10 requirement for live state)
        System.out.println("Starting Fake Worker heartbeat sequence...");
        for (int i = 1; i <= 5; i++) {
            Heartbeat beat = Heartbeat.newBuilder()
                    .setRunId("test-run-001")
                    .setNodeId("fake-node-A")
                    .setSeq(i)
                    .setQueueDepth(2) // Scripted queue depth
                    .setInflightCount(4) // Scripted inflight requests
                    .setRecentTokensPerS(45.5) // Scripted capability
                    .setEngineState("ready")
                    .build();

            requestObserver.onNext(beat);
            System.out.println("Sent heartbeat seq: " + i);

            Thread.sleep(1000); // 1-second interval
        }

        // 6. Clean up
        requestObserver.onCompleted();
        channel.shutdown();
        System.out.println("Fake Worker finished.");
    }
}