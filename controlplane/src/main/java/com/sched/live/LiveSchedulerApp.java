package com.sched.live;

import io.grpc.Server;
import io.grpc.ServerBuilder;
import java.io.IOException;

public class LiveSchedulerApp {
    public static void main(String[] args) throws IOException, InterruptedException {
        int port = 50051;
        Server server = ServerBuilder.forPort(port)
                .addService(new SchedulerGrpcService())
                .build()
                .start();

        System.out.println("Live Scheduler started, listening on port " + port);
        server.awaitTermination();
    }
}