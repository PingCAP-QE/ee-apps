package tekton

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/rs/zerolog/log"
	"github.com/tektoncd/pipeline/pkg/apis/pipeline/v1beta1"
	tektoncloudevent "github.com/tektoncd/pipeline/pkg/reconciler/events/cloudevent"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/sets"
	"k8s.io/apimachinery/pkg/util/validation"
	"knative.dev/pkg/apis"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/config"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/events/handler"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/lark"
)

const (
	eventContextAnnotationKey = "tekton.dev/ce-context"
	maxStepLogBytes           = 1 << 20
)

// devbuildSourceReg matches the cloud event sources of devbuilds created by
// both tibuild v1 (tibuild.pingcap.net/api/devbuilds/<id>) and tibuild-v2
// (tibuild.pingcap.net/api/v2/devbuilds/<id>).
var devbuildSourceReg = regexp.MustCompile(`^tibuild\.pingcap\.net/api/(v2/)?devbuilds/`)

var stepLogHTTPClient = &http.Client{
	Timeout: 10 * time.Second,
	CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
		return http.ErrUseLastResponse
	},
}

func NewHandler(cfg config.Tekton) (handler.EventHandler, error) {
	larkClient := lark.NewClient(cfg.Lark.AppID, cfg.Lark.AppSecret)
	ret := new(handler.CompositeEventHandler).AddHandlers(
		&pipelineRunHandler{LarkClient: larkClient, Tekton: cfg},
		&taskRunHandler{LarkClient: larkClient, Tekton: cfg},
	)

	return ret, nil
}

// eventContext is the JSON payload stored in the `tekton.dev/ce-context`
// annotation. The Tekton EventListener writes it when the run is created from a
// cloud event.
type eventContext struct {
	Source  string `json:"source"`
	Subject string `json:"subject"`
	User    string `json:"user"`
}

func getEventContext(run AnnotationsGetter) eventContext {
	var ret eventContext

	raw := run.GetAnnotations()[eventContextAnnotationKey]
	if raw == "" {
		return ret
	}

	_ = json.Unmarshal([]byte(raw), &ret)
	return ret
}

func isDevbuildSource(source string) bool {
	return devbuildSourceReg.MatchString(source)
}

// skipTriggerUser reports whether the event's trigger user must not be used as
// a notification receiver. Devbuilds are already notified by tibuild-v2 with a
// single, continuously updated card, so notifying the trigger user here would
// only duplicate it.
func skipTriggerUser(ctx eventContext) bool {
	return isDevbuildSource(ctx.Source)
}

// resolveReceivers decides who should receive the notification for a run:
//   - devbuild events: fall back to the configured notification receivers;
//   - other events: the trigger user from the ce-context when it exists,
//     otherwise the configured notification receivers.
func resolveReceivers(run AnnotationsGetter, event cloudevents.Event, cfgs []config.TektonNotification) []string {
	ctx := getEventContext(run)
	if ctx.User != "" && !skipTriggerUser(ctx) {
		return []string{ctx.User}
	}

	return getReceivers(event, cfgs)
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
	case data.CustomRun != nil:
		if !fillInfosWithCustomRun(data.CustomRun, &ret) {
			return nil, nil
		}
	}

	return &ret, nil
}

func fillInfosWithCustomRun(data *v1beta1.CustomRun, ret *cardMessageInfos) bool {
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
	logs, err := getStepLogWithClient(stepLogHTTPClient, baseURL, ns, podName, containerName, tailLines)
	if err != nil {
		log.Error().Err(err).
			Str("namespace", ns).
			Str("pod", podName).
			Str("container", containerName).
			Int("tail", tailLines).
			Msg("get step log failed")
	}
	return logs, err
}

func getStepLogWithClient(client *http.Client, baseURL, ns, podName, containerName string, tailLines int) (string, error) {
	// CloudEvent JSON is not Kubernetes admission-validated. Reject separators,
	// dot segments and query delimiters before any URL normalization or I/O.
	for _, field := range []struct {
		name, value string
		validate    func(string) []string
	}{
		{"namespace", ns, validation.IsDNS1123Label},
		{"pod", podName, validation.IsDNS1123Subdomain},
		{"container", containerName, validation.IsDNS1123Label},
	} {
		if len(field.validate(field.value)) != 0 {
			return "", fmt.Errorf("invalid Tekton log %s", field.name)
		}
	}

	base, err := url.Parse(baseURL)
	if err != nil || (base.Scheme != "http" && base.Scheme != "https") || base.Host == "" || base.User != nil || base.RawQuery != "" || base.Fragment != "" {
		return "", fmt.Errorf("invalid Tekton dashboard base URL")
	}
	apiURL := base.JoinPath("api", "v1", "namespaces", ns, "pods", podName, "log")
	apiURL.RawQuery = url.Values{
		"container": {containerName},
		"tailLines": {strconv.Itoa(tailLines)},
	}.Encode()

	resp, err := client.Get(apiURL.String())
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		return "", fmt.Errorf("Tekton log request returned HTTP %d", resp.StatusCode)
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, maxStepLogBytes+1))
	if err != nil {
		return "", err
	}
	if len(body) > maxStepLogBytes {
		return "", fmt.Errorf("Tekton step log exceeds %d bytes", maxStepLogBytes)
	}
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
