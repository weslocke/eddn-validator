-- The public site shows when continuous collection began (for the "new
-- applications" list), so the web role may read the listener's per-minute counts.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eddn_ro') THEN
        GRANT SELECT ON ingest_stats TO eddn_ro;
    END IF;
END
$$;
