package tekton

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	larksdk "github.com/larksuite/oapi-sdk-go/v3"
	"github.com/tektoncd/pipeline/pkg/apis/pipeline/v1alpha1"
	"github.com/tektoncd/pipeline/pkg/apis/pipeline/v1beta1"
	tektoncloudevent "github.com/tektoncd/pipeline/pkg/reconciler/events/cloudevent"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"knative.dev/pkg/apis"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/config"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events/handler"
)

const (
	eventContextAnnotationKey          = "tekton.dev/ce-context"
	eventContextAnnotationInnerKeyUser = "user"
)

func NewHandler(cfg config.Tekton, larkClient *larksdk.Client) (handler.EventHandler, error) {
	ret := new(handler.CompositeEventHandler).AddHandlers(
		&pipelineRunHandler{LarkClient: larkClient, Tekton: cfg},
		&taskRunHandler{LarkClient: larkClient, Tekton: cfg},
	)

	return ret, nil
}

func getTriggerUser(run AnnotationsGetter) string {
	eventContext := run.GetAnnotations()[eventContextAnnotationKey]
	if eventContext == "" {
		return ""
	}

	contextData := make(map[string]string)
	if err := json.Unmarshal([]byte(eventContext), &contextData); err != nil {
		return ""
	}

	return contextData[eventContextAnnotationInnerKeyUser]
}

// <dashboard base url>/#/namespaces/<namespace>/<run-type>s/<run-name>
// source: /apis///namespaces/<namespace>//<run-name>
// https://tekton.abc.com/tekton/apis/tekton.dev/v1beta1/namespaces/ee-cd/pipelineruns/auto-compose-multi-arch-image-run-g5hqv
//
//	"source": "/apis///namespaces/ee-cd//build-package-tikv-tikv-linux-9bn55-build-binaries",
func newDetailURL(etype, source, baseURL string) string {
	words := strings.Split(source, "/")
	runName := words[len(words)-1]
	runType := words[len(words)-2]
	runNamespace := words[len(words)-3]

	if runType == "" {
		runType = strings.Split(etype, ".")[3] + "s"
	}

	return fmt.Sprintf("%s/#/namespaces/%s/%s/%s", baseURL, runNamespace, runType, runName)
}

func extractLarkInfosFromEvent(event cloudevents.Event, baseURL string) (*cardMessageInfos, error) {
	var data tektoncloudevent.TektonCloudEventData
	if err := event.DataAs(&data); err != nil {
		return nil, err
	}

	ret := cardMessageInfos{
		Title:         newLarkTitle(event.Type(), event.Subject()),
		TitleTemplate: larkCardHeaderTemplates[tektoncloudevent.TektonEventType(event.Type())],
		ViewURL:       newDetailURL(event.Type(), event.Source(), baseURL),
	}

	switch {
	case data.PipelineRun != nil:
		fillInfosWithPipelineRun(data.PipelineRun, &ret)
		if event.Type() == string(tektoncloudevent.PipelineRunFailedEventV1) {
			ret.FailedTasks = getFailedTasks(data.PipelineRun)
		}
	case data.TaskRun != nil:
		fillInfosWithTaskRun(data.TaskRun, &ret)
		if event.Type() == string(tektoncloudevent.TaskRunFailedEventV1) {
			ret.StepStatuses = getStepStatuses(&data.TaskRun.Status)
		}
	case data.Run != nil:
		fillInfosWithCustomRun(data.Run, &ret)
	}

	return &ret, nil
}

func fillInfosWithCustomRun(data *v1alpha1.Run, ret *cardMessageInfos) {
	fillTimeFileds(ret, data.Status.StartTime, data.Status.CompletionTime)

	for _, p := range data.Spec.Params {
		v, _ := p.Value.MarshalJSON()
		ret.Params = append(ret.Params, [2]string{p.Name, string(v)})
	}
	if results := data.Status.Results; len(results) > 0 {
		var parts []string
		for _, r := range results {
			parts = append(parts, fmt.Sprintf("**%s**:", r.Name), r.Value, "---")
			ret.Results = append(ret.Results, [2]string{r.Name, r.Value})

		}
	}
}

func fillInfosWithTaskRun(data *v1beta1.TaskRun, ret *cardMessageInfos) {
	fillTimeFileds(ret, data.Status.StartTime, data.Status.CompletionTime)

	for _, p := range data.Spec.Params {
		v, _ := p.Value.MarshalJSON()
		ret.Params = append(ret.Params, [2]string{p.Name, string(v)})
	}
	if data.Status.GetCondition(apis.ConditionSucceeded).IsFalse() {
		ret.RerunURL = fmt.Sprintf("tkn -n %s task start %s --use-taskrun %s",
			data.Namespace, data.Spec.TaskRef.Name, data.Name)
	}
	if results := data.Status.TaskRunResults; len(results) > 0 {
		for _, r := range results {
			v, _ := r.Value.MarshalJSON()
			ret.Results = append(ret.Results, [2]string{r.Name, string(v)})
		}
	}

	getStepStatuses(&data.Status)
}

func fillInfosWithPipelineRun(data *v1beta1.PipelineRun, ret *cardMessageInfos) {
	fillTimeFileds(ret, data.Status.StartTime, data.Status.CompletionTime)

	for _, p := range data.Spec.Params {
		v, _ := p.Value.MarshalJSON()
		ret.Params = append(ret.Params, [2]string{p.Name, string(v)})
	}
	if data.Status.GetCondition(apis.ConditionSucceeded).IsFalse() {
		ret.RerunURL = fmt.Sprintf("tkn -n %s pipeline start %s --use-pipelinerun %s",
			data.Namespace, data.Spec.PipelineRef.Name, data.Name)
	}
	if results := data.Status.PipelineResults; len(results) > 0 {
		for _, r := range results {
			ret.Results = append(ret.Results, [2]string{r.Name, r.Value.StringVal})
		}
	}

}

func getFailedTasks(data *v1beta1.PipelineRun) map[string][][2]string {
	ret := make(map[string][][2]string)
	for _, v := range data.Status.TaskRuns {
		succeededCondition := v.Status.GetCondition(apis.ConditionSucceeded)
		if !succeededCondition.IsTrue() {
			ret[v.PipelineTaskName] = getStepStatuses(v.Status)
		}
	}

	return ret
}

func getStepStatuses(status *v1beta1.TaskRunStatus) [][2]string {
	var ret [][2]string
	for _, s := range status.Steps {
		if s.Terminated != nil {
			ret = append(ret, [2]string{s.Name, s.Terminated.Reason})
		}
	}

	return ret
}

func fillTimeFileds(ret *cardMessageInfos, startTime, endTime *metav1.Time) {
	if startTime != nil {
		ret.StartTime = startTime.Format(time.RFC3339)
	}
	if endTime != nil {
		ret.EndTime = endTime.Format(time.RFC3339)
	}
	if startTime != nil && endTime != nil {
		ret.TimeCost = time.Duration(endTime.UnixNano() - startTime.UnixNano()).String()
	}
}
