package repo

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"testing"
	"time"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/service"
)

type httpStub struct {
	req   []*http.Request
	resp  []*http.Response
	err   []error
	index int
}

func (h *httpStub) Do(req *http.Request) (*http.Response, error) {
	now := h.index
	h.index++
	h.req = append(h.req, req)
	return h.resp[now], h.err[now]
}

func TestGithubHeadCreator_CreateBranchFromTag(t *testing.T) {
	shaResp := `{"object":{"sha":"123456789abcde"}}`
	stub := &httpStub{
		resp: []*http.Response{
			{StatusCode: 200, Body: io.NopCloser(bytes.NewBufferString(shaResp))},
			{StatusCode: 200, Body: io.NopCloser(bytes.NewBuffer(nil))},
		},
		err: []error{nil, nil},
	}
	creator := githubHeadCreator{httpDoer: stub, Token: "the-token"}
	s := service.NewRepoServiceForTest(creator, func() time.Time {
		return time.Date(2022, 9, 11, 2, 0, 0, 0, time.UTC)
	})
	branch, err := s.CreateBranch(context.TODO(), service.BranchCreateReq{Prod: service.ProductBr, BaseVersion: "v6.1.1"})
	require.NoError(t, err)
	require.Equal(t,
		service.BranchCreateResp{
			Branch:    "release-6.1-20220911-v6.1.1",
			BranchURL: "https://github.com/pingcap/tidb/tree/release-6.1-20220911-v6.1.1"},
		*branch)
	require.Equal(t, "https://api.github.com/repos/pingcap/tidb/git/refs/tags/v6.1.1", stub.req[0].URL.String())
	require.Equal(t, "token the-token", stub.req[1].Header.Get(HeaderAuthorization))
	require.Equal(t, "https://api.github.com/repos/pingcap/tidb/git/refs", stub.req[1].URL.String())
	postBody, err := io.ReadAll(stub.req[1].Body)
	require.NoError(t, err)
	require.JSONEq(t, `{"ref":"refs/heads/release-6.1-20220911-v6.1.1","sha":"123456789abcde"}`, string(postBody))
}
