# Worker alerts: channels, enablement and verification

The image-sync worker (`cmd/tibuild-worker`) raises operational alerts from the
task queue. This note covers where the recipients come from, which clusters
deliver, and how to change or verify the setup.

## What alerts exist

| Condition | Config key | Default |
| --- | --- | --- |
| Backlog non-empty for a while | `worker.alerts.backlog_after` | `10m` |
| Oldest PENDING task too old | `worker.alerts.oldest_pending_after` | `30m` |
| A lease was reclaimed recently | `worker.alerts.lease_reclaim_window` | `5m` |
| Repeat suppression per condition | `worker.alerts.dedupe` | `1h` |
| Master switch | `worker.alerts.enabled` | `true` |

`worker.alerts.enabled: false` stops alert delivery only; `/metrics` on the
worker health port keeps refreshing.

## Who sends, and how often

All worker replicas of all clusters share one `image_sync_tasks` queue, so every
replica used to see the same condition. The worker now elects a single evaluator
through the `alert_leases` table (`name = publish-alerts`):

- the holder renames its lease with one guarded `UPDATE`; an expired lease
  (`lease_until < now`) may be taken over by any other worker;
- the lease TTL is `2 × worker.metrics_interval`;
- only the holder calls the notifier, so one condition produces one message even
  across prod2 / tencentcloud / gcp.

A worker whose config has **no** `lark.channels` does not compete for the lease.
Otherwise a cluster without recipients could win the election and silently drop
alerts for the whole fleet.

## Where the recipients live

Channels are not stored in git. They come from **GCP Secret Manager**:

| Item | Value |
| --- | --- |
| Secret | `tibuild_v2_lark_channels` |
| Project | `pingcap-testing-account` |
| Format | JSON array: `[{"name":"tibuild-alerts","chat_id":"oc_..."}]` |
| ClusterSecretStore | `ee-gcp-sm` |
| ExternalSecret | `v2-config` in namespace `tibuild` |

`apps/prod2/tibuild/v2/external-secret-config.yaml` maps the secret key to
`lark.channels`:

```yaml
lark:
  enabled: true
  app_id: {{ .lark_app_id }}
  app_secret: {{ .lark_app_secret }}
  channels: {{ .lark_channels }}
```

As of 2026-10-30 only **prod2** injects `channels`. The gcp and tencentcloud
configs set `lark.enabled` with app credentials but no channels, so those
workers keep serving metrics and never hold the alert lease.

## Change the recipient list

1. Get the target `chat_id`:

   - Lark desktop: right-click the group → **Copy Chat ID** → `oc_...`
   - API: `GET https://open.feishu.cn/open-apis/im/v1/chats?page_size=50`

   The bot must already be a member of that group.

2. Read the current value and add a version (never paste the file into git):

   ```bash
   gcloud secrets versions access latest \
     --secret=tibuild_v2_lark_channels \
     --project=pingcap-testing-account > /tmp/lark-channels.json
   # edit /tmp/lark-channels.json, keeping it a JSON array
   gcloud secrets versions add tibuild_v2_lark_channels \
     --project=pingcap-testing-account \
     --data-file=/tmp/lark-channels.json
   rm -f /tmp/lark-channels.json
   ```

3. Force External Secrets to resync and confirm the key is rendered:

   ```bash
   kubectl -n tibuild annotate externalsecret v2-config \
     force-sync="$(date +%s)" --overwrite
   kubectl -n tibuild get externalsecret v2-config \
     -o jsonpath='{.status.conditions[*].reason}{"\n"}'
   # expect SecretSynced
   ```

4. Restart the worker so it picks up the new config. The worker reads
   `lark.channels` at startup; a config hot reload does not re-wire the notifier.

   ```bash
   kubectl -n tibuild rollout restart deploy/v2-tibuild-v2-worker
   kubectl -n tibuild rollout status deploy/v2-tibuild-v2-worker
   ```

5. Confirm delivery with a real message (see below).

## Verify

**Alert path against the real app** (one message in the configured group):

```bash
LARK_APP_ID=... LARK_APP_SECRET=... LARK_ALERT_CHAT_ID=oc_... \
  go test -tags=manual -run TestSendLarkAlert_Manual ./internal/service/impl
```

**Who currently holds the lease:**

```sql
SELECT name, holder, lease_until, TIMESTAMPDIFF(SECOND, NOW(3), lease_until) AS ttl_s
FROM alert_leases WHERE name = 'publish-alerts';
```

The holder should be one of the prod2 worker pods and `ttl_s` should stay near
`2 × metrics_interval` (30s by default).

**Metrics still flow from every replica:**

```bash
kubectl -n tibuild port-forward deploy/v2-tibuild-v2-worker 8081:8081
curl -s localhost:8081/metrics | grep publish_task_
```

**Shorten the thresholds for an e2e run:** set
`metrics_interval: "5s"`, `alerts.backlog_after: "1m"` and
`alerts.oldest_pending_after: "1m"` in a non-production environment. The
defaults stay untouched in production.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No alerts at all | `worker.alerts.enabled`, `lark.enabled`, app id/secret, and whether `lark.channels` is non-empty |
| Alerts duplicated | More than one `alert_leases` row, or replicas running an image without the election |
| Alerts stop after a rollout | New replica cannot write `alert_leases` (DB permission), see worker logs for `failed to acquire the alert lease` |
| Group gets no message but the API returns 0 | The bot is not a member of the group, or `chat_id` is not `oc_...` |
