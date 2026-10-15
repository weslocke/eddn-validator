-- Every value the reference apps have sent for closed-set fields (StarType,
-- PlanetClass, FactionState, ...), accumulated over time so that rare but real
-- values are known even when the current reference sample lacks them.
CREATE TABLE ref_values (
    event_type text        NOT NULL,
    path       text        NOT NULL,
    value      text        NOT NULL,
    count      bigint      NOT NULL,
    last_seen  timestamptz NOT NULL,
    PRIMARY KEY (event_type, path, value)
);
