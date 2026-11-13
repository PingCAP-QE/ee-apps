package controller

import "testing"

func TestShellQuote(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name string
		in   string
		want string
	}{
		{name: "plain path", in: "/tmp/build/tidb/build", want: `'/tmp/build/tidb/build'`},
		{name: "space", in: "/tmp/a b", want: `'/tmp/a b'`},
		{name: "semicolon stays inside the quotes", in: "/tmp/x;touch /tmp/pwned", want: `'/tmp/x;touch /tmp/pwned'`},
		{name: "command substitution stays inside the quotes", in: "$(id)", want: `'$(id)'`},
		{name: "single quote is escaped", in: "/tmp/it's", want: `'/tmp/it'\''s'`},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if got := shellQuote(tc.in); got != tc.want {
				t.Fatalf("shellQuote(%q) = %s, want %s", tc.in, got, tc.want)
			}
		})
	}
}
