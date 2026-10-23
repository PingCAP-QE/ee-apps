# Cost tab progressive-loading validation

Captured: 2026-10-23 UTC. Local change only; not deployed.

## Change

- Keep the spend-summary/trend, share, and bucketed-stack requests concurrent.
- Remove the single loading/error gate covering both top charts: share depends
  only on its own response; the stack/trend area still waits for its stack data
  and net-cost comparison data. Failure in one area does not hide the other.
- Defer Engineering Group shares until the three top requests settle, including
  errors, for the selected filter scope. Match response keys so previous-scope
  data cannot release the gate when dates/accounts/filters change.
- Remember readiness for the current filter scope so changing only the breakdown
  grouping or drilling down does not refetch unchanged Engineering Group data.
- Keep resource details and pagination on demand. A ready share chart can request
  its resources even while the trend is still pending.
- Show pending/unavailable summary values as `—`, not verified zero spending.

No API, SQL, accounting, date-window, chart-series or resource-pagination
contract changes are required for this Cost-tab adjustment.

## Verification

`web/src/__tests__/CostLoading.test.js` holds actual component fetch promises
unresolved, verifying both completion orders, isolated failures, request
prioritization after filter changes, grouping/drilldown reuse, summary
placeholders, resource requests while the trend waits, and aborts on unmount.
The tests failed against the previous coupled loading/parallel-bottom behavior.

Browser verification against the local Vite page used controlled API fixtures:
with both trend dependencies held, the owner-share donut rendered and remained
interactive while the trend showed its loader; no Engineering Group request
started. Releasing both dependencies rendered the trend and started the bottom
request. A desktop screenshot confirmed the original two-column layout remained
intact and there was no horizontal page overflow. Development StrictMode/HMR
aborted superseded requests as expected.

This establishes progressive rendering, not a measured production latency
speedup. After deployment, measure time to the first useful chart independently
from time to the complete page.
