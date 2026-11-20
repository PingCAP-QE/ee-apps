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
			name: "failed task renders its step name and reason",
			infos: &cardMessageInfos{
				Title: "title",
				FailedTasks: map[string][]v1beta1.StepState{
					"hello": {
						{
							Name: "Step 1",
							ContainerState: corev1.ContainerState{
								Terminated: &corev1.ContainerStateTerminated{Reason: "hello"},
							},
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
