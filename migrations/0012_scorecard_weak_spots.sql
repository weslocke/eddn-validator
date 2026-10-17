-- Event types that score below C despite enough data, kept beside the card so the app lists
-- can flag them without reading whole cards. Nullable, no default: instant.
SET LOCAL lock_timeout = '10s';
ALTER TABLE scorecards ADD COLUMN IF NOT EXISTS weak_spots jsonb;
