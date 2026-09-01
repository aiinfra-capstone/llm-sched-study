package com.sched.v1;

import static io.grpc.MethodDescriptor.generateFullMethodName;

/**
 */
@javax.annotation.Generated(
    value = "by gRPC proto compiler (version 1.58.0)",
    comments = "Source: scheduling.proto")
@io.grpc.stub.annotations.GrpcGenerated
public final class WorkerGrpc {

  private WorkerGrpc() {}

  public static final java.lang.String SERVICE_NAME = "sched.v1.Worker";

  // Static method descriptors that strictly reflect the proto.
  private static volatile io.grpc.MethodDescriptor<com.sched.v1.ExecuteRequest,
      com.sched.v1.ExecuteAck> getExecuteMethod;

  @io.grpc.stub.annotations.RpcMethod(
      fullMethodName = SERVICE_NAME + '/' + "Execute",
      requestType = com.sched.v1.ExecuteRequest.class,
      responseType = com.sched.v1.ExecuteAck.class,
      methodType = io.grpc.MethodDescriptor.MethodType.UNARY)
  public static io.grpc.MethodDescriptor<com.sched.v1.ExecuteRequest,
      com.sched.v1.ExecuteAck> getExecuteMethod() {
    io.grpc.MethodDescriptor<com.sched.v1.ExecuteRequest, com.sched.v1.ExecuteAck> getExecuteMethod;
    if ((getExecuteMethod = WorkerGrpc.getExecuteMethod) == null) {
      synchronized (WorkerGrpc.class) {
        if ((getExecuteMethod = WorkerGrpc.getExecuteMethod) == null) {
          WorkerGrpc.getExecuteMethod = getExecuteMethod =
              io.grpc.MethodDescriptor.<com.sched.v1.ExecuteRequest, com.sched.v1.ExecuteAck>newBuilder()
              .setType(io.grpc.MethodDescriptor.MethodType.UNARY)
              .setFullMethodName(generateFullMethodName(SERVICE_NAME, "Execute"))
              .setSampledToLocalTracing(true)
              .setRequestMarshaller(io.grpc.protobuf.ProtoUtils.marshaller(
                  com.sched.v1.ExecuteRequest.getDefaultInstance()))
              .setResponseMarshaller(io.grpc.protobuf.ProtoUtils.marshaller(
                  com.sched.v1.ExecuteAck.getDefaultInstance()))
              .setSchemaDescriptor(new WorkerMethodDescriptorSupplier("Execute"))
              .build();
        }
      }
    }
    return getExecuteMethod;
  }

  private static volatile io.grpc.MethodDescriptor<com.sched.v1.BeginRun,
      com.sched.v1.ExecuteAck> getBeginMethod;

  @io.grpc.stub.annotations.RpcMethod(
      fullMethodName = SERVICE_NAME + '/' + "Begin",
      requestType = com.sched.v1.BeginRun.class,
      responseType = com.sched.v1.ExecuteAck.class,
      methodType = io.grpc.MethodDescriptor.MethodType.UNARY)
  public static io.grpc.MethodDescriptor<com.sched.v1.BeginRun,
      com.sched.v1.ExecuteAck> getBeginMethod() {
    io.grpc.MethodDescriptor<com.sched.v1.BeginRun, com.sched.v1.ExecuteAck> getBeginMethod;
    if ((getBeginMethod = WorkerGrpc.getBeginMethod) == null) {
      synchronized (WorkerGrpc.class) {
        if ((getBeginMethod = WorkerGrpc.getBeginMethod) == null) {
          WorkerGrpc.getBeginMethod = getBeginMethod =
              io.grpc.MethodDescriptor.<com.sched.v1.BeginRun, com.sched.v1.ExecuteAck>newBuilder()
              .setType(io.grpc.MethodDescriptor.MethodType.UNARY)
              .setFullMethodName(generateFullMethodName(SERVICE_NAME, "Begin"))
              .setSampledToLocalTracing(true)
              .setRequestMarshaller(io.grpc.protobuf.ProtoUtils.marshaller(
                  com.sched.v1.BeginRun.getDefaultInstance()))
              .setResponseMarshaller(io.grpc.protobuf.ProtoUtils.marshaller(
                  com.sched.v1.ExecuteAck.getDefaultInstance()))
              .setSchemaDescriptor(new WorkerMethodDescriptorSupplier("Begin"))
              .build();
        }
      }
    }
    return getBeginMethod;
  }

