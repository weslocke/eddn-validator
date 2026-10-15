-- Facts that other apps' reports have been paired against are kept for good (retention
-- skips them); the rest expire six months after a reference app last reported them.
-- Adding a column with a constant default is instant (no table rewrite); the short lock
-- timeout keeps it from queueing behind a long facts transaction.
SET LOCAL lock_timeout = '10s';
ALTER TABLE facts_system  ADD COLUMN IF NOT EXISTS pinned boolean NOT NULL DEFAULT false;
ALTER TABLE facts_body    ADD COLUMN IF NOT EXISTS pinned boolean NOT NULL DEFAULT false;
ALTER TABLE facts_station ADD COLUMN IF NOT EXISTS pinned boolean NOT NULL DEFAULT false;
