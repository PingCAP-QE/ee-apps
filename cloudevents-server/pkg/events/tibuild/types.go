package tibuild

const (
	EventTypeDevbuildFakeGithubPush        = "net.pingcap.tibuild.devbuild.push"         // Fake GitHub push events in the development build.
	EventTypeDevbuildFakeGithubCreate      = "net.pingcap.tibuild.devbuild.create"       // Fake GitHub create events in the development build.
	EventTypeDevbuildFakeGithubPullRequest = "net.pingcap.tibuild.devbuild.pull_request" // Fake GitHub pull request events in the development build.
	EventTypeHotfixFakeGithubPush          = "net.pingcap.tibuild.hotfix.push"           // Fake GitHub push events in the hotfix build.
	EventTypeHotfixFakeGithubCreate        = "net.pingcap.tibuild.hotfix.create"         // Fake GitHub create events in the hotfix build.
	EventTypeHotfixFakeGithubPullRequest   = "net.pingcap.tibuild.hotfix.pull_request"   // Fake GitHub pull request events in the hotfix build.
)
