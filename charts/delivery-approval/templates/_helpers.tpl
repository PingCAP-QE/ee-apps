{{/*
Expand the name of the chart.
*/}}
{{- define "delivery-approval.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "delivery-approval.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "delivery-approval.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "delivery-approval.labels" -}}
helm.sh/chart: {{ include "delivery-approval.chart" . }}
{{ include "delivery-approval.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "delivery-approval.selectorLabels" -}}
app.kubernetes.io/name: {{ include "delivery-approval.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "delivery-approval.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "delivery-approval.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}
