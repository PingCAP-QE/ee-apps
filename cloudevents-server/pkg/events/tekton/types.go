package tekton

import (
	larkcard "github.com/larksuite/oapi-sdk-go/v3/card"
	"github.com/tektoncd/pipeline/pkg/apis/pipeline/v1beta1"
	"github.com/tektoncd/pipeline/pkg/reconciler/events/cloudevent"
)

type cardMessageInfos struct {
	Title         string
	TitleTemplate string
	RerunURL      string
	ViewURL       string
	StartTime     string
	EndTime       string
	TimeCost      string
	FailedMessage string
	Params        [][2]string                    // key-value pairs.
	Results       [][2]string                    // Key-Value pairs.
	StepStatuses  []v1beta1.StepState            // name-status pairs.
	FailedTasks   map[string][]v1beta1.StepState // task id => step statuses.
}

var larkCardHeaderTemplates = map[cloudevent.TektonEventType]string{
	cloudevent.PipelineRunFailedEventV1:     larkcard.TemplateRed,
	cloudevent.PipelineRunRunningEventV1:    larkcard.TemplateBlue,
	cloudevent.PipelineRunStartedEventV1:    larkcard.TemplateYellow,
	cloudevent.PipelineRunSuccessfulEventV1: larkcard.TemplateGreen,
	cloudevent.PipelineRunUnknownEventV1:    larkcard.TemplateGrey,
	cloudevent.CustomRunFailedEventV1:       larkcard.TemplateRed,
	cloudevent.CustomRunRunningEventV1:      larkcard.TemplateBlue,
	cloudevent.CustomRunStartedEventV1:      larkcard.TemplateYellow,
	cloudevent.CustomRunSuccessfulEventV1:   larkcard.TemplateGreen,
	cloudevent.TaskRunFailedEventV1:         larkcard.TemplateRed,
	cloudevent.TaskRunRunningEventV1:        larkcard.TemplateBlue,
	cloudevent.TaskRunStartedEventV1:        larkcard.TemplateYellow,
	cloudevent.TaskRunSuccessfulEventV1:     larkcard.TemplateGreen,
	cloudevent.TaskRunUnknownEventV1:        larkcard.TemplateGrey,
}

var larkCardHeaderEmojis = map[cloudevent.TektonEventType]string{
	cloudevent.PipelineRunFailedEventV1:     "❌",
	cloudevent.TaskRunFailedEventV1:         "❌",
	cloudevent.CustomRunFailedEventV1:       "❌",
	cloudevent.PipelineRunRunningEventV1:    "🚧",
	cloudevent.TaskRunRunningEventV1:        "🚧",
	cloudevent.CustomRunRunningEventV1:      "🚧",
	cloudevent.PipelineRunStartedEventV1:    "🚀",
	cloudevent.TaskRunStartedEventV1:        "🚀",
	cloudevent.CustomRunStartedEventV1:      "🚀",
	cloudevent.PipelineRunSuccessfulEventV1: "✅",
	cloudevent.TaskRunSuccessfulEventV1:     "✅",
	cloudevent.CustomRunSuccessfulEventV1:   "✅",
	cloudevent.PipelineRunUnknownEventV1:    "⌛️",
	cloudevent.TaskRunUnknownEventV1:        "⌛️",
}

func headerEmoji(etype string) string {
	return larkCardHeaderEmojis[cloudevent.TektonEventType(etype)]
}
