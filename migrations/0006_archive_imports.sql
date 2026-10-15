-- Files imported from the EDDN archive (edgalaxydata.space), so that a re-run
-- of `import-archive` skips finished files.
CREATE TABLE archive_imports (
    url         text PRIMARY KEY,
    lines       bigint      NOT NULL,
    stored      bigint      NOT NULL,   -- non-reference messages stored
    folded      bigint      NOT NULL,   -- reference messages folded into facts
    finished_at timestamptz NOT NULL DEFAULT now()
);