  private static volatile io.grpc.MethodDescriptor<com.sched.v1.EndRun,
      com.sched.v1.ExecuteAck> getEndMethod;

  @io.grpc.stub.annotations.RpcMethod(
      fullMethodName = SERVICE_NAME + '/' + "End",
      requestType = com.sched.v1.EndRun.class,
      responseType = com.sched.v1.ExecuteAck.class,
      methodType = io.grpc.MethodDescriptor.MethodType.UNARY)
  public static io.grpc.MethodDescriptor<com.sched.v1.EndRun,
      com.sched.v1.ExecuteAck> getEndMethod() {
    io.grpc.MethodDescriptor<com.sched.v1.EndRun, com.sched.v1.ExecuteAck> getEndMethod;
    if ((getEndMethod = WorkerGrpc.getEndMethod) == null) {
      synchronized (WorkerGrpc.class) {
        if ((getEndMethod = WorkerGrpc.getEndMethod) == null) {
          WorkerGrpc.getEndMethod = getEndMethod =
              io.grpc.MethodDescriptor.<com.sched.v1.EndRun, com.sched.v1.ExecuteAck>newBuilder()
              .setType(io.grpc.MethodDescriptor.MethodType.UNARY)
              .setFullMethodName(generateFullMethodName(SERVICE_NAME, "End"))
              .setSampledToLocalTracing(true)
              .setRequestMarshaller(io.grpc.protobuf.ProtoUtils.marshaller(
                  com.sched.v1.EndRun.getDefaultInstance()))
              .setResponseMarshaller(io.grpc.protobuf.ProtoUtils.marshaller(
                  com.sched.v1.ExecuteAck.getDefaultInstance()))
              .setSchemaDescriptor(new WorkerMethodDescriptorSupplier("End"))
              .build();
        }
      }
    }
    return getEndMethod;
  }

  /**
   * Creates a new async stub that supports all call types for the service
   */
  public static WorkerStub newStub(io.grpc.Channel channel) {
    io.grpc.stub.AbstractStub.StubFactory<WorkerStub> factory =
      new io.grpc.stub.AbstractStub.StubFactory<WorkerStub>() {
        @java.lang.Override
        public WorkerStub newStub(io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
          return new WorkerStub(channel, callOptions);
        }
      };
    return WorkerStub.newStub(factory, channel);
  }

  /**
   * Creates a new blocking-style stub that supports unary and streaming output calls on the service
   */
  public static WorkerBlockingStub newBlockingStub(
      io.grpc.Channel channel) {
    io.grpc.stub.AbstractStub.StubFactory<WorkerBlockingStub> factory =
      new io.grpc.stub.AbstractStub.StubFactory<WorkerBlockingStub>() {
        @java.lang.Override
        public WorkerBlockingStub newStub(io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
          return new WorkerBlockingStub(channel, callOptions);
        }
      };
    return WorkerBlockingStub.newStub(factory, channel);
  }

  /**
   * Creates a new ListenableFuture-style stub that supports unary calls on the service
   */
  public static WorkerFutureStub newFutureStub(
      io.grpc.Channel channel) {
    io.grpc.stub.AbstractStub.StubFactory<WorkerFutureStub> factory =
      new io.grpc.stub.AbstractStub.StubFactory<WorkerFutureStub>() {
        @java.lang.Override
        public WorkerFutureStub newStub(io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
          return new WorkerFutureStub(channel, callOptions);
        }
      };
    return WorkerFutureStub.newStub(factory, channel);
  }

  /**
   */
  public interface AsyncService {

    /**
     */
    default void execute(com.sched.v1.ExecuteRequest request,
        io.grpc.stub.StreamObserver<com.sched.v1.ExecuteAck> responseObserver) {
      io.grpc.stub.ServerCalls.asyncUnimplementedUnaryCall(getExecuteMethod(), responseObserver);
    }

