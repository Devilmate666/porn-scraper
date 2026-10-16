-- Clears the rate-limit counters. Run after a bug (e.g. a schema error) caused many failed
-- attempts that legitimately exhausted the limits. Safe to run any number of times.
--   wrangler d1 execute porn-archive-db --remote --file=migrations/0003_clear_rate.sql
DELETE FROM rate;