# Delivery approval operations

Operator runbook for the delivery approval service: how a request moves, how to
approve or revoke one, where the audit trail lives, what the service logs, and
what to do when Lark, TiDB or `dl` is unavailable.

The service owns requester/approver identity, the request state machine, the
encrypted one-time `download_url`s and the Lark notifications. It does not own
the download itself: every link is a `dl` capability grant, so `dl` stays the
only component that can mint or revoke one. See `../README.md` for the developer
view (configuration, layout, design notes).

## Surfaces

Hosts are deliberately not repeated here: the browser-facing base is whatever
`public_url` says, the in-cluster `dl` origin is `dl_base_url`, and the anonymous
download origin belongs to the `dl` deployment.

| Surface (relative to the base above) | Who | Notes |
| --- | --- | --- |
| `public_url` + `/` | any employee whose email domain is in `allowed_email_domains` | OIDC-protected form; the only public surface |
| `public_url` + `/pending` | approvers | pending queue |
| `public_url` + `/requests`, `public_url` + `/requests/{id}` | requester and approvers | list and detail; approve/reject/revoke on the detail page |
| `dl_base_url` + `/api/v1/shares` | this service only | issuer `delivery-approval` |
| the `dl` public origin + `/d/{token}` | nobody logged in | the anonymous link handed to the customer |

`public_url` is also the mount point: the route publishes that path and rewrites
it before forwarding, so a link pasted without the prefix lands on the
unauthenticated `dl` application instead.

The application listener is `:8080`; `/livez`, `/healthz` and `/metrics` live on
the private `:8081` listener, which has no route and must never be published.

Identity comes from the Gateway: Envoy terminates the Google OIDC login and
forwards the ID token in `X-Approval-Id-Token`. The service validates that token
itself (signature, issuer, audience, `email_verified`, allowed domain). A request
without exactly one valid token is `401`; a valid identity outside
`allowed_email_domains` is `403`. No other identity header is trusted, including
on cluster-internal calls.

## Request lifecycle

```
pending ──approve──> approved ──deliver──> delivered (delivery_status)
   │                    │
   ├──reject──> rejected├──delivery failure──> failed + every share revoked
   ├──expire──> expired └──revoke──> revoked
   └──revoke──> revoked
```

- `approve`/`reject` require `status = pending`, enforced with
  `UPDATE … WHERE status = 'pending'`. A decision is final: there is no
  "approve again" or "change my mind". To undo an approval, revoke the request —
  revoking an `approved` request also revokes its issued `dl` shares, so the
  links stop working immediately.
- The requester can revoke their own `pending`/`approved` request; any approver
  can revoke anyone's. `rejected`/`revoked`/`expired` requests are terminal.
- The approver cannot be the requester. The detail page hides the buttons, and
  `POST` returns `403`.
- Losing a concurrent decision is not an error: the browser is redirected (303)
  to the detail page with `?notice=already-decided`. A non-browser client that
  loses the race between the pre-check and the `UPDATE` gets `409`.
- Someone else's request is `404`, not `403`, so ids cannot be probed.
- Approving starts delivery in the background. The approval itself succeeds even
  if delivery later fails; `status` and `delivery_status` are independent.

### Expiry

A sweeper runs every 60s (batch 100) and moves `pending` **and** `approved`
requests whose `expires_at` has passed to `expired`. For an `approved` request
it also revokes every issued `dl` share, records the transition, DMs the
requester (saying the links no longer work only when the request had been
approved), and finally patches the approval cards. Revocation, notification and
card failures are logged and never block the state change.

`expires_at` is fixed at creation (`created_at + ttl_seconds`); approving a
request does not extend it. A request approved near its expiry therefore
delivers links that die shortly afterwards — check `expires_at` before
approving.

### Rate limits

Per requester: at most `max_pending_per_requester` pending requests, and at most
`max_requests_per_day` creations in the last 24h. Both return `429`.

## Approvers

`approver_emails` in the service config is the whole allowlist; there is no role
in the database or the gateway. The file is re-read every 30s when the mounted
Secret changes, so adding or removing an approver needs no restart.

Only **environment** changes (DSNs, Lark app, `dl` issuer secret, URL encryption
key) require a rollout, because those are read once at startup from the
`ExternalSecret`-backed Secret.

Approval cards are sent per approver as DMs while
`lark_notification_chat_id` is empty. Setting it switches the announcing card to
that group and makes `lark_buzz` urgent the individual approvers instead.

## Database

Database `delivery_approval` on the shared TiDB cluster, separate from `dl`'s
tables. The runtime role has `SELECT, INSERT, UPDATE` and **no DDL**; the
migration Job (`-mode=migrate`, same image) is the only DDL writer.