    /**
     */
    default void begin(com.sched.v1.BeginRun request,
        io.grpc.stub.StreamObserver<com.sched.v1.ExecuteAck> responseObserver) {
      io.grpc.stub.ServerCalls.asyncUnimplementedUnaryCall(getBeginMethod(), responseObserver);
    }

    /**
     */
    default void end(com.sched.v1.EndRun request,
        io.grpc.stub.StreamObserver<com.sched.v1.ExecuteAck> responseObserver) {
      io.grpc.stub.ServerCalls.asyncUnimplementedUnaryCall(getEndMethod(), responseObserver);
    }
  }

  /**
   * Base class for the server implementation of the service Worker.
   */
  public static abstract class WorkerImplBase
      implements io.grpc.BindableService, AsyncService {

    @java.lang.Override public final io.grpc.ServerServiceDefinition bindService() {
      return WorkerGrpc.bindService(this);
    }
  }

  /**
   * A stub to allow clients to do asynchronous rpc calls to service Worker.
   */
  public static final class WorkerStub
      extends io.grpc.stub.AbstractAsyncStub<WorkerStub> {
    private WorkerStub(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      super(channel, callOptions);
    }

    @java.lang.Override
    protected WorkerStub build(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      return new WorkerStub(channel, callOptions);
    }

    /**
     */
    public void execute(com.sched.v1.ExecuteRequest request,
        io.grpc.stub.StreamObserver<com.sched.v1.ExecuteAck> responseObserver) {
      io.grpc.stub.ClientCalls.asyncUnaryCall(
          getChannel().newCall(getExecuteMethod(), getCallOptions()), request, responseObserver);
    }

    /**
     */
    public void begin(com.sched.v1.BeginRun request,
        io.grpc.stub.StreamObserver<com.sched.v1.ExecuteAck> responseObserver) {
      io.grpc.stub.ClientCalls.asyncUnaryCall(
          getChannel().newCall(getBeginMethod(), getCallOptions()), request, responseObserver);
    }

    /**
     */
    public void end(com.sched.v1.EndRun request,
        io.grpc.stub.StreamObserver<com.sched.v1.ExecuteAck> responseObserver) {
      io.grpc.stub.ClientCalls.asyncUnaryCall(
          getChannel().newCall(getEndMethod(), getCallOptions()), request, responseObserver);
    }
  }

  /**
   * A stub to allow clients to do synchronous rpc calls to service Worker.
   */
  public static final class WorkerBlockingStub
      extends io.grpc.stub.AbstractBlockingStub<WorkerBlockingStub> {
    private WorkerBlockingStub(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      super(channel, callOptions);
    }

    @java.lang.Override
    protected WorkerBlockingStub build(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      return new WorkerBlockingStub(channel, callOptions);
    }

    /**
     */
    public com.sched.v1.ExecuteAck execute(com.sched.v1.ExecuteRequest request) {
      return io.grpc.stub.ClientCalls.blockingUnaryCall(
          getChannel(), getExecuteMethod(), getCallOptions(), request);
    }

    /**
     */
    public com.sched.v1.ExecuteAck begin(com.sched.v1.BeginRun request) {
      return io.grpc.stub.ClientCalls.blockingUnaryCall(
          getChannel(), getBeginMethod(), getCallOptions(), request);
    }

    /**
     */
    public com.sched.v1.ExecuteAck end(com.sched.v1.EndRun request) {
      return io.grpc.stub.ClientCalls.blockingUnaryCall(
          getChannel(), getEndMethod(), getCallOptions(), request);
    }
  }

