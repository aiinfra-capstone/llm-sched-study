package com.sched.v1;

import static io.grpc.MethodDescriptor.generateFullMethodName;

/**
 */
@javax.annotation.Generated(
    value = "by gRPC proto compiler (version 1.58.0)",
    comments = "Source: scheduling.proto")
@io.grpc.stub.annotations.GrpcGenerated
public final class SchedulerGrpc {

  private SchedulerGrpc() {}

  public static final java.lang.String SERVICE_NAME = "sched.v1.Scheduler";

  // Static method descriptors that strictly reflect the proto.
  private static volatile io.grpc.MethodDescriptor<com.sched.v1.DispatchRequest,
      com.sched.v1.DispatchAck> getDispatchMethod;

  @io.grpc.stub.annotations.RpcMethod(
      fullMethodName = SERVICE_NAME + '/' + "Dispatch",
      requestType = com.sched.v1.DispatchRequest.class,
      responseType = com.sched.v1.DispatchAck.class,
      methodType = io.grpc.MethodDescriptor.MethodType.UNARY)
  public static io.grpc.MethodDescriptor<com.sched.v1.DispatchRequest,
      com.sched.v1.DispatchAck> getDispatchMethod() {
    io.grpc.MethodDescriptor<com.sched.v1.DispatchRequest, com.sched.v1.DispatchAck> getDispatchMethod;
    if ((getDispatchMethod = SchedulerGrpc.getDispatchMethod) == null) {
      synchronized (SchedulerGrpc.class) {
        if ((getDispatchMethod = SchedulerGrpc.getDispatchMethod) == null) {
          SchedulerGrpc.getDispatchMethod = getDispatchMethod =
              io.grpc.MethodDescriptor.<com.sched.v1.DispatchRequest, com.sched.v1.DispatchAck>newBuilder()
              .setType(io.grpc.MethodDescriptor.MethodType.UNARY)
              .setFullMethodName(generateFullMethodName(SERVICE_NAME, "Dispatch"))
              .setSampledToLocalTracing(true)
              .setRequestMarshaller(io.grpc.protobuf.ProtoUtils.marshaller(
                  com.sched.v1.DispatchRequest.getDefaultInstance()))
              .setResponseMarshaller(io.grpc.protobuf.ProtoUtils.marshaller(
                  com.sched.v1.DispatchAck.getDefaultInstance()))
              .setSchemaDescriptor(new SchedulerMethodDescriptorSupplier("Dispatch"))
              .build();
        }
      }
    }
    return getDispatchMethod;
  }

  private static volatile io.grpc.MethodDescriptor<com.sched.v1.Heartbeat,
      com.sched.v1.BeginRun> getStreamHeartbeatMethod;

  @io.grpc.stub.annotations.RpcMethod(
      fullMethodName = SERVICE_NAME + '/' + "StreamHeartbeat",
      requestType = com.sched.v1.Heartbeat.class,
      responseType = com.sched.v1.BeginRun.class,
      methodType = io.grpc.MethodDescriptor.MethodType.BIDI_STREAMING)
  public static io.grpc.MethodDescriptor<com.sched.v1.Heartbeat,
      com.sched.v1.BeginRun> getStreamHeartbeatMethod() {
    io.grpc.MethodDescriptor<com.sched.v1.Heartbeat, com.sched.v1.BeginRun> getStreamHeartbeatMethod;
    if ((getStreamHeartbeatMethod = SchedulerGrpc.getStreamHeartbeatMethod) == null) {
      synchronized (SchedulerGrpc.class) {
        if ((getStreamHeartbeatMethod = SchedulerGrpc.getStreamHeartbeatMethod) == null) {
          SchedulerGrpc.getStreamHeartbeatMethod = getStreamHeartbeatMethod =
              io.grpc.MethodDescriptor.<com.sched.v1.Heartbeat, com.sched.v1.BeginRun>newBuilder()
              .setType(io.grpc.MethodDescriptor.MethodType.BIDI_STREAMING)
              .setFullMethodName(generateFullMethodName(SERVICE_NAME, "StreamHeartbeat"))
              .setSampledToLocalTracing(true)
              .setRequestMarshaller(io.grpc.protobuf.ProtoUtils.marshaller(
                  com.sched.v1.Heartbeat.getDefaultInstance()))
              .setResponseMarshaller(io.grpc.protobuf.ProtoUtils.marshaller(
                  com.sched.v1.BeginRun.getDefaultInstance()))
              .setSchemaDescriptor(new SchedulerMethodDescriptorSupplier("StreamHeartbeat"))
              .build();
        }
      }
    }
    return getStreamHeartbeatMethod;
  }

