-- Files imported by the second archive pass (`import-archive --reference`), which
-- stores the reference apps' own messages. Kept apart from archive_imports (the
-- first pass) so that a first-pass import already running is unaffected.
CREATE TABLE archive_reference_imports (
    url         text PRIMARY KEY,
    lines       bigint      NOT NULL,
    stored      bigint      NOT NULL,
    finished_at timestamptz NOT NULL DEFAULT now()
);
