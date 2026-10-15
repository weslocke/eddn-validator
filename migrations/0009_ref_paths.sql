-- Every field path the reference apps have sent, per event type and branch, accumulated
-- over time like ref_values. The hourly profiles only see the newest 2000 messages per
-- app, so rare but real fields (StationState during an attack, say) would otherwise flicker
-- between "known" and "no reference app sends this" from one hour to the next.
CREATE TABLE ref_paths (
    event_type text        NOT NULL,
    branch     text        NOT NULL,
    path       text        NOT NULL,
    last_seen  timestamptz NOT NULL,
    PRIMARY KEY (event_type, branch, path)
);
