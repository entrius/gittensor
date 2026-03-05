-- Add pioneer scoring columns to pull_requests (idempotent).
-- Apply before deploying pioneer-scoring application code.

ALTER TABLE pull_requests
  ADD COLUMN IF NOT EXISTS pioneer_multiplier DOUBLE PRECISION NOT NULL DEFAULT 1.0;

ALTER TABLE pull_requests
  ADD COLUMN IF NOT EXISTS pioneer_rank INTEGER NOT NULL DEFAULT 0;

-- Supports efficient pioneer inactivity lookups by repo + merged timestamp.
CREATE INDEX IF NOT EXISTS idx_pull_requests_repo_merged_at
  ON pull_requests (repository_full_name, merged_at DESC);

-- WARNING: Destructive change.
-- For safest rollout/rollback support, run this DROP in a later cleanup migration.
-- ALTER TABLE pull_requests
--   DROP COLUMN IF EXISTS repository_uniqueness_multiplier;
