package command

import (
	"testing"
)

func TestCmd(t *testing.T) {
	tests := map[string]struct {
		input string
		want  string
	}{
		//采用map结构，可以很方便的添加或者删除测试用例
		"simple":       {input: "echo 'hello world'", want: "hello world"},
		"multiCommand": {input: "cd / && pwd", want: "/"},
		"multiLine":    {input: "rm -rf derekDemo && mkdir derekDemo && cd derekDemo && touch a b c && ls -a", want: ".\n..\na\nb\nc"},
	}

	for name, tc := range tests {
		t.Run(name, func(t *testing.T) { //name这里很关键，不然只知道出错，但是不知道具体是上面4个测试用例中哪一个用例出错。
			got, err := Cmd(tc.input)
			if err != nil {
				t.Fatalf("call Cmd function has a panic:%v\n", err.Error())
			}
			if tc.want != got {
				t.Fatalf("expected: [%v], got: [%v]\n", tc.want, got)
			}
		})
	}

}
