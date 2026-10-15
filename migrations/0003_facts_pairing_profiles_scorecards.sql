-- Phases 3 and 4: reference facts, same-object pairing, reference profiles,
-- scorecards. See docs/DESIGN.md §5, §6.3, §6.4, §7.

-- Progress markers for incremental jobs (e.g. the last message id processed).
CREATE TABLE job_state (
    name       text PRIMARY KEY,
    value      bigint      NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Static system facts from reference-app messages.
CREATE TABLE facts_system (
    system_address bigint PRIMARY KEY,
    star_system    text        NOT NULL,
    star_pos       float8[]    NOT NULL,
    obs_count      integer     NOT NULL DEFAULT 1,
    ref_groups     text[]      NOT NULL,
    conflict       boolean     NOT NULL DEFAULT false,
    last_seen      timestamptz NOT NULL
);

-- Static body facts from reference-app Scan messages.
CREATE TABLE facts_body (
    system_address bigint      NOT NULL,
    body_id        integer     NOT NULL,
    body_name      text,
    static         jsonb       NOT NULL,   -- field -> value, static fields only
    obs_count      integer     NOT NULL DEFAULT 1,
    conflict_fields text[]     NOT NULL DEFAULT '{}',   -- fields reference apps disagree on
    last_seen      timestamptz NOT NULL,
    PRIMARY KEY (system_address, body_id)
);

-- Station facts (live galaxy only); names can change (carrier renames), so
-- comparisons only use recent facts.
CREATE TABLE facts_station (
    market_id    bigint PRIMARY KEY,
    station_name text,
    station_type text,
    obs_count    integer     NOT NULL DEFAULT 1,
    last_seen    timestamptz NOT NULL
);

-- Recent market snapshots from reference apps, for the pairing window only.
CREATE TABLE facts_market (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    market_id   bigint      NOT NULL,
    observed_at timestamptz NOT NULL,
    source      text        NOT NULL,   -- journal / capi
    ref_group   text        NOT NULL,
    commodities jsonb       NOT NULL    -- name(lower) -> [buyPrice, sellPrice, stock, demand]
);
CREATE INDEX facts_market_idx ON facts_market (market_id, observed_at);

-- One row per paired comparison of a non-reference message with reference facts.
CREATE TABLE pair_results (
    message_id       bigint      NOT NULL,
    gw_ts            timestamptz NOT NULL,
    app_id           integer     NOT NULL,
    software_version text        NOT NULL,
    event_type       text        NOT NULL,
    pair_kind        text        NOT NULL,   -- system / body / station / market / navroute
    compared         integer     NOT NULL,   -- fields compared
    mismatched       integer     NOT NULL,
    mismatches       jsonb,                  -- {field: [app value, reference value]}, capped
    PRIMARY KEY (message_id, gw_ts, pair_kind)
);
CREATE INDEX pair_results_app_idx ON pair_results (app_id, software_version, gw_ts);

-- Reference statistics per bucket and reference group (rebuilt hourly).
CREATE TABLE ref_profiles (
    event_type text        NOT NULL,
    subtype    text        NOT NULL,
    branch     text        NOT NULL,
    ref_group  text        NOT NULL,
    n          integer     NOT NULL,
    profile    jsonb       NOT NULL,
    built_at   timestamptz NOT NULL,
    PRIMARY KEY (event_type, subtype, branch, ref_group)
);

-- Behaviour baselines of the reference apps (rebuilt with the profiles).
CREATE TABLE ref_baselines (
    ref_group text        PRIMARY KEY,
    metrics   jsonb       NOT NULL,
    built_at  timestamptz NOT NULL
);

-- Finished scorecards, the only scoring data the web front end reads.
CREATE TABLE scorecards (
    app_id       integer     NOT NULL,
    version      text        NOT NULL,
    range_days   integer     NOT NULL,
    computed_at  timestamptz NOT NULL,
    messages     bigint      NOT NULL,
    score        real,                  -- NULL when nothing could be scored
    grade        text,
    card         jsonb       NOT NULL,
    PRIMARY KEY (app_id, version, range_days)
);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eddn_ro') THEN
        GRANT SELECT ON scorecards TO eddn_ro;
    END IF;
END
$$;
