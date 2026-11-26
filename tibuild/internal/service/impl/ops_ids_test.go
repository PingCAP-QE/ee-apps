package impl

import (
	"encoding/json"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/hotfix"
)

const (
	testStepKey     = "e38e599a-de0c-4f48-be90-80f9327cb40e"
	testExecutionID = "5b022051-d3ab-4644-ba9f-b0001bbce33c"
)

func TestValidateBumpTagOpsIDs(t *testing.T) {
	cases := []struct {
		name    string
		meta    string
		wantErr string
	}{
		{name: "no meta", meta: `{}`},
		{name: "no ops_req", meta: `{"ops_req":{}}`},
		{
			name: "release and change only",
			meta: `{"ops_req":{"applicant":"a","release_id":"30082","change_id":"62440"}}`,
		},
		{
			name: "both ids",
			meta: `{"ops_req":{"release_id":"30082","change_id":"62440","step_key":"` + testStepKey + `","execution_id":"` + testExecutionID + `"}}`,
		},
		{
			// The fields are independent: a lone id is accepted, not judged
			// against the others.
			name: "step key alone",
			meta: `{"ops_req":{"step_key":"` + testStepKey + `"}}`,
		},
		{
			name: "execution id alone",
			meta: `{"ops_req":{"execution_id":"` + testExecutionID + `"}}`,
		},
		{
			name:    "step key is not a uuid",
			meta:    `{"ops_req":{"step_key":"nope"}}`,
			wantErr: `step_key "nope" is not a UUID`,
		},
		{
			name:    "execution id is not a uuid",
			meta:    `{"ops_req":{"execution_id":"5b022051"}}`,
			wantErr: `execution_id "5b022051" is not a UUID`,
		},
		{
			name:    "execution id has an urn prefix",
			meta:    `{"ops_req":{"execution_id":"urn:uuid:` + testExecutionID + `"}}`,
			wantErr: "is not a UUID",
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			meta := &hotfix.TiDBxBumpTagMeta{}
			require.NoError(t, json.Unmarshal([]byte(c.meta), meta))

			err := validateBumpTagOpsIDs(meta)
			if c.wantErr == "" {
				require.NoError(t, err)
				return
			}
			require.ErrorContains(t, err, c.wantErr)
		})
	}
}

func TestValidateBumpTagOpsIDsNilMeta(t *testing.T) {
	require.NoError(t, validateBumpTagOpsIDs(nil))
}

func TestNormalizeBumpTagOpsIDs(t *testing.T) {
	meta := &hotfix.TiDBxBumpTagMeta{}
	require.NoError(t, json.Unmarshal([]byte(`{"ops_req":{
		"applicant": "  ",
		"release_id": "30082",
		"change_id": "",
		"step_key": "",
		"execution_id": "5b022051-d3ab-4644-ba9f-b0001bbce33c"
	}}`), meta))

	normalizeBumpTagOpsIDs(meta)

	require.Nil(t, meta.OpsReq.Applicant, "whitespace-only value must be dropped")
	require.Nil(t, meta.OpsReq.ChangeID)
	require.Nil(t, meta.OpsReq.StepKey)
	require.NotNil(t, meta.OpsReq.ReleaseID)
	require.Equal(t, "30082", *meta.OpsReq.ReleaseID)
	require.Equal(t, testExecutionID, *meta.OpsReq.ExecutionID)
}

func TestNormalizeBumpTagOpsIDsKeepsNilSafe(t *testing.T) {
	normalizeBumpTagOpsIDs(nil)
	normalizeBumpTagOpsIDs(&hotfix.TiDBxBumpTagMeta{})
}

// TestNormalizeBumpTagOpsIDsRunsBeforeValidation pins the ordering: a
// whitespace-only id is dropped instead of being reported as a malformed UUID.
func TestNormalizeBumpTagOpsIDsRunsBeforeValidation(t *testing.T) {
	meta := &hotfix.TiDBxBumpTagMeta{}
	require.NoError(t, json.Unmarshal([]byte(`{"ops_req":{"step_key":"   "}}`), meta))

	normalizeBumpTagOpsIDs(meta)
	require.NoError(t, validateBumpTagOpsIDs(meta))
}