  /**
   * A stub to allow clients to do ListenableFuture-style rpc calls to service Worker.
   */
  public static final class WorkerFutureStub
      extends io.grpc.stub.AbstractFutureStub<WorkerFutureStub> {
    private WorkerFutureStub(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      super(channel, callOptions);
    }

    @java.lang.Override
    protected WorkerFutureStub build(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      return new WorkerFutureStub(channel, callOptions);
    }

    /**
     */
    public com.google.common.util.concurrent.ListenableFuture<com.sched.v1.ExecuteAck> execute(
        com.sched.v1.ExecuteRequest request) {
      return io.grpc.stub.ClientCalls.futureUnaryCall(
          getChannel().newCall(getExecuteMethod(), getCallOptions()), request);
    }

    /**
     */
    public com.google.common.util.concurrent.ListenableFuture<com.sched.v1.ExecuteAck> begin(
        com.sched.v1.BeginRun request) {
      return io.grpc.stub.ClientCalls.futureUnaryCall(
          getChannel().newCall(getBeginMethod(), getCallOptions()), request);
    }

    /**
     */
    public com.google.common.util.concurrent.ListenableFuture<com.sched.v1.ExecuteAck> end(
        com.sched.v1.EndRun request) {
      return io.grpc.stub.ClientCalls.futureUnaryCall(
          getChannel().newCall(getEndMethod(), getCallOptions()), request);
    }
  }

  private static final int METHODID_EXECUTE = 0;
  private static final int METHODID_BEGIN = 1;
  private static final int METHODID_END = 2;

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
        case METHODID_EXECUTE:
          serviceImpl.execute((com.sched.v1.ExecuteRequest) request,
              (io.grpc.stub.StreamObserver<com.sched.v1.ExecuteAck>) responseObserver);
          break;
        case METHODID_BEGIN:
          serviceImpl.begin((com.sched.v1.BeginRun) request,
              (io.grpc.stub.StreamObserver<com.sched.v1.ExecuteAck>) responseObserver);
          break;
        case METHODID_END:
          serviceImpl.end((com.sched.v1.EndRun) request,
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
        default:
          throw new AssertionError();
      }
    }
  }

  public static final io.grpc.ServerServiceDefinition bindService(AsyncService service) {
    return io.grpc.ServerServiceDefinition.builder(getServiceDescriptor())
        .addMethod(
          getExecuteMethod(),
          io.grpc.stub.ServerCalls.asyncUnaryCall(
            new MethodHandlers<
              com.sched.v1.ExecuteRequest,
              com.sched.v1.ExecuteAck>(
                service, METHODID_EXECUTE)))
        .addMethod(
          getBeginMethod(),
          io.grpc.stub.ServerCalls.asyncUnaryCall(
            new MethodHandlers<
              com.sched.v1.BeginRun,
              com.sched.v1.ExecuteAck>(
                service, METHODID_BEGIN)))
        .addMethod(
          getEndMethod(),
          io.grpc.stub.ServerCalls.asyncUnaryCall(
            new MethodHandlers<
              com.sched.v1.EndRun,
              com.sched.v1.ExecuteAck>(
                service, METHODID_END)))
        .build();
  }

  private static abstract class WorkerBaseDescriptorSupplier
      implements io.grpc.protobuf.ProtoFileDescriptorSupplier, io.grpc.protobuf.ProtoServiceDescriptorSupplier {
    WorkerBaseDescriptorSupplier() {}

    @java.lang.Override
    public com.google.protobuf.Descriptors.FileDescriptor getFileDescriptor() {
      return com.sched.v1.SchedulingProto.getDescriptor();
    }

    @java.lang.Override
    public com.google.protobuf.Descriptors.ServiceDescriptor getServiceDescriptor() {
      return getFileDescriptor().findServiceByName("Worker");
    }
  }

  private static final class WorkerFileDescriptorSupplier
      extends WorkerBaseDescriptorSupplier {
    WorkerFileDescriptorSupplier() {}
  }

  private static final class WorkerMethodDescriptorSupplier
      extends WorkerBaseDescriptorSupplier
      implements io.grpc.protobuf.ProtoMethodDescriptorSupplier {
    private final java.lang.String methodName;

    WorkerMethodDescriptorSupplier(java.lang.String methodName) {
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
      synchronized (WorkerGrpc.class) {
        result = serviceDescriptor;
        if (result == null) {
          serviceDescriptor = result = io.grpc.ServiceDescriptor.newBuilder(SERVICE_NAME)
              .setSchemaDescriptor(new WorkerFileDescriptorSupplier())
              .addMethod(getExecuteMethod())
              .addMethod(getBeginMethod())
              .addMethod(getEndMethod())
              .build();
        }
      }
    }
    return result;
  }
}