  private static volatile io.grpc.MethodDescriptor<com.sched.v1.Completion,
      com.sched.v1.ExecuteAck> getReportCompletionMethod;

  @io.grpc.stub.annotations.RpcMethod(
      fullMethodName = SERVICE_NAME + '/' + "ReportCompletion",
      requestType = com.sched.v1.Completion.class,
      responseType = com.sched.v1.ExecuteAck.class,
      methodType = io.grpc.MethodDescriptor.MethodType.UNARY)
  public static io.grpc.MethodDescriptor<com.sched.v1.Completion,
      com.sched.v1.ExecuteAck> getReportCompletionMethod() {
    io.grpc.MethodDescriptor<com.sched.v1.Completion, com.sched.v1.ExecuteAck> getReportCompletionMethod;
    if ((getReportCompletionMethod = SchedulerGrpc.getReportCompletionMethod) == null) {
      synchronized (SchedulerGrpc.class) {
        if ((getReportCompletionMethod = SchedulerGrpc.getReportCompletionMethod) == null) {
          SchedulerGrpc.getReportCompletionMethod = getReportCompletionMethod =
              io.grpc.MethodDescriptor.<com.sched.v1.Completion, com.sched.v1.ExecuteAck>newBuilder()
              .setType(io.grpc.MethodDescriptor.MethodType.UNARY)
              .setFullMethodName(generateFullMethodName(SERVICE_NAME, "ReportCompletion"))
              .setSampledToLocalTracing(true)
              .setRequestMarshaller(io.grpc.protobuf.ProtoUtils.marshaller(
                  com.sched.v1.Completion.getDefaultInstance()))
              .setResponseMarshaller(io.grpc.protobuf.ProtoUtils.marshaller(
                  com.sched.v1.ExecuteAck.getDefaultInstance()))
              .setSchemaDescriptor(new SchedulerMethodDescriptorSupplier("ReportCompletion"))
              .build();
        }
      }
    }
    return getReportCompletionMethod;
  }

  /**
   * Creates a new async stub that supports all call types for the service
   */
  public static SchedulerStub newStub(io.grpc.Channel channel) {
    io.grpc.stub.AbstractStub.StubFactory<SchedulerStub> factory =
      new io.grpc.stub.AbstractStub.StubFactory<SchedulerStub>() {
        @java.lang.Override
        public SchedulerStub newStub(io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
          return new SchedulerStub(channel, callOptions);
        }
      };
    return SchedulerStub.newStub(factory, channel);
  }

  /**
   * Creates a new blocking-style stub that supports unary and streaming output calls on the service
   */
  public static SchedulerBlockingStub newBlockingStub(
      io.grpc.Channel channel) {
    io.grpc.stub.AbstractStub.StubFactory<SchedulerBlockingStub> factory =
      new io.grpc.stub.AbstractStub.StubFactory<SchedulerBlockingStub>() {
        @java.lang.Override
        public SchedulerBlockingStub newStub(io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
          return new SchedulerBlockingStub(channel, callOptions);
        }
      };
    return SchedulerBlockingStub.newStub(factory, channel);
  }

  /**
   * Creates a new ListenableFuture-style stub that supports unary calls on the service
   */
  public static SchedulerFutureStub newFutureStub(
      io.grpc.Channel channel) {
    io.grpc.stub.AbstractStub.StubFactory<SchedulerFutureStub> factory =
      new io.grpc.stub.AbstractStub.StubFactory<SchedulerFutureStub>() {
        @java.lang.Override
        public SchedulerFutureStub newStub(io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
          return new SchedulerFutureStub(channel, callOptions);
        }
      };
    return SchedulerFutureStub.newStub(factory, channel);
  }

  /**
   */
  public interface AsyncService {

    /**
     */
    default void dispatch(com.sched.v1.DispatchRequest request,
        io.grpc.stub.StreamObserver<com.sched.v1.DispatchAck> responseObserver) {
      io.grpc.stub.ServerCalls.asyncUnimplementedUnaryCall(getDispatchMethod(), responseObserver);
    }

    /**
     */
    default io.grpc.stub.StreamObserver<com.sched.v1.Heartbeat> streamHeartbeat(
        io.grpc.stub.StreamObserver<com.sched.v1.BeginRun> responseObserver) {
      return io.grpc.stub.ServerCalls.asyncUnimplementedStreamingCall(getStreamHeartbeatMethod(), responseObserver);
    }

    /**
     */
    default void reportCompletion(com.sched.v1.Completion request,
        io.grpc.stub.StreamObserver<com.sched.v1.ExecuteAck> responseObserver) {
      io.grpc.stub.ServerCalls.asyncUnimplementedUnaryCall(getReportCompletionMethod(), responseObserver);
    }
  }

