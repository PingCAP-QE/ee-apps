package tekton

import (
	"context"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"testing/iotest"
	"time"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	tektoncloudevent "github.com/tektoncd/pipeline/pkg/reconciler/events/cloudevent"
)

func TestGetStepLogRejectsInvalidEventNames(t *testing.T) {
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		fmt.Fprint(w, "must not be returned")
	}))
	defer server.Close()

	tests := []struct {
		name, namespace, pod, container string
	}{
		{"namespace traversal", "../../../../secrets", "pod-1", "step-build"},
		{"pod traversal", "ee-cd", "../../../secrets", "step-build"},
		{"container query injection", "ee-cd", "pod-1", "step-build&tailLines=999&previous=true"},
		{"container fragment", "ee-cd", "pod-1", "step-build#fragment"},
		{"encoded traversal", "%2e%2e", "pod-1", "step-build"},
		{"encoded separator", "ee-cd", "pod%2fname", "step-build"},
		{"namespace dot", ".", "pod-1", "step-build"},
		{"pod dot dot", "ee-cd", "..", "step-build"},
		{"container slash", "ee-cd", "pod-1", "step/build"},
		{"empty namespace", "", "pod-1", "step-build"},
		{"empty pod", "ee-cd", "", "step-build"},
		{"empty container", "ee-cd", "pod-1", ""},
		{"uppercase namespace", "EE-CD", "pod-1", "step-build"},
		{"uppercase container", "ee-cd", "pod-1", "Step-build"},
		{"namespace too long", strings.Repeat("n", 64), "pod-1", "step-build"},
		{"pod too long", "ee-cd", strings.Repeat("p", 254), "step-build"},
		{"container too long", "ee-cd", "pod-1", strings.Repeat("s", 64)},
		{"query in pod", "ee-cd", "pod-1?follow=true", "step-build"},
		{"newline in container", "ee-cd", "pod-1", "step-build\n"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := getStepLog(server.URL+"/tekton", tt.namespace, tt.pod, tt.container, 20)
			if err == nil || got != "" {
				t.Errorf("getStepLog() = %q, %v; want empty result and validation error", got, err)
			}
		})
	}
	if got := requests.Load(); got != 0 {
		t.Fatalf("invalid event metadata issued %d HTTP requests", got)
	}
}

func TestGetStepLogPreservesDashboardPrefixAndQuery(t *testing.T) {
	for _, prefix := range []string{"", "/", "/tekton", "/tekton/", "/nested/dashboard"} {
		t.Run(prefix, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				wantPath := strings.TrimSuffix(prefix, "/") + "/api/v1/namespaces/ee-cd/pods/pod.worker-0/log"
				if r.URL.Path != wantPath {
					t.Errorf("path = %q, want %q", r.URL.Path, wantPath)
				}
				query := r.URL.Query()
				if len(query) != 2 || len(query["container"]) != 1 || query.Get("container") != "step-build" || len(query["tailLines"]) != 1 || query.Get("tailLines") != "20" {
					t.Errorf("unexpected query: %v", query)
				}
				fmt.Fprint(w, "expected step logs")
			}))
			defer server.Close()
			got, err := getStepLog(server.URL+prefix, "ee-cd", "pod.worker-0", "step-build", 20)
			if err != nil || got != "expected step logs" {
				t.Fatalf("getStepLog() = %q, %v", got, err)
			}
		})
	}
}

func TestGetStepLogRejectsUntrustedTLS(t *testing.T) {
	var requests atomic.Int32
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		fmt.Fprint(w, "untrusted logs")
	}))
	defer server.Close()
	got, err := getStepLog(server.URL, "ee-cd", "pod-1", "step-build", 20)
	if err == nil || got != "" {
		t.Fatalf("untrusted TLS returned %q, %v", got, err)
	}
	if requests.Load() != 0 {
		t.Fatal("untrusted TLS server received an HTTP request")
	}
}

func TestGetStepLogDoesNotFollowRedirects(t *testing.T) {
	var destinationRequests atomic.Int32
	destination := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		destinationRequests.Add(1)
		fmt.Fprint(w, "redirected sensitive response")
	}))
	defer destination.Close()
	for _, status := range []int{301, 302, 303, 307, 308} {
		t.Run(fmt.Sprint(status), func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				http.Redirect(w, r, destination.URL+"/unexpected", status)
			}))
			defer server.Close()
			got, err := getStepLog(server.URL, "ee-cd", "pod-1", "step-build", 20)
			if err == nil || got != "" {
				t.Errorf("redirect returned %q, %v", got, err)
			}
		})
	}
	if destinationRequests.Load() != 0 {
		t.Fatal("log client followed a redirect to another endpoint")
	}
}

