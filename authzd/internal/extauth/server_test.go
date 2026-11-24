package extauth

import (
	"context"
	"errors"
	"net"
	"testing"
	"time"

	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
	rpcstatus "google.golang.org/genproto/googleapis/rpc/status"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
)

type authorizerFunc func(context.Context, *authv3.AttributeContext) (*authv3.CheckResponse, error)

func (f authorizerFunc) Authorize(ctx context.Context, attributes *authv3.AttributeContext) (*authv3.CheckResponse, error) {
	return f(ctx, attributes)
}

func TestCheckReturnsAllowAndDenyResponses(t *testing.T) {
	for _, tc := range []struct {
		name    string
		allowed bool
	}{
		{name: "allow", allowed: true},
		{name: "deny", allowed: false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			want := &authv3.CheckResponse{
				Status:       &rpcstatus.Status{Code: int32(codes.OK)},
				HttpResponse: &authv3.CheckResponse_OkResponse{OkResponse: &authv3.OkHttpResponse{}},
			}
			if !tc.allowed {
				want = deniedResponse()
			}
			server := NewServer(authorizerFunc(func(context.Context, *authv3.AttributeContext) (*authv3.CheckResponse, error) {
				return want, nil
			}))
			got, err := server.Check(context.Background(), &authv3.CheckRequest{Attributes: &authv3.AttributeContext{}})
			if err != nil {
				t.Fatalf("Check() error = %v", err)
			}
			if got.GetStatus().GetCode() != want.GetStatus().GetCode() {
				t.Fatalf("status code = %v, want %v", got.GetStatus().GetCode(), want.GetStatus().GetCode())
			}
			if tc.allowed && got.GetOkResponse() == nil {
				t.Fatal("allow response has no ok_response")
			}
			if !tc.allowed && (got.GetDeniedResponse() == nil || got.GetDeniedResponse().GetStatus().GetCode() != 403) {
				t.Fatal("deny response must contain HTTP 403")
			}
		})
	}
}

func TestCheckPropagatesRequestDeadline(t *testing.T) {
	server := NewServer(authorizerFunc(func(ctx context.Context, _ *authv3.AttributeContext) (*authv3.CheckResponse, error) {
		<-ctx.Done()
		return nil, status.FromContextError(ctx.Err()).Err()
	}))
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()
	_, err := server.Check(ctx, &authv3.CheckRequest{Attributes: &authv3.AttributeContext{}})
	if status.Code(err) != codes.DeadlineExceeded {
		t.Fatalf("Check() error code = %v, want %v", status.Code(err), codes.DeadlineExceeded)
	}
}

func TestCheckFailsClosedWhenAuthorizerErrors(t *testing.T) {
	want := status.Error(codes.Unavailable, "authorization dependency unavailable")
	server := NewServer(authorizerFunc(func(context.Context, *authv3.AttributeContext) (*authv3.CheckResponse, error) {
		return nil, want
	}))
	_, err := server.Check(context.Background(), &authv3.CheckRequest{Attributes: &authv3.AttributeContext{}})
	if !errors.Is(err, want) && status.Code(err) != codes.Unavailable {
		t.Fatalf("Check() error = %v, want unavailable", err)
	}
}

func TestCheckRejectsMissingAttributes(t *testing.T) {
	server := NewServer(authorizerFunc(func(context.Context, *authv3.AttributeContext) (*authv3.CheckResponse, error) {
		t.Fatal("authorizer called for request without attributes")
		return nil, nil
	}))
	got, err := server.Check(context.Background(), &authv3.CheckRequest{})
	if err != nil {
		t.Fatalf("Check() error = %v", err)
	}
	if got.GetDeniedResponse() == nil {
		t.Fatal("missing attributes must be denied")
	}
}

func TestGRPCCheckServesEnvoyAuthorizationProtocol(t *testing.T) {
	listener := bufconn.Listen(1024 * 1024)
	grpcServer := grpc.NewServer()
	authv3.RegisterAuthorizationServer(grpcServer, NewServer(authorizerFunc(func(context.Context, *authv3.AttributeContext) (*authv3.CheckResponse, error) {
		return &authv3.CheckResponse{
			Status:       &rpcstatus.Status{Code: int32(codes.OK)},
			HttpResponse: &authv3.CheckResponse_OkResponse{OkResponse: &authv3.OkHttpResponse{}},
		}, nil
	})))
	go func() { _ = grpcServer.Serve(listener) }()
	t.Cleanup(func() {
		grpcServer.Stop()
		_ = listener.Close()
	})
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	conn, err := grpc.DialContext(ctx, "passthrough:///authzd-test",
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		t.Fatalf("dial test gRPC server: %v", err)
	}
	defer conn.Close()
	response, err := authv3.NewAuthorizationClient(conn).Check(ctx, &authv3.CheckRequest{
		Attributes: &authv3.AttributeContext{ContextExtensions: map[string]string{"route_id": "r"}},
	})
	if err != nil || response.GetOkResponse() == nil {
		t.Fatalf("Envoy Check RPC = (%v, %v), want allow response", response, err)
	}
}