  /**
   * Base class for the server implementation of the service Scheduler.
   */
  public static abstract class SchedulerImplBase
      implements io.grpc.BindableService, AsyncService {

    @java.lang.Override public final io.grpc.ServerServiceDefinition bindService() {
      return SchedulerGrpc.bindService(this);
    }
  }

  /**
   * A stub to allow clients to do asynchronous rpc calls to service Scheduler.
   */
  public static final class SchedulerStub
      extends io.grpc.stub.AbstractAsyncStub<SchedulerStub> {
    private SchedulerStub(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      super(channel, callOptions);
    }

    @java.lang.Override
    protected SchedulerStub build(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      return new SchedulerStub(channel, callOptions);
    }

    /**
     */
    public void dispatch(com.sched.v1.DispatchRequest request,
        io.grpc.stub.StreamObserver<com.sched.v1.DispatchAck> responseObserver) {
      io.grpc.stub.ClientCalls.asyncUnaryCall(
          getChannel().newCall(getDispatchMethod(), getCallOptions()), request, responseObserver);
    }

    /**
     */
    public io.grpc.stub.StreamObserver<com.sched.v1.Heartbeat> streamHeartbeat(
        io.grpc.stub.StreamObserver<com.sched.v1.BeginRun> responseObserver) {
      return io.grpc.stub.ClientCalls.asyncBidiStreamingCall(
          getChannel().newCall(getStreamHeartbeatMethod(), getCallOptions()), responseObserver);
    }

    /**
     */
    public void reportCompletion(com.sched.v1.Completion request,
        io.grpc.stub.StreamObserver<com.sched.v1.ExecuteAck> responseObserver) {
      io.grpc.stub.ClientCalls.asyncUnaryCall(
          getChannel().newCall(getReportCompletionMethod(), getCallOptions()), request, responseObserver);
    }
  }

  /**
   * A stub to allow clients to do synchronous rpc calls to service Scheduler.
   */
  public static final class SchedulerBlockingStub
      extends io.grpc.stub.AbstractBlockingStub<SchedulerBlockingStub> {
    private SchedulerBlockingStub(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      super(channel, callOptions);
    }

    @java.lang.Override
    protected SchedulerBlockingStub build(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      return new SchedulerBlockingStub(channel, callOptions);
    }

    /**
     */
    public com.sched.v1.DispatchAck dispatch(com.sched.v1.DispatchRequest request) {
      return io.grpc.stub.ClientCalls.blockingUnaryCall(
          getChannel(), getDispatchMethod(), getCallOptions(), request);
    }

    /**
     */
    public com.sched.v1.ExecuteAck reportCompletion(com.sched.v1.Completion request) {
      return io.grpc.stub.ClientCalls.blockingUnaryCall(
          getChannel(), getReportCompletionMethod(), getCallOptions(), request);
    }
  }

  /**
   * A stub to allow clients to do ListenableFuture-style rpc calls to service Scheduler.
   */
  public static final class SchedulerFutureStub
      extends io.grpc.stub.AbstractFutureStub<SchedulerFutureStub> {
    private SchedulerFutureStub(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      super(channel, callOptions);
    }

    @java.lang.Override
    protected SchedulerFutureStub build(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      return new SchedulerFutureStub(channel, callOptions);
    }

    /**
     */
    public com.google.common.util.concurrent.ListenableFuture<com.sched.v1.DispatchAck> dispatch(
        com.sched.v1.DispatchRequest request) {
      return io.grpc.stub.ClientCalls.futureUnaryCall(
          getChannel().newCall(getDispatchMethod(), getCallOptions()), request);
    }

    /**
     */
    public com.google.common.util.concurrent.ListenableFuture<com.sched.v1.ExecuteAck> reportCompletion(
        com.sched.v1.Completion request) {
      return io.grpc.stub.ClientCalls.futureUnaryCall(
          getChannel().newCall(getReportCompletionMethod(), getCallOptions()), request);
    }
  }

  private static final int METHODID_DISPATCH = 0;
  private static final int METHODID_REPORT_COMPLETION = 1;
  private static final int METHODID_STREAM_HEARTBEAT = 2;

