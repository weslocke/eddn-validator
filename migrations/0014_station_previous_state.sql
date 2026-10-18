-- A station's previous name and type, so a report is judged against the station as it was when
-- it was made: stations get renamed and rebuilt (MarketID 128016640 became "Galileo", an Ocellus,
-- on 2026-10-15). prev_seen_until: last report of the previous state; state_since: first report
-- of the current one. Nullable, no default: instant.
SET LOCAL lock_timeout = '10s';
ALTER TABLE facts_station ADD COLUMN IF NOT EXISTS prev_station_name text;
ALTER TABLE facts_station ADD COLUMN IF NOT EXISTS prev_station_type text;
ALTER TABLE facts_station ADD COLUMN IF NOT EXISTS prev_seen_until timestamptz;
ALTER TABLE facts_station ADD COLUMN IF NOT EXISTS state_since timestamptz;
