-- Apply only after the materialize-cost-allocations CronJob is removed and a
-- native-only Cost Insight image is deployed. Dashboard has no runtime reads
-- from either table.
DROP TABLE IF EXISTS cost_allocation_publication;
DROP TABLE IF EXISTS cost_allocation_daily;
