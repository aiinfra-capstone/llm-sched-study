package com.sched.v1;

import static io.grpc.MethodDescriptor.generateFullMethodName;

/**
 */
@javax.annotation.Generated(
    value = "by gRPC proto compiler (version 1.58.0)",
    comments = "Source: scheduling.proto")
@io.grpc.stub.annotations.GrpcGenerated
public final class ClientGrpc {

  private ClientGrpc() {}

  public static final java.lang.String SERVICE_NAME = "sched.v1.Client";

  // Static method descriptors that strictly reflect the proto.
  private static volatile io.grpc.MethodDescriptor<com.sched.v1.ResponseDelivery,
      com.sched.v1.ExecuteAck> getDeliverMethod;

  @io.grpc.stub.annotations.RpcMethod(
      fullMethodName = SERVICE_NAME + '/' + "Deliver",
      requestType = com.sched.v1.ResponseDelivery.class,
      responseType = com.sched.v1.ExecuteAck.class,
      methodType = io.grpc.MethodDescriptor.MethodType.UNARY)
  public static io.grpc.MethodDescriptor<com.sched.v1.ResponseDelivery,
      com.sched.v1.ExecuteAck> getDeliverMethod() {
    io.grpc.MethodDescriptor<com.sched.v1.ResponseDelivery, com.sched.v1.ExecuteAck> getDeliverMethod;
    if ((getDeliverMethod = ClientGrpc.getDeliverMethod) == null) {
      synchronized (ClientGrpc.class) {
        if ((getDeliverMethod = ClientGrpc.getDeliverMethod) == null) {
          ClientGrpc.getDeliverMethod = getDeliverMethod =
              io.grpc.MethodDescriptor.<com.sched.v1.ResponseDelivery, com.sched.v1.ExecuteAck>newBuilder()
              .setType(io.grpc.MethodDescriptor.MethodType.UNARY)
              .setFullMethodName(generateFullMethodName(SERVICE_NAME, "Deliver"))
              .setSampledToLocalTracing(true)
              .setRequestMarshaller(io.grpc.protobuf.ProtoUtils.marshaller(
                  com.sched.v1.ResponseDelivery.getDefaultInstance()))
              .setResponseMarshaller(io.grpc.protobuf.ProtoUtils.marshaller(
                  com.sched.v1.ExecuteAck.getDefaultInstance()))
              .setSchemaDescriptor(new ClientMethodDescriptorSupplier("Deliver"))
              .build();
        }
      }
    }
    return getDeliverMethod;
  }

  /**
   * Creates a new async stub that supports all call types for the service
   */
  public static ClientStub newStub(io.grpc.Channel channel) {
    io.grpc.stub.AbstractStub.StubFactory<ClientStub> factory =
      new io.grpc.stub.AbstractStub.StubFactory<ClientStub>() {
        @java.lang.Override
        public ClientStub newStub(io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
          return new ClientStub(channel, callOptions);
        }
      };
    return ClientStub.newStub(factory, channel);
  }

  /**
   * Creates a new blocking-style stub that supports unary and streaming output calls on the service
   */
  public static ClientBlockingStub newBlockingStub(
      io.grpc.Channel channel) {
    io.grpc.stub.AbstractStub.StubFactory<ClientBlockingStub> factory =
      new io.grpc.stub.AbstractStub.StubFactory<ClientBlockingStub>() {
        @java.lang.Override
        public ClientBlockingStub newStub(io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
          return new ClientBlockingStub(channel, callOptions);
        }
      };
    return ClientBlockingStub.newStub(factory, channel);
  }

  /**
   * Creates a new ListenableFuture-style stub that supports unary calls on the service
   */
  public static ClientFutureStub newFutureStub(
      io.grpc.Channel channel) {
    io.grpc.stub.AbstractStub.StubFactory<ClientFutureStub> factory =
      new io.grpc.stub.AbstractStub.StubFactory<ClientFutureStub>() {
        @java.lang.Override
        public ClientFutureStub newStub(io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
          return new ClientFutureStub(channel, callOptions);
        }
      };
    return ClientFutureStub.newStub(factory, channel);
  }

  /**
   */
  public interface AsyncService {

    /**
     */
    default void deliver(com.sched.v1.ResponseDelivery request,
        io.grpc.stub.StreamObserver<com.sched.v1.ExecuteAck> responseObserver) {
      io.grpc.stub.ServerCalls.asyncUnimplementedUnaryCall(getDeliverMethod(), responseObserver);
    }
  }

