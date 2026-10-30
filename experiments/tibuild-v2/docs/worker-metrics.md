# Worker metrics: scraping by cluster

The image-sync worker serves Prometheus metrics on its health port
(`worker.healthPort`, default `8081`) at `/metrics`:

| Metric | Labels | Meaning |
| --- | --- | --- |
| `publish_task_backlog` | `target` | PENDING tasks per target |
| `publish_task_oldest_pending_seconds` | `target` | Age of the oldest PENDING task |
| `publish_task_inflight` | `target` | PROCESSING tasks per target |
| `publish_task_lease_expired_total` | — | Tasks reclaimed after a lease expired |
| `publish_task_failed_total` | `target`, `reason` | Tasks that reached FAILED |

Every replica exposes the same series for the shared queue, so dashboards should
aggregate with `max by (target)` (or scrape a single replica) to avoid summing
the same task three times.

## prod2: PodMonitor

The chart renders a `PodMonitor` when `worker.podMonitor.enabled=true`:

```yaml
worker:
  podMonitor:
    enabled: true
    interval: 30s
    # Must match the Prometheus instance's podMonitorSelector; ask the platform
    # owner for the label, e.g. release: kube-prometheus-stack
    additionalLabels: {}
```

The PodMonitor selects the worker pods by
`app.kubernetes.io/name=<chart>-worker` + `app.kubernetes.io/instance=<release>`
and scrapes the named `health` port, so it keeps working when `healthPort`
changes.

Check that the target is up:

```bash
kubectl -n tibuild get podmonitor v2-tibuild-v2-worker -o yaml | head -30
# in the Prometheus UI / API:
#   up{namespace="tibuild", pod=~"v2-tibuild-v2-worker-.*"} == 1
```

If the target is missing, the usual cause is that the Prometheus instance's
`podMonitorSelector` does not match the PodMonitor labels; read it with
`kubectl get prometheus -A -o yaml` and mirror the label in
`worker.podMonitor.additionalLabels`.

## tke / gcp: no Prometheus Operator CRDs

Verified on `tke-pingcap-cicd` on 2026-10-30:

```console
$ kubectl --context tke-pingcap-cicd get crd | grep -c podmonitors.monitoring.coreos.com
0
$ kubectl --context tke-pingcap-cicd api-resources | grep -c monitoring.coreos.com
0
```

The namespace still runs `v2-tibuild-v2-worker`, but nothing can consume a
`PodMonitor` there. Options, in order of effort:

1. **External agent with Kubernetes SD** (recommended): vmagent, Grafana Agent
   or an OpenTelemetry Collector discovers pods by label and scrapes
   `:8081/metrics`. No CRDs, no chart change.
2. **Annotation-based scrape**: set
   `worker.podAnnotations: {prometheus.io/scrape: "true", prometheus.io/port: "8081"}`
   and let a Prometheus with annotation support pick it up. The Prometheus
   Operator ignores these annotations by default, so this only works if the
   cluster's scraper implements them.
3. **Install the CRDs + Operator**: the full solution, but a platform-level
   decision (new controllers, RBAC and storage).

Until one of these lands, the TKE/GCP workers still *record* the metrics; they
are simply not scraped.

## Short-lived verification without a scraper

```bash
kubectl -n tibuild port-forward deploy/v2-tibuild-v2-worker 8081:8081
curl -s localhost:8081/metrics | grep '^publish_task_'
```
