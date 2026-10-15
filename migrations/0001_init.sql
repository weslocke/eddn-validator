-- Phase 1: message storage, application registry, reference versions,
-- ingest statistics. See docs/DESIGN.md §5.

CREATE TABLE apps (
    id            integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    software_name text        NOT NULL UNIQUE,
    ref_group     text,                       -- EDMC / EDDiscovery / EDDLite, NULL otherwise
    first_seen    timestamptz NOT NULL,
    last_seen     timestamptz NOT NULL
);

CREATE TABLE app_versions (
    app_id     integer     NOT NULL REFERENCES apps (id),
    version    text        NOT NULL,
    first_seen timestamptz NOT NULL,
    last_seen  timestamptz NOT NULL,
    msg_count  bigint      NOT NULL DEFAULT 0,   -- total received, not reduced by retention
    PRIMARY KEY (app_id, version)
);

-- Reference-app releases from GitHub. is_current marks the versions that
-- currently form the reference pool (newest N non-pre-release releases).
CREATE TABLE ref_versions (
    ref_group    text        NOT NULL,
    version      text        NOT NULL,        -- as sent in header.softwareVersion
    tag          text        NOT NULL,
    published_at timestamptz,
    is_current   boolean     NOT NULL DEFAULT false,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (ref_group, version)
);

-- Every relayed message. Partitioned by day on the gateway timestamp; the
-- worker creates partitions ahead of time and drops expired ones.
-- An explicit sequence is used because identity columns on partitioned
-- tables need PostgreSQL 17.
CREATE SEQUENCE messages_id_seq AS bigint;

CREATE TABLE messages (
    id               bigint      NOT NULL DEFAULT nextval('messages_id_seq'),
    gw_ts            timestamptz NOT NULL,   -- header.gatewayTimestamp
    received_at      timestamptz NOT NULL,   -- when the listener received it
    msg_ts           timestamptz,            -- message.timestamp (NULL if unparseable)
    app_id           integer     NOT NULL,
    software_version text        NOT NULL,
    schema_ref       text        NOT NULL,
    schema_name      text        NOT NULL,   -- e.g. journal/1, commodity/3
    is_test          boolean     NOT NULL,   -- $schemaRef ends in /test
    event            text,
    event_type       text        NOT NULL,   -- journal/1:FSDJump, commodity/3, ...
    subtype          text,                   -- filled by later phases
    branch           text        NOT NULL,   -- live / legacy / unknown
    source           text        NOT NULL,   -- journal / capi
    game_version     text,                   -- NULL = header key absent
    game_build       text,
    is_big3          boolean     NOT NULL,
    content_hash     bytea       NOT NULL,   -- sha256 of $schemaRef + canonical message
    payload          jsonb       NOT NULL,   -- full envelope as relayed
    PRIMARY KEY (id, gw_ts)
) PARTITION BY RANGE (gw_ts);

ALTER SEQUENCE messages_id_seq OWNED BY messages.id;

-- Catches anything outside the pre-created daily partitions. Should stay empty.
CREATE TABLE messages_default PARTITION OF messages DEFAULT;

-- Scorecards: one app version over a time range, per event type.
CREATE INDEX messages_app_idx
    ON messages (app_id, software_version, event_type, gw_ts DESC);

-- Reference profiles and retention of the big three.
CREATE INDEX messages_big3_idx
    ON messages (app_id, event_type, branch, is_test, gw_ts DESC)
    WHERE is_big3;

-- Listener throughput per minute, for spotting gaps in the data.
CREATE TABLE ingest_stats (
    minute       timestamptz PRIMARY KEY,
    received     integer NOT NULL DEFAULT 0,
    written      integer NOT NULL DEFAULT 0,
    dropped      integer NOT NULL DEFAULT 0,
    parse_errors integer NOT NULL DEFAULT 0,
    db_errors    integer NOT NULL DEFAULT 0
);

-- The web role only ever reads the app list (and, in later phases, finished
-- scorecards). Skipped when the role does not exist, e.g. a local test DB.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eddn_ro') THEN
        GRANT SELECT ON apps, app_versions TO eddn_ro;
    END IF;
END
$$;