| Table | Contents |
| --- | --- |
| `requests` | one row per application: requester, `status`, `ttl_seconds`, `created_at`/`expires_at`/`decided_at`, approver, notes, `delivery_status`, `delivery_attempts`, `last_error_class` |
| `request_files` | one row per `repository`/`tag`/`filename`, plus `share_id`, `download_url_ciphertext`, `delivered_at` |
| `request_events` | append-only audit: `action`, `outcome`, `error_class`, `actor_email` |
| `approval_cards` | `request_id`, `approver_email`, `message_id` — only what is needed to patch a card |

`download_url_ciphertext` holds the AES-256-GCM sealed link and is cleared as
soon as the DM is sent. A non-null ciphertext on a `delivered` request means the
cleanup failed; the link is still valid, so treat it as a real (if encrypted)
secret and clear it.

### Useful queries

Pending queue, oldest first:

```sql
SELECT id, requester_email, created_at, expires_at, delivery_status
FROM requests WHERE status = 'pending' ORDER BY created_at;
```

Requests that were approved but never delivered — the usual sign of a disabled
or broken delivery path:

```sql
SELECT id, status, delivery_status, delivery_attempts, last_error_class, decided_at
FROM requests
WHERE status = 'approved' AND delivery_status <> 'delivered'
ORDER BY decided_at DESC;
```

Full audit trail for one request:

```sql
SELECT created_at, actor_email, action, outcome, error_class
FROM request_events WHERE request_id = ? ORDER BY id;
```

Files and links for one request (the ciphertext is encrypted; do not copy it):

```sql
SELECT filename, repository, tag, share_id, delivered_at,
       download_url_ciphertext IS NOT NULL AS still_sealed
FROM request_files WHERE request_id = ?;
```

Cards that should reflect a decision:

```sql
SELECT request_id, approver_email, message_id
FROM approval_cards WHERE request_id = ?;
```

Cross-check a delivered link with `dl`'s own audit (answer "who asked, who
approved, which link, how often downloaded"):

```sql
-- in the dl database
SELECT * FROM download_events WHERE share_id = ?;
```

### Schema changes

Ship the new schema in the image and update the migration Job's image tag in
ee-ops; the Job runs `-mode=migrate` before the Deployment rolls. The server
refuses to start when the schema is missing. Never run DDL as the runtime role.

## Logs and metrics

Logs go to stderr as `[delivery-approval] HH:MM:SS <message>`. The prefix carries
the time only, so use `kubectl logs --timestamps` when correlating with TiDB
rows or gateway logs. There are no request/customer/artifact labels, and no URL,
token or secret is ever logged — messages name a `request=` id at most.

| Message | Meaning |
| --- | --- |
| `delivery-approval listening on …` | startup; delivery enabled |
| `delivery disabled: Lark notifier is not configured` | **approvals will not produce links**; see the degradation table |
| `config "…" reload failed: …` | bad config file; the previous config is still in use |
| `approval notification failed request=…` | the announcing card did not reach anyone; the request still exists and is visible under `/pending` |
| `approval card persist failed request=…` | card sent but its `message_id` was not stored → that card will never be patched |
| `lark approval DM partial failure request=… failed=n/m` | some approvers did not get the card |
| `lark urgent app failed request=…` | buzz failed; the request state is unchanged |
| `lark send error: …` / `lark send rejected: code=… msg=…` | API-level failure, includes Lark's code for the vendor docs |
| `lark contact lookup failed` | email → `open_id` lookup failed; DMs to that person cannot be sent |
| `lark card update failed` / `approval card update failed request=…` | stale card; see troubleshooting |
| `delivery failed request=…` | `Deliver` returned an error; the request row has the reason |
| `delivery share revoke failed request=… share=…` | `DELETE` on a share failed; the share still works until its TTL |
| `delivery status update failed request=…` | DB write failed during delivery |
| `delivery ciphertext cleanup failed request=…` | a sealed URL outlived its delivery |
| `approval expiry transition failed request=…` | sweeper could not update the row; it retries next minute |
| `approval expiry notification failed request=…` | expiry DM failed; the request still expired |

`/metrics` on the private listener exposes six aggregate series with no labels:
`dl_approval_requests_created_total`, `…_approved_total`, `…_rejected_total`,
`…_denied_total`, `dl_approval_delivery_failed_total`,
`dl_approval_active_requests`.

## Degradation playbooks

### Lark is unavailable or misconfigured

Symptom: approving succeeds, the request stays `approved` with
`delivery_status = pending`, nobody receives a DM.

- If the service logged `delivery disabled: Lark notifier is not configured`, the
  Lark app credentials were absent at startup: `dl` shares are **never** issued
  and no link exists. Fix the secret, roll out, then **revoke the affected
  requests and ask for a new submission** — delivery is triggered only by the
  approve action, so an already-approved request is not retro-delivered.
