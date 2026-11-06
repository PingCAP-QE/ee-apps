package github

import "testing"

func TestGetPRList(t *testing.T) {
	repo := &Repo{Org: "pingcap", Repo: "tidb"}
	tests := map[string]struct {
		repo    *Repo
		pageNum int
		state   string /** open, closed, all **/
		want    int
	}{
		//采用map结构，可以很方便的添加或者删除测试用例
		"regular": {
			repo:    repo,
			pageNum: 1,
			state:   "all",
			want:    30},
		"pr list not exist": {
			repo:    repo,
			state:   "open",
			pageNum: 2000,
			want:    0},
	}

	for name, tc := range tests {
		t.Run(name, func(t *testing.T) { //name这里很关键，不然只知道出错，但是不知道具体是上面4个测试用例中哪一个用例出错。
			got, err := tc.repo.GetPRList(tc.pageNum, tc.state)
			if err != nil {
				t.Fatalf("get commitStatus error :%v\n", err.Error())
			}
			if tc.want != len(got) {
				t.Fatalf("expected: [%v], got: [%v]\n", tc.want, len(got))
			}
		})
	}
}

func TestGetPRListByBase(t *testing.T) {
	repo := &Repo{Org: "pingcap", Repo: "tidb"}
	tests := map[string]struct {
		repo    *Repo
		pageNum int
		state   string /** open, closed, all **/
		base    string /** base branch **/
		want    int
	}{
		//采用map结构，可以很方便的添加或者删除测试用例
		"regular": {
			repo:    repo,
			pageNum: 1,
			state:   "all",
			base:    "master",
			want:    30},
		"pr list not exist": {
			repo:    repo,
			state:   "open",
			base:    "master",
			pageNum: 2000,
			want:    0},
	}

	for name, tc := range tests {
		t.Run(name, func(t *testing.T) { //name这里很关键，不然只知道出错，但是不知道具体是上面4个测试用例中哪一个用例出错。
			got, err := tc.repo.GetPRListByBase(tc.pageNum, tc.state, tc.base)
			if err != nil {
				t.Fatalf("get commitStatus error :%v\n", err.Error())
			}
			if tc.want != len(got) {
				t.Fatalf("expected: [%v], got: [%v]\n", tc.want, len(got))
			}
		})
	}
}
