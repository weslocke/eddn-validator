-- The technical page shows how often each reference app fails each rule, read from the
-- reference profiles. Skipped when the role does not exist, e.g. a local test DB.
SET LOCAL lock_timeout = '10s';
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eddn_ro') THEN
        GRANT SELECT ON ref_profiles TO eddn_ro;
    END IF;
END
$$;
