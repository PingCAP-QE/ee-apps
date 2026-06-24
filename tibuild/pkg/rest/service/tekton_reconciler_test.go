package service

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	tekton "github.com/tektoncd/pipeline/pkg/apis/pipeline/v1beta1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	kubernetesfake "k8s.io/client-go/kubernetes/fake"
	"knative.dev/pkg/apis"
	duckv1 "knative.dev/pkg/apis/duck/v1"
)

type mockRepoForReconciler struct {
	builds  []DevBuild
	updated []DevBuild
}

func (m *mockRepoForReconciler) Create(ctx context.Context, req DevBuild) (resp *DevBuild, err error) {
	req.ID = len(m.builds) + 1
	m.builds = append(m.builds, req)
	return &req, nil
}

func (m *mockRepoForReconciler) Get(ctx context.Context, id int) (resp *DevBuild, err error) {
	for _, b := range m.builds {
		if b.ID == id {
			return &b, nil
		}
	}
	return nil, nil
}

func (m *mockRepoForReconciler) Update(ctx context.Context, id int, req DevBuild) (resp *DevBuild, err error) {
	for i, b := range m.builds {
		if b.ID == id {
			m.builds[i] = req
			m.updated = append(m.updated, req)
			return &req, nil
		}
	}
	return nil, nil
}

func (m *mockRepoForReconciler) List(ctx context.Context, option DevBuildListOption) (resp []DevBuild, err error) {
	return m.builds, nil
}

func TestTektonReconciler_Disabled(t *testing.T) {
	repo := &mockRepoForReconciler{}
	config := TektonReconcilerConfig{
		Enabled: false,
	}

	reconciler := NewTektonReconcilerWithClients(repo, config, nil, nil)
	assert.NotNil(t, reconciler)

	// Should not panic when starting disabled reconciler
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	reconciler.Start(ctx)
}

func TestTektonReconciler_SkipsNonTektonBuilds(t *testing.T) {
	repo := &mockRepoForReconciler{
		builds: []DevBuild{
			{
				ID: 1,
				Spec: DevBuildSpec{
					PipelineEngine: JenkinsEngine,
				},
				Status: DevBuildStatus{
					Status: BuildStatusProcessing,
				},
			},
		},
	}

	config := TektonReconcilerConfig{
		Enabled:        true,
		Namespace:      "ee-cd",
		Interval:       1 * time.Second,
		StaleThreshold: 0, // No threshold for test
	}

	reconciler := NewTektonReconcilerWithClients(repo, config, kubernetesfake.NewSimpleClientset(), nil)
	reconciler.now = func() time.Time { return time.Now().Add(10 * time.Minute) }

	// Run reconcile
	reconciler.reconcile(context.Background())

	// Should not have updated anything
	assert.Empty(t, repo.updated)
}

func TestTektonReconciler_SkipsNonProcessingBuilds(t *testing.T) {
	repo := &mockRepoForReconciler{
		builds: []DevBuild{
			{
				ID: 1,
				Spec: DevBuildSpec{
					PipelineEngine: TektonEngine,
				},
				Status: DevBuildStatus{
					Status: BuildStatusSuccess,
					TektonStatus: &TektonStatus{
						EventID: "test-event-id",
					},
				},
			},
		},
	}

	config := TektonReconcilerConfig{
		Enabled:        true,
		Namespace:      "ee-cd",
		Interval:       1 * time.Second,
		StaleThreshold: 0,
	}

	reconciler := NewTektonReconcilerWithClients(repo, config, kubernetesfake.NewSimpleClientset(), nil)
	reconciler.now = func() time.Time { return time.Now().Add(10 * time.Minute) }

	reconciler.reconcile(context.Background())

	assert.Empty(t, repo.updated)
}

func TestTektonReconciler_SkipsBuildsWithoutEventID(t *testing.T) {
	repo := &mockRepoForReconciler{
		builds: []DevBuild{
			{
				ID: 1,
				Spec: DevBuildSpec{
					PipelineEngine: TektonEngine,
				},
				Status: DevBuildStatus{
					Status:       BuildStatusProcessing,
					TektonStatus: &TektonStatus{},
				},
			},
		},
	}

	config := TektonReconcilerConfig{
		Enabled:        true,
		Namespace:      "ee-cd",
		Interval:       1 * time.Second,
		StaleThreshold: 0,
	}

	reconciler := NewTektonReconcilerWithClients(repo, config, kubernetesfake.NewSimpleClientset(), nil)
	reconciler.now = func() time.Time { return time.Now().Add(10 * time.Minute) }

	reconciler.reconcile(context.Background())

	assert.Empty(t, repo.updated)
}

