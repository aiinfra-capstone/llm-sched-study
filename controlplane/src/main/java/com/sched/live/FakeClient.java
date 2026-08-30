package com.sched.live;

import io.grpc.ManagedChannel;
import io.grpc.ManagedChannelBuilder;
import com.sched.v1.SchedulerGrpc;
import com.sched.v1.DispatchRequest;
import com.sched.v1.DispatchAck;
import java.util.Collections;

public class FakeClient {
    public static void main(String[] args) {
        // 1. Open connection to the Live Scheduler
        ManagedChannel channel = ManagedChannelBuilder.forAddress("localhost", 50051)
                .usePlaintext()
                .build();

        // 2. Create a blocking stub for a standard synchronous RPC call
        SchedulerGrpc.SchedulerBlockingStub stub = SchedulerGrpc.newBlockingStub(channel);

        // 3. Craft a dummy request representing an LLM prompt
        DispatchRequest req = DispatchRequest.newBuilder()
                .setReqId("live-req-001")
                .setPriority(1)
                .setOutputLen(50)
                .setBucketId("b1")
                // Simulating a 100-token prompt
                .addAllPromptTokenIds(Collections.nCopies(100, 0))
                .build();

        System.out.println("Sending dispatch request: " + req.getReqId());

        // 4. Send the request and wait for the acknowledgment
        DispatchAck ack = stub.dispatch(req);

        // 5. Print the F-1 wire schema response
        System.out.println("Received Ack:");
        System.out.println("  Accepted: " + ack.getAccepted());
        System.out.println("  Chosen Node: " + ack.getChosenNode());
        System.out.println("  Reject Reason: " + ack.getRejectReason());

        channel.shutdown();
    }
}