-- The event types each card covers, with message counts, so a card can tell "not seen in
-- this version yet" (an earlier version sent it) from "never sent", without reading every
-- version's whole card. Nullable, no default: instant.
SET LOCAL lock_timeout = '10s';
ALTER TABLE scorecards ADD COLUMN IF NOT EXISTS event_types jsonb;