func TestTektonReconciler_SkipsFreshBuilds(t *testing.T) {
	now := time.Now()
	repo := &mockRepoForReconciler{
		builds: []DevBuild{
			{
				ID: 1,
				Spec: DevBuildSpec{
					PipelineEngine: TektonEngine,
				},
				Meta: DevBuildMeta{
					UpdatedAt: now,
				},
				Status: DevBuildStatus{
					Status: BuildStatusProcessing,
					TektonStatus: &TektonStatus{
						EventID: "test-event-id",
					},
				},
			},
		},
	}

	config := TektonReconcilerConfig{
		Enabled:        true,
		Namespace:      "ee-cd",
		Interval:       1 * time.Second,
		StaleThreshold: 5 * time.Minute,
	}

	reconciler := NewTektonReconcilerWithClients(repo, config, kubernetesfake.NewSimpleClientset(), nil)
	reconciler.now = func() time.Time { return now.Add(1 * time.Minute) } // Only 1 minute old

	reconciler.reconcile(context.Background())

	assert.Empty(t, repo.updated)
}

func TestTektonReconciler_UpdatesCompletedPipelineRun(t *testing.T) {
	now := time.Now()
	repo := &mockRepoForReconciler{
		builds: []DevBuild{
			{
				ID: 1,
				Spec: DevBuildSpec{
					PipelineEngine: TektonEngine,
				},
				Meta: DevBuildMeta{
					UpdatedAt: now,
				},
				Status: DevBuildStatus{
					Status: BuildStatusProcessing,
					TektonStatus: &TektonStatus{
						EventID: "test-event-id",
					},
				},
			},
		},
	}

	config := TektonReconcilerConfig{
		Enabled:        true,
		Namespace:      "ee-cd",
		Interval:       1 * time.Second,
		StaleThreshold: 5 * time.Minute,
	}

	// Create a fake PipelineRun
	startTime := metav1.NewTime(now.Add(-10 * time.Minute))
	completionTime := metav1.NewTime(now.Add(-2 * time.Minute))
	pipelineRun := &tekton.PipelineRun{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-pipeline-run",
			Namespace: "ee-cd",
			Labels: map[string]string{
				eventIDLabel: "test-event-id",
			},
		},
		Spec: tekton.PipelineRunSpec{
			Params: []tekton.Param{
				{Name: "os", Value: tekton.ArrayOrString{StringVal: "linux"}},
				{Name: "arch", Value: tekton.ArrayOrString{StringVal: "amd64"}},
				{Name: "git-revision", Value: tekton.ArrayOrString{StringVal: "abc123def456abc123def456abc123def456abc1"}},
			},
		},
		Status: tekton.PipelineRunStatus{
			Status: duckv1.Status{
				Conditions: duckv1.Conditions{
					{
						Type:   apis.ConditionSucceeded,
						Status: "True",
					},
				},
			},
			PipelineRunStatusFields: tekton.PipelineRunStatusFields{
				StartTime:      &startTime,
				CompletionTime: &completionTime,
			},
		},
	}

	prLister := &fakePipelineRunLister{runs: []*tekton.PipelineRun{pipelineRun}}

	reconciler := NewTektonReconcilerWithClients(repo, config, kubernetesfake.NewSimpleClientset(), prLister)
	reconciler.now = func() time.Time { return now.Add(10 * time.Minute) }

	reconciler.reconcile(context.Background())

	// Should have updated the build
	require.Len(t, repo.updated, 1)
	updated := repo.updated[0]
	assert.Equal(t, BuildStatusSuccess, updated.Status.TektonStatus.Pipelines[0].Status)
	assert.Equal(t, "test-pipeline-run", updated.Status.TektonStatus.Pipelines[0].Name)
	assert.Equal(t, "linux/amd64", updated.Status.TektonStatus.Pipelines[0].Platform)
}

func TestTektonReconciler_NoPipelineRunsFound(t *testing.T) {
	now := time.Now()
	repo := &mockRepoForReconciler{
		builds: []DevBuild{
			{
				ID: 1,
				Spec: DevBuildSpec{
					PipelineEngine: TektonEngine,
				},
				Meta: DevBuildMeta{
					UpdatedAt: now,
				},
				Status: DevBuildStatus{
					Status: BuildStatusProcessing,
					TektonStatus: &TektonStatus{
						EventID: "test-event-id",
					},
				},
			},
		},
	}

	config := TektonReconcilerConfig{
		Enabled:        true,
		Namespace:      "ee-cd",
		Interval:       1 * time.Second,
		StaleThreshold: 5 * time.Minute,
	}

	// No PipelineRuns in the fake client
	prLister := &fakePipelineRunLister{runs: []*tekton.PipelineRun{}}

	reconciler := NewTektonReconcilerWithClients(repo, config, kubernetesfake.NewSimpleClientset(), prLister)
	reconciler.now = func() time.Time { return now.Add(10 * time.Minute) }

	reconciler.reconcile(context.Background())

	// Should not have updated anything
	assert.Empty(t, repo.updated)
}

// fakePipelineRunLister implements PipelineRunLister for testing.
type fakePipelineRunLister struct {
	runs []*tekton.PipelineRun
}

func (f *fakePipelineRunLister) List(ctx context.Context, opts metav1.ListOptions) (*tekton.PipelineRunList, error) {
	list := &tekton.PipelineRunList{}
	for _, run := range f.runs {
		list.Items = append(list.Items, *run)
	}
	return list, nil
}
