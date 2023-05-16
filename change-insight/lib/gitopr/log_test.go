package gitopr

import "testing"

func TestCommitInfomationByDate(t *testing.T) {
	tests := map[string]struct {
		since    string
		until    string
		fileName string
		want     int
	}{
		//采用map结构，可以很方便的添加或者删除测试用例
		"simple": {
			since:    "2022.06.12",
			until:    "2022.06.16",
			fileName: "./sessionctx/variable/",
			want:     1},
		"multiCommit": {
			since:    "2022.06.12",
			until:    "2022.06.20",
			fileName: "./sessionctx/variable/",
			want:     3},
	}
	gitObject := &GitObject{
		WS:     "~",
		Org:    "pingcap",
		Repo:   "tidb",
		Branch: "master"}

	for name, tc := range tests {
		t.Run(name, func(t *testing.T) { //name这里很关键，不然只知道出错，但是不知道具体是上面4个测试用例中哪一个用例出错。
			got, err := gitObject.CommitInfomationByDate(tc.since, tc.until, tc.fileName)
			if err != nil {
				t.Fatalf("call Cmd function has a panic:%v\n", err.Error())
			}
			if tc.want != len(got) {
				t.Fatalf("expected: [%v], got: [%v]\n", tc.want, got)
			}
		})
	}

}

func TestCommitInfomationByBranch(t *testing.T) {
	tests := map[string]struct {
		featureBranchA string
		featureBranchB string
		fileName       string
		want           int
	}{
		//采用map结构，可以很方便的添加或者删除测试用例
		"simple": {
			featureBranchA: "release-6.0",
			featureBranchB: "release-6.1",
			fileName:       "./sessionctx/variable/",
			want:           70},
	}
	gitObject := &GitObject{
		WS:     "~",
		Org:    "pingcap",
		Repo:   "tidb",
		Branch: "master"}

	for name, tc := range tests {
		t.Run(name, func(t *testing.T) { //name这里很关键，不然只知道出错，但是不知道具体是上面4个测试用例中哪一个用例出错。
			got, err := gitObject.CommitInfomationByBranch(tc.featureBranchA, tc.featureBranchB, tc.fileName)
			if err != nil {
				t.Fatalf("call Cmd function has a panic:%v\n", err.Error())
			}
			if tc.want != len(got) {
				t.Fatalf("expected: [%v], got: [%v]\n", tc.want, len(got))
			}
		})
	}

}