func TestGetStepLogRejectsNonSuccessfulResponses(t *testing.T) {
	for _, status := range []int{http.StatusUnauthorized, http.StatusForbidden, http.StatusNotFound, http.StatusInternalServerError} {
		t.Run(fmt.Sprint(status), func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(status)
				fmt.Fprint(w, "response must not enter notification card")
			}))
			defer server.Close()
			got, err := getStepLog(server.URL, "ee-cd", "pod-1", "step-build", 20)
			if err == nil || got != "" {
				t.Fatalf("status %d returned %q, %v", status, got, err)
			}
		})
	}
}

func TestGetStepLogRejectsInvalidBaseURL(t *testing.T) {
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		fmt.Fprint(w, "unexpected")
	}))
	defer server.Close()
	for _, base := range []string{
		"", "/relative", "ftp://example.invalid/tekton", "://invalid",
		server.URL + "?token=not-a-real-token", server.URL + "#fragment",
		strings.Replace(server.URL, "://", "://user:password@", 1),
	} {
		t.Run(base, func(t *testing.T) {
			got, err := getStepLog(base, "ee-cd", "pod-1", "step-build", 20)
			if err == nil || got != "" {
				t.Errorf("invalid base URL returned %q, %v", got, err)
			}
		})
	}
	if requests.Load() != 0 {
		t.Fatal("invalid base URL issued HTTP requests")
	}
}

func TestGetStepLogAcceptsTrustedTLSAndRejectsWrongHostname(t *testing.T) {
	var requests atomic.Int32
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		fmt.Fprint(w, "trusted logs")
	}))
	defer server.Close()
	client := *stepLogHTTPClient
	client.Transport = server.Client().Transport // Explicit root trust, not InsecureSkipVerify.
	got, err := getStepLogWithClient(&client, server.URL, "ee-cd", "pod-1", "step-build", 20)
	if err != nil || got != "trusted logs" {
		t.Fatalf("trusted TLS returned %q, %v", got, err)
	}
	wrongHost := strings.Replace(server.URL, "127.0.0.1", "localhost", 1)
	got, err = getStepLogWithClient(&client, wrongHost, "ee-cd", "pod-1", "step-build", 20)
	var hostnameError x509.HostnameError
	if !errors.As(err, &hostnameError) || got != "" || requests.Load() != 1 {
		t.Fatalf("wrong hostname was not rejected before HTTP: %q, %v, requests=%d", got, err, requests.Load())
	}
}

func TestGetStepLogRejectsOversizedResponses(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(w, strings.NewReader(strings.Repeat("x", (1<<20)+1)))
	}))
	defer server.Close()
	got, err := getStepLog(server.URL, "ee-cd", "pod-1", "step-build", 20)
	if err == nil || got != "" {
		t.Fatalf("oversized logs accepted: len=%d, error=%v", len(got), err)
	}
}

type stepLogRoundTripper func(*http.Request) (*http.Response, error)

func (f stepLogRoundTripper) RoundTrip(r *http.Request) (*http.Response, error) {
	return f(r)
}

type trackedStepLogBody struct {
	io.Reader
	closed    bool
	bytesRead int
}

func (b *trackedStepLogBody) Read(p []byte) (int, error) {
	n, err := b.Reader.Read(p)
	b.bytesRead += n
	return n, err
}

func (b *trackedStepLogBody) Close() error {
	b.closed = true
	return nil
}

func TestGetStepLogClosesAndBoundsResponses(t *testing.T) {
	for _, tt := range []struct {
		name       string
		status     int
		reader     io.Reader
		wantLength int
		wantError  bool
	}{
		{"success at limit", 200, strings.NewReader(strings.Repeat("x", maxStepLogBytes)), maxStepLogBytes, false},
		{"oversize", 200, strings.NewReader(strings.Repeat("x", maxStepLogBytes+1000)), 0, true},
		{"read failure", 200, iotest.ErrReader(errors.New("incomplete body")), 0, true},
		{"status failure", 500, strings.NewReader("not log content"), 0, true},
	} {
		t.Run(tt.name, func(t *testing.T) {
			body := &trackedStepLogBody{Reader: tt.reader}
			client := *stepLogHTTPClient
			client.Transport = stepLogRoundTripper(func(r *http.Request) (*http.Response, error) {
				return &http.Response{StatusCode: tt.status, Body: body, Header: make(http.Header), Request: r}, nil
			})
			got, err := getStepLogWithClient(&client, "https://dashboard.example/tekton", "ee-cd", "pod-1", "step-build", 20)
			if (err != nil) != tt.wantError || len(got) != tt.wantLength || !body.closed {
				t.Fatalf("len=%d, err=%v, closed=%v", len(got), err, body.closed)
			}
			if body.bytesRead > maxStepLogBytes+1 {
				t.Fatalf("read %d bytes beyond response budget", body.bytesRead)
			}
			if tt.status == 500 && body.bytesRead != 0 {
				t.Fatal("read an HTTP error body as logs")
			}
		})
	}
}

