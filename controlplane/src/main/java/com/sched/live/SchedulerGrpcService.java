package com.sched.live;

import io.grpc.stub.StreamObserver;
import com.sched.v1.SchedulerGrpc.SchedulerImplBase;
import com.sched.v1.DispatchRequest;
import com.sched.v1.DispatchAck;
import com.sched.v1.Heartbeat;
import com.sched.v1.BeginRun;
import com.sched.v1.Completion;
import com.sched.v1.ExecuteAck;

public class SchedulerGrpcService extends SchedulerImplBase {

    @Override
    public void dispatch(DispatchRequest request, StreamObserver<DispatchAck> responseObserver) {
        // TODO: In Week 3, this will pass the request to the Policy.choose() method.
        // For now, we just acknowledge receipt for our end-to-end Week 1 test.
        DispatchAck ack = DispatchAck.newBuilder()
                .setReqId(request.getReqId())
                .setAccepted(true)
                .setChosenNode("dummy-node-1")
                .build();

        responseObserver.onNext(ack);
        responseObserver.onCompleted();
    }

    @Override
    public StreamObserver<Heartbeat> streamHeartbeat(StreamObserver<BeginRun> responseObserver) {
        // This is a bidirectional stream. We receive heartbeats from the worker
        // and can send back BeginRun commands.
        return new StreamObserver<Heartbeat>() {
            @Override
            public void onNext(Heartbeat heartbeat) {
                // TODO: Update our StateStore with the worker's live queue depth and tok/s
                System.out.println("Received heartbeat from node: " + heartbeat.getNodeId());
            }

            @Override
            public void onError(Throwable t) {
                System.err.println("Heartbeat stream error: " + t.getMessage());
            }

            @Override
            public void onCompleted() {
                responseObserver.onCompleted();
            }
        };
    }

    @Override
    public void reportCompletion(Completion request, StreamObserver<ExecuteAck> responseObserver) {
        // F-11: Scheduler is off the response path, so this completion is just to
        // update state.
        ExecuteAck ack = ExecuteAck.newBuilder()
                .setReqId(request.getReqId())
                .setQueued(false)
                .build();

        responseObserver.onNext(ack);
        responseObserver.onCompleted();
    }
}