  private static final class MethodHandlers<Req, Resp> implements
      io.grpc.stub.ServerCalls.UnaryMethod<Req, Resp>,
      io.grpc.stub.ServerCalls.ServerStreamingMethod<Req, Resp>,
      io.grpc.stub.ServerCalls.ClientStreamingMethod<Req, Resp>,
      io.grpc.stub.ServerCalls.BidiStreamingMethod<Req, Resp> {
    private final AsyncService serviceImpl;
    private final int methodId;

    MethodHandlers(AsyncService serviceImpl, int methodId) {
      this.serviceImpl = serviceImpl;
      this.methodId = methodId;
    }

    @java.lang.Override
    @java.lang.SuppressWarnings("unchecked")
    public void invoke(Req request, io.grpc.stub.StreamObserver<Resp> responseObserver) {
      switch (methodId) {
        case METHODID_DISPATCH:
          serviceImpl.dispatch((com.sched.v1.DispatchRequest) request,
              (io.grpc.stub.StreamObserver<com.sched.v1.DispatchAck>) responseObserver);
          break;
        case METHODID_REPORT_COMPLETION:
          serviceImpl.reportCompletion((com.sched.v1.Completion) request,
              (io.grpc.stub.StreamObserver<com.sched.v1.ExecuteAck>) responseObserver);
          break;
        default:
          throw new AssertionError();
      }
    }

    @java.lang.Override
    @java.lang.SuppressWarnings("unchecked")
    public io.grpc.stub.StreamObserver<Req> invoke(
        io.grpc.stub.StreamObserver<Resp> responseObserver) {
      switch (methodId) {
        case METHODID_STREAM_HEARTBEAT:
          return (io.grpc.stub.StreamObserver<Req>) serviceImpl.streamHeartbeat(
              (io.grpc.stub.StreamObserver<com.sched.v1.BeginRun>) responseObserver);
        default:
          throw new AssertionError();
      }
    }
  }

  public static final io.grpc.ServerServiceDefinition bindService(AsyncService service) {
    return io.grpc.ServerServiceDefinition.builder(getServiceDescriptor())
        .addMethod(
          getDispatchMethod(),
          io.grpc.stub.ServerCalls.asyncUnaryCall(
            new MethodHandlers<
              com.sched.v1.DispatchRequest,
              com.sched.v1.DispatchAck>(
                service, METHODID_DISPATCH)))
        .addMethod(
          getStreamHeartbeatMethod(),
          io.grpc.stub.ServerCalls.asyncBidiStreamingCall(
            new MethodHandlers<
              com.sched.v1.Heartbeat,
              com.sched.v1.BeginRun>(
                service, METHODID_STREAM_HEARTBEAT)))
        .addMethod(
          getReportCompletionMethod(),
          io.grpc.stub.ServerCalls.asyncUnaryCall(
            new MethodHandlers<
              com.sched.v1.Completion,
              com.sched.v1.ExecuteAck>(
                service, METHODID_REPORT_COMPLETION)))
        .build();
  }

  private static abstract class SchedulerBaseDescriptorSupplier
      implements io.grpc.protobuf.ProtoFileDescriptorSupplier, io.grpc.protobuf.ProtoServiceDescriptorSupplier {
    SchedulerBaseDescriptorSupplier() {}

    @java.lang.Override
    public com.google.protobuf.Descriptors.FileDescriptor getFileDescriptor() {
      return com.sched.v1.SchedulingProto.getDescriptor();
    }

    @java.lang.Override
    public com.google.protobuf.Descriptors.ServiceDescriptor getServiceDescriptor() {
      return getFileDescriptor().findServiceByName("Scheduler");
    }
  }

  private static final class SchedulerFileDescriptorSupplier
      extends SchedulerBaseDescriptorSupplier {
    SchedulerFileDescriptorSupplier() {}
  }

  private static final class SchedulerMethodDescriptorSupplier
      extends SchedulerBaseDescriptorSupplier
      implements io.grpc.protobuf.ProtoMethodDescriptorSupplier {
    private final java.lang.String methodName;

    SchedulerMethodDescriptorSupplier(java.lang.String methodName) {
      this.methodName = methodName;
    }

    @java.lang.Override
    public com.google.protobuf.Descriptors.MethodDescriptor getMethodDescriptor() {
      return getServiceDescriptor().findMethodByName(methodName);
    }
  }

  private static volatile io.grpc.ServiceDescriptor serviceDescriptor;

  public static io.grpc.ServiceDescriptor getServiceDescriptor() {
    io.grpc.ServiceDescriptor result = serviceDescriptor;
    if (result == null) {
      synchronized (SchedulerGrpc.class) {
        result = serviceDescriptor;
        if (result == null) {
          serviceDescriptor = result = io.grpc.ServiceDescriptor.newBuilder(SERVICE_NAME)
              .setSchemaDescriptor(new SchedulerFileDescriptorSupplier())
              .addMethod(getDispatchMethod())
              .addMethod(getStreamHeartbeatMethod())
              .addMethod(getReportCompletionMethod())
              .build();
        }
      }
    }
    return result;
  }
}