func TestGetStepLogClientHasAndHonorsTimeout(t *testing.T) {
	if stepLogHTTPClient.Timeout <= 0 || stepLogHTTPClient.Timeout > 30*time.Second {
		t.Fatalf("unbounded log client timeout: %v", stepLogHTTPClient.Timeout)
	}
	client := *stepLogHTTPClient
	client.Timeout = 20 * time.Millisecond
	client.Transport = stepLogRoundTripper(func(r *http.Request) (*http.Response, error) {
		<-r.Context().Done()
		return nil, r.Context().Err()
	})
	got, err := getStepLogWithClient(&client, "https://dashboard.example", "ee-cd", "pod-1", "step-build", 20)
	if !errors.Is(err, context.DeadlineExceeded) || got != "" {
		t.Fatalf("timeout returned %q, %v", got, err)
	}
}

func TestTektonFailureEventsUseValidatedLogRequests(t *testing.T) {
	for _, fixture := range []struct {
		name string
		data []byte
	}{
		{"PipelineRun", pipelineRunFailedEventBytes},
		{"standalone TaskRun", standaloneTaskRunFailedEventBytes},
	} {
		for _, field := range []string{"valid", "namespace", "pod", "container"} {
			t.Run(fixture.name+"/"+field, func(t *testing.T) {
				var requests atomic.Int32
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					requests.Add(1)
					if r.URL.Path != "/tekton/api/v1/namespaces/ee-cd/pods/pod-1/log" || r.URL.Query().Get("container") != "step-build" {
						t.Errorf("unexpected log request: %s", r.URL.String())
					}
					fmt.Fprint(w, "expected failure logs")
				}))
				defer server.Close()
				event := cloudevents.NewEvent()
				if err := json.Unmarshal(fixture.data, &event); err != nil {
					t.Fatal(err)
				}
				var data tektoncloudevent.TektonCloudEventData
				if err := event.DataAs(&data); err != nil {
					t.Fatal(err)
				}
				namespace, pod, container := "ee-cd", "pod-1", "step-build"
				switch field {
				case "namespace":
					namespace = "../../../../secrets"
				case "pod":
					pod = "../../../secrets"
				case "container":
					container = "step-build&previous=true"
				}
				if data.PipelineRun != nil {
					data.PipelineRun.Namespace = namespace
					for _, task := range data.PipelineRun.Status.TaskRuns {
						if task.Status != nil {
							task.Status.PodName = pod
							for i := range task.Status.Steps {
								task.Status.Steps[i].ContainerName = container
							}
						}
					}
				} else {
					data.TaskRun.Namespace = namespace
					data.TaskRun.Status.PodName = pod
					for i := range data.TaskRun.Status.Steps {
						data.TaskRun.Status.Steps[i].ContainerName = container
					}
				}
				if err := event.SetData(cloudevents.ApplicationJSON, data); err != nil {
					t.Fatal(err)
				}
				infos, err := extractLarkInfosFromEvent(event, server.URL+"/tekton", 20)
				if err != nil || infos == nil {
					t.Fatalf("notification extraction failed: infos=%v, err=%v", infos, err)
				}
				foundLog := false
				for _, steps := range infos.FailedTasks {
					for _, step := range steps {
						foundLog = foundLog || step.Logs == "expected failure logs"
					}
				}
				for _, step := range infos.StepStatuses {
					foundLog = foundLog || step.Logs == "expected failure logs"
				}
				if field == "valid" {
					if requests.Load() == 0 || !foundLog {
						t.Fatal("valid failure event lost its step logs")
					}
				} else if requests.Load() != 0 || foundLog {
					t.Fatal("malformed event caused log requests or embedded response content")
				}
			})
		}
	}
}