  /**
   * Base class for the server implementation of the service Client.
   */
  public static abstract class ClientImplBase
      implements io.grpc.BindableService, AsyncService {

    @java.lang.Override public final io.grpc.ServerServiceDefinition bindService() {
      return ClientGrpc.bindService(this);
    }
  }

  /**
   * A stub to allow clients to do asynchronous rpc calls to service Client.
   */
  public static final class ClientStub
      extends io.grpc.stub.AbstractAsyncStub<ClientStub> {
    private ClientStub(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      super(channel, callOptions);
    }

    @java.lang.Override
    protected ClientStub build(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      return new ClientStub(channel, callOptions);
    }

    /**
     */
    public void deliver(com.sched.v1.ResponseDelivery request,
        io.grpc.stub.StreamObserver<com.sched.v1.ExecuteAck> responseObserver) {
      io.grpc.stub.ClientCalls.asyncUnaryCall(
          getChannel().newCall(getDeliverMethod(), getCallOptions()), request, responseObserver);
    }
  }

  /**
   * A stub to allow clients to do synchronous rpc calls to service Client.
   */
  public static final class ClientBlockingStub
      extends io.grpc.stub.AbstractBlockingStub<ClientBlockingStub> {
    private ClientBlockingStub(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      super(channel, callOptions);
    }

    @java.lang.Override
    protected ClientBlockingStub build(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      return new ClientBlockingStub(channel, callOptions);
    }

    /**
     */
    public com.sched.v1.ExecuteAck deliver(com.sched.v1.ResponseDelivery request) {
      return io.grpc.stub.ClientCalls.blockingUnaryCall(
          getChannel(), getDeliverMethod(), getCallOptions(), request);
    }
  }

  /**
   * A stub to allow clients to do ListenableFuture-style rpc calls to service Client.
   */
  public static final class ClientFutureStub
      extends io.grpc.stub.AbstractFutureStub<ClientFutureStub> {
    private ClientFutureStub(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      super(channel, callOptions);
    }

    @java.lang.Override
    protected ClientFutureStub build(
        io.grpc.Channel channel, io.grpc.CallOptions callOptions) {
      return new ClientFutureStub(channel, callOptions);
    }

    /**
     */
    public com.google.common.util.concurrent.ListenableFuture<com.sched.v1.ExecuteAck> deliver(
        com.sched.v1.ResponseDelivery request) {
      return io.grpc.stub.ClientCalls.futureUnaryCall(
          getChannel().newCall(getDeliverMethod(), getCallOptions()), request);
    }
  }

  private static final int METHODID_DELIVER = 0;

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
        case METHODID_DELIVER:
          serviceImpl.deliver((com.sched.v1.ResponseDelivery) request,
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
          getDeliverMethod(),
          io.grpc.stub.ServerCalls.asyncUnaryCall(
            new MethodHandlers<
              com.sched.v1.ResponseDelivery,
              com.sched.v1.ExecuteAck>(
                service, METHODID_DELIVER)))
        .build();
  }

  private static abstract class ClientBaseDescriptorSupplier
      implements io.grpc.protobuf.ProtoFileDescriptorSupplier, io.grpc.protobuf.ProtoServiceDescriptorSupplier {
    ClientBaseDescriptorSupplier() {}

    @java.lang.Override
    public com.google.protobuf.Descriptors.FileDescriptor getFileDescriptor() {
      return com.sched.v1.SchedulingProto.getDescriptor();
    }

    @java.lang.Override
    public com.google.protobuf.Descriptors.ServiceDescriptor getServiceDescriptor() {
      return getFileDescriptor().findServiceByName("Client");
    }
  }

  private static final class ClientFileDescriptorSupplier
      extends ClientBaseDescriptorSupplier {
    ClientFileDescriptorSupplier() {}
  }

  private static final class ClientMethodDescriptorSupplier
      extends ClientBaseDescriptorSupplier
      implements io.grpc.protobuf.ProtoMethodDescriptorSupplier {
    private final java.lang.String methodName;

    ClientMethodDescriptorSupplier(java.lang.String methodName) {
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
      synchronized (ClientGrpc.class) {
        result = serviceDescriptor;
        if (result == null) {
          serviceDescriptor = result = io.grpc.ServiceDescriptor.newBuilder(SERVICE_NAME)
              .setSchemaDescriptor(new ClientFileDescriptorSupplier())
              .addMethod(getDeliverMethod())
              .build();
        }
      }
    }
    return result;
  }
}
