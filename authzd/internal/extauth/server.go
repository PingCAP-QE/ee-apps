package extauth

import (
	"context"

	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
	typev3 "github.com/envoyproxy/go-control-plane/envoy/type/v3"
	rpcstatus "google.golang.org/genproto/googleapis/rpc/status"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Authorizer evaluates Envoy's request attributes. Implementations must fail
// closed and must not log raw attributes because they contain credentials.
type Authorizer interface {
	Authorize(context.Context, *authv3.AttributeContext) (*authv3.CheckResponse, error)
}

// Server implements Envoy's v3 external authorization gRPC service.
type Server struct {
	authv3.UnimplementedAuthorizationServer
	authorizer Authorizer
}

func NewServer(authorizer Authorizer) *Server {
	return &Server{authorizer: authorizer}
}

func (s *Server) Check(ctx context.Context, req *authv3.CheckRequest) (*authv3.CheckResponse, error) {
	if req == nil || req.GetAttributes() == nil || s.authorizer == nil {
		return deniedResponse(), nil
	}
	response, err := s.authorizer.Authorize(ctx, req.GetAttributes())
	if err != nil {
		if _, ok := status.FromError(err); ok {
			return nil, err
		}
		return nil, status.Error(codes.Unavailable, "authorization evaluation failed")
	}
	if response == nil || response.GetStatus() == nil {
		return nil, status.Error(codes.Internal, "authorization evaluator returned an empty response")
	}
	return response, nil
}

func deniedResponse() *authv3.CheckResponse {
	return &authv3.CheckResponse{
		Status: &rpcstatus.Status{Code: int32(codes.PermissionDenied)},
		HttpResponse: &authv3.CheckResponse_DeniedResponse{DeniedResponse: &authv3.DeniedHttpResponse{
			Status: &typev3.HttpStatus{Code: typev3.StatusCode_Forbidden},
		}},
	}
}
