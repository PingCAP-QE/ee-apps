package tidbcloud

import (
	"testing"

	"github.com/google/go-containerregistry/pkg/crane"
)

func TestGetCraneOptions(t *testing.T) {
	tests := []struct {
		name     string
		tpsCfg   *TestPlatformsConfig
		wantNil  bool
		wantAuth bool
	}{
		{
			name:    "nil config",
			tpsCfg:  nil,
			wantNil: true,
		},
		{
			name: "use default keychain",
			tpsCfg: &TestPlatformsConfig{
				ImageAuth: ImageAuthConfig{
					UseDefaultKeychain: true,
				},
			},
			wantNil: true,
		},
		{
			name: "username and password provided",
			tpsCfg: &TestPlatformsConfig{
				ImageAuth: ImageAuthConfig{
					Username: "testuser",
					Password: "testpass",
				},
			},
			wantNil:  false,
			wantAuth: true,
		},
		{
			name: "no credentials",
			tpsCfg: &TestPlatformsConfig{
				ImageAuth: ImageAuthConfig{},
			},
			wantNil: true,
		},
		{
			name: "only username",
			tpsCfg: &TestPlatformsConfig{
				ImageAuth: ImageAuthConfig{
					Username: "testuser",
				},
			},
			wantNil: true,
		},
		{
			name: "only password",
			tpsCfg: &TestPlatformsConfig{
				ImageAuth: ImageAuthConfig{
					Password: "testpass",
				},
			},
			wantNil: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			srvc := &tidbcloudsrvc{
				tpsCfg: tt.tpsCfg,
			}

			opts := srvc.getCraneOptions()

			if tt.wantNil {
				if opts != nil {
					t.Errorf("getCraneOptions() = %v, want nil", opts)
				}
			} else {
				if opts == nil {
					t.Errorf("getCraneOptions() = nil, want non-nil")
				}
				if tt.wantAuth && len(opts) != 1 {
					t.Errorf("getCraneOptions() returned %d options, want 1", len(opts))
				}
			}
		})
	}
}

func TestGetCraneOptionsWithAuth(t *testing.T) {
	srvc := &tidbcloudsrvc{
		tpsCfg: &TestPlatformsConfig{
			ImageAuth: ImageAuthConfig{
				Username: "testuser",
				Password: "testpass",
			},
		},
	}

	opts := srvc.getCraneOptions()

	if opts == nil {
		t.Fatal("getCraneOptions() returned nil")
	}

	if len(opts) != 1 {
		t.Fatalf("getCraneOptions() returned %d options, want 1", len(opts))
	}

	// Verify that the option can be applied without errors
	_ = crane.GetOptions(opts...)
}
