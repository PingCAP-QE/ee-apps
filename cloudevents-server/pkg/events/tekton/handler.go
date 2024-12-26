package tekton

import (
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	larksdk "github.com/larksuite/oapi-sdk-go/v3"
	"github.com/rs/zerolog/log"
	"github.com/tektoncd/pipeline/pkg/apis/pipeline/v1alpha1"
	"github.com/tektoncd/pipeline/pkg/apis/pipeline/v1beta1"
	tektoncloudevent "github.com/tektoncd/pipeline/pkg/reconciler/events/cloudevent"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/sets"
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

func getReceivers(event cloudevents.Event, cfgs []config.TektonNotification) []string {
	eventType := event.Type()
	eventSub := event.Subject()

	ret := sets.NewString()
	for _, cfg := range cfgs {
		if cfg.IsMatched(eventType, eventSub) {
			ret.Insert(cfg.Receivers...)
		}
	}

	return ret.List()
}

// <dashboard base url>/#/namespaces/<namespace>/<run-type>s/<run-name>
// source: /apis/namespaces/<namespace>/<run-name>
// https://tekton.abc.com/tekton/apis/tekton.dev/v1beta1/namespaces/ee-cd/pipelineruns/auto-compose-multi-arch-image-run-g5hqv
//
//	"source": "/apis/namespaces/ee-cd/build-package-tikv-tikv-linux-9bn55-build-binaries",
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

func extractLarkInfosFromEvent(event cloudevents.Event, baseURL string, tailLogLines int) (*cardMessageInfos, error) {
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
		if !fillInfosWithPipelineRun(data.PipelineRun, &ret) {
			return nil, nil
		}

		if event.Type() == string(tektoncloudevent.PipelineRunFailedEventV1) {
			namespace := data.PipelineRun.Namespace
			ret.FailedTasks = getFailedTasks(data.PipelineRun, func(podName, containerName string) string {
				logs, _ := getStepLog(baseURL, namespace, podName, containerName, tailLogLines)
				return logs
			})
		}
	case data.TaskRun != nil:
		if !fillInfosWithTaskRun(data.TaskRun, &ret) {
			return nil, nil
		}

		if event.Type() == string(tektoncloudevent.TaskRunFailedEventV1) {
			namespace := data.TaskRun.Namespace
			ret.StepStatuses = getStepStatuses(&data.TaskRun.Status, func(podName, containerName string) string {
				logs, _ := getStepLog(baseURL, namespace, podName, containerName, tailLogLines)
				log.Debug().Msg(logs)
				return logs
			})
		}
	case data.Run != nil:
		if !fillInfosWithCustomRun(data.Run, &ret) {
			return nil, nil
		}
	}

	return &ret, nil
}

func fillInfosWithCustomRun(data *v1alpha1.Run, ret *cardMessageInfos) bool {
	fillTimeFileds(ret, data.Status.StartTime, data.Status.CompletionTime)

	for _, p := range data.Spec.Params {
		v, _ := p.Value.MarshalJSON()
		ret.Params = append(ret.Params, [2]string{p.Name, string(v)})
	}
	if results := data.Status.Results; len(results) > 0 {
		for _, r := range results {
			ret.Results = append(ret.Results, [2]string{r.Name, r.Value})
		}
	}

	return true
}

func fillInfosWithTaskRun(data *v1beta1.TaskRun, ret *cardMessageInfos) bool {
	fillTimeFileds(ret, data.Status.StartTime, data.Status.CompletionTime)

	for _, p := range data.Spec.Params {
		v, _ := p.Value.MarshalJSON()
		ret.Params = append(ret.Params, [2]string{p.Name, string(v)})
	}
	succeededCondition := data.Status.GetCondition(apis.ConditionSucceeded)
	if succeededCondition.IsFalse() {
		if succeededCondition.GetReason() == "TaskRunCancelled" {
			return false
		}

		ret.FailedMessage = succeededCondition.Message
		ret.RerunURL = fmt.Sprintf("tkn -n %s task start %s --use-taskrun %s",
			data.Namespace, data.Spec.TaskRef.Name, data.Name)
	}
	if results := data.Status.TaskRunResults; len(results) > 0 {
		for _, r := range results {
			v, _ := r.Value.MarshalJSON()
			ret.Results = append(ret.Results, [2]string{r.Name, string(v)})
		}
	}

	return true
}

func fillInfosWithPipelineRun(data *v1beta1.PipelineRun, ret *cardMessageInfos) bool {
	fillTimeFileds(ret, data.Status.StartTime, data.Status.CompletionTime)

	for _, p := range data.Spec.Params {
		v, _ := p.Value.MarshalJSON()
		ret.Params = append(ret.Params, [2]string{p.Name, string(v)})
	}
	succeededCondition := data.Status.GetCondition(apis.ConditionSucceeded)
	if succeededCondition.IsFalse() {
		if succeededCondition.GetReason() == "Cancelled" {
			return false
		}

		ret.FailedMessage = succeededCondition.Message
		ret.RerunURL = fmt.Sprintf("tkn -n %s pipeline start %s --use-pipelinerun %s",
			data.Namespace, data.Spec.PipelineRef.Name, data.Name)
	}
	if results := data.Status.PipelineResults; len(results) > 0 {
		for _, r := range results {
			ret.Results = append(ret.Results, [2]string{r.Name, r.Value.StringVal})
		}
	}

	return true
}

func getFailedTasks(data *v1beta1.PipelineRun, logGetter func(podName, containerName string) string) map[string][]stepInfo {
	ret := make(map[string][]stepInfo)
	for _, v := range data.Status.TaskRuns {
		succeededCondition := v.Status.GetCondition(apis.ConditionSucceeded)
		if !succeededCondition.IsTrue() {
			ret[v.PipelineTaskName] = getStepStatuses(v.Status, logGetter)
		}
	}

	return ret
}

func getStepStatuses(status *v1beta1.TaskRunStatus, logGetter func(podName, containerName string) string) []stepInfo {
	var ret []stepInfo
	for _, s := range status.Steps {
		if s.Terminated != nil {
			if s.Terminated.Reason != "Completed" {
				ret = append(ret, stepInfo{s, logGetter(status.PodName, s.ContainerName)})
				break
			}
			ret = append(ret, stepInfo{s, ""})
		}
	}

	return ret
}

func getStepLog(baseURL, ns, podName, containerName string, tailLines int) (string, error) {
	errLogEvent := log.Error().
		Str("namespace", ns).
		Str("pod", podName).
		Str("container", containerName).
		Int("tail", tailLines)

	apiURL, err := url.JoinPath(baseURL, fmt.Sprintf("api/v1/namespaces/%s/pods/%s/log", ns, podName))
	if err != nil {
		errLogEvent.Err(err).Send()
		return "", err
	}

	// Create a custom transport with InsecureSkipVerify set to true
	transport := &http.Transport{
		TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
	}
	// Create a custom HTTP client with the custom transport
	client := &http.Client{Transport: transport}

	resp, err := client.Get(fmt.Sprintf("%s?container=%s&tailLines=%d", apiURL, containerName, tailLines))
	if err != nil {
		errLogEvent.Err(err).Send()
		return "", err
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		errLogEvent.Err(err).Send()
		return "", err
	}
	errLogEvent.Discard()

	log.Debug().
		Str("namespace", ns).
		Str("pod", podName).
		Str("container", containerName).
		Int("tail", tailLines).
		Msg("get log succeed.")
	return string(body), nil
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
