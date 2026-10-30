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

## prod2: ServiceMonitor

prod2 runs the KSY MSP vmagent (`prom-89e6db30/msp-vmagent-0`), which builds its
scrape config from `ServiceMonitor` objects only. The `PodMonitor` CRD happens to
be installed, but the generated config has eight `serviceMonitor/` jobs and no
`podMonitor` job, so a PodMonitor there is inert:

```console
$ kubectl -n prom-89e6db30 exec deploy/msp-vmagent-0 -c vmagent -- \
    grep -oE "job_name: [a-zA-Z]+/" /etc/vmagent/config_out/scrape.yml | sort | uniq -c
      8 job_name: serviceMonitor/
```

The worker has no Service in the chart (the API never calls it), so the metrics
pair lives in the delivery repo, `PingCAP-QE/ee-ops`,
`apps/prod2/tibuild/v2/worker-metrics.yaml`:

- Service `v2-tibuild-v2-worker-metrics` selects the worker pods and exposes the
  named `health` port
- ServiceMonitor `v2-tibuild-v2-worker` scrapes `/metrics` every 30s

Check the target:

```bash
kubectl -n tibuild get svc v2-tibuild-v2-worker-metrics
kubectl -n tibuild get servicemonitor v2-tibuild-v2-worker
# in the vmagent API:
#   job=serviceMonitor/tibuild/v2-tibuild-v2-worker/0, health=up
```

## Other clusters: opt-in PodMonitor

Where a real Prometheus Operator runs, the chart can render a `PodMonitor`
instead of the Service/ServiceMonitor pair:

```yaml
worker:
  podMonitor:
    enabled: true
    interval: 30s
    # Must match the instance's podMonitorSelector.
    additionalLabels: {}
```

It selects the worker pods by `app.kubernetes.io/name=<chart>-worker` +
`app.kubernetes.io/instance=<release>` and scrapes the named `health` port, so it
keeps working when `healthPort` changes. No cluster uses it today.

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
