package tekton

import (
	"testing"

	"github.com/tektoncd/pipeline/pkg/apis/pipeline/v1beta1"
	corev1 "k8s.io/api/core/v1"
)

func Test_newLarkCardWithGoTemplate(t *testing.T) {
	tests := []struct {
		name    string
		infos   *cardMessageInfos
		wantErr bool
	}{
		{
			name: "step log contains control characters",
			infos: &cardMessageInfos{
				Title: "title",
				FailedTasks: map[string][]stepInfo{
					"hello": {
						{
							StepState: v1beta1.StepState{
								Name: "Step 1",
								ContainerState: corev1.ContainerState{
									Terminated: &corev1.ContainerStateTerminated{Reason: "hello"},
								},
							},
							Logs: "\x1b[31mError\x1b[0m occurred\nDetails: \x07\x08",
						},
					},
				},
			},
			wantErr: false,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := newLarkCardWithGoTemplate(tt.infos)
			if (err != nil) != tt.wantErr {
				t.Logf("got:\n%s", got)
				t.Errorf("newLarkCardWithGoTemplate() error = %v, wantErr %v", err, tt.wantErr)
				return
			}
		})
	}
}
