package impl

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

// TestNightlyReconcilerRequiresTiUPKind pins the worker-role boundary: the
// reconciler may only run in a worker that can execute TiUP steps. Image
// workers share the same config file, and a worker that cannot execute a unit
// must never create it.
func TestNightlyReconcilerRequiresTiUPKind(t *testing.T) {
	nightly := nightlyTestConfig(true, "tiup-staging")
	newService := func(kinds ...string) *publishsrvc {
		return &publishsrvc{
			desired:    &desiredStore{},
			publishCfg: config.Publish{Nightly: nightly, Targets: []string{"tiup-staging"}},
			workerCfg:  config.Worker{Targets: []string{"tiup-staging"}, Kinds: kinds},
		}
	}

	require.False(t, newService(PublishKindImageCopy).nightlyEnabled(),
		"an image worker must not run the nightly reconciler")
	require.True(t, newService(PublishKindTiUP).nightlyEnabled(),
		"a TiUP worker must run the nightly reconciler")
	require.True(t, newService(PublishKindImageCopy, PublishKindTiUP).nightlyEnabled(),
		"a mixed worker that can execute TiUP must run the reconciler")
}
