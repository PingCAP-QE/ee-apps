package github

import "testing"

func TestGetCommitStatus(t *testing.T) {
	repo := &Repo{Org: "pingcap", Repo: "tidb"}
	tests := map[string]struct {
		repo     *Repo
		commitId string
		want     int
	}{
		//采用map结构，可以很方便的添加或者删除测试用例
		"regular": {
			repo:     repo,
			commitId: "a11996c8434d53a91ba6c7038a2e43c009433ec1",
			want:     9},
		"commit not exist": {
			repo:     repo,
			commitId: "--a11996c8434d53a91ba6c7038a2e43c009433ec1",
			want:     0},
	}

	for name, tc := range tests {
		t.Run(name, func(t *testing.T) { //name这里很关键，不然只知道出错，但是不知道具体是上面4个测试用例中哪一个用例出错。
			got, err := tc.repo.GetCommitStatus(tc.commitId)
			if err != nil {
				t.Fatalf("get commitStatus error :%v\n", err.Error())
			}
			if tc.want != len(got.Statuses) {
				t.Fatalf("expected: [%v], got: [%v]\n", tc.want, len(got.Statuses))
			}
		})
	}
}
