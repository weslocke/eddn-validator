-- Owner marks and SQLite imports.

-- For the owner's own application (ED Outrider): true when the message was
-- sent by the owner's own instance, false when by another player, NULL when
-- unknown or not applicable. Only the private Outrider page uses it.
ALTER TABLE messages ADD COLUMN mine boolean;

-- Progress of `eddn-validator import-sqlite`, so an interrupted import
-- resumes instead of duplicating rows.
CREATE TABLE sqlite_imports (
    source     text PRIMARY KEY,     -- resolved path of the SQLite file
    last_id    bigint      NOT NULL, -- highest source row id imported
    rows_done  bigint      NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);