- If shares were issued and the DM failed, delivery already retried 5 times
  (500ms linear backoff). On final failure the service revokes every share,
  sets `delivery_status = failed` with an `error_class`, and tries to send the
  requester a failure message. A requester who never receives it can be told
  manually: the request is failed and its links are dead; submit again.
- The usual root cause is `lark user not found` — the requester's email is not
  resolvable in the Lark directory (contractor, alias, left the company). Ask the
  requester for a company email that resolves, and submit a new request.
- Urgent (`buzz`) is best effort. A buzz failure does not change any state; use
  the pending queue and the group card instead.

### TiDB is unavailable

Symptom: `GET /healthz` is `503`, pages show `unavailable`, approval `POST`s
return `503`, the pod may restart-loop if the DB is down at startup (the server
exits when the schema cannot be read).

- The state is durable: nothing is lost while the DB is down, and requests stay
  `pending`. Restore the DB, then confirm `kubectl -n dl-approval get deploy
  dl-approval` and `/healthz`. No manual repair is needed.
- Already-issued links keep working: `dl` validates them without asking this
  service.
- Readiness failing is expected while the DB is down; do not scale the replica
  up or down to "fix" it.

### `dl` is unavailable or rejects the request

Symptom: `delivery_status = failed`, `last_error_class` is set, event
`delivery_failed` with `outcome = error`.

| `last_error_class` | Cause | Action |
| --- | --- | --- |
| `issue_failed` | `dl` returned 4xx (repository/tag/file outside the issuer policy, or bad artifact) or kept failing 5xx/timeouts after 3 tries | check the policy scope in `gcp_dl_delivery_policy_json` and that the artifact exists |
| `sealed_url_invalid` / `seal_failed` | the returned `download_url` was empty or the encryption key is wrong | verify `DL_APPROVAL_URL_ENCRYPTION_KEY` matches the key that sealed earlier rows |
| `notifier_unavailable` / `notify_failed` | Lark path failed (see above) | fix Lark, ask for a new request |
| `store_failed` | DB write failed mid-delivery | restore the DB; the shares issued so far were revoked |
| `expired` | the request expired between approval and delivery | submit a new request with a longer TTL |

A `401` from the `dl` management API means the issuer secret no longer matches the
policy hash or `download_issuers` disabled `delivery-approval`. Confirm with an
authenticated probe — `POST /api/v1/shares` with `{}` must return `400`
(`missing_field`), not `401`.

### URL encryption key lost

The key only protects links that have not been delivered yet; delivered links
are already gone from the DB. If the key is lost, revoke every request whose
`download_url_ciphertext` is not null (their links cannot be delivered anyway),
and submit new requests. Rotating the key is safe once no row holds a ciphertext
under the old key.

### A card still looks pending after the decision

Cards are patched best effort, and Lark refuses patches for messages older than
its own limit (and for cards the bot can no longer see). Check
`approval_cards` for the request, then the log line
`lark card update failed` / `approval card update failed`. The decision is
authoritative in `requests`; tell the approver to open the request URL, which
shows the real state.

## Troubleshooting

| Symptom | Cause | Action |
| --- | --- | --- |
| `Forbidden - origin invalid`, address bar lost the `public_url` path | the link was pasted without the mount prefix and landed on the unauthenticated `dl` application | prepend the `public_url` path |
| `POST` fails with an origin error | a gateway CSRF filter is back in the `SecurityPolicy`; CSRF is enforced in the app | remove `csrf` from `dl-approval-oidc` (`ee-ops#2874`) |
| Detail page has no Approve/Reject | the viewer is the requester | let another approver decide |
| `401 unauthorized` from the app | no `X-Approval-Id-Token`, or the token failed validation | check the route's `SecurityPolicy` still forwards the ID token |
| `403 forbidden` on every page | the email domain is not in `allowed_email_domains` | add the domain or use a company account |
| `429` | pending/daily limit reached | decide or revoke the older requests |
| TTL rejected (`invalid TTL`) | day/week units are not accepted | use `168h` instead of `7d` |
| An anonymous request to a probe path on the legacy `dl` route redirects to the OIDC provider | that route carries its own OIDC policy | expected; probes run on the private listener |
| Links stop working with `404`/`410` | revoked, expired, or `max_sessions` reached | issue a new request; check `dl`'s `download_events` |

## Rollback

- **Stop the service:** scale to zero or remove the HTTPRoute. `dl` and its
  public downloads are unaffected; existing shares keep working until their TTL.
  Pending requests survive in the DB and can be decided after the service is
  back.
- **Stop new issuance only:** disable the `delivery-approval` row in `dl`'s
  `download_issuers` — every replica denies immediately, without a rollout. The
  approval service then fails with `issue_failed`.
- **Disable notifications only:** empty `approver_emails` is a startup error, so
  this is not a supported state. To silence alerts, remove the Lark app
  credentials — delivery is then disabled (see above) and no links are produced,
  so prefer leaving notifications on and fixing the target.
