package tekton

import (
	larkcard "github.com/larksuite/oapi-sdk-go/v3/card"
	tektoncloudevent "github.com/tektoncd/pipeline/pkg/reconciler/events/cloudevent"
)

type cardMessageInfos struct {
	Title         string
	TitleTemplate string
	RerunURL      string
	ViewURL       string
	StartTime     string
	EndTime       string
	TimeCost      string
	Results       string
}

var larkCardHeaderTemplates = map[tektoncloudevent.TektonEventType]string{
	tektoncloudevent.PipelineRunFailedEventV1:     larkcard.TemplateRed,
	tektoncloudevent.PipelineRunRunningEventV1:    larkcard.TemplateBlue,
	tektoncloudevent.PipelineRunStartedEventV1:    larkcard.TemplateYellow,
	tektoncloudevent.PipelineRunSuccessfulEventV1: larkcard.TemplateGreen,
	tektoncloudevent.PipelineRunUnknownEventV1:    larkcard.TemplateGrey,
	tektoncloudevent.RunFailedEventV1:             larkcard.TemplateRed,
	tektoncloudevent.RunRunningEventV1:            larkcard.TemplateBlue,
	tektoncloudevent.RunStartedEventV1:            larkcard.TemplateYellow,
	tektoncloudevent.RunSuccessfulEventV1:         larkcard.TemplateGreen,
	tektoncloudevent.TaskRunFailedEventV1:         larkcard.TemplateRed,
	tektoncloudevent.TaskRunRunningEventV1:        larkcard.TemplateBlue,
	tektoncloudevent.TaskRunStartedEventV1:        larkcard.TemplateYellow,
	tektoncloudevent.TaskRunSuccessfulEventV1:     larkcard.TemplateGreen,
	tektoncloudevent.TaskRunUnknownEventV1:        larkcard.TemplateGrey,
}

var larkCardHeaderEmojis = map[tektoncloudevent.TektonEventType]string{
	tektoncloudevent.PipelineRunFailedEventV1:     "❌",
	tektoncloudevent.TaskRunFailedEventV1:         "❌",
	tektoncloudevent.RunFailedEventV1:             "❌",
	tektoncloudevent.PipelineRunRunningEventV1:    "🚧",
	tektoncloudevent.TaskRunRunningEventV1:        "🚧",
	tektoncloudevent.RunRunningEventV1:            "🚧",
	tektoncloudevent.PipelineRunStartedEventV1:    "🚀",
	tektoncloudevent.TaskRunStartedEventV1:        "🚀",
	tektoncloudevent.RunStartedEventV1:            "🚀",
	tektoncloudevent.PipelineRunSuccessfulEventV1: "✅",
	tektoncloudevent.TaskRunSuccessfulEventV1:     "✅",
	tektoncloudevent.RunSuccessfulEventV1:         "✅",
	tektoncloudevent.PipelineRunUnknownEventV1:    "⌛️",
	tektoncloudevent.TaskRunUnknownEventV1:        "⌛️",
}
